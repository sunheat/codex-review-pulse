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
        first_run: str,
        scheduler_kind: str,
        conversation_mode: str,
        target_thread_id: None,
        prompt_sha256: str,
    ) -> object:
        """Create one future standalone task with the unchanged prompt."""

    def read_task(self, task_id: str) -> Mapping[str, Any]:
        """Read normalized task metadata and the persisted first-run timestamp."""


BeginWake = Callable[[str, str, bool], Mapping[str, Any]]
CompleteWake = Callable[
    [str, str, Callable[[str], object], str | None], Mapping[str, Any]
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
    task: Mapping[str, Any], *, prompt: str, prompt_sha256: str
) -> None:
    expected = {
        "scheduler_kind": "cron",
        "conversation_mode": "standalone",
        "target_thread_id": None,
        "prompt": prompt,
        "prompt_sha256": prompt_sha256,
    }
    for key, value in expected.items():
        if task.get(key) != value:
            raise StandaloneInvocationError(
                f"Standalone task readback does not match {key}"
            )


def scheduled_preflight(
    checkpoint: Mapping[str, Any], *, now: str
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
                    return self._end(
                        {
                            "next_action": "PAUSE_BLOCKED",
                            "reason_code": "standalone_task_pause_unconfirmed",
                        }
                    )
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
                    preflight = scheduled_preflight(checkpoint, now=self.now)
                except Exception:
                    preflight = {
                        "next_action": "PAUSE_RECOVERY",
                        "reason_code": "checkpoint_invalid",
                    }
                if preflight is not None:
                    return self._end(preflight)

            result = self.begin_wake(self.wake_id, self.now, True)
            if not isinstance(result, Mapping) or "next_action" not in result:
                raise StandaloneInvocationError("begin-wake returned an invalid result")
            if result.get("next_action") != "WAKE_STARTED":
                return self._end(result)
            return dict(result)

    def complete(self, *, action: str, now: str, cadence_seconds: int) -> dict[str, Any]:
        """Create/read back one successor, complete once, and end immediately."""
        with self._serialized_operation():
            if not self.started or self.wake_id is None:
                raise StandaloneInvocationError("complete-wake requires an active wake")
            if action not in REARM_ACTIONS:
                raise StandaloneInvocationError(
                    f"The action {action!r} is not eligible for a standalone successor"
                )
            if isinstance(cadence_seconds, bool) or cadence_seconds <= 0:
                raise ValueError("Cadence must be positive")
            completion = _utc(now)
            expected_first_run = _ceil_to_second(
                completion + timedelta(seconds=cadence_seconds)
            ).isoformat()

            successor_id: str | None = None
            actual_first_run: object = None
            try:
                response = self.host.schedule_standalone_task(
                    prompt=self.prompt,
                    first_run=expected_first_run,
                    scheduler_kind="cron",
                    conversation_mode="standalone",
                    target_thread_id=None,
                    prompt_sha256=self.prompt_sha256,
                )
                successor_id = _task_id(response)
                task = self.host.read_task(successor_id)
                _validate_task_readback(
                    task, prompt=self.prompt, prompt_sha256=self.prompt_sha256
                )
                actual_first_run = task.get("first_run")
                if not isinstance(actual_first_run, str) or not actual_first_run.strip():
                    raise StandaloneInvocationError(
                        "Standalone task readback returned no persisted first run"
                    )
            except Exception:
                successor_id = None
                actual_first_run = None

            result = self.complete_wake(
                self.wake_id,
                now,
                lambda _expected: actual_first_run,
                successor_id,
            )
            if not isinstance(result, Mapping) or "next_action" not in result:
                raise StandaloneInvocationError("complete-wake returned an invalid result")
            return self._end(result)
