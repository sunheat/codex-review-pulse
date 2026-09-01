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
from standalone_orchestration import StandaloneInvocation, StandaloneInvocationError  # noqa: E402
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


class InMemoryHost:
    """Small host double for the default skill's standalone-task contract."""

    def __init__(
        self,
        *,
        state: dict[str, object] | None = None,
        wake_ids: tuple[str, ...] = ("fresh-wake-1",),
        checkpoint_missing: bool = False,
        checkpoint_unreadable: bool = False,
        schedule_fails: bool = False,
        first_run: str | None = None,
        pause_succeeds: bool = True,
        complete_raises: bool = False,
    ) -> None:
        self.state = deepcopy(state)
        self._wake_ids = iter(wake_ids)
        self.issued_ids: list[str] = []
        self.operations: list[tuple[str, str]] = []
        self.checkpoint_missing = checkpoint_missing
        self.checkpoint_unreadable = checkpoint_unreadable
        self.schedule_fails = schedule_fails
        self.first_run = first_run
        self.pause_succeeds = pause_succeeds
        self.complete_raises = complete_raises
        self.created_tasks: dict[str, dict[str, object]] = {}

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

    def pause_task(self, task_id: str) -> bool:
        self.operations.append(("pause-task", task_id))
        return self.pause_succeeds

    def read_checkpoint_directly(self) -> dict[str, object]:
        self.operations.append(("checkpoint-read", "direct"))
        if self.checkpoint_missing or self.checkpoint_unreadable or self.state is None:
            raise RuntimeError("checkpoint unavailable")
        return deepcopy(self.state)

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
        self.operations.append(("schedule-standalone", first_run))
        if self.schedule_fails:
            raise RuntimeError("scheduler rejected standalone task")
        task_id = f"task-{len(self.created_tasks) + 2}"
        self.created_tasks[task_id] = {
            "prompt": prompt,
            "first_run": first_run,
            "scheduler_kind": scheduler_kind,
            "conversation_mode": conversation_mode,
            "target_thread_id": target_thread_id,
            "prompt_sha256": prompt_sha256,
        }
        return {"id": task_id}

    def read_task(self, task_id: str) -> dict[str, object]:
        self.operations.append(("read-standalone", task_id))
        task = self.created_tasks[task_id]
        return {
            **task,
            "first_run": self.first_run or task["first_run"],
        }


class HostInvocation:
    """Drive the real standalone invocation guard with injected callbacks."""

    def __init__(self, host: InMemoryHost, *, scheduled: bool, now: str) -> None:
        self.host = host
        self.invocation = StandaloneInvocation(
            host,
            task_id="task-1",
            prompt="standalone prompt",
            scheduled=scheduled,
            now=now,
            begin_wake=self._begin_wake,
            complete_wake=self._complete_wake,
        )

    @property
    def wake_id(self) -> str | None:
        return self.invocation.wake_id

    @property
    def ended(self) -> bool:
        return self.invocation.ended

    def _begin_wake(self, wake_id: str, now: str, pause_confirmed: bool) -> dict[str, object]:
        self.host.operations.append(("begin-wake", wake_id))
        state = self.host.state or empty_checkpoint("Owner/Repo", 17)
        self.host.state, result = pulse.begin_wake(
            state,
            wake_id=wake_id,
            now=now,
            pause_heartbeat=lambda: pause_confirmed,
        )
        return result

    def _complete_wake(
        self,
        wake_id: str,
        now: str,
        schedule_next_wake,
        scheduled_task_id: str | None,
    ) -> dict[str, object]:
        self.host.operations.append(("complete-wake", scheduled_task_id or "unconfirmed"))
        if self.host.complete_raises:
            raise RuntimeError("checkpoint persistence failed")
        self.host.state, result = pulse.complete_wake(
            self.host.state or {},
            wake_id=wake_id,
            now=now,
            schedule_next_wake=schedule_next_wake,
            scheduled_task_id=scheduled_task_id,
        )
        return result

    def begin(self) -> dict[str, object]:
        return self.invocation.begin()

    def snapshot(self) -> dict[str, object]:
        if self.invocation.ended:
            raise StandaloneInvocationError("invocation ended")
        if not self.invocation.started or self.wake_id is None:
            raise AssertionError("snapshot requires the active wake")
        self.host.operations.append(("snapshot", self.wake_id))
        self.host.state, result = pulse.record_snapshot(
            self.host.state or {},
            review_snapshot(eyes=True),
            wake_id=self.wake_id,
            now=self.invocation.now,
        )
        return result

    def complete(
        self, *, reanchor_succeeds: bool, action: str = "WAIT_REVIEW"
    ) -> dict[str, object]:
        if not reanchor_succeeds:
            self.host.schedule_fails = True
        return self.invocation.complete(
            action=action,
            now=(datetime.fromisoformat(self.invocation.now) + timedelta(minutes=1)).isoformat(),
            cadence_seconds=600,
        )


class DefaultHostWakeContractTests(unittest.TestCase):
    def test_one_invocation_can_begin_only_one_wake(self) -> None:
        host = InMemoryHost(wake_ids=("fresh-wake-1",))
        invocation = HostInvocation(host, scheduled=False, now=NOW)

        self.assertEqual(invocation.begin()["next_action"], "WAKE_STARTED")
        with self.assertRaises(StandaloneInvocationError):
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
                self.assertEqual(host.operations[-1][0], "complete-wake")
                if reanchor_succeeds:
                    self.assertEqual(host.operations[-3][0], "schedule-standalone")
                    self.assertEqual(host.operations[-2][0], "read-standalone")
                    self.assertEqual(result["scheduled_task_id"], "task-2")
                with self.assertRaises(StandaloneInvocationError):
                    invocation.snapshot()

    def test_successor_is_standalone_and_reuses_only_the_canonical_prompt(self) -> None:
        host = InMemoryHost(
            state=waiting_checkpoint(),
            wake_ids=("fresh-wake-2",),
        )
        invocation = HostInvocation(host, scheduled=True, now=NEXT_WAKE)
        invocation.begin()
        invocation.snapshot()

        result = invocation.complete(reanchor_succeeds=True)

        self.assertEqual(result["next_action"], "WAIT_REVIEW")
        self.assertEqual(host.created_tasks["task-2"]["prompt"], "standalone prompt")
        self.assertEqual(host.created_tasks["task-2"]["scheduler_kind"], "cron")
        self.assertEqual(host.created_tasks["task-2"]["conversation_mode"], "standalone")
        self.assertIsNone(host.created_tasks["task-2"]["target_thread_id"])
        self.assertEqual(host.created_tasks["task-2"]["first_run"], "2026-08-26T00:47:00+00:00")
        self.assertEqual(host.operations[:4], [
            ("pause-task", "task-1"),
            ("checkpoint-read", "direct"),
            ("begin-wake", "fresh-wake-2"),
            ("snapshot", "fresh-wake-2"),
        ])

    def test_successor_creation_failure_completes_once_and_keeps_checkpoint_paused(self) -> None:
        host = InMemoryHost(
            state=waiting_checkpoint(),
            wake_ids=("fresh-wake-2",),
            schedule_fails=True,
        )
        invocation = HostInvocation(host, scheduled=True, now=NEXT_WAKE)
        invocation.begin()
        invocation.snapshot()

        result = invocation.complete(reanchor_succeeds=True)

        self.assertEqual(result["next_action"], "PAUSE_BLOCKED")
        self.assertEqual(result["reason_code"], "scheduled_task_reanchor_unavailable")
        self.assertTrue(invocation.ended)
        self.assertEqual(host.operations.count(("complete-wake", "unconfirmed")), 1)
        with self.assertRaises(StandaloneInvocationError):
            invocation.complete(reanchor_succeeds=True)

    def test_successor_readback_mismatch_is_unverified_and_keeps_checkpoint_paused(self) -> None:
        host = InMemoryHost(
            state=waiting_checkpoint(),
            wake_ids=("fresh-wake-2",),
            first_run="2026-08-26T00:48:00+00:00",
        )
        invocation = HostInvocation(host, scheduled=True, now=NEXT_WAKE)
        invocation.begin()
        invocation.snapshot()

        result = invocation.complete(reanchor_succeeds=True)

        self.assertEqual(result["next_action"], "PAUSE_BLOCKED")
        self.assertEqual(result["reason_code"], "scheduled_task_reanchor_mismatch")
        self.assertNotIn("scheduled_task_id", result)
        self.assertEqual(host.state["scheduled_task_disposition"], "PAUSED")
        self.assertTrue(invocation.ended)

    def test_terminal_result_cannot_create_a_successor_or_call_complete(self) -> None:
        host = InMemoryHost(
            state=waiting_checkpoint(),
            wake_ids=("fresh-wake-2",),
        )
        invocation = HostInvocation(host, scheduled=True, now=NEXT_WAKE)
        invocation.begin()
        invocation.snapshot()

        with self.assertRaises(StandaloneInvocationError):
            invocation.complete(reanchor_succeeds=True, action="STOP_TERMINAL")

        self.assertNotIn(("schedule-standalone", "2026-08-26T00:47:00+00:00"), host.operations)
        self.assertNotIn(("complete-wake", "task-2"), host.operations)

    def test_failed_task_pause_stops_before_checkpoint_read(self) -> None:
        host = InMemoryHost(
            state=waiting_checkpoint(),
            wake_ids=("fresh-wake-2",),
            pause_succeeds=False,
        )
        invocation = HostInvocation(host, scheduled=True, now=NEXT_WAKE)

        result = invocation.begin()

        self.assertEqual(result["reason_code"], "heartbeat_pause_unconfirmed")
        self.assertEqual(host.state["scheduled_task_disposition"], "PAUSED")
        self.assertEqual(host.state["failure_latch"]["reason_code"], "heartbeat_pause_unconfirmed")
        self.assertEqual(host.operations, [("pause-task", "task-1"), ("begin-wake", "fresh-wake-2")])

    def test_completion_callback_failure_ends_invocation_without_duplicate_successor(self) -> None:
        host = InMemoryHost(
            state=waiting_checkpoint(),
            wake_ids=("fresh-wake-2",),
            complete_raises=True,
        )
        invocation = HostInvocation(host, scheduled=True, now=NEXT_WAKE)
        invocation.begin()
        invocation.snapshot()

        with self.assertRaises(RuntimeError):
            invocation.complete(reanchor_succeeds=True)

        self.assertTrue(invocation.ended)
        self.assertEqual(len(host.created_tasks), 1)
        with self.assertRaises(StandaloneInvocationError):
            invocation.complete(reanchor_succeeds=True)

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
