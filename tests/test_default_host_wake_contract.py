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
        scheduled_task_id="task-1",
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
        created_at: str | None = None,
        pause_succeeds: bool = True,
        pause_results: tuple[bool, ...] | None = None,
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
        self.created_at = created_at
        self.next_creation_time: str | None = None
        self.pause_succeeds = pause_succeeds
        self.pause_results = pause_results
        self.pause_calls = 0
        self.complete_raises = complete_raises
        self.created_tasks: dict[str, dict[str, object]] = {}
        self.paused_task_ids: list[str] = []

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
        self.pause_calls += 1
        result = (
            self.pause_results[self.pause_calls - 1]
            if self.pause_results is not None and self.pause_calls <= len(self.pause_results)
            else self.pause_succeeds
        )
        if result:
            self.paused_task_ids.append(task_id)
        return result

    def read_checkpoint_directly(self) -> dict[str, object]:
        self.operations.append(("checkpoint-read", "direct"))
        if self.checkpoint_missing or self.checkpoint_unreadable or self.state is None:
            raise RuntimeError("checkpoint unavailable")
        return deepcopy(self.state)

    def schedule_standalone_task(
        self,
        *,
        prompt: str,
        cadence_seconds: int,
        scheduler_kind: str,
        conversation_mode: str,
        target_thread_id: None,
        prompt_sha256: str,
    ) -> object:
        self.operations.append(("schedule-standalone", str(cadence_seconds)))
        if self.schedule_fails:
            raise RuntimeError("scheduler rejected standalone task")
        task_id = f"task-{len(self.created_tasks) + 2}"
        created_at = self.created_at or self.next_creation_time
        if created_at is None:
            raise AssertionError("test host requires a creation timestamp")
        first_run = (
            datetime.fromisoformat(created_at) + timedelta(seconds=cadence_seconds)
        ).isoformat()
        self.created_tasks[task_id] = {
            "prompt": prompt,
            "first_run": first_run,
            "created_at": created_at,
            "cadence_seconds": cadence_seconds,
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

    def __init__(
        self,
        host: InMemoryHost,
        *,
        scheduled: bool,
        now: str,
        task_id: str = "task-1",
    ) -> None:
        self.host = host
        self.invocation = StandaloneInvocation(
            host,
            task_id=task_id,
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

    def _begin_wake(
        self,
        wake_id: str,
        now: str,
        pause_confirmed: bool,
        delivered_task_id: str | None,
    ) -> dict[str, object]:
        self.host.operations.append(("begin-wake", wake_id))
        state = self.host.state or empty_checkpoint("Owner/Repo", 17)
        self.host.state, result = pulse.begin_wake(
            state,
            wake_id=wake_id,
            now=now,
            pause_heartbeat=lambda: pause_confirmed,
            delivered_task_id=delivered_task_id,
        )
        return result

    def _complete_wake(
        self,
        wake_id: str,
        now: str,
        schedule_next_wake,
        scheduled_task_id: str | None,
        scheduled_created_at: str | None,
        completion_failure: dict[str, object] | None,
    ) -> dict[str, object]:
        self.host.operations.append(("complete-wake", scheduled_task_id or "unconfirmed"))
        if self.host.complete_raises:
            raise RuntimeError("checkpoint persistence failed")
        self.host.state, result = pulse.complete_wake(
            self.host.state or {},
            wake_id=wake_id,
            now=now,
            schedule_next_wake=schedule_next_wake,
            schedule_anchor_created_at=scheduled_created_at,
            scheduled_task_id=scheduled_task_id,
            completion_failure=completion_failure,
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
        self,
        *,
        reanchor_succeeds: bool,
        action: str = "WAIT_REVIEW",
        cadence_seconds: int = 600,
    ) -> dict[str, object]:
        if not reanchor_succeeds:
            self.host.schedule_fails = True
        completion_now = (
            datetime.fromisoformat(self.invocation.now) + timedelta(minutes=1)
        ).isoformat()
        self.host.next_creation_time = completion_now
        return self.invocation.complete(
            action=action,
            now=completion_now,
            cadence_seconds=cadence_seconds,
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

        second = HostInvocation(
            host,
            scheduled=True,
            now="2026-08-26T00:47:00+00:00",
            task_id="task-2",
        )
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
        self.assertEqual(host.created_tasks["task-2"]["created_at"], "2026-08-26T00:37:00+00:00")
        self.assertEqual(host.created_tasks["task-2"]["cadence_seconds"], 600)
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
        self.assertIn("task-2", host.paused_task_ids)
        self.assertIn(("pause-task", "task-2"), host.operations)
        self.assertTrue(invocation.ended)

    def test_successor_cleanup_failure_latches_recovery_without_claiming_cleanup(self) -> None:
        host = InMemoryHost(
            state=waiting_checkpoint(),
            wake_ids=("fresh-wake-2",),
            first_run="2026-08-26T00:48:00+00:00",
            pause_results=(True, False),
        )
        invocation = HostInvocation(host, scheduled=True, now=NEXT_WAKE)
        invocation.begin()
        invocation.snapshot()

        result = invocation.complete(reanchor_succeeds=True)

        self.assertEqual(result["next_action"], "PAUSE_RECOVERY")
        self.assertEqual(result["reason_code"], "successor_cleanup_unconfirmed")
        self.assertEqual(
            result["evidence"],
            {"successor_task_id": "task-2", "pause_confirmed": False},
        )
        self.assertEqual(
            host.state["failure_latch"]["reason_code"],
            "successor_cleanup_unconfirmed",
        )
        self.assertEqual(host.state["scheduled_task_disposition"], "PAUSED")
        self.assertIn(("pause-task", "task-2"), host.operations)
        self.assertTrue(invocation.ended)

    def test_incomplete_batch_publication_cannot_schedule_a_successor(self) -> None:
        host = InMemoryHost(
            state=waiting_checkpoint(),
            wake_ids=("fresh-wake-2",),
        )
        invocation = HostInvocation(host, scheduled=True, now=NEXT_WAKE)
        invocation.begin()
        host.state["last_decision"] = {
            "next_action": "RUN_BATCH",
            "mutation_occurred": False,
        }
        host.state["active_batch"] = {"publication": {"status": "not_started"}}

        result = invocation.complete(reanchor_succeeds=True, action="RUN_BATCH")

        self.assertEqual(result["next_action"], "PAUSE_RECOVERY")
        self.assertEqual(result["reason_code"], "batch_publication_incomplete")
        self.assertNotIn(("schedule-standalone", "600"), host.operations)
        self.assertNotIn(("read-standalone", "task-2"), host.operations)
        self.assertEqual(host.operations[-2:], [
            ("checkpoint-read", "direct"),
            ("complete-wake", "unconfirmed"),
        ])
        self.assertTrue(invocation.ended)

    def test_completion_rejects_an_action_that_differs_from_the_checkpoint(self) -> None:
        host = InMemoryHost(
            state=waiting_checkpoint(),
            wake_ids=("fresh-wake-2",),
        )
        invocation = HostInvocation(host, scheduled=True, now=NEXT_WAKE)
        invocation.begin()
        host.state["last_decision"] = {
            "next_action": "RUN_BATCH",
            "mutation_occurred": False,
        }
        host.state["active_batch"] = {"publication": {"status": "not_started"}}

        result = invocation.complete(reanchor_succeeds=True, action="WAIT_REVIEW")

        self.assertEqual(result["next_action"], "PAUSE_RECOVERY")
        self.assertEqual(result["reason_code"], "completion_action_mismatch")
        self.assertEqual(
            result["evidence"],
            {"requested_action": "WAIT_REVIEW", "persisted_action": "RUN_BATCH"},
        )
        self.assertNotIn(("schedule-standalone", "600"), host.operations)
        self.assertTrue(invocation.ended)

    def test_completion_rejects_a_cadence_that_differs_from_the_checkpoint(self) -> None:
        state = waiting_checkpoint()
        state["last_decision"] = {
            "next_action": "WAIT_REVIEW",
            "mutation_occurred": False,
        }
        state["automation_policy"]["cadence_seconds"] = 1200
        host = InMemoryHost(state=state, wake_ids=("fresh-wake-2",))
        invocation = HostInvocation(host, scheduled=True, now=NEXT_WAKE)
        invocation.begin()
        host.state["last_decision"] = {
            "next_action": "WAIT_REVIEW",
            "mutation_occurred": False,
        }

        result = invocation.complete(reanchor_succeeds=True, cadence_seconds=600)

        self.assertEqual(result["next_action"], "PAUSE_RECOVERY")
        self.assertEqual(result["reason_code"], "completion_cadence_mismatch")
        self.assertEqual(
            result["evidence"],
            {
                "requested_cadence_seconds": 600,
                "persisted_cadence_seconds": 1200,
            },
        )
        self.assertNotIn(("schedule-standalone", "600"), host.operations)
        self.assertTrue(invocation.ended)

    def test_unconfirmed_review_trigger_cannot_schedule_a_successor(self) -> None:
        for event in ({}, {"status": "attempted"}):
            with self.subTest(event=event):
                host = InMemoryHost(
                    state=waiting_checkpoint(),
                    wake_ids=("fresh-wake-2",),
                )
                invocation = HostInvocation(host, scheduled=True, now=NEXT_WAKE)
                invocation.begin()
                host.state["last_decision"] = {
                    "next_action": "REQUEST_REVIEW",
                    "mutation_occurred": False,
                }
                host.state["trigger_events"] = {"HEAD1": event}

                result = invocation.complete(
                    reanchor_succeeds=True,
                    action="REQUEST_REVIEW",
                )

                self.assertEqual(result["next_action"], "PAUSE_RECOVERY")
                self.assertEqual(result["reason_code"], "review_trigger_not_confirmed")
                self.assertNotIn(
                    ("schedule-standalone", "600"),
                    host.operations,
                )
                self.assertNotIn(("read-standalone", "task-2"), host.operations)
                self.assertEqual(host.state["scheduled_task_disposition"], "PAUSED")
                self.assertEqual(
                    host.state["failure_latch"]["reason_code"],
                    "review_trigger_not_confirmed",
                )
                self.assertTrue(invocation.ended)

    def test_scheduled_begin_requires_the_persisted_active_successor(self) -> None:
        cases = (
            ("wrong task", {}, "stale-task"),
            ("missing task", {"scheduled_task_id": None}, "task-1"),
            ("paused task", {"scheduled_task_disposition": "PAUSED"}, "task-1"),
        )
        for name, changes, delivered_task_id in cases:
            with self.subTest(name=name):
                state = waiting_checkpoint()
                state.update(changes)
                host = InMemoryHost(state=state, wake_ids=("fresh-wake-2",))
                invocation = HostInvocation(
                    host,
                    scheduled=True,
                    now=NEXT_WAKE,
                    task_id=delivered_task_id,
                )

                result = invocation.begin()

                self.assertEqual(result["next_action"], "PAUSE_RECOVERY")
                self.assertEqual(result["reason_code"], "scheduled_task_identity_mismatch")
                self.assertTrue(invocation.ended)
                self.assertNotIn(("begin-wake", invocation.wake_id), host.operations)

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

        self.assertNotIn(("schedule-standalone", "600"), host.operations)
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
            delivered_task_id="task-1",
        )
        self.assertEqual(early["next_action"], "PAUSE_BLOCKED")
        self.assertEqual(early["reason_code"], "cadence_not_elapsed")

        before_replay = deepcopy(state)
        state, replay = pulse.begin_wake(
            state,
            wake_id="early-fresh-wake",
            now="2026-08-26T00:28:00+00:00",
            pause_heartbeat=lambda: True,
            delivered_task_id="task-1",
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
