from __future__ import annotations

from copy import deepcopy
import subprocess
import sys
import unittest


ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "codex-review-pulse" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from state_model import empty_checkpoint, record_resolved_thread  # noqa: E402
import pulse  # noqa: E402


NOW = "2026-08-26T00:00:00+00:00"
PENDING_REPAIR = {
    "patch_path": "C:/git-common/codex-review-pulse/pending.patch",
    "patch_sha256": "a" * 64,
    "frozen_head_oid": "HEAD1",
}


def snapshot(
    *,
    head: str = "HEAD1",
    targeted: list[str] | None = None,
    eyes: bool = False,
    approval: str = "awaiting_current_head_approval",
    stable: bool = True,
) -> dict:
    return {
        "head_oid": head,
        "pull_request_state": "OPEN",
        "targeted_thread_ids": targeted or [],
        "review_in_progress": {"active": eyes},
        "review_activity_ok": True,
        "approval_evidence": {"status": approval},
        "snapshot_stable": stable,
        "server_evidence": {"head_before": head, "head_after": head},
    }


def started(checkpoint=None, *, wake_id: str = "wake-1", now: str = NOW):
    state = checkpoint or empty_checkpoint("Owner/Repo", 17)
    delivered_task_id = (
        state.get("scheduled_task_id")
        if state.get("scheduled_task_disposition") == "ACTIVE"
        else None
    )
    return pulse.begin_wake(
        state,
        wake_id=wake_id,
        now=now,
        pause_heartbeat=lambda: True,
        delivered_task_id=delivered_task_id,
    )


class DefaultLifecycleTests(unittest.TestCase):
    def _setup_provenance(self, task_id: str = "setup-task") -> dict:
        handoff = pulse.build_standalone_task_handoff("Owner/Repo", 17)
        return {
            "task_id": task_id,
            "pause_confirmed": True,
            "readback": {
                "id": task_id,
                "status": "PAUSED",
                "prompt": handoff["prompt"],
                "prompt_sha256": handoff["prompt_sha256"],
                "scheduler_kind": "cron",
                "conversation_mode": "standalone",
                "target_thread_id": None,
                "model": handoff["model"],
                "reasoning_effort": handoff["reasoning_effort"],
                "cadence_seconds": 600,
                "created_at": "2026-08-26T00:00:00+00:00",
                "first_run": "2026-08-26T00:10:00+00:00",
            },
        }

    def _retirement_ready_state(self) -> tuple[dict, str]:
        state, _ = pulse.begin_wake(
            empty_checkpoint("Owner/Repo", 17),
            wake_id="wake-1",
            now=NOW,
            pause_heartbeat=lambda: True,
            setup_task_provenance=self._setup_provenance(),
        )
        state, _ = pulse.record_snapshot(
            state, snapshot(), wake_id="wake-1", now=NOW
        )
        state, prepared = pulse.prepare_task_retirement(
            state,
            wake_id="wake-1",
            now="2026-08-26T00:25:00+00:00",
            worktree_cleanup_confirmed=True,
        )
        self.assertEqual(prepared["next_action"], "RETIREMENT_PENDING")
        state, confirmed = pulse.confirm_task_retirement(
            state,
            wake_id="wake-1",
            now="2026-08-26T00:25:01+00:00",
            task_id="setup-task",
            role="setup",
            outcome="confirmed",
            evidence={"delete": "confirmed"},
        )
        self.assertEqual(confirmed["next_action"], "RETIREMENT_CONFIRMED")
        return state, "2026-08-26T00:26:00+00:00"

    def _record_verified_successor(self, state: dict, anchor: str) -> dict:
        state, created = pulse.record_retirement_successor_creation(
            state,
            wake_id="wake-1",
            now=anchor,
            outcome="CREATED_EXACT_ID",
            task_id="successor-task",
            completion_anchor=anchor,
        )
        self.assertEqual(created["next_action"], "SUCCESSOR_CREATED")
        handoff = state["task_retirement"]["handoff"]
        readback = {
            "id": "successor-task",
            "status": "PAUSED",
            "prompt": handoff["prompt"],
            "prompt_sha256": handoff["prompt_sha256"],
            "scheduler_kind": "cron",
            "conversation_mode": "standalone",
            "target_thread_id": None,
            "model": handoff["model"],
            "reasoning_effort": handoff["reasoning_effort"],
            "cadence_seconds": handoff["cadence_seconds"],
            "created_at": anchor,
            "first_run": "2026-08-26T00:36:00+00:00",
        }
        state, ready = pulse.record_retirement_successor_readback(
            state, wake_id="wake-1", now=anchor, task=readback
        )
        self.assertEqual(ready["next_action"], "SUCCESSOR_READY")
        return state

    @staticmethod
    def _delivered_provenance(state: dict, task_id: str = "successor-task") -> dict:
        durable = state["task_retirement"]["successor"]["readback"]
        return {
            "task_id": task_id,
            "pause_confirmed": True,
            "pre_pause_readback": {**durable, "status": "ACTIVE"},
            "post_pause_readback": {**durable, "status": "PAUSED"},
        }

    def _authorized_successor_state(self) -> dict:
        state, anchor = self._retirement_ready_state()
        state = self._record_verified_successor(state, anchor)
        state, authorized = pulse.authorize_successor(
            state,
            wake_id="wake-1",
            now=anchor,
            scheduled_created_at=anchor,
            scheduled_first_run="2026-08-26T00:36:00+00:00",
            scheduled_task_id="successor-task",
        )
        self.assertEqual(authorized["next_action"], "SUCCESSOR_AUTHORIZED")
        state, completed = pulse.complete_wake(
            state,
            wake_id="wake-1",
            now=anchor,
            schedule_next_wake=lambda _: "2026-08-26T00:36:00+00:00",
            schedule_anchor_created_at=anchor,
            scheduled_task_id="successor-task",
            require_schedule_anchor=True,
        )
        self.assertEqual(completed["next_action"], "WAIT_REVIEW")
        return state

    def test_valid_structured_delivery_admits_once(self) -> None:
        state = self._authorized_successor_state()

        state, result = pulse.begin_wake(
            state,
            wake_id="wake-2",
            now="2026-08-26T00:36:00+00:00",
            pause_heartbeat=lambda: True,
            delivered_task_id="successor-task",
            delivered_task_provenance=self._delivered_provenance(state),
        )

        self.assertEqual(result["next_action"], "WAKE_STARTED")
        self.assertEqual(state["wake_count"], 2)

    def test_missing_post_pause_readback_fails_closed(self) -> None:
        state = self._authorized_successor_state()
        provenance = self._delivered_provenance(state)
        provenance.pop("post_pause_readback")

        state, result = pulse.begin_wake(
            state,
            wake_id="wake-2",
            now="2026-08-26T00:36:00+00:00",
            pause_heartbeat=lambda: True,
            delivered_task_id="successor-task",
            delivered_task_provenance=provenance,
        )

        self.assertEqual(result["next_action"], "PAUSE_RECOVERY")
        self.assertEqual(result["reason_code"], "scheduler_provenance_invalid")
        self.assertEqual(state["wake_count"], 1)

    def test_delivered_metadata_drift_fails_closed(self) -> None:
        state = self._authorized_successor_state()
        provenance = self._delivered_provenance(state)
        provenance["post_pause_readback"]["model"] = "gpt-5.6-terra"

        state, result = pulse.begin_wake(
            state,
            wake_id="wake-2",
            now="2026-08-26T00:36:00+00:00",
            pause_heartbeat=lambda: True,
            delivered_task_id="successor-task",
            delivered_task_provenance=provenance,
        )

        self.assertEqual(result["next_action"], "PAUSE_RECOVERY")
        self.assertEqual(result["reason_code"], "scheduler_provenance_invalid")
        self.assertEqual(state["wake_count"], 1)

    def test_exact_setup_retirement_is_pending_before_delete_then_finalizes_successor(self) -> None:
        state, anchor = self._retirement_ready_state()
        self.assertEqual(state["scheduled_task_disposition"], "NONE")
        self.assertIsNone(state["scheduled_task_id"])
        state = self._record_verified_successor(state, anchor)
        state, authorized = pulse.authorize_successor(
            state,
            wake_id="wake-1",
            now=anchor,
            scheduled_created_at=anchor,
            scheduled_first_run="2026-08-26T00:36:00+00:00",
            scheduled_task_id="successor-task",
        )
        self.assertEqual(authorized["next_action"], "SUCCESSOR_AUTHORIZED")
        state, completed = pulse.complete_wake(
            state,
            wake_id="wake-1",
            now=anchor,
            schedule_next_wake=lambda _: "2026-08-26T00:36:00+00:00",
            schedule_anchor_created_at=anchor,
            scheduled_task_id="successor-task",
            require_schedule_anchor=True,
        )
        self.assertEqual(completed["next_action"], "WAIT_REVIEW")
        self.assertEqual(state["scheduled_task_disposition"], "AUTHORIZED")
        self.assertEqual(state["task_retirement"]["phase"], "confirmed")
        self.assertEqual(state["task_retirement"]["successor"]["status"], "authorized")

    def test_unknown_retirement_blocks_pr_work_and_requires_exact_reconciliation(self) -> None:
        state, _ = self._retirement_ready_state()
        # Rebuild the pending state to exercise a delete outcome that cannot
        # establish either presence or absence.
        state, _ = pulse.begin_wake(
            empty_checkpoint("Owner/Repo", 17),
            wake_id="wake-2",
            now=NOW,
            pause_heartbeat=lambda: True,
            setup_task_provenance=self._setup_provenance("setup-2"),
        )
        state, _ = pulse.record_snapshot(state, snapshot(), wake_id="wake-2", now=NOW)
        state, _ = pulse.prepare_task_retirement(
            state, wake_id="wake-2", now=NOW, worktree_cleanup_confirmed=True
        )
        state, result = pulse.confirm_task_retirement(
            state,
            wake_id="wake-2",
            now=NOW,
            task_id="setup-2",
            role="setup",
            outcome="unknown",
        )
        self.assertEqual(result["reason_code"], "task_retirement_unknown")
        self.assertEqual(state["scheduled_task_disposition"], "UNKNOWN")
        with self.assertRaisesRegex(pulse.DefaultWakeError, "durable recovery latch"):
            pulse.record_snapshot(state, snapshot(), wake_id="wake-2", now=NOW)
        state, reconciled = pulse.reconcile_task_retirement(
            state,
            wake_id="wake-2",
            now=NOW,
            task_id="setup-2",
            role="setup",
            lookup="PRESENT",
        )
        self.assertEqual(reconciled["next_action"], "PAUSE_RECOVERY")
        self.assertEqual(state["scheduled_task_id"], "setup-2")
        self.assertEqual(state["scheduled_task_disposition"], "PAUSED")

    def test_v2_active_checkpoint_without_exact_task_identity_fails_closed(self) -> None:
        legacy = empty_checkpoint("Owner/Repo", 17)
        legacy.update(
            {
                "default_mode_schema_version": 2,
                "active_wake_id": "legacy-wake",
                "scheduled_task_id": None,
                "scheduled_task_disposition": "PAUSED",
            }
        )
        with self.assertRaisesRegex(pulse.DefaultWakeError, "legacy_active_task_identity_unknown"):
            pulse.ensure_default_lifecycle(legacy)

    def test_non_deletion_reconciliation_restores_handoff_recovery(self) -> None:
        state, _ = pulse.begin_wake(
            empty_checkpoint("Owner/Repo", 17),
            wake_id="wake-1",
            now=NOW,
            pause_heartbeat=lambda: True,
            setup_task_provenance=self._setup_provenance("setup-1"),
        )
        state, _ = pulse.record_snapshot(
            state, snapshot(), wake_id="wake-1", now=NOW
        )
        state, _ = pulse.prepare_task_retirement(
            state,
            wake_id="wake-1",
            now=NOW,
            worktree_cleanup_confirmed=True,
        )
        state, blocked = pulse.confirm_task_retirement(
            state,
            wake_id="wake-1",
            now=NOW,
            task_id="setup-1",
            role="setup",
            outcome="non_deletion",
        )
        self.assertEqual(blocked["reason_code"], "task_retirement_not_confirmed")
        self.assertIsNone(state["active_wake_id"])

        state, confirmed = pulse.reconcile_task_retirement(
            state,
            wake_id="wake-1",
            now=NOW,
            task_id="setup-1",
            role="setup",
            lookup="AUTHORITATIVE_NOT_FOUND",
        )
        self.assertEqual(confirmed["next_action"], "RETIREMENT_CONFIRMED")
        self.assertEqual(state["active_wake_id"], "wake-1")
        self.assertEqual(
            state["failure_latch"]["reason_code"], "task_retirement_not_confirmed"
        )

        state = self._record_verified_successor(
            state, "2026-08-26T00:26:00+00:00"
        )
        task = state["task_retirement"]["successor"]["readback"]
        state, recovered = pulse.recover_retirement_successor(
            state,
            wake_id="wake-1",
            now="2026-08-26T00:27:00+00:00",
            action="authorize",
            task=task,
            delivery_observed=False,
            activation_confirmed=False,
        )
        self.assertEqual(recovered["next_action"], "SUCCESSOR_AUTHORIZED")
        self.assertIsNone(state["failure_latch"])
        self.assertEqual(state["scheduled_task_disposition"], "PAUSED")

    def test_malformed_completion_failure_is_recoverable_after_retirement(self) -> None:
        state, anchor = self._retirement_ready_state()
        state = self._record_verified_successor(state, anchor)
        state, _ = pulse.authorize_successor(
            state,
            wake_id="wake-1",
            now=anchor,
            scheduled_created_at=anchor,
            scheduled_first_run="2026-08-26T00:36:00+00:00",
            scheduled_task_id="successor-task",
        )

        state, blocked = pulse.complete_wake(
            state,
            wake_id="wake-1",
            now=anchor,
            completion_failure={"evidence": {}},
        )
        self.assertEqual(blocked["reason_code"], "completion_failure_malformed")
        task = state["task_retirement"]["successor"]["readback"]
        state, recovered = pulse.recover_retirement_successor(
            state,
            wake_id="wake-1",
            now="2026-08-26T00:27:00+00:00",
            action="authorize",
            task=task,
            delivery_observed=False,
            activation_confirmed=False,
        )
        self.assertEqual(recovered["next_action"], "SUCCESSOR_AUTHORIZED")
        self.assertIsNone(state["failure_latch"])

    def test_retirement_recovery_rejects_changed_successor_timestamps(self) -> None:
        state, anchor = self._retirement_ready_state()
        state = self._record_verified_successor(state, anchor)
        task = deepcopy(state["task_retirement"]["successor"]["readback"])
        task["created_at"] = "2026-08-26T00:27:00+00:00"
        task["first_run"] = "2026-08-26T00:37:00+00:00"
        with self.assertRaisesRegex(
            pulse.DefaultWakeError, "timestamps do not match persisted evidence"
        ):
            pulse.recover_retirement_successor(
                state,
                wake_id="wake-1",
                now="2026-08-26T00:27:00+00:00",
                action="authorize",
                task=task,
                delivery_observed=False,
                activation_confirmed=False,
            )
        self.assertEqual(
            state["task_retirement"]["successor"]["created_at"], anchor
        )
        self.assertEqual(
            state["task_retirement"]["successor"]["first_run"],
            "2026-08-26T00:36:00+00:00",
        )

    def test_recovered_authorized_successor_finalizes_without_replaying_pr_work(self) -> None:
        state, anchor = self._retirement_ready_state()
        state = self._record_verified_successor(state, anchor)
        state, _ = pulse.authorize_successor(
            state,
            wake_id="wake-1",
            now=anchor,
            scheduled_created_at=anchor,
            scheduled_first_run="2026-08-26T00:36:00+00:00",
            scheduled_task_id="successor-task",
        )
        pulse._retirement_pause(
            state,
            reason_code="successor_finalization_interrupted",
            now="2026-08-26T00:27:00+00:00",
        )
        task = state["task_retirement"]["successor"]["readback"]
        state, result = pulse.recover_retirement_successor(
            state,
            wake_id="wake-1",
            now="2026-08-26T00:28:00+00:00",
            action="finalize",
            task=task,
            delivery_observed=False,
            activation_confirmed=False,
        )
        self.assertEqual(result["next_action"], "WAIT_REVIEW")
        self.assertEqual(state["scheduled_task_disposition"], "AUTHORIZED")
        with self.assertRaisesRegex(pulse.DefaultWakeError, "active wake"):
            pulse.record_snapshot(state, snapshot(), wake_id="wake-2", now=NOW)

    def test_activation_failure_recovery_restores_released_wake_ownership(self) -> None:
        state, anchor = self._retirement_ready_state()
        state = self._record_verified_successor(state, anchor)
        state, _ = pulse.authorize_successor(
            state,
            wake_id="wake-1",
            now=anchor,
            scheduled_created_at=anchor,
            scheduled_first_run="2026-08-26T00:36:00+00:00",
            scheduled_task_id="successor-task",
        )
        state, finalized = pulse.complete_wake(
            state,
            wake_id="wake-1",
            now=anchor,
            schedule_next_wake=lambda _: "2026-08-26T00:36:00+00:00",
            schedule_anchor_created_at=anchor,
            scheduled_task_id="successor-task",
            require_schedule_anchor=True,
        )
        self.assertEqual(finalized["next_action"], "WAIT_REVIEW")
        self.assertIsNone(state["active_wake_id"])
        state, blocked = pulse.complete_wake(
            state,
            wake_id="wake-1",
            now=anchor,
            scheduled_task_id="successor-task",
            completion_failure={
                "reason_code": "successor_activation_unconfirmed",
                "evidence": {"pause_confirmed": True},
            },
        )
        self.assertEqual(blocked["reason_code"], "successor_activation_unconfirmed")
        task = state["task_retirement"]["successor"]["readback"]
        state, recovered = pulse.recover_retirement_successor(
            state,
            wake_id="wake-1",
            now="2026-08-26T00:27:00+00:00",
            action="authorize",
            task=task,
            delivery_observed=False,
            activation_confirmed=False,
        )
        self.assertEqual(recovered["next_action"], "SUCCESSOR_AUTHORIZED")
        self.assertEqual(state["active_wake_id"], "wake-1")
        self.assertIsNone(state["failure_latch"])

    def test_standalone_handoff_is_target_bound_and_orders_publication(self) -> None:
        handoff = pulse.build_heartbeat_handoff("Owner/Repo", 17)

        self.assertEqual(handoff["repository"], "owner/repo")
        self.assertEqual(handoff["pull_request_number"], 17)
        self.assertEqual(handoff["protocol_version"], 13)
        self.assertEqual(handoff["model"], "gpt-5.6-luna")
        self.assertEqual(handoff["reasoning_effort"], "xhigh")
        self.assertEqual(handoff["scheduler_kind"], "cron")
        self.assertEqual(handoff["conversation_mode"], "standalone")
        self.assertFalse(handoff["reuse_conversation"])
        self.assertIsNone(handoff["target_thread_id"])
        self.assertEqual(handoff["checkpoint_scope"], "git-common-dir")
        self.assertEqual(handoff["checkout_mode"], "new-linked-worktree-per-wake")
        self.assertEqual(
            handoff["configured_checkout_role"], "read-only-repository-locator"
        )
        self.assertFalse(handoff["reuse_worktree"])
        self.assertEqual(
            handoff["schedule_anchor_mode"], "persisted-created-at-plus-cadence"
        )
        self.assertFalse(handoff["submit_dtstart"])
        self.assertEqual(
            handoff["batch_order"],
            [
                "record-outcome",
                "focused-validation",
                "exact-resolution",
                "aggregate-validation",
                "prepare-publication",
                "commit",
                "prepare-publication",
                "push",
                "record-publication",
            ],
        )
        self.assertIn("owner/repo#17", handoff["prompt"])
        self.assertIn("Never commit or push before every frozen thread is resolved", handoff["prompt"])
        self.assertIn("new standalone task/conversation", handoff["prompt"])
        self.assertIn("AGENTS.md", handoff["prompt"])
        self.assertIn("new task-owned clean linked worktree", handoff["prompt"])
        self.assertIn(
            "canonical durable lifecycle and control authority", handoff["prompt"]
        )
        self.assertIn("auxiliary recovery or evidence artifacts", handoff["prompt"])
        self.assertNotIn("Use only the target repository's", handoff["prompt"])
        self.assertIn(
            "authoritative in the persisted automation policy and task metadata",
            handoff["prompt"],
        )
        self.assertNotIn('"model":"gpt-5.6-luna"', handoff["prompt"])
        self.assertNotIn('"reasoning_effort":"xhigh"', handoff["prompt"])
        self.assertIn("passing it as --repository-path", handoff["prompt"])
        self.assertIn("configured/main checkout", handoff["prompt"])
        self.assertIn("read-only repository locator", handoff["prompt"])
        self.assertIn("do not submit DTSTART", handoff["prompt"])
        self.assertIn("full cron update payload", handoff["prompt"])
        self.assertIn("Never send a status-only update", handoff["prompt"])
        self.assertIn("pre-pause readback", handoff["prompt"])
        self.assertIn("post_pause_readback", handoff["prompt"])
        self.assertIn("--delivered-task-provenance", handoff["prompt"])
        self.assertIn("under the Git common dir and", handoff["prompt"])
        self.assertNotIn("under the Git-common dir and", handoff["prompt"])
        self.assertIn("checkpoint must remain AUTHORIZED until delivery", handoff["prompt"])
        self.assertIn("never activate from authorization alone", handoff["prompt"])
        self.assertIn("must never be reactivated", handoff["prompt"])
        self.assertIn("registers its verified paused setup task atomically", handoff["prompt"])
        self.assertIn("delete only that registered task ID", handoff["prompt"])
        self.assertIn("remove that worktree", handoff["prompt"])
        self.assertIn("persisted created_at plus cadence", handoff["prompt"])
        self.assertIn("Desktop-native update_plan tool", handoff["prompt"])
        self.assertIn("before the first PR/review operation", handoff["prompt"])
        self.assertIn("small outcome-oriented plan", handoff["prompt"])
        self.assertIn("exactly one step in_progress", handoff["prompt"])
        self.assertIn("Do not track setup or wait for delivery", handoff["prompt"])
        self.assertIn("reuse a plan across standalone wakes", handoff["prompt"])
        self.assertIn("never simulate it or persist plan state", handoff["prompt"])
        self.assertIn("CLI-specific behavior", handoff["prompt"])
        self.assertNotIn("same heartbeat", handoff["prompt"].lower())
        self.assertEqual(
            handoff["prompt_sha256"],
            __import__("hashlib").sha256(handoff["prompt"].encode()).hexdigest(),
        )
        self.assertEqual(
            pulse.build_standalone_task_handoff("owner/repo", 17), handoff
        )

        custom = pulse.build_standalone_task_handoff(
            "owner/repo",
            17,
            policy={"model": "gpt-5.6-terra", "reasoning_effort": "medium"},
        )
        self.assertEqual(custom["model"], "gpt-5.6-terra")
        self.assertEqual(custom["reasoning_effort"], "medium")
        self.assertEqual(custom["prompt"], handoff["prompt"])
        self.assertEqual(custom["prompt_sha256"], handoff["prompt_sha256"])

    def test_schema_one_checkpoint_migrates_to_policy_schema(self) -> None:
        legacy = empty_checkpoint("Owner/Repo", 17)
        legacy["default_mode_schema_version"] = 1
        migrated = pulse.ensure_default_lifecycle(legacy)
        self.assertEqual(migrated["default_mode_schema_version"], 4)
        self.assertEqual(migrated["automation_policy"]["profile"], "autonomous")
        self.assertIsNone(migrated["automation_policy"]["max_wakes"])
        self.assertEqual(migrated["retry_state"]["wake_attempts"], 0)

    def test_legacy_schema_three_admitted_count_fails_closed(self) -> None:
        legacy = empty_checkpoint("Owner/Repo", 17)
        legacy["default_mode_schema_version"] = 3
        legacy["wake_count"] = 1

        with self.assertRaisesRegex(
            pulse.DefaultWakeError, "legacy_wake_accounting_unverified"
        ):
            pulse.ensure_default_lifecycle(legacy)

    def test_setup_creation_intent_is_verified_before_initial_admission(self) -> None:
        state, intent = pulse.record_creation_intent(
            empty_checkpoint("Owner/Repo", 17),
            role="setup",
            now=NOW,
            creation_nonce="setup-nonce",
        )
        self.assertEqual(intent["next_action"], "CREATION_INTENT_RECORDED")
        state, recorded = pulse.record_setup_creation_id(
            state,
            now=NOW,
            task_id="setup-task",
        )
        self.assertEqual(recorded["next_action"], "SETUP_TASK_ID_RECORDED")
        handoff = pulse.build_standalone_task_handoff("Owner/Repo", 17)
        readback = {
            "id": "setup-task",
            "status": "PAUSED",
            "prompt": handoff["prompt"],
            "prompt_sha256": handoff["prompt_sha256"],
            "scheduler_kind": "cron",
            "conversation_mode": "standalone",
            "target_thread_id": None,
            "model": handoff["model"],
            "reasoning_effort": handoff["reasoning_effort"],
            "cadence_seconds": 600,
            "created_at": NOW,
            "first_run": "2026-08-26T00:10:00+00:00",
        }
        state, verified = pulse.record_setup_creation_readback(
            state,
            now=NOW,
            task=readback,
        )
        self.assertEqual(verified["next_action"], "SETUP_TASK_VERIFIED")
        state, admitted = pulse.begin_wake(
            state,
            wake_id="wake-1",
            now=NOW,
            pause_heartbeat=lambda: True,
            setup_task_provenance=verified["setup_task_provenance"],
        )

        self.assertEqual(admitted["next_action"], "WAKE_STARTED")
        self.assertEqual(state["wake_count"], 1)
        self.assertEqual(state["task_retirement"]["task_id"], "setup-task")
        self.assertIsNone(state["creation_intent"])

    def test_unresolved_setup_intent_blocks_a_second_external_create(self) -> None:
        state, _ = pulse.record_creation_intent(
            empty_checkpoint("Owner/Repo", 17),
            role="setup",
            now=NOW,
            creation_nonce="first-nonce",
        )

        replay_state, replay = pulse.record_creation_intent(
            state,
            role="setup",
            now=NOW,
            creation_nonce="second-nonce",
        )

        self.assertEqual(replay["next_action"], "CREATION_INTENT_RECORDED")
        self.assertEqual(replay["reason_code"], "creation_intent_already_pending")
        self.assertEqual(
            replay_state["creation_intent"]["creation_nonce"], "first-nonce"
        )

    def test_failed_pause_does_not_consume_admission_budget(self) -> None:
        state, result = pulse.begin_wake(
            empty_checkpoint("Owner/Repo", 17),
            wake_id="wake-1",
            now=NOW,
            pause_heartbeat=lambda: False,
        )

        self.assertEqual(result["reason_code"], "heartbeat_pause_unconfirmed")
        self.assertEqual(state["wake_count"], 0)

    def test_pushes_between_wakes_coalesce_to_the_latest_stable_head(self) -> None:
        state, _ = started()
        state, _ = pulse.record_snapshot(
            state,
            snapshot(eyes=True),
            wake_id="wake-1",
            now=NOW,
        )
        state, _ = pulse.complete_wake(
            state,
            wake_id="wake-1",
            now="2026-08-26T00:01:00+00:00",
            schedule_next_wake=lambda expected: expected,
            scheduled_task_id="task-1",
        )

        # HEAD2 and then HEAD3 were pushed before the scheduler delivered wake 2.
        state, _ = started(
            state,
            wake_id="wake-2",
            now="2026-08-26T00:11:00+00:00",
        )
        state, result = pulse.record_snapshot(
            state,
            snapshot(head="HEAD3", targeted=["T3"]),
            wake_id="wake-2",
            now="2026-08-26T00:11:00+00:00",
        )
        self.assertEqual(result["next_action"], "RUN_BATCH")

        state, result = pulse.freeze_default_batch(state, wake_id="wake-2")
        self.assertEqual(result["frozen_head_oid"], "HEAD3")
        self.assertEqual(result["targeted_thread_ids"], ["T3"])

    def test_default_policy_is_unbounded_but_optional_wake_limit_is_enforced(self) -> None:
        state, result = pulse.begin_wake(
            empty_checkpoint("Owner/Repo", 17),
            wake_id="wake-1",
            now=NOW,
            policy_overrides={"max_wakes": 1},
            pause_heartbeat=lambda: True,
        )
        self.assertEqual(result["next_action"], "WAKE_STARTED")
        state, _ = pulse.record_snapshot(state, snapshot(), wake_id="wake-1", now=NOW)
        state, _ = pulse.complete_wake(
            state,
            wake_id="wake-1",
            now="2026-08-26T00:01:00+00:00",
            schedule_next_wake=lambda expected: expected,
            scheduled_task_id="task-1",
        )
        state, result = pulse.begin_wake(
            state,
            wake_id="wake-2",
            now="2026-08-26T00:11:00+00:00",
            pause_heartbeat=lambda: True,
            delivered_task_id="task-1",
        )
        self.assertEqual(result["next_action"], "STOP_POLICY_LIMIT")
        self.assertEqual(result["reason_code"], "maximum_wakes_reached")
        self.assertEqual(state["wake_count"], 1)

    def test_completion_rejects_cadence_override_that_differs_from_policy(self) -> None:
        state, _ = pulse.begin_wake(
            empty_checkpoint("Owner/Repo", 17),
            wake_id="wake-1",
            now=NOW,
            policy_overrides={"cadence_seconds": 1200},
            pause_heartbeat=lambda: True,
        )
        with self.assertRaisesRegex(ValueError, "match the persisted automation policy"):
            pulse.complete_wake(
                state,
                wake_id="wake-1",
                now="2026-08-26T00:01:00+00:00",
                cadence_seconds=600,
                schedule_next_wake=lambda expected: expected,
            )
        self.assertEqual(state["automation_policy"]["cadence_seconds"], 1200)
        self.assertEqual(state["active_wake_id"], "wake-1")

    def test_incomplete_wake_pause_preserves_the_original_wake_marker(self) -> None:
        state, _ = started()

        state, result = pulse.begin_wake(
            state,
            wake_id="wake-2",
            now="2026-08-26T00:01:00+00:00",
            pause_heartbeat=lambda: True,
        )

        self.assertEqual(result["next_action"], "PAUSE_RECOVERY")
        self.assertEqual(result["reason_code"], "incomplete_wake")
        self.assertEqual(state["active_wake_id"], "wake-1")
        self.assertEqual(state["last_wake_id"], "wake-1")
        self.assertEqual(
            state["failure_latch"]["evidence"]["active_wake_id"], "wake-1"
        )

    def test_deadline_is_optional_and_stops_before_work(self) -> None:
        state, result = pulse.begin_wake(
            empty_checkpoint("Owner/Repo", 17),
            wake_id="wake-1",
            now="2026-08-26T00:01:00+00:00",
            policy_overrides={"deadline_at": "2026-08-26T00:00:00+00:00"},
            pause_heartbeat=lambda: True,
        )
        self.assertEqual(result["next_action"], "STOP_POLICY_LIMIT")
        self.assertEqual(result["reason_code"], "deadline_reached")
        self.assertEqual(state["wake_count"], 0)

    def test_recoverable_retry_resumes_the_same_frozen_batch_on_next_wake(self) -> None:
        state, _ = started()
        state, _ = pulse.record_snapshot(
            state, snapshot(targeted=["T1"]), wake_id="wake-1", now=NOW
        )
        state, _ = pulse.freeze_default_batch(state, wake_id="wake-1")
        state, result = pulse.record_retry(
            state,
            wake_id="wake-1",
            reason_code="transient_validation_failure",
            now=NOW,
            signature="test-failure",
            pending_repair=PENDING_REPAIR,
        )
        self.assertEqual(result["next_action"], "WAIT_RETRY")
        self.assertEqual(state["active_batch"]["pending_repair"], PENDING_REPAIR)
        state, _ = pulse.complete_wake(
            state,
            wake_id="wake-1",
            now="2026-08-26T00:01:00+00:00",
            schedule_next_wake=lambda expected: expected,
            scheduled_task_id="task-1",
        )
        self.assertEqual(state["wake_phase"], "retry_waiting")
        state, result = started(
            state, wake_id="wake-2", now="2026-08-26T00:11:00+00:00"
        )
        self.assertEqual(result["next_action"], "WAKE_STARTED")
        self.assertTrue(result["resume_pending_batch"])
        self.assertEqual(state["last_decision"]["reason_code"], "resume_pending_batch")

    def test_retry_waiting_can_authorize_a_successor(self) -> None:
        state, _ = started()
        state, _ = pulse.record_snapshot(
            state, snapshot(targeted=["T1"]), wake_id="wake-1", now=NOW
        )
        state, _ = pulse.freeze_default_batch(state, wake_id="wake-1")
        state, _ = pulse.record_default_outcome(
            state,
            wake_id="wake-1",
            thread_id="T1",
            classification="fix-now",
            now=NOW,
        )
        state, result = pulse.record_retry(
            state,
            wake_id="wake-1",
            reason_code="transient_validation_failure",
            now=NOW,
            signature="test-failure",
            pending_repair=PENDING_REPAIR,
        )
        self.assertEqual(result["next_action"], "WAIT_RETRY")

        state, result = pulse.authorize_successor(
            state,
            wake_id="wake-1",
            now="2026-08-26T00:01:00+00:00",
            scheduled_created_at="2026-08-26T00:01:00+00:00",
            scheduled_first_run="2026-08-26T00:11:00+00:00",
            scheduled_task_id="task-1",
        )

        self.assertEqual(result["next_action"], "SUCCESSOR_AUTHORIZED")
        self.assertEqual(state["scheduled_task_disposition"], "PAUSED")
        self.assertEqual(state["wake_phase"], "successor_authorized")
        self.assertEqual(state["active_wake_id"], "wake-1")
        self.assertIsNone(state["wake_completed_at"])

        state, completed = pulse.complete_wake(
            state,
            wake_id="wake-1",
            now="2026-08-26T00:01:00+00:00",
            schedule_next_wake=lambda expected: expected,
            schedule_anchor_created_at="2026-08-26T00:01:00+00:00",
            scheduled_task_id="task-1",
        )
        self.assertEqual(completed["next_action"], "WAIT_RETRY")
        self.assertEqual(state["scheduled_task_disposition"], "AUTHORIZED")
        self.assertEqual(state["wake_phase"], "successor_finalized")
        self.assertIsNone(state["active_wake_id"])

        state, delivered = pulse.begin_wake(
            state,
            wake_id="wake-2",
            now="2026-08-26T00:11:00+00:00",
            pause_heartbeat=lambda: True,
            delivered_task_id="task-1",
        )
        self.assertEqual(delivered["next_action"], "WAKE_STARTED")
        self.assertTrue(delivered["resume_pending_batch"])

    def test_authorize_successor_requires_a_rearmable_decision(self) -> None:
        state, _ = started()

        state, result = pulse.authorize_successor(
            state,
            wake_id="wake-1",
            now="2026-08-26T00:01:00+00:00",
            scheduled_created_at="2026-08-26T00:01:00+00:00",
            scheduled_first_run="2026-08-26T00:11:00+00:00",
            scheduled_task_id="task-1",
        )

        self.assertEqual(result["next_action"], "PAUSE_RECOVERY")
        self.assertEqual(result["reason_code"], "successor_not_rearmable")
        self.assertEqual(state["scheduled_task_disposition"], "PAUSED")
        self.assertIsNone(state.get("successor_authorization"))

    def test_authorize_successor_requires_confirmed_predecessor_retirement(self) -> None:
        state, _ = pulse.begin_wake(
            empty_checkpoint("Owner/Repo", 17),
            wake_id="wake-1",
            now=NOW,
            pause_heartbeat=lambda: True,
            setup_task_provenance=self._setup_provenance("setup-1"),
        )
        state, _ = pulse.record_snapshot(
            state, snapshot(), wake_id="wake-1", now=NOW
        )

        with self.assertRaisesRegex(
            pulse.DefaultWakeError,
            "confirmed predecessor retirement",
        ):
            pulse.authorize_successor(
                state,
                wake_id="wake-1",
                now="2026-08-26T00:01:00+00:00",
                scheduled_created_at="2026-08-26T00:01:00+00:00",
                scheduled_first_run="2026-08-26T00:11:00+00:00",
                scheduled_task_id="successor-task",
            )

        self.assertEqual(state["task_retirement"]["phase"], "registered")
        self.assertEqual(state["task_retirement"]["task_id"], "setup-1")

    def test_authorize_successor_requires_completed_publication(self) -> None:
        state, _ = started()
        state, _ = pulse.record_snapshot(
            state, snapshot(targeted=["T1"]), wake_id="wake-1", now=NOW
        )
        state, _ = pulse.freeze_default_batch(state, wake_id="wake-1")

        state, result = pulse.authorize_successor(
            state,
            wake_id="wake-1",
            now="2026-08-26T00:01:00+00:00",
            scheduled_created_at="2026-08-26T00:01:00+00:00",
            scheduled_first_run="2026-08-26T00:11:00+00:00",
            scheduled_task_id="task-1",
        )

        self.assertEqual(result["next_action"], "PAUSE_RECOVERY")
        self.assertEqual(result["reason_code"], "batch_publication_incomplete")
        self.assertEqual(state["scheduled_task_disposition"], "PAUSED")
        self.assertIsNone(state.get("successor_authorization"))

    def test_authorize_successor_requires_confirmed_review_trigger(self) -> None:
        state, _ = started()
        state["last_decision"] = {"next_action": "REQUEST_REVIEW"}
        state["last_snapshot"] = {"head_oid": "HEAD1"}
        state["trigger_events"] = {}

        state, result = pulse.authorize_successor(
            state,
            wake_id="wake-1",
            now="2026-08-26T00:01:00+00:00",
            scheduled_created_at="2026-08-26T00:01:00+00:00",
            scheduled_first_run="2026-08-26T00:11:00+00:00",
            scheduled_task_id="task-1",
        )

        self.assertEqual(result["next_action"], "PAUSE_RECOVERY")
        self.assertEqual(result["reason_code"], "review_trigger_not_confirmed")
        self.assertEqual(state["scheduled_task_disposition"], "PAUSED")
        self.assertIsNone(state.get("successor_authorization"))

    def test_retry_waiting_is_a_mutation_boundary_but_can_complete(self) -> None:
        state, _ = started()
        state, _ = pulse.record_snapshot(
            state, snapshot(targeted=["T1"]), wake_id="wake-1", now=NOW
        )
        state, _ = pulse.freeze_default_batch(state, wake_id="wake-1")
        state, _ = pulse.record_default_outcome(
            state,
            wake_id="wake-1",
            thread_id="T1",
            classification="fix-now",
            now=NOW,
        )
        state, result = pulse.record_retry(
            state,
            wake_id="wake-1",
            reason_code="transient_validation_failure",
            now=NOW,
            signature="test-failure",
            pending_repair=PENDING_REPAIR,
        )
        self.assertEqual(result["next_action"], "WAIT_RETRY")

        with self.assertRaisesRegex(pulse.DefaultWakeError, "terminal boundary"):
            pulse.record_default_outcome(
                state,
                wake_id="wake-1",
                thread_id="T1",
                classification="fix-now",
                now=NOW,
            )

        with self.assertRaisesRegex(pulse.DefaultWakeError, "terminal boundary"):
            pulse.resolve_default_thread(
                state,
                wake_id="wake-1",
                thread_id="T1",
                graphql_call=lambda *_: {},
            )
        with self.assertRaisesRegex(pulse.DefaultWakeError, "terminal boundary"):
            pulse.prepare_default_publication(
                state,
                wake_id="wake-1",
                now=NOW,
                actual_head_oid="HEAD1",
            )
        with self.assertRaisesRegex(pulse.DefaultWakeError, "terminal boundary"):
            pulse.record_publication_result(
                state,
                wake_id="wake-1",
                status="succeeded",
                now=NOW,
                published_commit="HEAD1",
            )

        state, completed = pulse.complete_wake(
            state,
            wake_id="wake-1",
            now="2026-08-26T00:01:00+00:00",
            schedule_next_wake=lambda expected: expected,
            scheduled_task_id="task-1",
        )
        self.assertEqual(completed["next_action"], "WAIT_RETRY")

    def test_retry_with_uncommitted_fix_requires_a_pending_repair_manifest(self) -> None:
        state, _ = started()
        state, _ = pulse.record_snapshot(
            state, snapshot(targeted=["T1"]), wake_id="wake-1", now=NOW
        )
        state, _ = pulse.freeze_default_batch(state, wake_id="wake-1")
        state, _ = pulse.record_default_outcome(
            state,
            wake_id="wake-1",
            thread_id="T1",
            classification="fix-now",
            now=NOW,
        )

        state, result = pulse.record_retry(
            state,
            wake_id="wake-1",
            reason_code="transient_validation_failure",
            now=NOW,
            signature="test-failure",
        )

        self.assertEqual(result["next_action"], "PAUSE_RECOVERY")
        self.assertEqual(result["reason_code"], "pending_repair_unpersisted")
        self.assertEqual(state["failure_latch"]["reason_code"], "pending_repair_unpersisted")

    def test_retry_requires_updated_pending_repair_when_one_already_exists(self) -> None:
        state, _ = started()
        state, _ = pulse.record_snapshot(
            state, snapshot(targeted=["T1"]), wake_id="wake-1", now=NOW
        )
        state, _ = pulse.freeze_default_batch(state, wake_id="wake-1")
        state, _ = pulse.record_default_outcome(
            state, wake_id="wake-1", thread_id="T1",
            classification="fix-now", now=NOW,
        )
        state["active_batch"]["pending_repair"] = PENDING_REPAIR.copy()

        state, result = pulse.record_retry(
            state, wake_id="wake-1",
            reason_code="transient_validation_failure",
            now=NOW, signature="test-failure",
        )

        self.assertEqual(result["next_action"], "PAUSE_RECOVERY")
        self.assertEqual(result["reason_code"], "pending_repair_unpersisted")

    def test_repair_restore_guard_survives_snapshot_state_reset(self) -> None:
        state, _ = started()
        state["active_batch"] = {
            "frozen_head_oid": "HEAD1",
            "targeted_thread_ids": ["T1"],
            "thread_outcomes": {"T1": {"classification": "fix-now"}},
            "pending_repair": PENDING_REPAIR.copy(),
        }
        state["active_wake_id"] = "wake-1"
        state["wake_phase"] = "processing"
        state["resume_pending_batch"] = False
        with self.assertRaisesRegex(pulse.DefaultWakeError, "Restore"):
            pulse.resolve_default_thread(
                state, wake_id="wake-1", thread_id="T1", graphql_call=lambda *_: {}
            )

    def test_freeze_pauses_when_worktree_head_differs_from_snapshot(self) -> None:
        state, _ = started()
        state, _ = pulse.record_snapshot(
            state, snapshot(targeted=["T1"]), wake_id="wake-1", now=NOW
        )

        state, result = pulse.freeze_default_batch(
            state,
            wake_id="wake-1",
            worktree_head_oid="OTHER_HEAD",
            now=NOW,
        )

        self.assertEqual(result["next_action"], "PAUSE_RECOVERY")
        self.assertEqual(result["reason_code"], "worktree_head_mismatch")
        self.assertEqual(
            result["evidence"],
            {"snapshot_head_oid": "HEAD1", "worktree_head_oid": "OTHER_HEAD"},
        )
        self.assertIsNone(state["active_batch"])

    def test_retry_resume_preserves_frozen_targets_when_review_threads_disappear(self) -> None:
        state, _ = started()
        state, _ = pulse.record_snapshot(
            state, snapshot(targeted=["T1"]), wake_id="wake-1", now=NOW
        )
        state, _ = pulse.freeze_default_batch(state, wake_id="wake-1")
        state, _ = pulse.record_retry(
            state,
            wake_id="wake-1",
            reason_code="transient_validation_failure",
            now=NOW,
            signature="test-failure",
        )
        state, _ = pulse.complete_wake(
            state,
            wake_id="wake-1",
            now="2026-08-26T00:01:00+00:00",
            schedule_next_wake=lambda expected: expected,
            scheduled_task_id="task-1",
        )
        state, _ = started(
            state, wake_id="wake-2", now="2026-08-26T00:11:00+00:00"
        )

        state, result = pulse.record_snapshot(
            state, snapshot(targeted=[]), wake_id="wake-2", now="2026-08-26T00:11:00+00:00"
        )

        self.assertEqual(result["next_action"], "RUN_BATCH")
        self.assertEqual(result["reason_code"], "resume_pending_batch")
        self.assertEqual(state["latest_target_snapshot"]["targeted_unresolved_thread_ids"], ["T1"])
        state, batch = pulse.freeze_default_batch(state, wake_id="wake-2")
        self.assertEqual(batch["targeted_thread_ids"], ["T1"])

    def test_retry_resume_fails_closed_when_frozen_head_changes(self) -> None:
        state, _ = started()
        state, _ = pulse.record_snapshot(
            state, snapshot(targeted=["T1"]), wake_id="wake-1", now=NOW
        )
        state, _ = pulse.freeze_default_batch(state, wake_id="wake-1")
        state, _ = pulse.record_retry(
            state,
            wake_id="wake-1",
            reason_code="transient_validation_failure",
            now=NOW,
            signature="test-failure",
        )
        state, _ = pulse.complete_wake(
            state,
            wake_id="wake-1",
            now="2026-08-26T00:01:00+00:00",
            schedule_next_wake=lambda expected: expected,
            scheduled_task_id="task-1",
        )
        state, _ = started(
            state, wake_id="wake-2", now="2026-08-26T00:11:00+00:00"
        )

        state, result = pulse.record_snapshot(
            state,
            snapshot(head="HEAD2", targeted=[]),
            wake_id="wake-2",
            now="2026-08-26T00:11:00+00:00",
        )

        self.assertEqual(result["next_action"], "PAUSE_RECOVERY")
        self.assertEqual(result["reason_code"], "retry_batch_head_changed")
        self.assertEqual(state["active_batch"]["frozen_head_oid"], "HEAD1")

    def test_repeated_no_progress_reaches_pause_limit(self) -> None:
        state, _ = pulse.begin_wake(
            empty_checkpoint("Owner/Repo", 17),
            wake_id="wake-1",
            now=NOW,
            policy_overrides={"no_progress_limit": 2},
            pause_heartbeat=lambda: True,
        )
        state, _ = pulse.record_snapshot(
            state, snapshot(targeted=["T1"]), wake_id="wake-1", now=NOW
        )
        state, _ = pulse.freeze_default_batch(state, wake_id="wake-1")
        state, _ = pulse.record_retry(
            state,
            wake_id="wake-1",
            reason_code="validation_failed",
            now=NOW,
            signature="same-failure",
            count_no_progress=True,
        )
        state, _ = pulse.complete_wake(
            state,
            wake_id="wake-1",
            now="2026-08-26T00:01:00+00:00",
            schedule_next_wake=lambda expected: expected,
            scheduled_task_id="task-1",
        )
        state, _ = started(state, wake_id="wake-2", now="2026-08-26T00:11:00+00:00")
        state, result = pulse.record_retry(
            state,
            wake_id="wake-2",
            reason_code="validation_failed",
            now="2026-08-26T00:11:00+00:00",
            signature="same-failure",
            count_no_progress=True,
        )
        self.assertEqual(result["next_action"], "PAUSE_BLOCKED")
        self.assertEqual(result["reason_code"], "no_progress_limit_reached")

    def test_supervised_profile_pauses_before_thread_resolution(self) -> None:
        state, _ = pulse.begin_wake(
            empty_checkpoint("Owner/Repo", 17),
            wake_id="wake-1",
            now=NOW,
            policy_overrides={"profile": "supervised"},
            pause_heartbeat=lambda: True,
        )
        state, _ = pulse.record_snapshot(
            state, snapshot(targeted=["T1"]), wake_id="wake-1", now=NOW
        )
        state, _ = pulse.freeze_default_batch(state, wake_id="wake-1")
        state, result = pulse.record_default_outcome(
            state,
            wake_id="wake-1",
            thread_id="T1",
            classification="fix-now",
            now=NOW,
        )
        self.assertEqual(result["next_action"], "PAUSE_POLICY_CONFIRMATION")
        self.assertEqual(result["reason_code"], "policy_requires_confirmation")

        state, result = pulse.confirm_policy_operation(
            state, operation="thread_resolution", now="2026-08-26T00:01:00+00:00"
        )
        self.assertEqual(result["next_action"], "POLICY_CONFIRMATION_RECORDED")
        self.assertIsNone(state["failure_latch"])
        self.assertEqual(state["wake_phase"], "confirmation_ready")

        state, result = started(
            state, wake_id="wake-2", now="2026-08-26T00:11:00+00:00"
        )
        self.assertTrue(result["resume_pending_batch"])
        state, result = pulse.record_snapshot(
            state, snapshot(targeted=[]), wake_id="wake-2", now="2026-08-26T00:11:00+00:00"
        )
        self.assertEqual(result["reason_code"], "resume_confirmed_batch")
        state, result = pulse.record_default_outcome(
            state,
            wake_id="wake-2",
            thread_id="T1",
            classification="fix-now",
            now="2026-08-26T00:11:00+00:00",
        )
        self.assertEqual(result["next_action"], "PROCESS_BATCH")

        def graphql_call(query: str, variables: dict[str, object]) -> dict[str, object]:
            if "resolveReviewThread" in query:
                return {
                    "data": {
                        "resolveReviewThread": {
                            "thread": {"id": variables["threadId"], "isResolved": True}
                        }
                    }
                }
            return {
                "data": {
                    "repository": {
                        "nameWithOwner": "owner/repo",
                        "pullRequest": {
                            "number": 17,
                            "headRefOid": "HEAD1",
                            "reviewThreads": {
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                                "nodes": [
                                    {
                                        "id": "T1",
                                        "isResolved": False,
                                        "comments": {
                                            "nodes": [
                                                {"author": {"login": "chatgpt-codex-connector"}}
                                            ]
                                        },
                                    }
                                ],
                            },
                        },
                    }
                }
            }

        state, result = pulse.resolve_default_thread(
            state,
            wake_id="wake-2",
            thread_id="T1",
            graphql_call=graphql_call,
        )
        self.assertEqual(result["next_action"], "THREAD_RESOLVED")
        self.assertIsNone(state["policy_confirmation"])

    def test_supervised_thread_confirmation_survives_until_the_batch_is_resolved(self) -> None:
        state, _ = pulse.begin_wake(
            empty_checkpoint("Owner/Repo", 17),
            wake_id="wake-1",
            now=NOW,
            policy_overrides={"profile": "supervised"},
            pause_heartbeat=lambda: True,
        )
        state, _ = pulse.record_snapshot(
            state,
            snapshot(targeted=["T1", "T2"]),
            wake_id="wake-1",
            now=NOW,
        )
        state, _ = pulse.freeze_default_batch(state, wake_id="wake-1")
        state, result = pulse.record_default_outcome(
            state,
            wake_id="wake-1",
            thread_id="T1",
            classification="fix-now",
            now=NOW,
        )
        self.assertEqual(result["next_action"], "PAUSE_POLICY_CONFIRMATION")
        state, _ = pulse.confirm_policy_operation(
            state, operation="thread_resolution", now="2026-08-26T00:01:00+00:00"
        )
        state, _ = started(state, wake_id="wake-2", now="2026-08-26T00:11:00+00:00")
        state, _ = pulse.record_snapshot(
            state,
            snapshot(targeted=[]),
            wake_id="wake-2",
            now="2026-08-26T00:11:00+00:00",
        )
        state, _ = pulse.record_default_outcome(
            state,
            wake_id="wake-2",
            thread_id="T1",
            classification="fix-now",
            now="2026-08-26T00:11:00+00:00",
        )

        def graphql_call(query: str, variables: dict[str, object]) -> dict[str, object]:
            if "resolveReviewThread" in query:
                return {
                    "data": {
                        "resolveReviewThread": {
                            "thread": {"id": variables["threadId"], "isResolved": True}
                        }
                    }
                }
            return {
                "data": {
                    "repository": {
                        "nameWithOwner": "owner/repo",
                        "pullRequest": {
                            "number": 17,
                            "headRefOid": "HEAD1",
                            "reviewThreads": {
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                                "nodes": [
                                    {
                                        "id": thread_id,
                                        "isResolved": False,
                                        "comments": {
                                            "nodes": [
                                                {"author": {"login": "chatgpt-codex-connector"}}
                                            ]
                                        },
                                    }
                                    for thread_id in ("T1", "T2")
                                ],
                            },
                        },
                    }
                }
            }

        state, _ = pulse.resolve_default_thread(
            state,
            wake_id="wake-2",
            thread_id="T1",
            graphql_call=graphql_call,
        )
        self.assertIsNotNone(state["policy_confirmation"])
        state, _ = pulse.record_default_outcome(
            state,
            wake_id="wake-2",
            thread_id="T2",
            classification="fix-now",
            now="2026-08-26T00:11:00+00:00",
        )
        state, _ = pulse.resolve_default_thread(
            state,
            wake_id="wake-2",
            thread_id="T2",
            graphql_call=graphql_call,
        )
        self.assertIsNone(state["policy_confirmation"])

    def test_supervised_confirmation_must_match_the_pending_operation(self) -> None:
        state, _ = pulse.begin_wake(
            empty_checkpoint("Owner/Repo", 17),
            wake_id="wake-1",
            now=NOW,
            policy_overrides={"profile": "supervised"},
            pause_heartbeat=lambda: True,
        )
        state, _ = pulse.record_snapshot(
            state, snapshot(targeted=["T1"]), wake_id="wake-1", now=NOW
        )
        state, _ = pulse.freeze_default_batch(state, wake_id="wake-1")
        state, _ = pulse.record_default_outcome(
            state,
            wake_id="wake-1",
            thread_id="T1",
            classification="fix-now",
            now=NOW,
        )
        with self.assertRaisesRegex(pulse.DefaultWakeError, "does not match"):
            pulse.confirm_policy_operation(
                state, operation="aggregate_publication", now=NOW
            )
        self.assertIsNotNone(state["failure_latch"])

    def test_never_policy_cannot_be_confirmed(self) -> None:
        state, _ = pulse.begin_wake(
            empty_checkpoint("Owner/Repo", 17),
            wake_id="wake-1",
            now=NOW,
            policy_overrides={"thread_resolution": "never"},
            pause_heartbeat=lambda: True,
        )
        state, _ = pulse.record_snapshot(
            state, snapshot(targeted=["T1"]), wake_id="wake-1", now=NOW
        )
        state, _ = pulse.freeze_default_batch(state, wake_id="wake-1")
        state, result = pulse.record_default_outcome(
            state,
            wake_id="wake-1",
            thread_id="T1",
            classification="fix-now",
            now=NOW,
        )
        self.assertEqual(result["next_action"], "PAUSE_POLICY_CONFIRMATION")
        with self.assertRaisesRegex(pulse.DefaultWakeError, "does not permit confirmation"):
            pulse.confirm_policy_operation(
                state, operation="thread_resolution", now="2026-08-26T00:01:00+00:00"
            )
        self.assertIsNotNone(state["failure_latch"])
        self.assertIsNone(state["policy_confirmation"])

    def test_initial_wake_replay_precedes_later_policy_override_validation(self) -> None:
        state, result = pulse.begin_wake(
            empty_checkpoint("Owner/Repo", 17),
            wake_id="wake-1",
            now=NOW,
            policy_overrides={"max_wakes": 5},
            pause_heartbeat=lambda: True,
        )
        before = deepcopy(state)
        replayed, replay_result = pulse.begin_wake(
            state,
            wake_id="wake-1",
            now="2026-08-26T00:01:00+00:00",
            policy_overrides={"not_a_policy": True},
            pause_heartbeat=lambda: False,
        )
        self.assertEqual(replayed, before)
        self.assertEqual(replay_result, result)

    def test_supervised_review_trigger_confirmation_allows_exact_trigger(self) -> None:
        state, _ = pulse.begin_wake(
            empty_checkpoint("Owner/Repo", 17),
            wake_id="wake-1",
            now=NOW,
            policy_overrides={"profile": "supervised"},
            pause_heartbeat=lambda: True,
        )
        state, result = pulse.record_snapshot(
            state, snapshot(), wake_id="wake-1", now=NOW
        )
        self.assertEqual(result["next_action"], "WAIT_REVIEW")
        state, _ = pulse.complete_wake(
            state,
            wake_id="wake-1",
            now="2026-08-26T00:01:00+00:00",
            schedule_next_wake=lambda expected: expected,
            scheduled_task_id="task-1",
        )

        state, _ = started(
            state, wake_id="wake-2", now="2026-08-26T00:11:00+00:00"
        )
        state, result = pulse.record_snapshot(
            state, snapshot(), wake_id="wake-2", now="2026-08-26T00:11:00+00:00"
        )
        self.assertEqual(result["next_action"], "PAUSE_POLICY_CONFIRMATION")
        self.assertEqual(result["reason_code"], "policy_requires_confirmation")
        state["active_batch"] = {
            "frozen_head_oid": "OLDER_HEAD",
            "publication": {"status": "succeeded"},
            "targeted_thread_ids": ["OLD_THREAD"],
        }

        state, result = pulse.confirm_policy_operation(
            state, operation="review_trigger", now="2026-08-26T00:12:00+00:00"
        )
        self.assertEqual(result["next_action"], "POLICY_CONFIRMATION_RECORDED")

        state, _ = started(
            state, wake_id="wake-3", now="2026-08-26T00:22:00+00:00"
        )
        state, result = pulse.record_snapshot(
            state, snapshot(), wake_id="wake-3", now="2026-08-26T00:22:00+00:00"
        )
        self.assertEqual(result["next_action"], "REQUEST_REVIEW")
        state, result = pulse.record_default_trigger(
            state,
            wake_id="wake-3",
            evidence={
                "attempted_head_oid": "HEAD1",
                "head_before": "HEAD1",
                "head_after": "HEAD1",
                "comment_node_id": "COMMENT1",
                "created_at": "2026-08-26T00:22:00+00:00",
            },
        )
        self.assertEqual(result["reason_code"], "review_trigger_recorded")
        self.assertIsNone(state["policy_confirmation"])

    def test_validation_failure_policy_can_disable_automatic_retry(self) -> None:
        state, _ = pulse.begin_wake(
            empty_checkpoint("Owner/Repo", 17),
            wake_id="wake-1",
            now=NOW,
            policy_overrides={"validation_failure": "pause"},
            pause_heartbeat=lambda: True,
        )
        state, result = pulse.record_retry(
            state,
            wake_id="wake-1",
            reason_code="validation_failed",
            now=NOW,
        )
        self.assertEqual(result["next_action"], "PAUSE_POLICY_CONFIRMATION")
        self.assertEqual(state["scheduled_task_disposition"], "PAUSED")

    def test_policy_update_cannot_split_an_unfinished_frozen_batch(self) -> None:
        state, _ = started()
        state, _ = pulse.record_snapshot(
            state, snapshot(targeted=["T1"]), wake_id="wake-1", now=NOW
        )
        state, _ = pulse.freeze_default_batch(state, wake_id="wake-1")
        state, _ = pulse.record_retry(
            state,
            wake_id="wake-1",
            reason_code="transient_failure",
            now=NOW,
        )
        state, _ = pulse.complete_wake(
            state,
            wake_id="wake-1",
            now="2026-08-26T00:01:00+00:00",
            schedule_next_wake=lambda expected: expected,
            scheduled_task_id="task-1",
        )
        with self.assertRaisesRegex(pulse.DefaultWakeError, "unfinished"):
            pulse.update_default_policy(
                state,
                overrides={"max_wakes": 2},
                now=NOW,
            )

    def test_default_import_does_not_load_hardened_authority_modules(self) -> None:
        process = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; sys.path.insert(0, r'%s'); import pulse; print(','.join(sorted(name for name in ('recurring_contract','recurring_model','heartbeat_tick') if name in sys.modules)))"
                % SCRIPTS,
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(process.stdout.strip(), "")

    def test_wake_starts_paused_and_increments_once(self) -> None:
        state, result = started()
        self.assertEqual(result["next_action"], "WAKE_STARTED")
        self.assertEqual(state["scheduled_task_disposition"], "PAUSED")
        self.assertEqual(state["wake_phase"], "started")
        self.assertEqual(state["wake_count"], 1)

        replay, replay_result = pulse.begin_wake(
            state,
            wake_id="wake-1",
            now="2026-08-26T00:01:00+00:00",
            pause_heartbeat=lambda: True,
        )
        self.assertEqual(replay["wake_count"], 1)
        self.assertEqual(replay_result, result)

    def test_pause_failure_blocks_snapshot_and_all_pr_mutations(self) -> None:
        state, result = pulse.begin_wake(
            empty_checkpoint("Owner/Repo", 17),
            wake_id="wake-1",
            now=NOW,
            pause_heartbeat=lambda: False,
        )
        self.assertEqual(result["next_action"], "PAUSE_BLOCKED")
        self.assertEqual(result["reason_code"], "heartbeat_pause_unconfirmed")
        self.assertEqual(state["scheduled_task_disposition"], "PAUSED")
        with self.assertRaises(pulse.DefaultWakeError):
            pulse.record_snapshot(state, snapshot(targeted=["T1"]), wake_id="wake-1", now=NOW)
        self.assertIsNone(state.get("active_batch"))

    def test_paused_result_preserves_prior_thread_resolution_mutation(self) -> None:
        state, _ = started()
        state, _ = pulse.record_snapshot(
            state,
            snapshot(targeted=["T1", "T2"]),
            wake_id="wake-1",
            now=NOW,
        )
        state, _ = pulse.freeze_default_batch(state, wake_id="wake-1")
        state, _ = pulse.record_default_outcome(
            state,
            wake_id="wake-1",
            thread_id="T1",
            classification="no-fix",
            reference="false positive",
            now=NOW,
        )

        def graphql_call(query: str, variables: dict[str, object]) -> dict[str, object]:
            if "resolveReviewThread" in query:
                return {
                    "data": {
                        "resolveReviewThread": {
                            "thread": {"id": variables["threadId"], "isResolved": True}
                        }
                    }
                }
            return {
                "data": {
                    "repository": {
                        "nameWithOwner": "owner/repo",
                        "pullRequest": {
                            "number": 17,
                            "headRefOid": "HEAD1",
                            "reviewThreads": {
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                                "nodes": [
                                    {
                                        "id": "T1",
                                        "isResolved": False,
                                        "comments": {
                                            "nodes": [
                                                {"author": {"login": "chatgpt-codex-connector"}}
                                            ]
                                        },
                                    },
                                    {
                                        "id": "T2",
                                        "isResolved": False,
                                        "comments": {
                                            "nodes": [
                                                {"author": {"login": "chatgpt-codex-connector"}}
                                            ]
                                        },
                                    }
                                ],
                            },
                        },
                    }
                }
            }

        state, resolved = pulse.resolve_default_thread(
            state,
            wake_id="wake-1",
            thread_id="T1",
            graphql_call=graphql_call,
        )
        self.assertTrue(resolved["mutation_occurred"])

        state, paused = pulse.record_default_outcome(
            state,
            wake_id="wake-1",
            thread_id="T2",
            classification="ambiguous",
            reference="uncertain finding",
            now=NOW,
        )
        self.assertEqual(paused["next_action"], "PAUSE_BLOCKED")
        self.assertEqual(paused["reason_code"], "ambiguous_thread_outcome")
        self.assertTrue(paused["mutation_occurred"])
        self.assertTrue(state["last_wake_result"]["mutation_occurred"])

    def test_duplicate_snapshot_does_not_plan_or_increment_wake(self) -> None:
        state, _ = started()
        state, first = pulse.record_snapshot(state, snapshot(), wake_id="wake-1", now=NOW)
        state, second = pulse.record_snapshot(state, snapshot(), wake_id="wake-1", now=NOW)
        self.assertEqual(first, second)
        self.assertEqual(state["wake_count"], 1)

    def test_completion_relative_cadence_uses_completion_not_start(self) -> None:
        state, _ = started()
        state, _ = pulse.record_snapshot(state, snapshot(), wake_id="wake-1", now=NOW)
        state, result = pulse.complete_wake(
            state,
            wake_id="wake-1",
            now="2026-08-26T00:26:00+00:00",
            schedule_next_wake=lambda expected: expected,
            scheduled_task_id="task-1",
        )
        self.assertEqual(result["next_not_before"], "2026-08-26T00:36:00+00:00")
        self.assertEqual(state["scheduled_task_disposition"], "ACTIVE")

    def test_creation_anchored_schedule_uses_persisted_task_creation_time(self) -> None:
        state, _ = started()
        state, _ = pulse.record_snapshot(state, snapshot(), wake_id="wake-1", now=NOW)
        state, result = pulse.complete_wake(
            state,
            wake_id="wake-1",
            now="2026-08-26T00:26:00+00:00",
            schedule_next_wake=lambda _: "2026-08-26T00:36:03+00:00",
            schedule_anchor_created_at="2026-08-26T00:26:02.250000+00:00",
            scheduled_task_id="task-1",
        )

        self.assertEqual(result["next_action"], "WAIT_REVIEW")
        self.assertEqual(result["next_not_before"], "2026-08-26T00:36:02+00:00")
        self.assertEqual(
            result["scheduled_task_created_at"],
            "2026-08-26T00:26:02.250000+00:00",
        )
        self.assertEqual(state["next_not_before"], "2026-08-26T00:36:02+00:00")
        self.assertEqual(state["scheduled_task_disposition"], "ACTIVE")

    def test_authorized_successor_stays_intermediate_until_activation_or_delivery(self) -> None:
        state, _ = started()
        state, _ = pulse.record_snapshot(state, snapshot(), wake_id="wake-1", now=NOW)
        state, result = pulse.authorize_successor(
            state,
            wake_id="wake-1",
            now="2026-08-26T00:26:00+00:00",
            scheduled_created_at="2026-08-26T00:26:00+00:00",
            scheduled_first_run="2026-08-26T00:36:00+00:00",
            scheduled_task_id="task-a",
        )

        self.assertEqual(result["next_action"], "SUCCESSOR_AUTHORIZED")
        self.assertEqual(state["scheduled_task_disposition"], "PAUSED")
        self.assertEqual(state["active_wake_id"], "wake-1")
        self.assertIsNone(state["wake_completed_at"])

        interrupted = deepcopy(state)
        interrupted, delivered = pulse.begin_wake(
            interrupted,
            wake_id="wake-2",
            now="2026-08-26T00:36:00+00:00",
            pause_heartbeat=lambda: True,
            delivered_task_id="task-a",
        )
        self.assertEqual(delivered["next_action"], "PAUSE_RECOVERY")
        self.assertEqual(delivered["reason_code"], "incomplete_wake")
        self.assertEqual(state["active_wake_id"], "wake-1")

        state, completed = pulse.complete_wake(
            state,
            wake_id="wake-1",
            now="2026-08-26T00:26:00+00:00",
            schedule_next_wake=lambda expected: expected,
            schedule_anchor_created_at="2026-08-26T00:26:00+00:00",
            scheduled_task_id="task-a",
        )
        self.assertEqual(completed["next_action"], "WAIT_REVIEW")
        self.assertEqual(state["scheduled_task_disposition"], "AUTHORIZED")
        self.assertEqual(state["wake_phase"], "successor_finalized")

        state, delivered = pulse.begin_wake(
            state,
            wake_id="wake-3",
            now="2026-08-26T00:36:00+00:00",
            pause_heartbeat=lambda: True,
            delivered_task_id="task-a",
        )
        self.assertEqual(delivered["next_action"], "WAKE_STARTED")
        self.assertEqual(state["scheduled_task_disposition"], "PAUSED")
        self.assertIsNone(state["successor_authorization"])

    def test_authorized_successor_can_be_reconciled_after_host_restart(self) -> None:
        state, _ = started()
        state, _ = pulse.record_snapshot(state, snapshot(), wake_id="wake-1", now=NOW)
        state, _ = pulse.authorize_successor(
            state,
            wake_id="wake-1",
            now="2026-08-26T00:26:00+00:00",
            scheduled_created_at="2026-08-26T00:26:00+00:00",
            scheduled_first_run="2026-08-26T00:36:00+00:00",
            scheduled_task_id="task-a",
        )
        state, _ = pulse.complete_wake(
            state,
            wake_id="wake-1",
            now="2026-08-26T00:26:00+00:00",
            schedule_next_wake=lambda expected: expected,
            schedule_anchor_created_at="2026-08-26T00:26:00+00:00",
            scheduled_task_id="task-a",
        )

        state, result = pulse.reconcile_authorized_successor(
            state,
            now="2026-08-26T00:27:00+00:00",
            scheduled_task_id="task-a",
            action="activate",
            confirmed=True,
            evidence={
                "host_activation": "confirmed",
                "task_status": "PAUSED",
                "delivery_observed": False,
            },
        )

        self.assertEqual(result["next_action"], "SUCCESSOR_RECONCILED")
        self.assertEqual(state["scheduled_task_disposition"], "ACTIVE")
        self.assertEqual(state["wake_phase"], "completed")
        self.assertIsNone(state["successor_authorization"])

    def test_authorized_successor_reconciliation_can_fail_closed_paused(self) -> None:
        state, _ = started()
        state, _ = pulse.record_snapshot(state, snapshot(), wake_id="wake-1", now=NOW)
        state, _ = pulse.authorize_successor(
            state,
            wake_id="wake-1",
            now="2026-08-26T00:26:00+00:00",
            scheduled_created_at="2026-08-26T00:26:00+00:00",
            scheduled_first_run="2026-08-26T00:36:00+00:00",
            scheduled_task_id="task-a",
        )
        state, _ = pulse.complete_wake(
            state,
            wake_id="wake-1",
            now="2026-08-26T00:26:00+00:00",
            schedule_next_wake=lambda expected: expected,
            schedule_anchor_created_at="2026-08-26T00:26:00+00:00",
            scheduled_task_id="task-a",
        )

        state, result = pulse.reconcile_authorized_successor(
            state,
            now="2026-08-26T00:27:00+00:00",
            scheduled_task_id="task-a",
            action="pause",
            confirmed=True,
            evidence={
                "host_pause": "confirmed",
                "task_status": "PAUSED",
                "delivery_observed": False,
            },
        )

        self.assertEqual(result["next_action"], "PAUSE_RECOVERY")
        self.assertEqual(state["scheduled_task_disposition"], "PAUSED")
        self.assertEqual(
            state["failure_latch"]["reason_code"],
            "successor_activation_recovery_required",
        )

    def test_authorization_interruption_cannot_be_reconciled_as_active(self) -> None:
        state, _ = started()
        state, _ = pulse.record_snapshot(state, snapshot(), wake_id="wake-1", now=NOW)
        state, _ = pulse.authorize_successor(
            state,
            wake_id="wake-1",
            now="2026-08-26T00:26:00+00:00",
            scheduled_created_at="2026-08-26T00:26:00+00:00",
            scheduled_first_run="2026-08-26T00:36:00+00:00",
            scheduled_task_id="task-a",
        )

        self.assertEqual(state["scheduled_task_disposition"], "PAUSED")
        self.assertEqual(state["active_wake_id"], "wake-1")
        self.assertIsNone(state["wake_completed_at"])
        with self.assertRaisesRegex(
            pulse.DefaultWakeError, "before the wake is durably finalized"
        ):
            pulse.reconcile_authorized_successor(
                state,
                now="2026-08-26T00:27:00+00:00",
                scheduled_task_id="task-a",
                action="activate",
                confirmed=True,
                evidence={
                    "task_status": "PAUSED",
                    "delivery_observed": False,
                },
            )

    def test_reconciliation_rejects_one_sided_or_delivered_evidence(self) -> None:
        state, _ = started()
        state, _ = pulse.record_snapshot(state, snapshot(), wake_id="wake-1", now=NOW)
        state, _ = pulse.authorize_successor(
            state,
            wake_id="wake-1",
            now="2026-08-26T00:26:00+00:00",
            scheduled_created_at="2026-08-26T00:26:00+00:00",
            scheduled_first_run="2026-08-26T00:36:00+00:00",
            scheduled_task_id="task-a",
        )
        state, _ = pulse.complete_wake(
            state,
            wake_id="wake-1",
            now="2026-08-26T00:26:00+00:00",
            schedule_next_wake=lambda expected: expected,
            schedule_anchor_created_at="2026-08-26T00:26:00+00:00",
            scheduled_task_id="task-a",
        )

        for evidence in (
            {"host_activation": "confirmed"},
            {
                "task_status": "ACTIVE",
                "delivery_observed": True,
                "delivered_task_id": "task-a",
            },
        ):
            with self.subTest(evidence=evidence):
                with self.assertRaises(pulse.DefaultWakeError):
                    pulse.reconcile_authorized_successor(
                        state,
                        now="2026-08-26T00:27:00+00:00",
                        scheduled_task_id="task-a",
                        action="activate",
                        confirmed=True,
                        evidence=evidence,
                    )

    def test_complete_wake_rejects_successor_authorization_mismatch(self) -> None:
        state, _ = started()
        state, _ = pulse.record_snapshot(state, snapshot(), wake_id="wake-1", now=NOW)
        state, _ = pulse.authorize_successor(
            state,
            wake_id="wake-1",
            now="2026-08-26T00:26:00+00:00",
            scheduled_created_at="2026-08-26T00:26:00+00:00",
            scheduled_first_run="2026-08-26T00:36:00+00:00",
            scheduled_task_id="task-a",
        )

        state, result = pulse.complete_wake(
            state,
            wake_id="wake-1",
            now="2026-08-26T00:26:00+00:00",
            schedule_next_wake=lambda _: "2026-08-26T00:36:00+00:00",
            schedule_anchor_created_at="2026-08-26T00:26:00+00:00",
            scheduled_task_id="task-b",
        )

        self.assertEqual(result["next_action"], "PAUSE_RECOVERY")
        self.assertEqual(result["reason_code"], "successor_authorization_mismatch")
        self.assertEqual(state["scheduled_task_disposition"], "PAUSED")

    def test_begin_wake_persists_malformed_next_not_before(self) -> None:
        state = empty_checkpoint("Owner/Repo", 17)
        state["scheduled_task_disposition"] = "ACTIVE"
        state["scheduled_task_id"] = "task-1"
        state["next_not_before"] = "not-a-timestamp"

        state, result = pulse.begin_wake(
            state,
            wake_id="wake-1",
            now="2026-08-26T00:11:00+00:00",
            pause_heartbeat=lambda: False,
            delivered_task_id="task-1",
        )

        self.assertEqual(result["next_action"], "PAUSE_RECOVERY")
        self.assertEqual(result["reason_code"], "checkpoint_invalid")
        self.assertEqual(state["failure_latch"]["reason_code"], "checkpoint_invalid")
        self.assertEqual(state["scheduled_task_disposition"], "PAUSED")

    def test_reanchored_schedule_requires_a_persisted_creation_anchor(self) -> None:
        state, _ = started()
        state, _ = pulse.record_snapshot(state, snapshot(), wake_id="wake-1", now=NOW)
        callback_calls: list[str] = []
        state, result = pulse.complete_wake(
            state,
            wake_id="wake-1",
            now="2026-08-26T00:26:00+00:00",
            schedule_next_wake=lambda expected: callback_calls.append(expected) or expected,
            scheduled_task_id="task-1",
            require_schedule_anchor=True,
        )

        self.assertEqual(result["next_action"], "PAUSE_RECOVERY")
        self.assertEqual(result["reason_code"], "scheduled_task_anchor_missing")
        self.assertEqual(
            result["evidence"],
            {
                "wake_completed_at": "2026-08-26T00:26:00+00:00",
                "scheduled_task_created_at": None,
                "scheduled_task_id": "task-1",
            },
        )
        self.assertEqual(callback_calls, [])
        self.assertEqual(state["scheduled_task_disposition"], "PAUSED")

    def test_creation_anchored_schedule_rejects_a_precompletion_anchor(self) -> None:
        state, _ = started()
        state, _ = pulse.record_snapshot(state, snapshot(), wake_id="wake-1", now=NOW)
        state, result = pulse.complete_wake(
            state,
            wake_id="wake-1",
            now="2026-08-26T00:26:00+00:00",
            schedule_next_wake=lambda _: "2026-08-26T00:35:59+00:00",
            schedule_anchor_created_at="2026-08-26T00:25:59+00:00",
            scheduled_task_id="task-1",
        )

        self.assertEqual(result["next_action"], "PAUSE_BLOCKED")
        self.assertEqual(result["reason_code"], "scheduled_task_anchor_mismatch")
        self.assertEqual(state["scheduled_task_disposition"], "PAUSED")

    def test_creation_anchored_schedule_accepts_same_scheduler_second_as_completion(self) -> None:
        state, _ = started()
        state, _ = pulse.record_snapshot(state, snapshot(), wake_id="wake-1", now=NOW)
        state, result = pulse.complete_wake(
            state,
            wake_id="wake-1",
            now="2026-08-26T00:26:00.500000+00:00",
            schedule_next_wake=lambda _: "2026-08-26T00:36:00+00:00",
            schedule_anchor_created_at="2026-08-26T00:26:00+00:00",
            scheduled_task_id="task-1",
        )

        self.assertEqual(result["next_action"], "WAIT_REVIEW")
        self.assertEqual(result["next_not_before"], "2026-08-26T00:36:00+00:00")
        self.assertEqual(state["scheduled_task_disposition"], "ACTIVE")

    def test_creation_anchored_schedule_accepts_truncated_scheduler_first_run(self) -> None:
        state, _ = started()
        state, _ = pulse.record_snapshot(state, snapshot(), wake_id="wake-1", now=NOW)
        state, result = pulse.complete_wake(
            state,
            wake_id="wake-1",
            now="2026-09-02T08:46:13.761000+00:00",
            schedule_next_wake=lambda _: "2026-09-02T08:56:13+00:00",
            schedule_anchor_created_at="2026-09-02T08:46:13.793000+00:00",
            scheduled_task_id="task-1",
        )

        self.assertEqual(result["next_action"], "WAIT_REVIEW")
        self.assertEqual(result["next_not_before"], "2026-09-02T08:56:13+00:00")
        self.assertEqual(
            result["scheduled_task_created_at"],
            "2026-09-02T08:46:13.793000+00:00",
        )
        self.assertEqual(state["scheduled_task_disposition"], "ACTIVE")

    def test_creation_anchored_deadline_uses_persisted_first_run(self) -> None:
        state, _ = started()
        state, _ = pulse.record_snapshot(state, snapshot(), wake_id="wake-1", now=NOW)
        state, result = pulse.complete_wake(
            state,
            wake_id="wake-1",
            now="2026-09-02T08:46:13.999000+00:00",
            schedule_next_wake=lambda _: "2026-09-02T08:56:13+00:00",
            schedule_anchor_created_at="2026-09-02T08:46:13.100000+00:00",
            scheduled_task_id="task-1",
        )

        self.assertEqual(result["next_action"], "WAIT_REVIEW")
        self.assertEqual(result["next_not_before"], "2026-09-02T08:56:13+00:00")
        self.assertEqual(state["next_not_before"], "2026-09-02T08:56:13+00:00")

    def test_public_anchored_completion_requires_successor_authorization(self) -> None:
        state, _ = started()
        state, _ = pulse.record_snapshot(state, snapshot(), wake_id="wake-1", now=NOW)
        state, result = pulse.complete_wake(
            state,
            wake_id="wake-1",
            now="2026-08-26T00:26:00+00:00",
            schedule_next_wake=lambda _: "2026-08-26T00:36:00+00:00",
            schedule_anchor_created_at="2026-08-26T00:26:00+00:00",
            scheduled_task_id="task-1",
            require_schedule_anchor=True,
        )

        self.assertEqual(result["next_action"], "PAUSE_RECOVERY")
        self.assertEqual(result["reason_code"], "successor_authorization_required")
        self.assertEqual(state["scheduled_task_disposition"], "PAUSED")

    def test_creation_anchored_schedule_rejects_genuinely_early_first_run(self) -> None:
        state, _ = started()
        state, _ = pulse.record_snapshot(state, snapshot(), wake_id="wake-1", now=NOW)
        state, result = pulse.complete_wake(
            state,
            wake_id="wake-1",
            now="2026-09-02T08:46:13.761000+00:00",
            schedule_next_wake=lambda _: "2026-09-02T08:56:12+00:00",
            schedule_anchor_created_at="2026-09-02T08:46:13.793000+00:00",
            scheduled_task_id="task-1",
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
        self.assertEqual(state["scheduled_task_disposition"], "PAUSED")

    def test_creation_anchored_schedule_keeps_one_second_late_tolerance(self) -> None:
        for observed, accepted in (
            ("2026-09-02T08:56:14+00:00", True),
            ("2026-09-02T08:56:15+00:00", False),
        ):
            with self.subTest(observed=observed):
                state, _ = started()
                state, _ = pulse.record_snapshot(
                    state, snapshot(), wake_id="wake-1", now=NOW
                )
                state, result = pulse.complete_wake(
                    state,
                    wake_id="wake-1",
                    now="2026-09-02T08:46:13.761000+00:00",
                    schedule_next_wake=lambda _: observed,
                    schedule_anchor_created_at="2026-09-02T08:46:13.793000+00:00",
                    scheduled_task_id="task-1",
                )

                self.assertEqual(
                    result["next_action"],
                    "WAIT_REVIEW" if accepted else "PAUSE_BLOCKED",
                )

    def test_active_schedule_requires_a_delivered_task_id(self) -> None:
        state, _ = started()
        state, _ = pulse.record_snapshot(
            state, snapshot(), wake_id="wake-1", now=NOW
        )
        state, _ = pulse.complete_wake(
            state,
            wake_id="wake-1",
            now="2026-08-26T00:01:00+00:00",
            schedule_next_wake=lambda expected: expected,
            scheduled_task_id="task-1",
        )

        state, result = pulse.begin_wake(
            state,
            wake_id="wake-2",
            now="2026-08-26T00:11:00+00:00",
            pause_heartbeat=lambda: True,
        )

        self.assertEqual(result["next_action"], "PAUSE_RECOVERY")
        self.assertEqual(result["reason_code"], "scheduled_task_identity_mismatch")
        self.assertEqual(result["evidence"]["delivered_task_id"], None)
        self.assertEqual(state["failure_latch"]["reason_code"], "scheduled_task_identity_mismatch")

    def test_schedule_reanchor_tolerance_is_direction_aware(self) -> None:
        expected = "2026-08-26T00:36:00+00:00"
        self.assertTrue(
            pulse._schedule_times_match(
                expected,
                "2026-08-26T00:36:01+00:00",
                ordered=True,
            )
        )
        self.assertFalse(
            pulse._schedule_times_match(
                expected,
                "2026-08-26T00:35:59.999999+00:00",
                ordered=True,
            )
        )
        self.assertFalse(
            pulse._schedule_times_match(
                expected,
                "2026-08-26T00:36:01.000001+00:00",
                ordered=True,
            )
        )
        self.assertTrue(
            pulse._schedule_times_match(
                expected,
                "2026-08-26T00:35:59+00:00",
                ordered=False,
            )
        )
        self.assertTrue(
            pulse._schedule_times_match(
                expected,
                "2026-08-26T00:36:01+00:00",
                ordered=False,
            )
        )
        self.assertFalse(
            pulse._schedule_times_match(
                expected,
                "2026-08-26T00:35:58.999999+00:00",
                ordered=False,
            )
        )
        self.assertFalse(
            pulse._schedule_times_match(
                expected,
                "2026-08-26T00:36:01.000001+00:00",
                ordered=False,
            )
        )

    def test_completion_rounds_up_and_accepts_one_second_late_reanchor(self) -> None:
        state, _ = started()
        state, _ = pulse.record_snapshot(state, snapshot(), wake_id="wake-1", now=NOW)
        state, result = pulse.complete_wake(
            state,
            wake_id="wake-1",
            now="2026-08-26T00:26:00.250000+00:00",
            schedule_next_wake=lambda expected: "2026-08-26T00:36:02+00:00",
            scheduled_task_id="task-1",
        )
        self.assertEqual(result["next_action"], "WAIT_REVIEW")
        self.assertEqual(result["next_not_before"], "2026-08-26T00:36:01+00:00")
        self.assertEqual(state["scheduled_task_disposition"], "ACTIVE")

    def test_fixed_cadence_wakes_before_completion_boundary_are_absorbed(self) -> None:
        state, _ = started()
        state, _ = pulse.record_snapshot(state, snapshot(), wake_id="wake-1", now=NOW)
        state, _ = pulse.complete_wake(
            state,
            wake_id="wake-1",
            now="2026-08-26T00:26:00+00:00",
            schedule_next_wake=lambda expected: expected,
            scheduled_task_id="task-1",
        )
        for index, when in enumerate(("00:10:00", "00:20:00", "00:30:00"), start=2):
            candidate = deepcopy(state)
            candidate, result = pulse.begin_wake(
                candidate,
                wake_id=f"wake-{index}",
                now=f"2026-08-26T{when}+00:00",
                pause_heartbeat=lambda: True,
                delivered_task_id="task-1",
            )
            self.assertEqual(result["next_action"], "PAUSE_BLOCKED")
            self.assertEqual(result["reason_code"], "cadence_not_elapsed")
            self.assertEqual(candidate["wake_count"], 1)

    def test_pause_is_absorbing_and_recovery_id_is_not_a_default_operation(self) -> None:
        state, _ = started()
        state, result = pulse.record_snapshot(
            state,
            snapshot(stable=False),
            wake_id="wake-1",
            now=NOW,
        )
        self.assertEqual(result["next_action"], "PAUSE_BLOCKED")
        self.assertEqual(state["scheduled_task_disposition"], "PAUSED")
        with self.assertRaises(pulse.DefaultWakeError):
            pulse.freeze_default_batch(state, wake_id="wake-1")
        with self.assertRaises(pulse.DefaultWakeError):
            pulse.record_default_outcome(
                state,
                wake_id="wake-1",
                thread_id="T1",
                classification="no-fix",
                now=NOW,
            )
        self.assertNotIn("recovery_authorization_id", state)

    def test_eyes_wait_without_freezing_partial_threads(self) -> None:
        state, _ = started()
        state, result = pulse.record_snapshot(
            state,
            snapshot(targeted=["T1"], eyes=True),
            wake_id="wake-1",
            now=NOW,
        )
        self.assertEqual(result["next_action"], "WAIT_REVIEW")
        with self.assertRaises(pulse.DefaultWakeError):
            pulse.freeze_default_batch(state, wake_id="wake-1")

    def test_eyes_disappear_with_targets_runs_and_without_targets_reaches_trigger_boundary(self) -> None:
        state, _ = started()
        state, _ = pulse.record_snapshot(
            state, snapshot(targeted=["T1"], eyes=True), wake_id="wake-1", now=NOW
        )
        # A new wake is needed after the WAIT boundary.
        state, _ = pulse.complete_wake(
            state,
            wake_id="wake-1",
            now="2026-08-26T00:01:00+00:00",
            schedule_next_wake=lambda expected: expected,
            scheduled_task_id="task-1",
        )
        state, _ = started(state, wake_id="wake-2", now="2026-08-26T00:11:00+00:00")
        state, result = pulse.record_snapshot(
            state, snapshot(targeted=["T1"], eyes=False), wake_id="wake-2", now="2026-08-26T00:11:00+00:00"
        )
        self.assertEqual(result["next_action"], "RUN_BATCH")

        clean = empty_checkpoint("Owner/Repo", 17)
        clean, _ = started(clean)
        clean, _ = pulse.record_snapshot(clean, snapshot(eyes=True), wake_id="wake-1", now=NOW)
        clean, _ = pulse.complete_wake(clean, wake_id="wake-1", now="2026-08-26T00:01:00+00:00", schedule_next_wake=lambda expected: expected, scheduled_task_id="task-1")
        clean, _ = started(clean, wake_id="wake-2", now="2026-08-26T00:11:00+00:00")
        clean, result = pulse.record_snapshot(clean, snapshot(), wake_id="wake-2", now="2026-08-26T00:11:00+00:00")
        self.assertEqual(result["next_action"], "WAIT_REVIEW")
        clean, _ = pulse.complete_wake(
            clean,
            wake_id="wake-2",
            now="2026-08-26T00:12:00+00:00",
            schedule_next_wake=lambda expected: expected,
            scheduled_task_id="task-2",
        )
        clean, _ = started(clean, wake_id="wake-3", now="2026-08-26T00:22:00+00:00")
        clean, result = pulse.record_snapshot(
            clean, snapshot(), wake_id="wake-3", now="2026-08-26T00:22:00+00:00"
        )
        self.assertEqual(result["next_action"], "REQUEST_REVIEW")

    def test_current_head_approval_and_historical_approval_are_distinct(self) -> None:
        approved, _ = started()
        approved, result = pulse.record_snapshot(
            approved,
            snapshot(approval="approved_current_head"),
            wake_id="wake-1",
            now=NOW,
        )
        self.assertEqual(result["next_action"], "STOP_TERMINAL")

        ambiguous, _ = started()
        ambiguous, result = pulse.record_snapshot(
            ambiguous,
            snapshot(approval="ambiguous_existing_reaction"),
            wake_id="wake-1",
            now=NOW,
        )
        self.assertEqual(result["next_action"], "WAIT_REVIEW")

    def test_stop_terminal_rejects_a_new_wake(self) -> None:
        state, _ = started()
        state, result = pulse.record_snapshot(
            state,
            snapshot(approval="approved_current_head"),
            wake_id="wake-1",
            now=NOW,
        )
        self.assertEqual(result["next_action"], "STOP_TERMINAL")
        before = deepcopy(state)

        with self.assertRaisesRegex(pulse.DefaultWakeError, "absorbing stop"):
            pulse.begin_wake(
                state,
                wake_id="wake-2",
                now="2026-08-26T00:01:00+00:00",
                pause_heartbeat=lambda: True,
            )

        self.assertEqual(state, before)

    def test_stop_closed_rejects_a_new_wake(self) -> None:
        state, _ = started()
        closed_snapshot = snapshot()
        closed_snapshot["pull_request_state"] = "CLOSED"
        state, result = pulse.record_snapshot(
            state,
            closed_snapshot,
            wake_id="wake-1",
            now=NOW,
        )
        self.assertEqual(result["next_action"], "STOP_CLOSED")
        before = deepcopy(state)

        with self.assertRaisesRegex(pulse.DefaultWakeError, "absorbing stop"):
            pulse.begin_wake(
                state,
                wake_id="wake-2",
                now="2026-08-26T00:01:00+00:00",
                pause_heartbeat=lambda: True,
            )

        self.assertEqual(state, before)

    def test_no_fix_batch_can_publish_without_empty_commit(self) -> None:
        state, _ = started()
        state, _ = pulse.record_snapshot(
            state, snapshot(targeted=["T1"]), wake_id="wake-1", now=NOW
        )
        state, _ = pulse.freeze_default_batch(state, wake_id="wake-1")
        state, _ = pulse.record_default_outcome(
            state,
            wake_id="wake-1",
            thread_id="T1",
            classification="no-fix",
            reference="false positive",
            now=NOW,
        )
        state = record_resolved_thread(state, "T1")
        state, prepared = pulse.prepare_default_publication(
            state,
            wake_id="wake-1",
            now=NOW,
            actual_head_oid="HEAD1",
        )
        self.assertEqual(prepared["next_action"], "PUBLISH_BATCH")
        state, prepared = pulse.prepare_default_publication(
            state,
            wake_id="wake-1",
            now=NOW,
            actual_head_oid="HEAD1",
        )
        self.assertEqual(prepared["preparation_count"], 2)
        state, result = pulse.record_publication_result(
            state,
            wake_id="wake-1",
            status="succeeded",
            now=NOW,
            published_commit=None,
        )
        self.assertIsNone(result["published_commit"])
        self.assertEqual((state["active_batch"]["publication"]["status"]), "succeeded")

    def test_completed_no_fix_batch_preserves_thread_resolution_mutation(self) -> None:
        state, _ = started()
        state, _ = pulse.record_snapshot(
            state, snapshot(targeted=["T1"]), wake_id="wake-1", now=NOW
        )
        state, _ = pulse.freeze_default_batch(state, wake_id="wake-1")
        state, _ = pulse.record_default_outcome(
            state,
            wake_id="wake-1",
            thread_id="T1",
            classification="no-fix",
            reference="false positive",
            now=NOW,
        )

        def graphql_call(query: str, variables: dict[str, object]) -> dict[str, object]:
            if "resolveReviewThread" in query:
                return {
                    "data": {
                        "resolveReviewThread": {
                            "thread": {"id": variables["threadId"], "isResolved": True}
                        }
                    }
                }
            return {
                "data": {
                    "repository": {
                        "nameWithOwner": "owner/repo",
                        "pullRequest": {
                            "number": 17,
                            "headRefOid": "HEAD1",
                            "reviewThreads": {
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                                "nodes": [
                                    {
                                        "id": "T1",
                                        "isResolved": False,
                                        "comments": {
                                            "nodes": [
                                                {"author": {"login": "chatgpt-codex-connector"}}
                                            ]
                                        },
                                    }
                                ],
                            },
                        },
                    }
                }
            }

        state, resolved = pulse.resolve_default_thread(
            state,
            wake_id="wake-1",
            thread_id="T1",
            graphql_call=graphql_call,
        )
        self.assertTrue(resolved["mutation_occurred"])
        state, _ = pulse.prepare_default_publication(
            state, wake_id="wake-1", now=NOW, actual_head_oid="HEAD1"
        )
        state, _ = pulse.prepare_default_publication(
            state, wake_id="wake-1", now=NOW, actual_head_oid="HEAD1"
        )
        state, publication = pulse.record_publication_result(
            state,
            wake_id="wake-1",
            status="succeeded",
            now=NOW,
            published_commit=None,
        )
        self.assertTrue(publication["mutation_occurred"])

        state, completed = pulse.complete_wake(
            state,
            wake_id="wake-1",
            now="2026-08-26T00:01:00+00:00",
            schedule_next_wake=lambda expected: expected,
            scheduled_task_id="task-1",
        )

        self.assertTrue(completed["mutation_occurred"])
        self.assertTrue(state["last_wake_result"]["mutation_occurred"])

    def test_completed_publication_preserves_mutation_audit_flag(self) -> None:
        state, _ = started()
        state, _ = pulse.record_snapshot(
            state, snapshot(targeted=["T1"]), wake_id="wake-1", now=NOW
        )
        state, _ = pulse.freeze_default_batch(state, wake_id="wake-1")
        state, _ = pulse.record_default_outcome(
            state,
            wake_id="wake-1",
            thread_id="T1",
            classification="fix-now",
            reference="src/example.py",
            now=NOW,
        )
        state = record_resolved_thread(state, "T1")
        state, _ = pulse.prepare_default_publication(
            state,
            wake_id="wake-1",
            now=NOW,
            actual_head_oid="HEAD1",
        )
        state, _ = pulse.prepare_default_publication(
            state,
            wake_id="wake-1",
            now=NOW,
            actual_head_oid="HEAD1",
        )
        state, publication = pulse.record_publication_result(
            state,
            wake_id="wake-1",
            status="succeeded",
            now=NOW,
            published_commit="abc1234",
        )
        self.assertTrue(publication["mutation_occurred"])

        state, completed = pulse.complete_wake(
            state,
            wake_id="wake-1",
            now="2026-08-26T00:01:00+00:00",
            schedule_next_wake=lambda expected: expected,
            scheduled_task_id="task-1",
        )

        self.assertTrue(completed["mutation_occurred"])
        self.assertTrue(state["last_wake_result"]["mutation_occurred"])

    def test_publication_before_exact_resolution_pauses_without_authority(self) -> None:
        state, _ = started()
        state, _ = pulse.record_snapshot(
            state, snapshot(targeted=["T1"]), wake_id="wake-1", now=NOW
        )
        state, _ = pulse.freeze_default_batch(state, wake_id="wake-1")
        state, _ = pulse.record_default_outcome(
            state,
            wake_id="wake-1",
            thread_id="T1",
            classification="fix-now",
            reference="src/example.py",
            now=NOW,
        )

        state, result = pulse.prepare_default_publication(
            state,
            wake_id="wake-1",
            now=NOW,
            actual_head_oid="HEAD1",
        )

        self.assertEqual(result["next_action"], "PAUSE_RECOVERY")
        self.assertEqual(result["reason_code"], "publication_not_ready")
        self.assertEqual(result["evidence"]["unresolved_thread_ids"], ["T1"])
        self.assertEqual(state["scheduled_task_disposition"], "PAUSED")

    def test_publication_requires_a_same_head_preparation_gate(self) -> None:
        state, _ = started()
        state, _ = pulse.record_snapshot(
            state, snapshot(targeted=["T1"]), wake_id="wake-1", now=NOW
        )
        state, _ = pulse.freeze_default_batch(state, wake_id="wake-1")
        state, _ = pulse.record_default_outcome(
            state,
            wake_id="wake-1",
            thread_id="T1",
            classification="no-fix",
            now=NOW,
        )
        state = record_resolved_thread(state, "T1")

        state, result = pulse.prepare_default_publication(
            state,
            wake_id="wake-1",
            now=NOW,
            actual_head_oid="HEAD2",
        )

        self.assertEqual(result["next_action"], "PAUSE_RECOVERY")
        self.assertEqual(result["reason_code"], "publication_head_changed")
        self.assertEqual(result["evidence"]["frozen_head_oid"], "HEAD1")
        self.assertEqual(result["evidence"]["actual_head_oid"], "HEAD2")

    def test_publication_result_requires_both_preparations(self) -> None:
        state, _ = started()
        state, _ = pulse.record_snapshot(
            state, snapshot(targeted=["T1"]), wake_id="wake-1", now=NOW
        )
        state, _ = pulse.freeze_default_batch(state, wake_id="wake-1")
        state, _ = pulse.record_default_outcome(
            state,
            wake_id="wake-1",
            thread_id="T1",
            classification="fix-now",
            now=NOW,
        )
        state = record_resolved_thread(state, "T1")
        state, prepared = pulse.prepare_default_publication(
            state,
            wake_id="wake-1",
            now=NOW,
            actual_head_oid="HEAD1",
        )
        self.assertEqual(prepared["preparation_count"], 1)

        state, result = pulse.record_publication_result(
            state,
            wake_id="wake-1",
            status="succeeded",
            now=NOW,
            published_commit="NEW_HEAD",
        )

        self.assertEqual(result["next_action"], "PAUSE_RECOVERY")
        self.assertEqual(result["reason_code"], "publication_not_prepared")
        self.assertEqual(result["evidence"]["published_commit"], "NEW_HEAD")
        self.assertNotEqual(state["active_batch"]["publication"]["status"], "succeeded")

    def test_trigger_is_once_per_head_and_empty_followup_pauses(self) -> None:
        state, _ = started()
        state, _ = pulse.record_snapshot(state, snapshot(), wake_id="wake-1", now=NOW)
        state, _ = pulse.complete_wake(state, wake_id="wake-1", now="2026-08-26T00:01:00+00:00", schedule_next_wake=lambda expected: expected, scheduled_task_id="task-1")
        state, _ = started(state, wake_id="wake-2", now="2026-08-26T00:11:00+00:00")
        state, result = pulse.record_snapshot(state, snapshot(), wake_id="wake-2", now="2026-08-26T00:11:00+00:00")
        self.assertEqual(result["next_action"], "REQUEST_REVIEW")
        state, result = pulse.record_default_trigger(
            state,
            wake_id="wake-2",
            evidence={
                "attempted_head_oid": "HEAD1",
                "head_before": "HEAD1",
                "head_after": "HEAD1",
                "comment_node_id": "COMMENT1",
                "created_at": "2026-08-26T00:11:00+00:00",
            },
        )
        self.assertEqual(result["reason_code"], "review_trigger_recorded")
        self.assertTrue(result["mutation_occurred"])
        state, completed = pulse.complete_wake(state, wake_id="wake-2", now="2026-08-26T00:12:00+00:00", schedule_next_wake=lambda expected: expected, scheduled_task_id="task-2")
        self.assertTrue(completed["mutation_occurred"])
        state, _ = started(state, wake_id="wake-3", now="2026-08-26T00:22:00+00:00")
        state, result = pulse.record_snapshot(state, snapshot(), wake_id="wake-3", now="2026-08-26T00:22:00+00:00")
        self.assertEqual(result["next_action"], "PAUSE_BLOCKED")
        self.assertEqual(result["reason_code"], "review_trigger_did_not_start")


if __name__ == "__main__":
    unittest.main()
