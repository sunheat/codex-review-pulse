from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "codex-review-pulse" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import pulse  # noqa: E402
from state_model import empty_checkpoint  # noqa: E402


NOW = "2026-08-26T00:00:00+00:00"
NEXT_WAKE = "2026-08-26T00:36:00+00:00"


def review_snapshot(*, eyes: bool = False) -> dict[str, object]:
    return {
        "head_oid": "HEAD1",
        "pull_request_state": "OPEN",
        "targeted_thread_ids": [],
        "review_in_progress": {"active": eyes},
        "review_activity_ok": True,
        "approval_evidence": {"status": "awaiting_current_head_approval"},
        "snapshot_stable": True,
        "server_evidence": {"head_before": "HEAD1", "head_after": "HEAD1"},
    }


def waiting_checkpoint() -> dict[str, object]:
    state, _ = pulse.begin_wake(
        empty_checkpoint("Owner/Repo", 17),
        wake_id="seed-wake",
        now=NOW,
        pause_heartbeat=lambda: True,
    )
    state, _ = pulse.record_snapshot(
        state,
        review_snapshot(),
        wake_id="seed-wake",
        now=NOW,
    )
    state, _ = pulse.complete_wake(
        state,
        wake_id="seed-wake",
        now="2026-08-26T00:26:00+00:00",
        schedule_next_wake=lambda expected: expected,
    )
    return state


class InvocationEnded(RuntimeError):
    """Raised when a host tries to do work after the invocation boundary."""


class CheckpointUnavailable(RuntimeError):
    """Raised by the in-memory host for a missing or unreadable checkpoint."""


class InMemoryHost:
    """Small host double for the default skill's invocation contract."""

    def __init__(
        self,
        *,
        state: dict[str, object] | None = None,
        wake_ids: tuple[str, ...] = ("fresh-wake-1",),
        checkpoint_missing: bool = False,
        checkpoint_unreadable: bool = False,
    ) -> None:
        self.state = deepcopy(state)
        self._wake_ids = iter(wake_ids)
        self.issued_ids: list[str] = []
        self.operations: list[tuple[str, str]] = []
        self.checkpoint_missing = checkpoint_missing
        self.checkpoint_unreadable = checkpoint_unreadable

    def new_opaque_wake_id(self) -> str:
        wake_id = next(self._wake_ids)
        checkpoint_ids = {
            (self.state or {}).get("active_wake_id"),
            (self.state or {}).get("last_wake_id"),
        }
        if not wake_id or wake_id in self.issued_ids or wake_id in checkpoint_ids:
            raise AssertionError("host wake IDs must be fresh and opaque")
        self.issued_ids.append(wake_id)
        return wake_id

    def pause_task(self, task_id: str) -> None:
        self.operations.append(("pause-task", task_id))

    def read_checkpoint_directly(self) -> dict[str, object]:
        self.operations.append(("checkpoint-read", "direct"))
        if self.checkpoint_missing or self.checkpoint_unreadable or self.state is None:
            raise CheckpointUnavailable
        return deepcopy(self.state)


class HostInvocation:
    """Model one host invocation and make post-completion calls impossible."""

    def __init__(self, host: InMemoryHost, *, scheduled: bool, now: str) -> None:
        self.host = host
        self.scheduled = scheduled
        self.now = now
        self.wake_id: str | None = None
        self.started = False
        self.ended = False

    def _ensure_open(self) -> None:
        if self.ended:
            raise InvocationEnded("the host invocation ended after its final wake result")

    def _end(self, result: dict[str, object]) -> dict[str, object]:
        self.host.operations.append(("report", str(result["next_action"])))
        self.ended = True
        return result

    @staticmethod
    def _preflight_result(checkpoint: dict[str, object], now: str) -> dict[str, object] | None:
        if checkpoint.get("active_wake_id"):
            return {"next_action": "PAUSE_RECOVERY", "reason_code": "incomplete_wake"}
        if checkpoint.get("failure_latch"):
            return {"next_action": "PAUSE_RECOVERY", "reason_code": "failure_latched"}
        next_not_before = checkpoint.get("next_not_before")
        if next_not_before and datetime.fromisoformat(now) < datetime.fromisoformat(str(next_not_before)):
            return {"next_action": "PAUSE_BLOCKED", "reason_code": "cadence_not_elapsed"}
        return None

    def begin(self) -> dict[str, object]:
        self._ensure_open()
        if self.started:
            raise AssertionError("one host invocation may begin only one wake")
        self.started = True
        self.wake_id = self.host.new_opaque_wake_id()

        if self.scheduled:
            self.host.pause_task("task-1")
            try:
                checkpoint = self.host.read_checkpoint_directly()
            except CheckpointUnavailable:
                return self._end(
                    {"next_action": "PAUSE_RECOVERY", "reason_code": "checkpoint_unavailable"}
                )
            preflight = self._preflight_result(checkpoint, self.now)
            if preflight is not None:
                return self._end(preflight)

        self.host.operations.append(("begin-wake", self.wake_id))
        state = self.host.state or empty_checkpoint("Owner/Repo", 17)
        self.host.state, result = pulse.begin_wake(
            state,
            wake_id=self.wake_id,
            now=self.now,
            pause_heartbeat=lambda: True,
        )
        if result["next_action"] != "WAKE_STARTED":
            return self._end(result)
        return result

    def snapshot(self) -> dict[str, object]:
        self._ensure_open()
        if not self.started or self.wake_id is None:
            raise AssertionError("snapshot requires the active wake")
        self.host.operations.append(("snapshot", self.wake_id))
        self.host.state, result = pulse.record_snapshot(
            self.host.state or {},
            review_snapshot(eyes=True),
            wake_id=self.wake_id,
            now=self.now,
        )
        return result

    def complete(self, *, reanchor_succeeds: bool) -> dict[str, object]:
        self._ensure_open()
        if not self.started or self.wake_id is None:
            raise AssertionError("complete requires the active wake")
        self.host.operations.append(("reanchor-task", "success" if reanchor_succeeds else "failed"))
        self.host.operations.append(
            (
                "complete-wake",
                "--schedule-reanchored" if reanchor_succeeds else "unconfirmed",
            )
        )
        self.host.state, result = pulse.complete_wake(
            self.host.state or {},
            wake_id=self.wake_id,
            now=(datetime.fromisoformat(self.now) + timedelta(minutes=1)).isoformat(),
            schedule_next_wake=(
                (lambda expected: expected) if reanchor_succeeds else (lambda _: None)
            ),
        )
        return self._end(result)


class DefaultHostWakeContractTests(unittest.TestCase):
    def test_one_invocation_can_begin_only_one_wake(self) -> None:
        host = InMemoryHost(wake_ids=("fresh-wake-1",))
        invocation = HostInvocation(host, scheduled=False, now=NOW)

        self.assertEqual(invocation.begin()["next_action"], "WAKE_STARTED")
        with self.assertRaises(AssertionError):
            invocation.begin()
        self.assertEqual(host.issued_ids, ["fresh-wake-1"])

    def test_each_new_invocation_generates_a_fresh_wake_id(self) -> None:
        host = InMemoryHost(
            state=waiting_checkpoint(),
            wake_ids=("fresh-wake-2", "fresh-wake-3"),
        )
        first = HostInvocation(host, scheduled=True, now=NEXT_WAKE)
        first.begin()
        first.snapshot()
        first.complete(reanchor_succeeds=True)

        second = HostInvocation(host, scheduled=True, now="2026-08-26T00:47:00+00:00")
        second.begin()

        self.assertEqual(host.issued_ids, ["fresh-wake-2", "fresh-wake-3"])
        self.assertNotEqual(first.wake_id, second.wake_id)
        self.assertEqual(host.operations.count(("begin-wake", "fresh-wake-2")), 1)
        self.assertEqual(host.operations.count(("begin-wake", "fresh-wake-3")), 1)

    def test_scheduled_begin_requires_direct_checkpoint_preflight(self) -> None:
        cases = (
            ("missing", {"checkpoint_missing": True}, "checkpoint_unavailable"),
            ("unreadable", {"checkpoint_unreadable": True}, "checkpoint_unavailable"),
            ("active", {"state": {**waiting_checkpoint(), "active_wake_id": "other-wake"}}, "incomplete_wake"),
            (
                "latched",
                {"state": {**waiting_checkpoint(), "failure_latch": {"reason_code": "old-failure"}}},
                "failure_latched",
            ),
            ("early", {"state": waiting_checkpoint()}, "cadence_not_elapsed"),
        )
        for name, options, reason_code in cases:
            with self.subTest(name=name):
                host = InMemoryHost(**options)
                invocation = HostInvocation(host, scheduled=True, now="2026-08-26T00:27:00+00:00")
                result = invocation.begin()

                self.assertEqual(result["reason_code"], reason_code)
                self.assertTrue(invocation.ended)
                self.assertNotIn(("begin-wake", invocation.wake_id), host.operations)

    def test_complete_wake_ends_successful_and_failed_reanchor_invocations(self) -> None:
        for reanchor_succeeds, expected_action in (
            (True, "WAIT_REVIEW"),
            (False, "PAUSE_BLOCKED"),
        ):
            with self.subTest(reanchor_succeeds=reanchor_succeeds):
                host = InMemoryHost(
                    state=waiting_checkpoint(),
                    wake_ids=("fresh-wake-2",),
                )
                invocation = HostInvocation(host, scheduled=True, now=NEXT_WAKE)
                invocation.begin()
                invocation.snapshot()
                result = invocation.complete(reanchor_succeeds=reanchor_succeeds)

                self.assertEqual(result["next_action"], expected_action)
                self.assertTrue(invocation.ended)
                self.assertEqual(host.operations[-2][0], "complete-wake")
                self.assertEqual(host.operations[-1], ("report", expected_action))
                with self.assertRaises(InvocationEnded):
                    invocation.snapshot()

    def test_premature_fresh_id_latches_and_reuse_is_idempotent(self) -> None:
        state = waiting_checkpoint()
        state, early = pulse.begin_wake(
            state,
            wake_id="early-fresh-wake",
            now="2026-08-26T00:27:00+00:00",
            pause_heartbeat=lambda: True,
        )
        self.assertEqual(early["next_action"], "PAUSE_BLOCKED")
        self.assertEqual(early["reason_code"], "cadence_not_elapsed")

        before_replay = deepcopy(state)
        state, replay = pulse.begin_wake(
            state,
            wake_id="early-fresh-wake",
            now="2026-08-26T00:28:00+00:00",
            pause_heartbeat=lambda: True,
        )
        self.assertEqual(replay, early)
        self.assertEqual(state, before_replay)

        state, recovery = pulse.begin_wake(
            state,
            wake_id="another-fresh-wake",
            now="2026-08-26T00:29:00+00:00",
            pause_heartbeat=lambda: True,
        )
        self.assertEqual(recovery["next_action"], "PAUSE_RECOVERY")
        self.assertEqual(recovery["reason_code"], "failure_latched")


if __name__ == "__main__":
    unittest.main()
