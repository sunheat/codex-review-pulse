from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "codex-review-pulse" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from recurring_model import (  # noqa: E402
    advance_observation_state,
    empty_run_state,
    evaluate_recurring_action,
    latch_failure,
    record_trigger_result,
)


def contract(**overrides: object) -> dict:
    value = {
        "schema_version": 1,
        "repository": "owner/repo",
        "pull_request_number": 17,
        "reviewer_logins": ["chatgpt-codex-connector"],
        "approval_logins": ["chatgpt-codex-connector"],
        "expected_installation": {
            "version": "0.3.1",
            "source_commit": "a" * 40,
            "skill_path": str((ROOT / "installed" / "codex-review-pulse").resolve()),
        },
        "authorization_id": "auth-1",
        "runner_identity": "operator-a",
        "automation_identity": "scheduled-task-a",
        "maximum_wakes": 5,
        "expires_at": "2026-08-26T00:00:00+00:00",
        "connector_capability": "unknown",
        "wait_policy": {
            "minimum_server_wait_seconds": 600,
            "minimum_stable_observations": 2,
        },
        "mutation_scope": {
            "recurring_execution": True,
            "code_edits": True,
            "resolve_threads": True,
            "commit": True,
            "push": True,
            "review_trigger": False,
            "issue_creation": False,
            "merge": False,
            "auto_merge": False,
            "base_change": False,
            "force_push": False,
            "generic_reviewer_handling": False,
            "non_target_thread_resolution": False,
        },
        "review_trigger_head_oid": None,
        "paths": {
            name: str((ROOT / "runtime" / f"{name}.json").resolve())
            for name in ("checkpoint", "lease", "run_state")
        },
    }
    value.update(overrides)
    return value


def observation(**overrides: object) -> dict:
    value = {
        "snapshot_stable": True,
        "mixed_head": False,
        "auth_ok": True,
        "api_ok": True,
        "run_contract_ok": True,
        "install_ok": True,
        "local_checkout_ok": True,
        "lease_status": "owned",
        "recovery_status": "none",
        "pull_request_state": "OPEN",
        "head_oid": "HEAD1",
        "targeted_thread_ids": [],
        "non_target_thread_ids": [],
        "approval_status": "awaiting_current_head_approval",
        "approval_evidence_ids": [],
        "server_time": "2026-08-25T00:20:00+00:00",
        "batch_publication_event": {
            "head_oid": "HEAD1",
            "created_at": "2026-08-25T00:00:00+00:00",
        },
        "relevant_codex_events": [],
    }
    value.update(overrides)
    return value


def evaluated(contract_value: dict, observation_value: dict, state: dict) -> dict:
    return evaluate_recurring_action(
        contract=contract_value,
        observation=observation_value,
        state=state,
        now="2026-08-25T00:20:00+00:00",
    )


class RecurringModelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.contract = contract()
        self.state = empty_run_state(self.contract)

    def test_identical_snapshot_waits_for_review(self) -> None:
        current = advance_observation_state(self.state, observation())
        current = advance_observation_state(current, observation())
        result = evaluated(self.contract, observation(), current)
        self.assertEqual(result["next_action"], "WAIT_REVIEW")
        self.assertEqual(result["reason_code"], "connector_capability_unknown")

    def test_new_targeted_thread_runs_one_batch(self) -> None:
        result = evaluated(
            self.contract, observation(targeted_thread_ids=["T1"]), self.state
        )
        self.assertEqual(result["next_action"], "RUN_BATCH")

    def test_current_head_approval_stops_immediately(self) -> None:
        result = evaluated(
            self.contract,
            observation(approval_status="approved_current_head"),
            self.state,
        )
        self.assertEqual(result["next_action"], "STOP_TERMINAL")

    def test_closed_or_merged_pr_stops_separately(self) -> None:
        for state in ("CLOSED", "MERGED"):
            with self.subTest(state=state):
                result = evaluated(
                    self.contract,
                    observation(pull_request_state=state),
                    self.state,
                )
                self.assertEqual(result["next_action"], "STOP_CLOSED")

    def test_old_head_approval_is_non_terminal(self) -> None:
        result = evaluated(
            self.contract,
            observation(approval_status="old_head_approval"),
            self.state,
        )
        self.assertEqual(result["next_action"], "WAIT_REVIEW")

    def test_mixed_head_snapshot_pauses_and_latches(self) -> None:
        result = evaluated(
            self.contract, observation(snapshot_stable=False, mixed_head=True), self.state
        )
        self.assertEqual(result["next_action"], "PAUSE_BLOCKED")
        self.assertTrue(result["latch"])

    def test_external_head_advancement_pauses_recovery(self) -> None:
        result = evaluated(
            self.contract, observation(external_head_advance=True), self.state
        )
        self.assertEqual(result["next_action"], "PAUSE_RECOVERY")

    def test_unfinished_and_failed_batch_recovery_pause(self) -> None:
        for recovery in ("unfinished_frozen_batch", "publication_failed:push"):
            with self.subTest(recovery=recovery):
                result = evaluated(
                    self.contract, observation(recovery_status=recovery), self.state
                )
                self.assertEqual(result["next_action"], "PAUSE_RECOVERY")
                self.assertTrue(result["latch"])

    def test_auth_and_api_failure_pause_and_latch(self) -> None:
        for field in ("auth_ok", "api_ok"):
            with self.subTest(field=field):
                result = evaluated(
                    self.contract, observation(**{field: False}), self.state
                )
                self.assertEqual(result["next_action"], "PAUSE_BLOCKED")
                self.assertTrue(result["latch"])

    def test_failure_latch_survives_next_wake(self) -> None:
        state = latch_failure(
            self.state,
            reason_code="failed_push_unknown_remote_result",
            observed_at="2026-08-25T00:00:00+00:00",
        )
        result = evaluated(self.contract, observation(targeted_thread_ids=["T1"]), state)
        self.assertEqual(result["next_action"], "PAUSE_RECOVERY")

    def test_publication_and_local_recovery_codes_never_become_retry_authority(self) -> None:
        reasons = (
            "resolved_before_publication",
            "local_pending_commit",
            "unexpected_remote_head_advance",
            "failed_push_unknown_remote_result",
            "install_provenance_drift",
            "github_api_failed",
        )
        for reason in reasons:
            with self.subTest(reason=reason):
                state = latch_failure(
                    self.state,
                    reason_code=reason,
                    observed_at="2026-08-25T00:00:00+00:00",
                )
                result = evaluated(
                    self.contract,
                    observation(targeted_thread_ids=["T1"]),
                    state,
                )
                self.assertEqual(result["next_action"], "PAUSE_RECOVERY")
                self.assertEqual(result["failure_latch"]["reason_code"], reason)

    def test_lease_loss_pauses_concurrent_runner(self) -> None:
        result = evaluated(
            self.contract, observation(lease_status="lost"), self.state
        )
        self.assertEqual(result["next_action"], "PAUSE_CONCURRENT")

    def test_wake_budget_and_deadline_are_bounded(self) -> None:
        over_budget = deepcopy(self.state)
        over_budget["wake_count"] = self.contract["maximum_wakes"]
        self.assertEqual(
            evaluated(self.contract, observation(), over_budget)["next_action"],
            "PAUSE_EXPIRED",
        )
        self.assertEqual(
            evaluated(
                self.contract,
                observation(targeted_thread_ids=["T1"]),
                over_budget,
            )["next_action"],
            "RUN_BATCH",
        )
        expired_contract = contract(expires_at="2026-08-25T00:20:00+00:00")
        self.assertEqual(
            evaluated(expired_contract, observation(), self.state)["next_action"],
            "PAUSE_EXPIRED",
        )

    def test_unknown_connector_fails_closed_even_after_wait_policy(self) -> None:
        state = advance_observation_state(self.state, observation())
        state = advance_observation_state(state, observation())
        result = evaluated(self.contract, observation(), state)
        self.assertEqual(result["next_action"], "WAIT_REVIEW")
        self.assertEqual(result["reason_code"], "connector_capability_unknown")

    def test_server_time_and_stable_observations_are_both_required_for_trigger(self) -> None:
        authorized = contract(
            connector_capability="manual_trigger",
            review_trigger_head_oid="HEAD1",
            mutation_scope={**self.contract["mutation_scope"], "review_trigger": True},
        )
        one = advance_observation_state(self.state, observation())
        self.assertEqual(
            evaluated(authorized, observation(), one)["next_action"], "WAIT_REVIEW"
        )
        two = advance_observation_state(one, observation())
        self.assertEqual(
            evaluated(authorized, observation(), two)["next_action"], "REQUEST_REVIEW"
        )
        early = observation(server_time="2026-08-25T00:05:00+00:00")
        early_state = advance_observation_state(self.state, early)
        early_state = advance_observation_state(early_state, early)
        self.assertEqual(
            evaluated(authorized, early, early_state)["next_action"], "WAIT_REVIEW"
        )
        boundary = observation(server_time="2026-08-25T00:10:00+00:00")
        boundary_state = advance_observation_state(self.state, boundary)
        boundary_state = advance_observation_state(boundary_state, boundary)
        self.assertEqual(
            evaluated(authorized, boundary, boundary_state)["next_action"],
            "REQUEST_REVIEW",
        )
        event_observation = observation(
            relevant_codex_events=[
                {
                    "id": "REV1",
                    "head_oid": "HEAD1",
                    "created_at": "2026-08-25T00:05:00+00:00",
                }
            ]
        )
        event_state = advance_observation_state(self.state, event_observation)
        event_state = advance_observation_state(event_state, event_observation)
        event_result = evaluated(authorized, event_observation, event_state)
        self.assertEqual(event_result["next_action"], "WAIT_REVIEW")
        self.assertEqual(
            event_result["reason_code"], "relevant_codex_event_observed"
        )
        new_head = observation(
            head_oid="HEAD2",
            batch_publication_event={
                "head_oid": "HEAD2",
                "created_at": "2026-08-25T00:00:00+00:00",
            },
        )
        new_state = advance_observation_state(self.state, new_head)
        new_state = advance_observation_state(new_state, new_head)
        result = evaluated(authorized, new_head, new_state)
        self.assertEqual(result["next_action"], "PAUSE_BLOCKED")
        self.assertEqual(result["reason_code"], "review_trigger_head_not_authorized")

    def test_trigger_idempotency_survives_restart_and_is_head_scoped(self) -> None:
        state = record_trigger_result(
            self.state,
            attempted_head_oid="HEAD1",
            head_before="HEAD1",
            head_after="HEAD1",
            comment_node_id="IC_1",
            created_at="2026-08-25T00:00:00+00:00",
        )
        restarted = deepcopy(state)
        with self.assertRaisesRegex(ValueError, "already recorded"):
            record_trigger_result(
                restarted,
                attempted_head_oid="HEAD1",
                head_before="HEAD1",
                head_after="HEAD1",
                comment_node_id="IC_2",
                created_at="2026-08-25T00:01:00+00:00",
            )
        new_head = record_trigger_result(
            restarted,
            attempted_head_oid="HEAD2",
            head_before="HEAD2",
            head_after="HEAD2",
            comment_node_id="IC_3",
            created_at="2026-08-25T00:02:00+00:00",
        )
        self.assertEqual(set(new_head["trigger_events"]), {"HEAD1", "HEAD2"})

    def test_head_change_during_trigger_is_latched(self) -> None:
        state = record_trigger_result(
            self.state,
            attempted_head_oid="HEAD1",
            head_before="HEAD1",
            head_after="HEAD2",
            comment_node_id="IC_1",
            created_at="2026-08-25T00:00:00+00:00",
        )
        self.assertEqual(
            state["trigger_events"]["HEAD1"]["status"],
            "head_changed_during_trigger",
        )
        self.assertEqual(state["failure_latch"]["reason_code"], "trigger_head_changed")

    def test_non_target_only_threads_are_never_run_as_batch(self) -> None:
        result = evaluated(
            self.contract,
            observation(non_target_thread_ids=["HUMAN_THREAD"]),
            self.state,
        )
        self.assertEqual(result["next_action"], "WAIT_REVIEW")
        self.assertEqual(result["reason_code"], "non_target_only")


if __name__ == "__main__":
    unittest.main()
