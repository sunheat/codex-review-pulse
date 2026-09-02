"""Guard the default path's standalone-task invocation boundary.

The scheduler and Codex task APIs are host capabilities, so this module keeps
them behind injected adapters.  It owns only the ordering and single-use
contract: one scheduler delivery creates one wake, and one invocation can
schedule at most one standalone successor before it ends at ``complete-wake``.
"""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime, timedelta
import hashlib
from threading import RLock
from typing import Any, Callable, Mapping, Protocol

from default_policy import PolicyError, normalize_policy


REARM_ACTIONS = {"WAIT_REVIEW", "WAIT_RETRY", "REQUEST_REVIEW", "RUN_BATCH"}


class StandaloneInvocationError(RuntimeError):
    """Raised when a host tries to violate the standalone invocation boundary."""


class CheckpointUnavailable(StandaloneInvocationError):
    """Raised by a host adapter when direct checkpoint evidence is unavailable."""


class StandaloneTaskHost(Protocol):
    """Host operations required by one standalone scheduled invocation."""

    def new_opaque_wake_id(self) -> str:
        """Return a fresh ID for this invocation only."""

    def pause_task(self, task_id: str) -> object:
        """Pause the delivered task and return an explicit confirmation."""

    def read_checkpoint_directly(self) -> Mapping[str, Any]:
        """Read the Git-common-dir checkpoint without model context."""

    def schedule_standalone_task(
        self,
        *,
        prompt: str,
        cadence_seconds: int,
        model: str,
        reasoning_effort: str,
        scheduler_kind: str,
        conversation_mode: str,
        target_thread_id: None,
        prompt_sha256: str,
    ) -> object:
        """Create one cadence-only standalone task anchored by persisted creation."""

    def read_task(self, task_id: str) -> Mapping[str, Any]:
        """Read normalized task metadata and the persisted first-run timestamp."""


BeginWake = Callable[[str, str, bool, str | None], Mapping[str, Any]]
CompleteWake = Callable[
    [
        str,
        str,
        Callable[[str], object],
        str | None,
        str | None,
        Mapping[str, Any] | None,
    ],
    Mapping[str, Any],
]


def _confirmed(value: object) -> bool:
    if isinstance(value, dict):
        return value.get("confirmed") is True
    return value is True


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Timestamp must include a timezone")
    return parsed.astimezone(UTC)


def _ceil_to_second(value: datetime) -> datetime:
    value = value.astimezone(UTC)
    if value.microsecond:
        value += timedelta(seconds=1)
    return value.replace(microsecond=0)


def _first_run_matches(expected: str, observed: object) -> bool:
    if not isinstance(observed, str) or not observed.strip():
        return False
    try:
        delta = _utc(observed) - _utc(expected)
    except (TypeError, ValueError):
        return False
    return timedelta(0) <= delta <= timedelta(seconds=1)


def _task_id(response: object) -> str:
    if isinstance(response, str) and response.strip():
        return response
    if isinstance(response, Mapping):
        for key in ("task_id", "id"):
            value = response.get(key)
            if isinstance(value, str) and value.strip():
                return value
    raise StandaloneInvocationError("Standalone task creation returned no task ID")


def _validate_task_readback(
    task: Mapping[str, Any],
    *,
    prompt: str,
    prompt_sha256: str,
    cadence_seconds: int,
    model: str,
    reasoning_effort: str,
) -> None:
    expected = {
        "scheduler_kind": "cron",
        "conversation_mode": "standalone",
        "target_thread_id": None,
        "prompt": prompt,
        "prompt_sha256": prompt_sha256,
        "cadence_seconds": cadence_seconds,
        "model": model,
        "reasoning_effort": reasoning_effort,
    }
    for key, value in expected.items():
        if task.get(key) != value:
            raise StandaloneInvocationError(
                f"Standalone task readback does not match {key}"
            )


def scheduled_preflight(
    checkpoint: Mapping[str, Any], *, now: str, task_id: str | None = None
) -> dict[str, Any] | None:
    """Return a fail-closed result before ``begin-wake`` or ``None`` if ready."""
    try:
        current = _utc(now)
    except (TypeError, ValueError):
        return {
            "next_action": "PAUSE_RECOVERY",
            "reason_code": "checkpoint_invalid",
        }
    if checkpoint.get("active_wake_id"):
        return {
            "next_action": "PAUSE_RECOVERY",
            "reason_code": "incomplete_wake",
        }
    if checkpoint.get("failure_latch"):
        return {
            "next_action": "PAUSE_RECOVERY",
            "reason_code": "failure_latched",
        }
    if (
        checkpoint.get("scheduled_task_disposition") != "ACTIVE"
        or checkpoint.get("scheduled_task_id") != task_id
        or not isinstance(task_id, str)
        or not task_id.strip()
    ):
        return {
            "next_action": "PAUSE_RECOVERY",
            "reason_code": "scheduled_task_identity_mismatch",
            "evidence": {
                "delivered_task_id": task_id,
                "scheduled_task_id": checkpoint.get("scheduled_task_id"),
                "scheduled_task_disposition": checkpoint.get(
                    "scheduled_task_disposition"
                ),
            },
        }
    next_not_before = checkpoint.get("next_not_before")
    if next_not_before:
        try:
            not_before = _utc(str(next_not_before))
        except (TypeError, ValueError):
            return {
                "next_action": "PAUSE_RECOVERY",
                "reason_code": "checkpoint_invalid",
            }
        if current < not_before:
            return {
                "next_action": "PAUSE_BLOCKED",
                "reason_code": "cadence_not_elapsed",
                "evidence": {"next_not_before": next_not_before},
            }
    return None


class StandaloneInvocation:
    """Serialize host operations for exactly one standalone task delivery."""

    def __init__(
        self,
        host: StandaloneTaskHost,
        *,
        task_id: str,
        prompt: str,
        scheduled: bool,
        now: str,
        begin_wake: BeginWake,
        complete_wake: CompleteWake,
    ) -> None:
        if not task_id.strip():
            raise ValueError("A task ID is required")
        if not prompt.strip():
            raise ValueError("A standalone task prompt is required")
        self.host = host
        self.task_id = task_id
        self.prompt = prompt
        self.scheduled = scheduled
        self.now = now
        self.begin_wake = begin_wake
        self.complete_wake = complete_wake
        self.prompt_sha256 = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        self.wake_id: str | None = None
        self.started = False
        self.ended = False
        self._completion_attempted = False
        self._busy = False
        self._lock = RLock()

    def _ensure_open(self) -> None:
        if self.ended:
            raise StandaloneInvocationError(
                "The standalone invocation ended after its final wake result"
            )

    def _end(self, result: Mapping[str, Any]) -> dict[str, Any]:
        self.ended = True
        return deepcopy(dict(result))

    @contextmanager
    def _serialized_operation(self):
        with self._lock:
            self._ensure_open()
            if self._busy:
                raise StandaloneInvocationError(
                    "Standalone host operations cannot run in parallel"
                )
            self._busy = True
            try:
                yield
            finally:
                self._busy = False

    def begin(self) -> dict[str, Any]:
        """Pause, preflight, and begin at most one wake."""
        with self._serialized_operation():
            if self.started:
                raise StandaloneInvocationError(
                    "One standalone invocation may begin only one wake"
                )
            self.started = True
            self.wake_id = self.host.new_opaque_wake_id()
            if not isinstance(self.wake_id, str) or not self.wake_id.strip():
                raise StandaloneInvocationError("Host returned an invalid wake ID")

            if self.scheduled:
                try:
                    pause_confirmed = _confirmed(self.host.pause_task(self.task_id))
                except Exception:
                    pause_confirmed = False
                if not pause_confirmed:
                    try:
                        result = self.begin_wake(
                            self.wake_id,
                            self.now,
                            False,
                            self.task_id if self.scheduled else None,
                        )
                    except Exception:
                        return self._end(
                            {
                                "next_action": "PAUSE_RECOVERY",
                                "reason_code": "pause_failure_persistence_failed",
                            }
                        )
                    if not isinstance(result, Mapping) or "next_action" not in result:
                        return self._end(
                            {
                                "next_action": "PAUSE_RECOVERY",
                                "reason_code": "pause_failure_persistence_failed",
                            }
                        )
                    return self._end(result)
                try:
                    checkpoint = self.host.read_checkpoint_directly()
                except Exception:
                    return self._end(
                        {
                            "next_action": "PAUSE_RECOVERY",
                            "reason_code": "checkpoint_unavailable",
                        }
                    )
                try:
                    preflight = scheduled_preflight(
                        checkpoint, now=self.now, task_id=self.task_id
                    )
                except Exception:
                    preflight = {
                        "next_action": "PAUSE_RECOVERY",
                        "reason_code": "checkpoint_invalid",
                    }
                if preflight is not None:
                    try:
                        persisted = self.begin_wake(
                            self.wake_id,
                            self.now,
                            False,
                            self.task_id if self.scheduled else None,
                        )
                    except Exception:
                        return self._end(
                            {
                                "next_action": "PAUSE_RECOVERY",
                                "reason_code": "pause_failure_persistence_failed",
                            }
                        )
                    if not isinstance(persisted, Mapping) or "next_action" not in persisted:
                        return self._end(
                            {
                                "next_action": "PAUSE_RECOVERY",
                                "reason_code": "pause_failure_persistence_failed",
                            }
                        )
                    return self._end(persisted)

            result = self.begin_wake(
                self.wake_id,
                self.now,
                True,
                self.task_id if self.scheduled else None,
            )
            if not isinstance(result, Mapping) or "next_action" not in result:
                raise StandaloneInvocationError("begin-wake returned an invalid result")
            if result.get("next_action") != "WAKE_STARTED":
                return self._end(result)
            return dict(result)

    def _finish_completion(
        self,
        *,
        now: str,
        actual_first_run: object,
        successor_id: str | None,
        scheduled_created_at: str | None = None,
        completion_failure: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self.wake_id is None:
            raise StandaloneInvocationError("complete-wake requires an active wake")
        try:
            result = self.complete_wake(
                self.wake_id,
                now,
                lambda _expected: actual_first_run,
                successor_id,
                scheduled_created_at,
                completion_failure,
            )
        except Exception as error:
            cleanup_confirmed = True
            if successor_id is not None:
                try:
                    cleanup_confirmed = _confirmed(self.host.pause_task(successor_id))
                except Exception:
                    cleanup_confirmed = False
            self.ended = True
            if successor_id is not None and not cleanup_confirmed:
                raise StandaloneInvocationError(
                    "complete-wake failed and successor cleanup was not confirmed"
                ) from error
            raise
        if not isinstance(result, Mapping) or "next_action" not in result:
            self.ended = True
            raise StandaloneInvocationError("complete-wake returned an invalid result")
        return self._end(result)

    def complete(self, *, action: str, now: str, cadence_seconds: int) -> dict[str, Any]:
        """Create/read back one successor, complete once, and end immediately."""
        with self._serialized_operation():
            if not self.started or self.wake_id is None:
                raise StandaloneInvocationError("complete-wake requires an active wake")
            if action not in REARM_ACTIONS:
                raise StandaloneInvocationError(
                    f"The action {action!r} is not eligible for a standalone successor"
                )
            if self._completion_attempted:
                raise StandaloneInvocationError(
                    "The standalone invocation already attempted complete-wake"
                )
            if isinstance(cadence_seconds, bool) or cadence_seconds <= 0:
                raise ValueError("Cadence must be positive")
            self._completion_attempted = True
            try:
                checkpoint = self.host.read_checkpoint_directly()
            except Exception:
                return self._finish_completion(
                    now=now,
                    actual_first_run=None,
                    successor_id=None,
                    completion_failure={
                        "reason_code": "checkpoint_unavailable",
                        "evidence": {},
                    },
                )
            if not isinstance(checkpoint, Mapping):
                return self._finish_completion(
                    now=now,
                    actual_first_run=None,
                    successor_id=None,
                    completion_failure={
                        "reason_code": "checkpoint_invalid",
                        "evidence": {"checkpoint_type": type(checkpoint).__name__},
                    },
                )
            persisted_decision = checkpoint.get("last_decision")
            persisted_action = (
                persisted_decision.get("next_action")
                if isinstance(persisted_decision, Mapping)
                else None
            )
            if persisted_action != action:
                return self._finish_completion(
                    now=now,
                    actual_first_run=None,
                    successor_id=None,
                    completion_failure={
                        "reason_code": "completion_action_mismatch",
                        "evidence": {
                            "requested_action": action,
                            "persisted_action": persisted_action,
                        },
                    },
                )
            policy = checkpoint.get("automation_policy")
            persisted_cadence = (
                policy.get("cadence_seconds")
                if isinstance(policy, Mapping)
                else None
            )
            if (
                isinstance(persisted_cadence, bool)
                or not isinstance(persisted_cadence, int)
                or persisted_cadence <= 0
            ):
                return self._finish_completion(
                    now=now,
                    actual_first_run=None,
                    successor_id=None,
                    completion_failure={
                        "reason_code": "checkpoint_invalid",
                        "evidence": {"persisted_cadence_seconds": persisted_cadence},
                    },
                )
            if persisted_cadence != cadence_seconds:
                return self._finish_completion(
                    now=now,
                    actual_first_run=None,
                    successor_id=None,
                    completion_failure={
                        "reason_code": "completion_cadence_mismatch",
                        "evidence": {
                            "requested_cadence_seconds": cadence_seconds,
                            "persisted_cadence_seconds": persisted_cadence,
                        },
                    },
                )
            try:
                normalized_policy = normalize_policy(policy)
            except PolicyError as error:
                return self._finish_completion(
                    now=now,
                    actual_first_run=None,
                    successor_id=None,
                    completion_failure={
                        "reason_code": "checkpoint_invalid",
                        "evidence": {"automation_policy_error": str(error)},
                    },
                )
            model = normalized_policy["model"]
            reasoning_effort = normalized_policy["reasoning_effort"]
            completion = _utc(now)
            if action == "RUN_BATCH":
                batch = checkpoint.get("active_batch")
                publication = (
                    batch.get("publication")
                    if isinstance(batch, Mapping)
                    else None
                )
                publication_complete = (
                    isinstance(publication, Mapping)
                    and publication.get("status") == "succeeded"
                )
                if not publication_complete:
                    return self._finish_completion(
                        now=now,
                        actual_first_run=None,
                        successor_id=None,
                    )
            elif action == "REQUEST_REVIEW":
                snapshot = checkpoint.get("last_snapshot")
                head_oid = (
                    snapshot.get("head_oid")
                    if isinstance(snapshot, Mapping)
                    else None
                )
                trigger_events = checkpoint.get("trigger_events")
                event = (
                    trigger_events.get(head_oid, {})
                    if isinstance(trigger_events, Mapping)
                    and isinstance(head_oid, str)
                    else {}
                )
                trigger_confirmed = (
                    isinstance(event, Mapping)
                    and event.get("status") == "emitted"
                )
                if not trigger_confirmed:
                    return self._finish_completion(
                        now=now,
                        actual_first_run=None,
                        successor_id=None,
                        completion_failure={
                            "reason_code": "review_trigger_not_confirmed",
                            "evidence": dict(event) if isinstance(event, Mapping) else {},
                        },
                    )

            successor_id: str | None = None
            actual_first_run: object = None
            observed_first_run: object = None
            scheduled_created_at: str | None = None
            first_run_mismatch = False
            readback_failed = False
            completion_failure: Mapping[str, Any] | None = None
            try:
                response = self.host.schedule_standalone_task(
                    prompt=self.prompt,
                    cadence_seconds=cadence_seconds,
                    model=model,
                    reasoning_effort=reasoning_effort,
                    scheduler_kind="cron",
                    conversation_mode="standalone",
                    target_thread_id=None,
                    prompt_sha256=self.prompt_sha256,
                )
                successor_id = _task_id(response)
                readback_failed = True
                task = self.host.read_task(successor_id)
                _validate_task_readback(
                    task,
                    prompt=self.prompt,
                    prompt_sha256=self.prompt_sha256,
                    cadence_seconds=cadence_seconds,
                    model=model,
                    reasoning_effort=reasoning_effort,
                )
                raw_created_at = task.get("created_at")
                if not isinstance(raw_created_at, str) or not raw_created_at.strip():
                    raise StandaloneInvocationError(
                        "Standalone task readback returned no persisted creation time"
                    )
                created_at = _utc(raw_created_at)
                if created_at < completion:
                    raise StandaloneInvocationError(
                        "Standalone task creation predates wake completion"
                    )
                scheduled_created_at = created_at.isoformat()
                expected_first_run = _ceil_to_second(
                    created_at + timedelta(seconds=cadence_seconds)
                ).isoformat()
                observed_first_run = task.get("first_run")
                if not isinstance(observed_first_run, str) or not observed_first_run.strip():
                    raise StandaloneInvocationError(
                        "Standalone task readback returned no persisted first run"
                    )
                if not _first_run_matches(expected_first_run, observed_first_run):
                    first_run_mismatch = True
                    raise StandaloneInvocationError(
                        "Standalone task readback returned an invalid first run"
                    )
                actual_first_run = observed_first_run
                readback_failed = False
            except Exception:
                if successor_id is not None:
                    try:
                        pause_confirmed = _confirmed(self.host.pause_task(successor_id))
                    except Exception:
                        pause_confirmed = False
                    if not pause_confirmed:
                        completion_failure = {
                            "reason_code": "successor_cleanup_unconfirmed",
                            "evidence": {
                                "successor_task_id": successor_id,
                                "pause_confirmed": False,
                            },
                        }
                if completion_failure is None:
                    successor_id = None
                if first_run_mismatch:
                    actual_first_run = observed_first_run
                elif readback_failed:
                    actual_first_run = ""
                else:
                    actual_first_run = None

            return self._finish_completion(
                now=now,
                actual_first_run=actual_first_run,
                successor_id=successor_id,
                scheduled_created_at=scheduled_created_at,
                completion_failure=completion_failure,
            )
