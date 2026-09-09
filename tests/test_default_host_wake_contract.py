from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "codex-review-pulse" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import pulse  # noqa: E402
from standalone_orchestration import (  # noqa: E402
    StandaloneInvocation,
    StandaloneInvocationError,
    create_initial_setup_task,
)
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


def waiting_checkpoint(
    *, model: str = "gpt-5.6-luna", reasoning_effort: str = "xhigh"
) -> dict[str, object]:
    state, _ = pulse.begin_wake(
        empty_checkpoint("Owner/Repo", 17),
        wake_id="seed-wake",
        now=NOW,
        pause_heartbeat=lambda: True,
        policy_overrides={
            "model": model,
            "reasoning_effort": reasoning_effort,
        },
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
        schedule_anchor_created_at="2026-08-26T00:26:00+00:00",
        scheduled_task_id="task-1",
    )
    return state


def strict_waiting_checkpoint() -> dict[str, object]:
    state = waiting_checkpoint()
    handoff = pulse._handoff_identity(state)
    durable = {
        "id": "task-1",
        "status": "PAUSED",
        "prompt": handoff["prompt"],
        "prompt_sha256": handoff["prompt_sha256"],
        "scheduler_kind": "cron",
        "conversation_mode": "standalone",
        "target_thread_id": None,
        "model": handoff["model"],
        "reasoning_effort": handoff["reasoning_effort"],
        "cadence_seconds": handoff["cadence_seconds"],
        "created_at": "2026-08-26T00:37:00+00:00",
        "first_run": "2026-08-26T00:47:00+00:00",
    }
    state["task_retirement"] = {
        "phase": "confirmed",
        "wake_id": "seed-wake",
        "task_id": "setup-task",
        "role": "setup",
        "provenance": {"task_id": "setup-task"},
        "handoff": handoff,
        "rearm": {
            "action": "WAIT_REVIEW",
            "source_action": "WAIT_REVIEW",
            "proof": {"decision": deepcopy(state["last_decision"])},
        },
        "successor": {
            "task_id": "task-1",
            "status": "authorized",
            "completion_anchor": "2026-08-26T00:36:00+00:00",
            "created_at": durable["created_at"],
            "first_run": durable["first_run"],
            "readback": durable,
        },
    }
    state["scheduled_task_id"] = "task-1"
    state["scheduled_task_disposition"] = "AUTHORIZED"
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
        schedule_response_omits_id: bool = False,
        authorize_succeeds: bool = True,
        activate_succeeds: bool = True,
        first_run: str | None = None,
        created_at: str | None = None,
        readback_task_id: str | None = None,
        readback_model: str | None = None,
        readback_reasoning_effort: str | None = None,
        cleanup_succeeds: bool = True,
        cleanup_completion_time: str | None = None,
        pause_succeeds: bool = True,
        pause_results: tuple[bool, ...] | None = None,
        complete_raises: bool = False,
        complete_returns_invalid: bool = False,
    ) -> None:
        self.state = deepcopy(state)
        self._wake_ids = iter(wake_ids)
        self.issued_ids: list[str] = []
        self.operations: list[tuple[str, str]] = []
        self.checkpoint_missing = checkpoint_missing
        self.checkpoint_unreadable = checkpoint_unreadable
        self.schedule_fails = schedule_fails
        self.schedule_response_omits_id = schedule_response_omits_id
        self.authorize_succeeds = authorize_succeeds
        self.activate_succeeds = activate_succeeds
        self.first_run = first_run
        self.created_at = created_at
        self.readback_task_id = readback_task_id
        self.readback_model = readback_model
        self.readback_reasoning_effort = readback_reasoning_effort
        self.cleanup_succeeds = cleanup_succeeds
        self.cleanup_completion_time = cleanup_completion_time
        self.cleanup_pending_repairs: list[dict[str, object] | None] = []
        self.next_creation_time: str | None = None
        self.completion_time: str | None = None
        self.pause_succeeds = pause_succeeds
        self.pause_results = pause_results
        self.pause_calls = 0
        self.complete_raises = complete_raises
        self.complete_returns_invalid = complete_returns_invalid
        self.created_tasks: dict[str, dict[str, object]] = {}
        self.scheduled_statuses: list[str] = []
        self.paused_task_ids: list[str] = []
        self.authorized_task_ids: list[str] = []
        self.authorized_completion_times: list[str] = []
        self.deleted_task_ids: list[str] = []

    def now_utc(self) -> str:
        if self.completion_time is not None:
            return self.completion_time
        if self.created_at is not None:
            return self.created_at
        raise RuntimeError("test host has no completion clock value")

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
            if task_id in self.created_tasks:
                self.created_tasks[task_id]["status"] = "PAUSED"
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
        model: str,
        reasoning_effort: str,
        scheduler_kind: str,
        conversation_mode: str,
        target_thread_id: None,
        prompt_sha256: str,
        status: str,
    ) -> object:
        self.operations.append(("schedule-standalone", str(cadence_seconds)))
        self.scheduled_statuses.append(status)
        if self.schedule_fails:
            raise RuntimeError("scheduler rejected standalone task")
        task_id = f"task-{len(self.created_tasks) + 2}"
        created_at = self.created_at or self.next_creation_time
        if created_at is None:
            raise AssertionError("test host requires a creation timestamp")
        first_run = (
            datetime.fromisoformat(created_at) + timedelta(seconds=cadence_seconds)
        ).replace(microsecond=0).isoformat()
        self.created_tasks[task_id] = {
            "id": task_id,
            "prompt": prompt,
            "first_run": first_run,
            "created_at": created_at,
            "cadence_seconds": cadence_seconds,
            "model": model,
            "reasoning_effort": reasoning_effort,
            "scheduler_kind": scheduler_kind,
            "conversation_mode": conversation_mode,
            "target_thread_id": target_thread_id,
            "prompt_sha256": prompt_sha256,
            "status": status,
        }
        return {} if self.schedule_response_omits_id else {"id": task_id}

    def read_task(self, task_id: str) -> dict[str, object]:
        self.operations.append(("read-standalone", task_id))
        if task_id not in self.created_tasks:
            retirement = (self.state or {}).get("task_retirement")
            successor = (
                retirement.get("successor")
                if isinstance(retirement, dict)
                else None
            )
            durable = (
                successor.get("readback")
                if isinstance(successor, dict)
                else None
            )
            if isinstance(durable, dict) and successor.get("task_id") == task_id:
                self.created_tasks[task_id] = {
                    **deepcopy(durable),
                    "status": "ACTIVE",
                }
        task = self.created_tasks[task_id]
        return {
            **task,
            "id": self.readback_task_id or task_id,
            "first_run": self.first_run or task["first_run"],
            "model": self.readback_model
            if self.readback_model is not None
            else task["model"],
            "reasoning_effort": self.readback_reasoning_effort
            if self.readback_reasoning_effort is not None
            else task["reasoning_effort"],
        }

    def delete_task(self, task_id: str) -> bool:
        self.operations.append(("delete-task", task_id))
        self.deleted_task_ids.append(task_id)
        self.created_tasks.pop(task_id, None)
        return True

    def lookup_task(self, task_id: str) -> dict[str, object]:
        self.operations.append(("lookup-task", task_id))
        if task_id in self.created_tasks:
            return {"outcome": "PRESENT"}
        return {"outcome": "AUTHORITATIVE_NOT_FOUND"}

    def cleanup_worktree(
        self, *, pending_repair: dict[str, object] | None = None
    ) -> bool:
        self.operations.append(("cleanup-worktree", "task-owned"))
        self.cleanup_pending_repairs.append(
            deepcopy(pending_repair) if pending_repair is not None else None
        )
        if self.cleanup_completion_time is not None:
            self.completion_time = self.cleanup_completion_time
            self.next_creation_time = self.cleanup_completion_time
        return self.cleanup_succeeds

    def authorize_successor(
        self,
        *,
        wake_id: str,
        completed_at: str,
        task_id: str,
        created_at: str,
        first_run: str,
        cadence_seconds: int,
    ) -> bool:
        self.operations.append(("authorize-successor", task_id))
        self.authorized_completion_times.append(completed_at)
        if self.authorize_succeeds:
            self.authorized_task_ids.append(task_id)
            self.state, _ = pulse.authorize_successor(
                self.state or {},
                wake_id=wake_id,
                now=completed_at,
                scheduled_created_at=created_at,
                scheduled_first_run=first_run,
                scheduled_task_id=task_id,
            )
        return self.authorize_succeeds

    def activate_task(self, task_id: str) -> bool:
        self.operations.append(("activate-task", task_id))
        if task_id not in self.authorized_task_ids:
            raise AssertionError("successor must be authorized before activation")
        if self.activate_succeeds:
            self.created_tasks[task_id]["status"] = "ACTIVE"
        return self.activate_succeeds


class HostInvocation:
    """Drive the real standalone invocation guard with injected callbacks."""

    def __init__(
        self,
        host: InMemoryHost,
        *,
        scheduled: bool,
        now: str,
        task_id: str = "task-1",
        prompt: str = "standalone prompt",
        retirement: bool = False,
    ) -> None:
        self.host = host
        self.retirement = retirement
        self.invocation = StandaloneInvocation(
            host,
            task_id=task_id,
            prompt=prompt,
            scheduled=scheduled,
            now=now,
            begin_wake=self._begin_wake,
            complete_wake=self._complete_wake,
            prepare_retirement=self._prepare_retirement if retirement else None,
            confirm_retirement=self._confirm_retirement if retirement else None,
            record_successor_intent=self._record_successor_intent if retirement else None,
            record_successor_creation=self._record_successor_creation if retirement else None,
            record_successor_readback=self._record_successor_readback if retirement else None,
            record_successor_pause=self._record_successor_pause if retirement else None,
            allow_legacy_direct_callbacks=not retirement,
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
        setup_task_provenance: dict[str, object] | None = None,
        delivered_task_provenance: dict[str, object] | None = None,
    ) -> dict[str, object]:
        self.host.operations.append(("begin-wake", wake_id))
        state = self.host.state or empty_checkpoint("Owner/Repo", 17)
        self.host.state, result = pulse.begin_wake(
            state,
            wake_id=wake_id,
            now=now,
            pause_heartbeat=lambda: pause_confirmed,
            delivered_task_id=delivered_task_id,
            setup_task_provenance=setup_task_provenance,
            delivered_task_provenance=delivered_task_provenance,
        )
        if not self.retirement and delivered_task_id is not None:
            # This test-only branch models the legacy direct callback path.
            # The production standalone adapter retains and retires the exact
            # registered predecessor through the supplied callbacks.
            self.host.state["task_retirement"] = None
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
        if self.host.complete_returns_invalid:
            return {"reason_code": "malformed_completion"}
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

    def _prepare_retirement(
        self, wake_id: str, now: str, worktree_cleanup_confirmed: bool
    ) -> dict[str, object]:
        self.host.operations.append(("prepare-retirement", wake_id))
        self.host.state, result = pulse.prepare_task_retirement(
            self.host.state or {},
            wake_id=wake_id,
            now=now,
            worktree_cleanup_confirmed=worktree_cleanup_confirmed,
        )
        return result

    def _confirm_retirement(
        self,
        wake_id: str,
        now: str,
        task_id: str,
        role: str,
        outcome: str,
        evidence: dict[str, object] | None,
    ) -> dict[str, object]:
        self.host.operations.append(("confirm-retirement", task_id))
        self.host.state, result = pulse.confirm_task_retirement(
            self.host.state or {},
            wake_id=wake_id,
            now=now,
            task_id=task_id,
            role=role,
            outcome=outcome,
            evidence=evidence,
        )
        return result

    def _record_successor_creation(
        self,
        wake_id: str,
        now: str,
        outcome: str,
        task_id: str | None,
        completion_anchor: str | None,
        evidence: dict[str, object] | None,
    ) -> dict[str, object]:
        self.host.operations.append(("record-successor-creation", task_id or outcome))
        self.host.state, result = pulse.record_retirement_successor_creation(
            self.host.state or {},
            wake_id=wake_id,
            now=now,
            outcome=outcome,
            task_id=task_id,
            completion_anchor=completion_anchor,
            evidence=evidence,
        )
        return result

    def _record_successor_intent(
        self, wake_id: str, now: str, creation_nonce: str
    ) -> dict[str, object]:
        self.host.operations.append(("record-successor-intent", creation_nonce))
        self.host.state, result = pulse.record_creation_intent(
            self.host.state or {},
            role="successor",
            wake_id=wake_id,
            now=now,
            creation_nonce=creation_nonce,
        )
        return result

    def _record_successor_readback(
        self, wake_id: str, now: str, task: dict[str, object]
    ) -> dict[str, object]:
        self.host.operations.append(("record-successor-readback", str(task["id"])))
        self.host.state, result = pulse.record_retirement_successor_readback(
            self.host.state or {}, wake_id=wake_id, now=now, task=task
        )
        return result

    def _record_successor_pause(
        self,
        wake_id: str,
        now: str,
        task_id: str,
        confirmed: bool,
        evidence: dict[str, object] | None,
    ) -> dict[str, object]:
        self.host.operations.append(("record-successor-pause", task_id))
        self.host.state, result = pulse.record_retirement_successor_pause(
            self.host.state or {},
            wake_id=wake_id,
            now=now,
            task_id=task_id,
            confirmed=confirmed,
            evidence=evidence,
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
        self.host.completion_time = completion_now
        return self.invocation.complete(
            action=action,
            now=completion_now,
            cadence_seconds=cadence_seconds,
        )


class DefaultHostWakeContractTests(unittest.TestCase):
    def test_initial_setup_creation_is_journaled_without_admitting_a_wake(self) -> None:
        host = InMemoryHost(
            state=empty_checkpoint("Owner/Repo", 17),
            created_at="2026-08-26T00:00:00+00:00",
        )

        def record_intent(now: str, nonce: str) -> dict[str, object]:
            host.state, result = pulse.record_creation_intent(
                host.state or {},
                role="setup",
                now=now,
                creation_nonce=nonce,
            )
            return result

        def record_id(now: str, task_id: str) -> dict[str, object]:
            host.state, result = pulse.record_setup_creation_id(
                host.state or {},
                now=now,
                task_id=task_id,
            )
            return result

        def record_unknown(
            now: str, evidence: dict[str, object]
        ) -> dict[str, object]:
            host.state, result = pulse.record_setup_creation_unknown(
                host.state or {},
                now=now,
                evidence=evidence,
            )
            return result

        def record_readback(
            now: str, task: dict[str, object]
        ) -> dict[str, object]:
            host.state, result = pulse.record_setup_creation_readback(
                host.state or {},
                now=now,
                task=task,
            )
            return result

        handoff = pulse.build_standalone_task_handoff("Owner/Repo", 17)
        result = create_initial_setup_task(
            host,
            prompt=handoff["prompt"],
            cadence_seconds=600,
            model=handoff["model"],
            reasoning_effort=handoff["reasoning_effort"],
            record_setup_intent=record_intent,
            record_setup_id=record_id,
            record_setup_readback=record_readback,
            record_setup_unknown=record_unknown,
            creation_nonce="initial-setup-nonce",
        )

        self.assertEqual(host.scheduled_statuses, ["PAUSED"])
        self.assertEqual(host.state["wake_count"], 0)
        self.assertEqual(result["task_id"], "task-2")
        self.assertEqual(
            result["provenance"]["creation_authority"]["creation_nonce"],
            "initial-setup-nonce",
        )

    def test_scheduled_rearm_retires_only_the_authenticated_delivered_task(self) -> None:
        state = strict_waiting_checkpoint()
        host = InMemoryHost(
            state=state,
            created_at="2026-08-26T00:37:00+00:00",
        )
        handoff = pulse.build_standalone_task_handoff("Owner/Repo", 17)
        invocation = HostInvocation(
            host,
            scheduled=True,
            now="2026-08-26T00:36:00+00:00",
            task_id="task-1",
            prompt=handoff["prompt"],
            retirement=True,
        )
        self.assertEqual(invocation.begin()["next_action"], "WAKE_STARTED")
        self.assertEqual(host.state["wake_count"], 2)
        invocation.snapshot()
        result = invocation.complete(reanchor_succeeds=True)

        self.assertEqual(result["next_action"], "WAIT_REVIEW")
        self.assertEqual(host.deleted_task_ids, ["task-1"])
        self.assertEqual(host.state["task_retirement"]["phase"], "confirmed")
        self.assertEqual(host.state["task_retirement"]["role"], "delivered")
        self.assertEqual(host.state["scheduled_task_id"], "task-2")
        self.assertEqual(host.state["scheduled_task_disposition"], "AUTHORIZED")
        names = [operation[0] for operation in host.operations]
        self.assertLess(names.index("cleanup-worktree"), names.index("prepare-retirement"))
        self.assertLess(names.index("prepare-retirement"), names.index("delete-task"))
        self.assertLess(names.index("delete-task"), names.index("schedule-standalone"))
        self.assertLess(names.index("authorize-successor"), names.index("complete-wake"))
        self.assertEqual(names[-1], "activate-task")

    def test_valid_structured_delivery_replay_does_not_increment_again(self) -> None:
        host = InMemoryHost(
            state=strict_waiting_checkpoint(),
            created_at="2026-08-26T00:37:00+00:00",
            wake_ids=("fresh-wake-1",),
        )
        invocation = HostInvocation(
            host,
            scheduled=True,
            now="2026-08-26T00:36:00+00:00",
            task_id="task-1",
            prompt=pulse.build_standalone_task_handoff("Owner/Repo", 17)["prompt"],
            retirement=True,
        )

        first = invocation.begin()
        replay = pulse.begin_wake(
            host.state,
            wake_id=invocation.wake_id or "",
            now="2026-08-26T00:36:01+00:00",
            pause_heartbeat=lambda: True,
            delivered_task_id="task-1",
            delivered_task_provenance={
                "task_id": "task-1",
                "pause_confirmed": True,
                "pre_pause_readback": {
                    **host.created_tasks["task-1"],
                    "status": "ACTIVE",
                },
                "post_pause_readback": host.created_tasks["task-1"],
            },
        )[1]

        self.assertEqual(first["next_action"], "WAKE_STARTED")
        self.assertEqual(replay, first)
        self.assertEqual(host.state["wake_count"], 2)

    def test_mismatched_post_pause_scheduler_metadata_is_rejected_before_wake(self) -> None:
        host = InMemoryHost(
            state=strict_waiting_checkpoint(),
            created_at="2026-08-26T00:37:00+00:00",
            readback_model="gpt-5.6-terra",
        )
        invocation = HostInvocation(
            host,
            scheduled=True,
            now="2026-08-26T00:36:00+00:00",
            task_id="task-1",
            prompt=pulse.build_standalone_task_handoff("Owner/Repo", 17)["prompt"],
            retirement=True,
        )

        result = invocation.begin()

        self.assertEqual(result["next_action"], "PAUSE_RECOVERY")
        self.assertEqual(result["reason_code"], "scheduler_provenance_invalid")
        self.assertEqual(host.state["wake_count"], 1)
        self.assertNotIn(("snapshot", invocation.wake_id), host.operations)

    def test_final_admitted_wake_retires_without_creating_a_sixth_task(self) -> None:
        state = strict_waiting_checkpoint()
        state["automation_policy"]["max_wakes"] = 2
        host = InMemoryHost(
            state=state,
            created_at="2026-08-26T00:37:00+00:00",
        )
        invocation = HostInvocation(
            host,
            scheduled=True,
            now="2026-08-26T00:36:00+00:00",
            task_id="task-1",
            prompt=pulse.build_standalone_task_handoff("Owner/Repo", 17)["prompt"],
            retirement=True,
        )

        invocation.begin()
        invocation.snapshot()
        result = invocation.complete(reanchor_succeeds=True)

        self.assertEqual(result["next_action"], "STOP_POLICY_LIMIT")
        self.assertEqual(result["reason_code"], "maximum_wakes_reached_after_admission")
        self.assertNotIn(("schedule-standalone", "600"), host.operations)
        self.assertNotIn(("activate-task", "task-2"), host.operations)

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
            (
                "malformed",
                {"state": {**waiting_checkpoint(), "next_not_before": "not-a-timestamp"}},
                "checkpoint_invalid",
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
                if name not in {"missing", "unreadable"}:
                    self.assertIn(("begin-wake", invocation.wake_id), host.operations)
                else:
                    self.assertNotIn(("begin-wake", invocation.wake_id), host.operations)
                if name == "early":
                    self.assertEqual(
                        host.state["failure_latch"]["reason_code"],
                        "cadence_not_elapsed",
                    )
                if name == "malformed":
                    self.assertEqual(
                        host.state["failure_latch"]["reason_code"],
                        "checkpoint_invalid",
                    )

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
                if reanchor_succeeds:
                    self.assertEqual(host.operations[-1][0], "activate-task")
                    self.assertEqual(host.operations[-5][0], "schedule-standalone")
                    self.assertEqual(host.operations[-4][0], "read-standalone")
                    self.assertEqual(host.operations[-3][0], "authorize-successor")
                    self.assertEqual(host.operations[-2][0], "complete-wake")
                    self.assertEqual(result["scheduled_task_id"], "task-2")
                else:
                    self.assertEqual(host.operations[-1][0], "complete-wake")
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
        self.assertEqual(host.created_tasks["task-2"]["model"], "gpt-5.6-luna")
        self.assertEqual(host.created_tasks["task-2"]["reasoning_effort"], "xhigh")
        self.assertEqual(host.created_tasks["task-2"]["first_run"], "2026-08-26T00:47:00+00:00")
        self.assertEqual(host.scheduled_statuses, ["PAUSED"])
        self.assertEqual(host.created_tasks["task-2"]["status"], "ACTIVE")
        self.assertEqual(host.authorized_task_ids, ["task-2"])
        self.assertEqual(host.operations[:4], [
            ("pause-task", "task-1"),
            ("checkpoint-read", "direct"),
            ("begin-wake", "fresh-wake-2"),
            ("snapshot", "fresh-wake-2"),
        ])

    def test_delivered_authorized_successor_is_consumed_without_reactivation(self) -> None:
        host = InMemoryHost(
            state=waiting_checkpoint(),
            wake_ids=("fresh-wake-2", "fresh-wake-3"),
        )
        first = HostInvocation(host, scheduled=True, now=NEXT_WAKE)
        first.begin()
        first.snapshot()
        first.complete(reanchor_succeeds=True)

        self.assertEqual(host.state["scheduled_task_disposition"], "AUTHORIZED")
        self.assertEqual(host.state["wake_phase"], "successor_finalized")
        self.assertEqual(host.created_tasks["task-2"]["status"], "ACTIVE")

        second = HostInvocation(
            host,
            scheduled=True,
            now="2026-08-26T00:47:00+00:00",
            task_id="task-2",
        )
        result = second.begin()

        self.assertEqual(result["next_action"], "WAKE_STARTED")
        self.assertEqual(host.state["scheduled_task_disposition"], "PAUSED")
        self.assertEqual(host.operations.count(("activate-task", "task-2")), 1)
        self.assertEqual(host.created_tasks["task-2"]["status"], "PAUSED")

    def test_completion_anchor_is_taken_after_long_cleanup(self) -> None:
        host = InMemoryHost(
            state=waiting_checkpoint(),
            wake_ids=("fresh-wake-2",),
            cleanup_completion_time="2026-08-26T00:50:00+00:00",
        )
        invocation = HostInvocation(host, scheduled=True, now=NEXT_WAKE)
        invocation.begin()
        invocation.snapshot()

        result = invocation.complete(reanchor_succeeds=True)

        self.assertEqual(result["next_action"], "WAIT_REVIEW")
        self.assertEqual(host.authorized_completion_times, ["2026-08-26T00:50:00+00:00"])
        self.assertEqual(
            host.created_tasks["task-2"]["created_at"],
            "2026-08-26T00:50:00+00:00",
        )
        self.assertEqual(
            host.created_tasks["task-2"]["first_run"],
            "2026-08-26T01:00:00+00:00",
        )
        self.assertEqual(host.state["next_not_before"], "2026-08-26T01:00:00+00:00")

    def test_successor_reuses_full_canonical_prompt_and_digest(self) -> None:
        handoff = pulse.build_standalone_task_handoff("owner/repo", 17)
        host = InMemoryHost(
            state=waiting_checkpoint(),
            wake_ids=("fresh-wake-2",),
        )
        invocation = HostInvocation(
            host,
            scheduled=True,
            now=NEXT_WAKE,
            prompt=handoff["prompt"],
        )
        invocation.begin()
        invocation.snapshot()

        invocation.complete(reanchor_succeeds=True)

        successor = host.created_tasks["task-2"]
        expected_digest = hashlib.sha256(
            handoff["prompt"].encode("utf-8")
        ).hexdigest()
        self.assertEqual(successor["prompt"], handoff["prompt"])
        self.assertEqual(successor["prompt_sha256"], expected_digest)
        self.assertEqual(successor["prompt_sha256"], handoff["prompt_sha256"])

    def test_successor_reuses_the_persisted_model_configuration(self) -> None:
        host = InMemoryHost(
            state=waiting_checkpoint(
                model="gpt-5.6-terra", reasoning_effort="medium"
            ),
            wake_ids=("fresh-wake-2",),
        )
        invocation = HostInvocation(host, scheduled=True, now=NEXT_WAKE)
        invocation.begin()
        invocation.snapshot()

        invocation.complete(reanchor_succeeds=True)

        self.assertEqual(host.created_tasks["task-2"]["model"], "gpt-5.6-terra")
        self.assertEqual(
            host.created_tasks["task-2"]["reasoning_effort"], "medium"
        )

    def test_successor_creation_same_scheduler_second_as_completion_is_accepted(self) -> None:
        host = InMemoryHost(
            state=waiting_checkpoint(),
            wake_ids=("fresh-wake-2",),
            created_at="2026-08-26T00:37:00+00:00",
        )
        invocation = HostInvocation(host, scheduled=True, now=NEXT_WAKE)
        invocation.begin()
        invocation.snapshot()

        result = invocation.invocation.complete(
            action="WAIT_REVIEW",
            now="2026-08-26T00:37:00.500000+00:00",
            cadence_seconds=600,
        )

        self.assertEqual(result["next_action"], "WAIT_REVIEW")
        self.assertEqual(result["scheduled_task_created_at"], "2026-08-26T00:37:00+00:00")
        self.assertEqual(result["scheduled_task_id"], "task-2")

    def test_truncated_scheduler_first_run_is_accepted_after_later_creation(self) -> None:
        host = InMemoryHost(
            state=waiting_checkpoint(),
            wake_ids=("fresh-wake-2",),
            created_at="2026-09-02T08:46:13.793000+00:00",
        )
        invocation = HostInvocation(host, scheduled=True, now=NEXT_WAKE)
        invocation.begin()
        invocation.snapshot()

        result = invocation.invocation.complete(
            action="WAIT_REVIEW",
            now="2026-09-02T08:46:13.761000+00:00",
            cadence_seconds=600,
        )

        self.assertEqual(result["next_action"], "WAIT_REVIEW")
        self.assertEqual(
            host.created_tasks["task-2"]["first_run"],
            "2026-09-02T08:56:13+00:00",
        )
        self.assertEqual(result["next_not_before"], "2026-09-02T08:56:13+00:00")
        self.assertEqual(
            result["scheduled_task_created_at"],
            "2026-09-02T08:46:13.793000+00:00",
        )

    def test_genuinely_early_scheduler_first_run_is_rejected(self) -> None:
        host = InMemoryHost(
            state=waiting_checkpoint(),
            wake_ids=("fresh-wake-2",),
            created_at="2026-09-02T08:46:13.793000+00:00",
        )
        invocation = HostInvocation(host, scheduled=True, now=NEXT_WAKE)
        invocation.begin()
        invocation.snapshot()
        host.first_run = "2026-09-02T08:56:12+00:00"

        result = invocation.invocation.complete(
            action="WAIT_REVIEW",
            now="2026-09-02T08:46:13.761000+00:00",
            cadence_seconds=600,
        )

        self.assertEqual(result["next_action"], "PAUSE_BLOCKED")
        self.assertEqual(result["reason_code"], "scheduled_task_reanchor_mismatch")
        self.assertEqual(
            result["evidence"],
            {
                "expected_first_run": "2026-09-02T08:56:13+00:00",
                "observed_first_run": "2026-09-02T08:56:12+00:00",
            },
        )

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

    def test_missing_successor_id_leaves_created_task_paused(self) -> None:
        host = InMemoryHost(
            state=waiting_checkpoint(),
            wake_ids=("fresh-wake-2",),
            schedule_response_omits_id=True,
        )
        invocation = HostInvocation(host, scheduled=True, now=NEXT_WAKE)
        invocation.begin()
        invocation.snapshot()

        result = invocation.complete(reanchor_succeeds=True)

        self.assertEqual(result["next_action"], "PAUSE_BLOCKED")
        self.assertEqual(result["reason_code"], "scheduled_task_reanchor_unavailable")
        self.assertIn(("schedule-standalone", "600"), host.operations)
        self.assertNotIn(("activate-task", "task-2"), host.operations)
        self.assertNotIn(("pause-task", "task-2"), host.operations)

    def test_unconfirmed_successor_activation_keeps_task_paused(self) -> None:
        host = InMemoryHost(
            state=waiting_checkpoint(),
            wake_ids=("fresh-wake-2",),
            activate_succeeds=False,
        )
        invocation = HostInvocation(host, scheduled=True, now=NEXT_WAKE)
        invocation.begin()
        invocation.snapshot()

        result = invocation.complete(reanchor_succeeds=True)

        self.assertEqual(result["next_action"], "PAUSE_RECOVERY")
        self.assertEqual(result["reason_code"], "successor_activation_unconfirmed")
        self.assertEqual(host.created_tasks["task-2"]["status"], "PAUSED")
        self.assertIn(("activate-task", "task-2"), host.operations)
        self.assertIn(("pause-task", "task-2"), host.operations)

    def test_unconfirmed_successor_authorization_never_activates_task(self) -> None:
        host = InMemoryHost(
            state=waiting_checkpoint(),
            wake_ids=("fresh-wake-2",),
            authorize_succeeds=False,
        )
        invocation = HostInvocation(host, scheduled=True, now=NEXT_WAKE)
        invocation.begin()
        invocation.snapshot()

        result = invocation.complete(reanchor_succeeds=True)

        self.assertEqual(result["next_action"], "PAUSE_RECOVERY")
        self.assertEqual(result["reason_code"], "successor_authorization_unconfirmed")
        self.assertEqual(host.created_tasks["task-2"]["status"], "PAUSED")
        self.assertIn(("authorize-successor", "task-2"), host.operations)
        self.assertNotIn(("activate-task", "task-2"), host.operations)
        self.assertIn(("pause-task", "task-2"), host.operations)

    def test_successor_model_readback_mismatch_is_unverified(self) -> None:
        host = InMemoryHost(
            state=waiting_checkpoint(),
            wake_ids=("fresh-wake-2",),
            readback_model="gpt-5.6-terra",
        )
        invocation = HostInvocation(host, scheduled=True, now=NEXT_WAKE)
        invocation.begin()
        invocation.snapshot()

        result = invocation.complete(reanchor_succeeds=True)

        self.assertEqual(result["next_action"], "PAUSE_BLOCKED")
        self.assertEqual(result["reason_code"], "scheduled_task_reanchor_mismatch")
        self.assertEqual(host.state["scheduled_task_disposition"], "PAUSED")
        self.assertIn("task-2", host.paused_task_ids)

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

    def test_successor_task_id_readback_mismatch_is_unverified(self) -> None:
        host = InMemoryHost(
            state=waiting_checkpoint(),
            wake_ids=("fresh-wake-2",),
            readback_task_id="stale-task",
        )
        invocation = HostInvocation(host, scheduled=True, now=NEXT_WAKE)
        invocation.begin()
        invocation.snapshot()

        result = invocation.complete(reanchor_succeeds=True)

        self.assertEqual(result["next_action"], "PAUSE_BLOCKED")
        self.assertEqual(result["reason_code"], "scheduled_task_reanchor_mismatch")
        self.assertEqual(host.state["scheduled_task_disposition"], "PAUSED")
        self.assertIn("task-2", host.paused_task_ids)
        self.assertNotIn(("authorize-successor", "task-2"), host.operations)

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

    def test_worktree_cleanup_precedes_successor_activation_and_completion(self) -> None:
        host = InMemoryHost(
            state=waiting_checkpoint(),
            wake_ids=("fresh-wake-2",),
        )
        invocation = HostInvocation(host, scheduled=True, now=NEXT_WAKE)
        invocation.begin()
        invocation.snapshot()

        result = invocation.complete(reanchor_succeeds=True)

        self.assertEqual(result["next_action"], "WAIT_REVIEW")
        operation_names = [operation[0] for operation in host.operations]
        self.assertLess(
            operation_names.index("cleanup-worktree"),
            operation_names.index("activate-task"),
        )
        self.assertLess(
            operation_names.index("cleanup-worktree"),
            operation_names.index("complete-wake"),
        )
        self.assertLess(
            operation_names.index("complete-wake"),
            operation_names.index("activate-task"),
        )
        self.assertEqual(operation_names[-1], "activate-task")

    def test_worktree_cleanup_failure_persists_blocker_before_activation(self) -> None:
        host = InMemoryHost(
            state=waiting_checkpoint(),
            wake_ids=("fresh-wake-2",),
            cleanup_succeeds=False,
        )
        invocation = HostInvocation(host, scheduled=True, now=NEXT_WAKE)
        invocation.begin()
        invocation.snapshot()

        result = invocation.complete(reanchor_succeeds=True)

        self.assertEqual(result["next_action"], "PAUSE_RECOVERY")
        self.assertEqual(result["reason_code"], "worktree_cleanup_unconfirmed")
        self.assertEqual(
            result["evidence"],
            {
                "worktree_cleanup_confirmed": False,
                "pause_confirmed": False,
            },
        )
        self.assertEqual(host.state["scheduled_task_disposition"], "PAUSED")
        self.assertEqual(
            host.state["failure_latch"]["reason_code"],
            "worktree_cleanup_unconfirmed",
        )
        self.assertNotIn(("schedule-standalone", "600"), host.operations)
        self.assertNotIn(("activate-task", "task-2"), host.operations)

    def test_retry_pending_repair_uses_manifest_aware_cleanup(self) -> None:
        pending_repair = {
            "patch_path": ".git/codex-review-pulse/pending.patch",
            "patch_sha256": "a" * 64,
            "frozen_head_oid": "HEAD1",
        }
        state = waiting_checkpoint()
        host = InMemoryHost(
            state=state,
            wake_ids=("fresh-wake-2",),
        )
        invocation = HostInvocation(host, scheduled=True, now=NEXT_WAKE)
        invocation.begin()
        host.state["last_decision"] = {
            "next_action": "WAIT_RETRY",
            "mutation_occurred": False,
        }
        host.state["active_batch"] = {
            "frozen_head_oid": "HEAD1",
            "pending_repair": pending_repair,
            "publication": {"status": "not_started"},
        }

        result = invocation.complete(reanchor_succeeds=True, action="WAIT_RETRY")

        self.assertEqual(result["next_action"], "WAIT_RETRY")
        self.assertEqual(host.cleanup_pending_repairs, [pending_repair])
        operation_names = [operation[0] for operation in host.operations]
        self.assertLess(
            operation_names.index("cleanup-worktree"),
            operation_names.index("activate-task"),
        )

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
                self.assertIn(("begin-wake", invocation.wake_id), host.operations)

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
        self.assertIn(("pause-task", "task-2"), host.operations)
        self.assertIn("task-2", host.paused_task_ids)
        with self.assertRaises(StandaloneInvocationError):
            invocation.complete(reanchor_succeeds=True)

    def test_completion_callback_failure_reports_unconfirmed_successor_cleanup(self) -> None:
        host = InMemoryHost(
            state=waiting_checkpoint(),
            wake_ids=("fresh-wake-2",),
            pause_results=(True, False),
            complete_raises=True,
        )
        invocation = HostInvocation(host, scheduled=True, now=NEXT_WAKE)
        invocation.begin()
        invocation.snapshot()

        with self.assertRaisesRegex(
            StandaloneInvocationError,
            "successor cleanup was not confirmed",
        ):
            invocation.complete(reanchor_succeeds=True)

        self.assertTrue(invocation.ended)
        self.assertIn(("pause-task", "task-2"), host.operations)

    def test_malformed_completion_result_pauses_known_successor(self) -> None:
        host = InMemoryHost(
            state=waiting_checkpoint(),
            wake_ids=("fresh-wake-2",),
            complete_returns_invalid=True,
        )
        invocation = HostInvocation(host, scheduled=True, now=NEXT_WAKE)
        invocation.begin()
        invocation.snapshot()

        with self.assertRaisesRegex(
            StandaloneInvocationError,
            "complete-wake returned an invalid result",
        ):
            invocation.complete(reanchor_succeeds=True)

        self.assertTrue(invocation.ended)
        self.assertEqual(host.created_tasks["task-2"]["status"], "PAUSED")
        self.assertIn(("pause-task", "task-2"), host.operations)
        self.assertIn("task-2", host.paused_task_ids)

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
