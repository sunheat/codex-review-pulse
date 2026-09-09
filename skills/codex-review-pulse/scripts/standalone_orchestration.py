"""Guard the default path's standalone-task invocation boundary.

The scheduler and Codex task APIs are host capabilities, so this module keeps
them behind injected adapters.  It owns only the ordering and single-use
contract: one scheduler delivery creates one wake; a rearmable wake durably
retires its one exact registered predecessor before successor creation; and one
invocation can schedule at most one standalone successor before it ends at
``complete-wake``.
"""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime, timedelta
import hashlib
import secrets
from threading import RLock
from typing import Any, Callable, Mapping, Protocol

from default_policy import PolicyError, normalize_policy


REARM_ACTIONS = {"WAIT_REVIEW", "WAIT_RETRY", "REQUEST_REVIEW", "RUN_BATCH"}
# The local scheduler exposes task metadata at whole-second precision.  Keep
# host-side derivation aligned with pulse.py's re-anchor validation.
SCHEDULER_TIMESTAMP_PRECISION = "whole-second-truncation"


class StandaloneInvocationError(RuntimeError):
    """Raised when a host tries to violate the standalone invocation boundary."""


class CheckpointUnavailable(StandaloneInvocationError):
    """Raised by a host adapter when direct checkpoint evidence is unavailable."""


class StandaloneTaskHost(Protocol):
    """Host operations required by one standalone scheduled invocation."""

    def new_opaque_wake_id(self) -> str:
        """Return a fresh ID for this invocation only."""

    def pause_task(self, task_id: str) -> object:
        """Pause using the host's full persisted task metadata and confirm it."""

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
        status: str,
    ) -> object:
        """Create one paused cadence-only task anchored by persisted creation."""

    def read_task(self, task_id: str) -> Mapping[str, Any]:
        """Read normalized task metadata and the persisted first-run timestamp."""

    def delete_task(self, task_id: str) -> object:
        """Delete only the exact registered predecessor task ID."""

    def lookup_task(self, task_id: str) -> object:
        """Return PRESENT, AUTHORITATIVE_NOT_FOUND, or READBACK_UNKNOWN."""

    def cleanup_worktree(
        self, *, pending_repair: Mapping[str, Any] | None = None
    ) -> object:
        """Verify, remove, and prune this wake's task-owned worktree.

        A retry with a persisted pending-repair manifest may still have
        intentional uncommitted changes. The host must verify that manifest
        (including its immutable patch bytes and digest) before removing that
        dirty worktree; ordinary cleanup remains strict when it is absent.
        """

    def now_utc(self) -> str:
        """Return the host's current UTC time after final cleanup."""

    def authorize_successor(
        self,
        *,
        wake_id: str,
        completed_at: str,
        task_id: str,
        created_at: str,
        first_run: str,
        cadence_seconds: int,
    ) -> object:
        """Persist a verified successor while the host task remains paused."""

    def activate_task(self, task_id: str) -> object:
        """Activate with the host's full persisted task metadata and confirm it."""


BeginWake = Callable[..., Mapping[str, Any]]
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
PrepareRetirement = Callable[[str, str, bool], Mapping[str, Any]]
ConfirmRetirement = Callable[[str, str, str, str, str, Mapping[str, Any] | None], Mapping[str, Any]]
RecordSuccessorCreation = Callable[
    [str, str, str, str | None, str | None, Mapping[str, Any] | None], Mapping[str, Any]
]
RecordSuccessorIntent = Callable[[str, str, str], Mapping[str, Any]]
RecordSuccessorReadback = Callable[[str, str, Mapping[str, Any]], Mapping[str, Any]]
RecordSuccessorPause = Callable[[str, str, str, bool, Mapping[str, Any] | None], Mapping[str, Any]]
RecordSetupIntent = Callable[[str, str], Mapping[str, Any]]
RecordSetupID = Callable[[str, str], Mapping[str, Any]]
RecordSetupReadback = Callable[[str, Mapping[str, Any]], Mapping[str, Any]]
RecordSetupUnknown = Callable[[str, Mapping[str, Any]], Mapping[str, Any]]


def _confirmed(value: object) -> bool:
    if isinstance(value, dict):
        return value.get("confirmed") is True
    return value is True


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Timestamp must include a timezone")
    return parsed.astimezone(UTC)


def _iso(value: str | datetime) -> str:
    return _utc(value if isinstance(value, str) else value.isoformat()).isoformat()


def _truncate_to_scheduler_precision(value: datetime) -> datetime:
    """Represent a scheduler timestamp using its authoritative whole second."""
    return value.astimezone(UTC).replace(microsecond=0)


def _first_run_matches(expected: str, observed: object) -> bool:
    if not isinstance(observed, str) or not observed.strip():
        return False
    try:
        delta = _truncate_to_scheduler_precision(_utc(observed)) - (
            _truncate_to_scheduler_precision(_utc(expected))
        )
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


def _lookup_outcome(response: object) -> str:
    """Normalize an exact-ID scheduler lookup without treating errors as absence."""
    if isinstance(response, Mapping):
        value = response.get("outcome", response.get("status"))
        if value in {"PRESENT", "AUTHORITATIVE_NOT_FOUND", "READBACK_UNKNOWN"}:
            return str(value)
    return "READBACK_UNKNOWN"


def _creation_outcome(response: object) -> str:
    """Normalize host creation evidence; missing IDs are never proven no-create."""
    if isinstance(response, Mapping):
        value = response.get("outcome")
        if value in {
            "CREATED_EXACT_ID",
            "AUTHORITATIVE_NO_SUCCESSOR",
            "SUCCESSOR_CREATION_UNKNOWN",
        }:
            return str(value)
    try:
        _task_id(response)
    except StandaloneInvocationError:
        return "SUCCESSOR_CREATION_UNKNOWN"
    return "CREATED_EXACT_ID"


def _retirement_delete_outcome(host: StandaloneTaskHost, task_id: str) -> tuple[str, dict[str, Any]]:
    """Classify exact delete using only a fresh exact task lookup on ambiguity."""
    try:
        response = host.delete_task(task_id)
    except Exception as error:
        response = {"exception": str(error)}
    if response is True or (
        isinstance(response, Mapping)
        and response.get("outcome") == "RETIREMENT_CONFIRMED"
    ):
        return "confirmed", {"delete": response}
    try:
        lookup = host.lookup_task(task_id)
    except Exception as error:
        lookup = {"exception": str(error)}
    normalized_lookup = _lookup_outcome(lookup)
    evidence = {"delete": response, "lookup": normalized_lookup}
    if normalized_lookup == "AUTHORITATIVE_NOT_FOUND":
        return "confirmed", evidence
    if normalized_lookup == "PRESENT":
        return "non_deletion", evidence
    return "unknown", evidence


def _validate_task_readback(
    task: Mapping[str, Any],
    *,
    task_id: str,
    prompt: str,
    prompt_sha256: str,
    cadence_seconds: int,
    model: str,
    reasoning_effort: str,
) -> None:
    persisted_task_id = task.get("id")
    if not isinstance(persisted_task_id, str) or not persisted_task_id.strip():
        persisted_task_id = task.get("task_id")
    if persisted_task_id != task_id:
        raise StandaloneInvocationError(
            "Standalone task readback does not match task ID"
        )
    expected = {
        "scheduler_kind": "cron",
        "conversation_mode": "standalone",
        "target_thread_id": None,
        "status": "PAUSED",
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


def _task_metadata_equal(
    left: Mapping[str, Any], right: Mapping[str, Any], *, ignore_status: bool
) -> bool:
    """Compare all host-round-trippable task fields, except status when allowed."""
    def canonical(value: Mapping[str, Any]) -> dict[str, Any]:
        result = deepcopy(dict(value))
        if "id" not in result and "task_id" in result:
            result["id"] = result["task_id"]
        result.pop("task_id", None)
        if ignore_status:
            result.pop("status", None)
        return result

    return canonical(left) == canonical(right)


def _validate_delivered_provenance(
    checkpoint: Mapping[str, Any],
    *,
    task_id: str,
    pre_pause: Mapping[str, Any],
    post_pause: Mapping[str, Any],
) -> None:
    """Validate the exact task definition before and after the pause update."""
    retirement = checkpoint.get("task_retirement")
    successor = retirement.get("successor") if isinstance(retirement, Mapping) else None
    durable = successor.get("readback") if isinstance(successor, Mapping) else None
    if (
        not isinstance(retirement, Mapping)
        or retirement.get("phase") != "confirmed"
        or not isinstance(successor, Mapping)
        or successor.get("task_id") != task_id
        or not isinstance(durable, Mapping)
    ):
        raise StandaloneInvocationError(
            "delivered task has no durable successor readback"
        )
    for label, task in (("pre-pause", pre_pause), ("post-pause", post_pause)):
        observed_id = task.get("id", task.get("task_id"))
        if observed_id != task_id:
            raise StandaloneInvocationError(
                f"delivered {label} readback does not match task ID"
            )
        if label == "pre-pause" and task.get("status") not in {
            "ACTIVE",
            "AUTHORIZED",
        }:
            raise StandaloneInvocationError(
                "delivered pre-pause task status is invalid"
            )
        if label == "post-pause" and task.get("status") != "PAUSED":
            raise StandaloneInvocationError(
                "delivered post-pause task is not PAUSED"
            )
        if not _task_metadata_equal(task, durable, ignore_status=True):
            raise StandaloneInvocationError(
                f"delivered {label} readback does not match durable successor definition"
            )
    if not _task_metadata_equal(pre_pause, post_pause, ignore_status=True):
        raise StandaloneInvocationError(
            "delivered pre/post task metadata changed outside status"
        )


def scheduled_preflight(
    checkpoint: Mapping[str, Any],
    *,
    now: str,
    task_id: str | None = None,
    pre_pause_readback: Mapping[str, Any] | None = None,
    post_pause_readback: Mapping[str, Any] | None = None,
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
        checkpoint.get("scheduled_task_disposition") not in {"ACTIVE", "AUTHORIZED"}
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
    if pre_pause_readback is not None or post_pause_readback is not None:
        if not isinstance(pre_pause_readback, Mapping) or not isinstance(
            post_pause_readback, Mapping
        ):
            return {
                "next_action": "PAUSE_RECOVERY",
                "reason_code": "scheduler_provenance_invalid",
            }
        try:
            _validate_delivered_provenance(
                checkpoint,
                task_id=task_id,
                pre_pause=pre_pause_readback,
                post_pause=post_pause_readback,
            )
        except StandaloneInvocationError as error:
            return {
                "next_action": "PAUSE_RECOVERY",
                "reason_code": "scheduler_provenance_invalid",
                "evidence": {"error": str(error)},
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


def create_initial_setup_task(
    host: StandaloneTaskHost,
    *,
    prompt: str,
    cadence_seconds: int,
    model: str,
    reasoning_effort: str,
    record_setup_intent: RecordSetupIntent,
    record_setup_id: RecordSetupID,
    record_setup_readback: RecordSetupReadback,
    record_setup_unknown: RecordSetupUnknown,
    creation_nonce: str | None = None,
) -> dict[str, Any]:
    """Journal, create, identify, and verify exactly one paused setup task.

    This helper deliberately stops at setup verification.  It does not admit
    a wake; the caller must pass the returned structured provenance to
    ``begin-wake`` in a later lifecycle transition.
    """
    nonce = creation_nonce or secrets.token_urlsafe(18)
    now = _iso(host.now_utc())
    intent = record_setup_intent(now, nonce)
    if (
        intent.get("next_action") != "CREATION_INTENT_RECORDED"
        or intent.get("reason_code") != "creation_intent_persisted_before_create"
    ):
        raise StandaloneInvocationError(
            "Setup creation intent was not freshly persisted; recover the exact pending task"
        )
    response = host.schedule_standalone_task(
        prompt=prompt,
        cadence_seconds=cadence_seconds,
        model=model,
        reasoning_effort=reasoning_effort,
        scheduler_kind="cron",
        conversation_mode="standalone",
        target_thread_id=None,
        prompt_sha256=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        status="PAUSED",
    )
    try:
        task_id = _task_id(response)
    except StandaloneInvocationError:
        record_setup_unknown(
            now,
            {"creation_response": deepcopy(response)},
        )
        raise
    recorded_id = record_setup_id(now, task_id)
    if recorded_id.get("next_action") != "SETUP_TASK_ID_RECORDED":
        raise StandaloneInvocationError("Setup task ID was not durably recorded")
    task = host.read_task(task_id)
    _validate_task_readback(
        task,
        task_id=task_id,
        prompt=prompt,
        prompt_sha256=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        cadence_seconds=cadence_seconds,
        model=model,
        reasoning_effort=reasoning_effort,
    )
    verified = record_setup_readback(now, task)
    if verified.get("next_action") != "SETUP_TASK_VERIFIED":
        raise StandaloneInvocationError("Setup task readback was not verified")
    provenance = verified.get("setup_task_provenance")
    if not isinstance(provenance, Mapping):
        raise StandaloneInvocationError(
            "Setup readback callback did not return structured provenance"
        )
    return {
        "task_id": task_id,
        "readback": deepcopy(dict(task)),
        "provenance": deepcopy(dict(provenance)),
    }


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
        setup_task_provenance: Mapping[str, Any] | None = None,
        prepare_retirement: PrepareRetirement | None = None,
        confirm_retirement: ConfirmRetirement | None = None,
        record_successor_intent: RecordSuccessorIntent | None = None,
        record_successor_creation: RecordSuccessorCreation | None = None,
        record_successor_readback: RecordSuccessorReadback | None = None,
        record_successor_pause: RecordSuccessorPause | None = None,
        allow_legacy_direct_callbacks: bool = False,
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
        self.setup_task_provenance = (
            deepcopy(dict(setup_task_provenance))
            if isinstance(setup_task_provenance, Mapping)
            else None
        )
        self.prepare_retirement = prepare_retirement
        self.confirm_retirement = confirm_retirement
        self.record_successor_intent = record_successor_intent
        self.record_successor_creation = record_successor_creation
        self.record_successor_readback = record_successor_readback
        self.record_successor_pause = record_successor_pause
        self.allow_legacy_direct_callbacks = allow_legacy_direct_callbacks
        self.strict_scheduler_evidence = not allow_legacy_direct_callbacks
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

    def _pause_successor(self, task_id: str) -> bool:
        try:
            return _confirmed(self.host.pause_task(task_id))
        except Exception:
            return False

    def _cleanup_worktree(
        self, *, pending_repair: Mapping[str, Any] | None = None
    ) -> bool:
        try:
            if pending_repair is None:
                result = self.host.cleanup_worktree()
            else:
                result = self.host.cleanup_worktree(
                    pending_repair=deepcopy(dict(pending_repair))
                )
            return _confirmed(result)
        except Exception:
            return False

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
                pre_pause_readback: Mapping[str, Any] | None = None
                post_pause_readback: Mapping[str, Any] | None = None
                try:
                    if self.strict_scheduler_evidence:
                        pre_pause_readback = self.host.read_task(self.task_id)
                    pause_confirmed = _confirmed(self.host.pause_task(self.task_id))
                    if pause_confirmed and self.strict_scheduler_evidence:
                        post_pause_readback = self.host.read_task(self.task_id)
                except Exception:
                    pause_confirmed = False
                if not pause_confirmed:
                    try:
                        result = self.begin_wake(
                            self.wake_id,
                            self.now,
                            False,
                            self.task_id if self.scheduled else None,
                            None,
                            {} if self.strict_scheduler_evidence else None,
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
                        checkpoint,
                        now=self.now,
                        task_id=self.task_id,
                        pre_pause_readback=pre_pause_readback
                        if self.strict_scheduler_evidence
                        else None,
                        post_pause_readback=post_pause_readback
                        if self.strict_scheduler_evidence
                        else None,
                    )
                except Exception:
                    preflight = {
                        "next_action": "PAUSE_RECOVERY",
                        "reason_code": "checkpoint_invalid",
                    }
                if preflight is not None:
                    invalid_provenance = (
                        {
                            "task_id": self.task_id,
                            "pause_confirmed": True,
                            "pre_pause_readback": dict(pre_pause_readback or {}),
                            "post_pause_readback": dict(post_pause_readback or {}),
                        }
                        if self.strict_scheduler_evidence
                        and preflight.get("reason_code") == "scheduler_provenance_invalid"
                        else None
                    )
                    try:
                        persisted = self.begin_wake(
                            self.wake_id,
                            self.now,
                            True if invalid_provenance is not None else False,
                            self.task_id if self.scheduled else None,
                            None,
                            invalid_provenance
                            if invalid_provenance is not None
                            else ({} if self.strict_scheduler_evidence else None),
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
                (
                    (
                        self.setup_task_provenance
                        if self.setup_task_provenance is not None
                        else ({ } if self.strict_scheduler_evidence else None)
                    )
                    if not self.scheduled
                    else None
                ),
                (
                    {
                        "task_id": self.task_id,
                        "pause_confirmed": True,
                        "pre_pause_readback": dict(pre_pause_readback or {}),
                        "post_pause_readback": dict(post_pause_readback or {}),
                    }
                    if self.scheduled and self.strict_scheduler_evidence
                    else None
                ),
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
        end: bool = True,
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
                cleanup_confirmed = self._pause_successor(successor_id)
            self.ended = True
            if successor_id is not None and not cleanup_confirmed:
                raise StandaloneInvocationError(
                    "complete-wake failed and successor cleanup was not confirmed"
                ) from error
            raise
        if not isinstance(result, Mapping) or "next_action" not in result:
            cleanup_confirmed = (
                successor_id is None or self._pause_successor(successor_id)
            )
            self.ended = True
            if successor_id is not None and not cleanup_confirmed:
                raise StandaloneInvocationError(
                    "complete-wake returned an invalid result and successor cleanup "
                    "was not confirmed"
                )
            raise StandaloneInvocationError("complete-wake returned an invalid result")
        return self._end(result) if end else deepcopy(dict(result))

    def complete(self, *, action: str, now: str, cadence_seconds: int) -> dict[str, Any]:
        """Finalize one successor handoff, activate last, and end immediately."""
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
            authorization_attempted = False
            authorization_confirmed = False
            completion_failure: Mapping[str, Any] | None = None
            pending_repair = None
            if action == "WAIT_RETRY":
                active_batch = checkpoint.get("active_batch")
                candidate = (
                    active_batch.get("pending_repair")
                    if isinstance(active_batch, Mapping)
                    else None
                )
                if isinstance(candidate, Mapping):
                    pending_repair = deepcopy(dict(candidate))
            # Worktree cleanup is part of the completion boundary.  Complete
            # it before taking the host clock anchor and before creating a
            # recurring successor, so a long cleanup cannot make the first
            # run relative to a fictitious pre-cleanup completion time.
            if not self._cleanup_worktree(pending_repair=pending_repair):
                return self._finish_completion(
                    now=now,
                    actual_first_run=None,
                    successor_id=None,
                    completion_failure={
                        "reason_code": "worktree_cleanup_unconfirmed",
                        "evidence": {
                            "worktree_cleanup_confirmed": False,
                            "pause_confirmed": False,
                        },
                    },
                )
            retirement_enabled = all(
                callback is not None
                for callback in (
                    self.prepare_retirement,
                    self.confirm_retirement,
                    self.record_successor_intent,
                    self.record_successor_creation,
                    self.record_successor_readback,
                    self.record_successor_pause,
                )
            )
            if self.strict_scheduler_evidence and not retirement_enabled:
                return self._finish_completion(
                    now=now,
                    actual_first_run=None,
                    successor_id=None,
                    completion_failure={
                        "reason_code": "retirement_controller_unavailable",
                        "evidence": {"creation_intent_required": True},
                    },
                )
            if not retirement_enabled and not self.allow_legacy_direct_callbacks:
                # A registered predecessor is deletion authority only when the
                # complete injected controller is present.  Do not silently
                # fall back to the old accumulating-task lifecycle.
                try:
                    checkpoint = self.host.read_checkpoint_directly()
                except Exception:
                    checkpoint = {}
                retirement = checkpoint.get("task_retirement") if isinstance(checkpoint, Mapping) else None
                if (
                    isinstance(retirement, Mapping)
                    and retirement.get("wake_id") == self.wake_id
                    and retirement.get("phase") in {"registered", "pending"}
                ):
                    return self._finish_completion(
                        now=now,
                        actual_first_run=None,
                        successor_id=None,
                        completion_failure={
                            "reason_code": "retirement_controller_unavailable",
                            "evidence": {"task_id": retirement.get("task_id")},
                        },
                    )
            if retirement_enabled:
                # The pending checkpoint write is the durable boundary before
                # the irreversible exact-ID host delete.  No completion clock
                # is taken until that deletion is confirmed.
                try:
                    prepared = self.prepare_retirement(  # type: ignore[misc]
                        self.wake_id,
                        now,
                        True,
                    )
                except Exception as error:
                    return self._end(
                        {
                            "next_action": "PAUSE_RECOVERY",
                            "reason_code": "retirement_prepare_persistence_failed",
                            "evidence": {"error": str(error)},
                        }
                    )
                if prepared.get("next_action") != "RETIREMENT_PENDING":
                    return self._end(prepared)
                predecessor_id = prepared.get("scheduled_task_id")
                role = prepared.get("role")
                if not isinstance(predecessor_id, str) or role not in {"setup", "delivered"}:
                    return self._end(
                        {
                            "next_action": "PAUSE_RECOVERY",
                            "reason_code": "retirement_pending_invalid",
                        }
                    )
                delete_outcome, delete_evidence = _retirement_delete_outcome(
                    self.host, predecessor_id
                )
                try:
                    retirement = self.confirm_retirement(  # type: ignore[misc]
                        self.wake_id,
                        now,
                        predecessor_id,
                        role,
                        delete_outcome,
                        delete_evidence,
                    )
                except Exception as error:
                    # Physical deletion may already have occurred.  The
                    # pending marker remains the only recovery authority; do
                    # not manufacture a successor or overwrite it.
                    return self._end(
                        {
                            "next_action": "PAUSE_RECOVERY",
                            "reason_code": "retirement_confirmation_persistence_failed",
                            "evidence": {"error": str(error), **delete_evidence},
                        }
                    )
                if retirement.get("next_action") != "RETIREMENT_CONFIRMED":
                    return self._end(retirement)
            try:
                post_retirement_checkpoint = self.host.read_checkpoint_directly()
            except Exception:
                post_retirement_checkpoint = {}
            post_retirement_policy = (
                post_retirement_checkpoint.get("automation_policy")
                if isinstance(post_retirement_checkpoint, Mapping)
                else None
            )
            post_retirement_retirement = (
                post_retirement_checkpoint.get("task_retirement")
                if isinstance(post_retirement_checkpoint, Mapping)
                else None
            )
            maximum_wakes = (
                post_retirement_policy.get("max_wakes")
                if isinstance(post_retirement_policy, Mapping)
                else None
            )
            if (
                maximum_wakes is not None
                and isinstance(post_retirement_checkpoint, Mapping)
                and post_retirement_checkpoint.get("wake_count", 0) >= maximum_wakes
                and isinstance(post_retirement_retirement, Mapping)
                and post_retirement_retirement.get("phase") == "confirmed"
                and post_retirement_retirement.get("successor") is None
            ):
                try:
                    terminal_completion_now = _iso(self.host.now_utc())
                except Exception:
                    return self._finish_completion(
                        now=now,
                        actual_first_run=None,
                        successor_id=None,
                        completion_failure={
                            "reason_code": "completion_anchor_unavailable",
                            "evidence": {"final_wake_budget": maximum_wakes},
                        },
                    )
                return self._finish_completion(
                    now=terminal_completion_now,
                    actual_first_run=None,
                    successor_id=None,
                )
            try:
                completion_now = _iso(self.host.now_utc())
            except Exception as error:
                return self._finish_completion(
                    now=now,
                    actual_first_run=None,
                    successor_id=None,
                    completion_failure={
                        "reason_code": "completion_anchor_unavailable",
                        "evidence": {"error": str(error)},
                    },
                )
            completion = _utc(completion_now)

            try:
                if self.strict_scheduler_evidence:
                    if self.record_successor_intent is None:
                        return self._finish_completion(
                            now=completion_now,
                            actual_first_run=None,
                            successor_id=None,
                            completion_failure={
                                "reason_code": "creation_intent_persistence_failed",
                                "evidence": {"role": "successor"},
                            },
                        )
                    try:
                        existing_intent = self.host.read_checkpoint_directly().get(
                            "creation_intent"
                        )
                    except Exception:
                        existing_intent = None
                    if isinstance(existing_intent, Mapping):
                        if existing_intent.get("role") != "successor":
                            raise StandaloneInvocationError(
                                "An unresolved non-successor creation intent blocks rearm"
                            )
                        recorded = self.record_successor_creation(  # type: ignore[misc]
                            self.wake_id,
                            completion_now,
                            "SUCCESSOR_CREATION_UNKNOWN",
                            None,
                            None,
                            {"unresolved_creation_intent": dict(existing_intent)},
                        )
                        return self._end(recorded)
                    intent_result = self.record_successor_intent(
                        self.wake_id,
                        completion_now,
                        secrets.token_urlsafe(18),
                    )
                    if intent_result.get("next_action") != "CREATION_INTENT_RECORDED":
                        return self._end(intent_result)
                response = self.host.schedule_standalone_task(
                    prompt=self.prompt,
                    cadence_seconds=cadence_seconds,
                    model=model,
                    reasoning_effort=reasoning_effort,
                    scheduler_kind="cron",
                    conversation_mode="standalone",
                    target_thread_id=None,
                    prompt_sha256=self.prompt_sha256,
                    status="PAUSED",
                )
                if retirement_enabled:
                    creation_outcome = _creation_outcome(response)
                    if creation_outcome != "CREATED_EXACT_ID":
                        recorded = self.record_successor_creation(  # type: ignore[misc]
                            self.wake_id,
                            completion_now,
                            creation_outcome,
                            None,
                            None,
                            {"creation_response": response},
                        )
                        return self._end(recorded)
                successor_id = _task_id(response)
                if retirement_enabled:
                    recorded = self.record_successor_creation(  # type: ignore[misc]
                        self.wake_id,
                        completion_now,
                        "CREATED_EXACT_ID",
                        successor_id,
                        completion_now,
                        {"creation_response": response},
                    )
                    if recorded.get("next_action") != "SUCCESSOR_CREATED":
                        return self._end(recorded)
                readback_failed = True
                task = self.host.read_task(successor_id)
                _validate_task_readback(
                    task,
                    task_id=successor_id,
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
                # Scheduler task metadata is represented at whole-second
                # precision.  Compare both timestamps at that precision so a
                # task created later in the same represented second is not
                # rejected because ``now`` retained microseconds.
                if created_at.replace(microsecond=0) < completion.replace(
                    microsecond=0
                ):
                    raise StandaloneInvocationError(
                        "Standalone task creation predates wake completion"
                    )
                scheduled_created_at = created_at.isoformat()
                expected_first_run = _truncate_to_scheduler_precision(
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
                if retirement_enabled:
                    recorded = self.record_successor_readback(  # type: ignore[misc]
                        self.wake_id,
                        completion_now,
                        task,
                    )
                    if recorded.get("next_action") != "SUCCESSOR_READY":
                        return self._end(recorded)
                authorization_attempted = True
                if not _confirmed(
                    self.host.authorize_successor(
                        wake_id=self.wake_id,
                        completed_at=completion_now,
                        task_id=successor_id,
                        created_at=scheduled_created_at,
                        first_run=observed_first_run,
                        cadence_seconds=cadence_seconds,
                    )
                ):
                    raise StandaloneInvocationError(
                        "Standalone successor authorization was not confirmed"
                    )
                authorization_confirmed = True
                actual_first_run = observed_first_run
                readback_failed = False
            except Exception:
                # Authorization is a checkpoint mutation.  A lost host
                # callback may still have durably authorized this exact
                # successor, in which case retrying would duplicate the
                # authorization boundary.
                if authorization_attempted and not authorization_confirmed and successor_id is not None:
                    try:
                        durable = self.host.read_checkpoint_directly()
                        authorization = durable.get("successor_authorization")
                        authorization_confirmed = (
                            isinstance(authorization, Mapping)
                            and authorization.get("wake_id") == self.wake_id
                            and authorization.get("scheduled_task_id") == successor_id
                            and durable.get("wake_phase") == "successor_authorized"
                        )
                        if authorization_confirmed:
                            actual_first_run = observed_first_run
                            readback_failed = False
                    except Exception:
                        authorization_confirmed = False
                # If a checkpoint reread proves durable authorization, the
                # exact successor is already safe to finalize.  Skip this
                # cleanup/fallback block so a lost callback cannot pause or
                # discard its identity.
                if not authorization_confirmed:
                    if successor_id is not None:
                        pause_confirmed = self._pause_successor(successor_id)
                        if retirement_enabled:
                            try:
                                self.record_successor_pause(  # type: ignore[misc]
                                    self.wake_id,
                                    completion_now,
                                    successor_id,
                                    pause_confirmed,
                                    {"readback_failed": readback_failed},
                                )
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
                    if (
                        completion_failure is None
                        and authorization_attempted
                        and not authorization_confirmed
                    ):
                        completion_failure = {
                            "reason_code": "successor_authorization_unconfirmed",
                            "evidence": {
                                "successor_task_id": successor_id,
                            },
                        }
                    if completion_failure is None:
                        if retirement_enabled:
                            try:
                                recorded = self.record_successor_creation(  # type: ignore[misc]
                                    self.wake_id,
                                    completion_now,
                                    "SUCCESSOR_CREATION_UNKNOWN",
                                    None,
                                    None,
                                    {"creation_response": "unavailable"},
                                )
                                return self._end(recorded)
                            except Exception:
                                pass
                        successor_id = None
                    if first_run_mismatch:
                        actual_first_run = observed_first_run
                    elif readback_failed:
                        actual_first_run = ""
                    else:
                        actual_first_run = None

            if not authorization_confirmed:
                return self._finish_completion(
                    now=now,
                    actual_first_run=actual_first_run,
                    successor_id=successor_id,
                    scheduled_created_at=scheduled_created_at,
                    completion_failure=completion_failure,
                )

            finalized = self._finish_completion(
                now=completion_now if authorization_confirmed else now,
                actual_first_run=actual_first_run,
                successor_id=successor_id,
                scheduled_created_at=scheduled_created_at,
                completion_failure=completion_failure,
                end=False,
            )
            if finalized.get("next_action") not in REARM_ACTIONS:
                return self._end(finalized)
            try:
                activation_confirmed = _confirmed(self.host.activate_task(successor_id))
            except Exception:
                activation_confirmed = False
            if activation_confirmed:
                # No pulse, cleanup, validation, or other host work follows
                # this final host mutation.  The durable checkpoint remains
                # AUTHORIZED so a delivered task consumes the handoff instead
                # of treating it as a restart that needs activation again.
                return self._end(finalized)

            pause_confirmed = self._pause_successor(successor_id)
            return self._finish_completion(
                now=completion_now,
                actual_first_run=actual_first_run,
                successor_id=successor_id,
                scheduled_created_at=scheduled_created_at,
                completion_failure={
                    "reason_code": (
                        "successor_activation_unconfirmed"
                        if pause_confirmed
                        else "successor_cleanup_unconfirmed"
                    ),
                    "evidence": {
                        "successor_task_id": successor_id,
                        "activation_confirmed": False,
                        "pause_confirmed": pause_confirmed,
                    },
                },
            )
