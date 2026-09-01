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
    return pulse.begin_wake(
        state,
        wake_id=wake_id,
        now=now,
        pause_heartbeat=lambda: True,
    )


class DefaultLifecycleTests(unittest.TestCase):
    def test_heartbeat_handoff_is_target_bound_and_orders_publication(self) -> None:
        handoff = pulse.build_heartbeat_handoff("Owner/Repo", 17)

        self.assertEqual(handoff["repository"], "owner/repo")
        self.assertEqual(handoff["pull_request_number"], 17)
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

    def test_schema_one_checkpoint_migrates_to_policy_schema(self) -> None:
        legacy = empty_checkpoint("Owner/Repo", 17)
        legacy["default_mode_schema_version"] = 1
        migrated = pulse.ensure_default_lifecycle(legacy)
        self.assertEqual(migrated["default_mode_schema_version"], 2)
        self.assertEqual(migrated["automation_policy"]["profile"], "autonomous")
        self.assertIsNone(migrated["automation_policy"]["max_wakes"])
        self.assertEqual(migrated["retry_state"]["wake_attempts"], 0)

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
        )
        state, result = pulse.begin_wake(
            state,
            wake_id="wake-2",
            now="2026-08-26T00:11:00+00:00",
            pause_heartbeat=lambda: True,
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
        )
        self.assertEqual(result["next_action"], "WAIT_RETRY")
        state, _ = pulse.complete_wake(
            state,
            wake_id="wake-1",
            now="2026-08-26T00:01:00+00:00",
            schedule_next_wake=lambda expected: expected,
        )
        self.assertEqual(state["wake_phase"], "retry_waiting")
        state, result = started(
            state, wake_id="wake-2", now="2026-08-26T00:11:00+00:00"
        )
        self.assertEqual(result["next_action"], "WAKE_STARTED")
        self.assertTrue(result["resume_pending_batch"])
        self.assertEqual(state["last_decision"]["reason_code"], "resume_pending_batch")

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
        )
        self.assertEqual(completed["next_action"], "WAIT_RETRY")

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
        )
        self.assertEqual(result["next_not_before"], "2026-08-26T00:36:00+00:00")
        self.assertEqual(state["scheduled_task_disposition"], "ACTIVE")

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
        )
        for index, when in enumerate(("00:10:00", "00:20:00", "00:30:00"), start=2):
            candidate = deepcopy(state)
            candidate, result = pulse.begin_wake(
                candidate,
                wake_id=f"wake-{index}",
                now=f"2026-08-26T{when}+00:00",
                pause_heartbeat=lambda: True,
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
        )
        state, _ = started(state, wake_id="wake-2", now="2026-08-26T00:11:00+00:00")
        state, result = pulse.record_snapshot(
            state, snapshot(targeted=["T1"], eyes=False), wake_id="wake-2", now="2026-08-26T00:11:00+00:00"
        )
        self.assertEqual(result["next_action"], "RUN_BATCH")

        clean = empty_checkpoint("Owner/Repo", 17)
        clean, _ = started(clean)
        clean, _ = pulse.record_snapshot(clean, snapshot(eyes=True), wake_id="wake-1", now=NOW)
        clean, _ = pulse.complete_wake(clean, wake_id="wake-1", now="2026-08-26T00:01:00+00:00", schedule_next_wake=lambda expected: expected)
        clean, _ = started(clean, wake_id="wake-2", now="2026-08-26T00:11:00+00:00")
        clean, result = pulse.record_snapshot(clean, snapshot(), wake_id="wake-2", now="2026-08-26T00:11:00+00:00")
        self.assertEqual(result["next_action"], "WAIT_REVIEW")
        clean, _ = pulse.complete_wake(
            clean,
            wake_id="wake-2",
            now="2026-08-26T00:12:00+00:00",
            schedule_next_wake=lambda expected: expected,
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
        state, _ = pulse.complete_wake(state, wake_id="wake-1", now="2026-08-26T00:01:00+00:00", schedule_next_wake=lambda expected: expected)
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
        state, completed = pulse.complete_wake(state, wake_id="wake-2", now="2026-08-26T00:12:00+00:00", schedule_next_wake=lambda expected: expected)
        self.assertTrue(completed["mutation_occurred"])
        state, _ = started(state, wake_id="wake-3", now="2026-08-26T00:22:00+00:00")
        state, result = pulse.record_snapshot(state, snapshot(), wake_id="wake-3", now="2026-08-26T00:22:00+00:00")
        self.assertEqual(result["next_action"], "PAUSE_BLOCKED")
        self.assertEqual(result["reason_code"], "review_trigger_did_not_start")


if __name__ == "__main__":
    unittest.main()
