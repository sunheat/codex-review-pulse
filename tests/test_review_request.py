from __future__ import annotations

from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "codex-review-pulse" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import campaign_model as m  # noqa: E402
import review_request  # noqa: E402


CODEX = "chatgpt-codex-connector"
H = "head-oid"
T0 = "2026-09-14T12:00:00Z"
T1 = "2026-09-14T12:00:05Z"
T2 = "2026-09-14T12:00:10Z"


def campaign() -> dict:
    return m.new_campaign(
        campaign_id="crp-20260914T120000Z-abc123",
        repository="owner/repo",
        pull_request_number=7,
        created_at=T0,
        max_rounds=6,
        model="a-model",
        reasoning_level="medium",
        interval_minutes=30,
        reviewer_logins=[CODEX],
        approval_logins=[CODEX],
    )


def committed() -> dict:
    """A campaign whose request round and allowance are already durably committed."""
    return m.reserve_request(campaign(), head_oid=H, reserved_at=T0, snapshot=snap(time=T0))


def snap(
    *,
    time: str,
    head: str = H,
    threads: list | None = None,
    reactions: list | None = None,
    reviews: list | None = None,
    comments: list | None = None,
    complete: bool = True,
    pr_state: str = "OPEN",
    node_id: str = "pr-node-1",
    viewer: str = "operator",
) -> dict:
    return {
        "complete": complete,
        "server_time": time,
        "pr_state": pr_state,
        "head_oid": head,
        "node_id": node_id,
        "viewer": viewer,
        "threads": threads or [],
        "reactions": reactions or [],
        "reviews": reviews or [],
        "comments": comments or [],
    }


def run(outcome_campaign: dict, *, observer, commenter, persist=lambda record: None) -> dict:
    """Drive the executor the way the CLI does, from the committed guard."""
    guard = review_request.committed_request_guard(outcome_campaign)
    return review_request.execute_request_attempt(
        campaign=outcome_campaign,
        head_oid=guard["head_oid"],
        reserved_at=guard["reserved_at"],
        baseline_comment_ids=set(guard.get("baseline", {}).get("comment_ids", [])),
        observer=observer,
        commenter=commenter,
        persist=persist,
    )


class CommittedGuardHandoffTests(unittest.TestCase):
    def test_exactly_one_committed_guard_is_required(self) -> None:
        with self.assertRaises(RuntimeError):
            review_request.committed_request_guard(campaign())
        with self.assertRaises(RuntimeError):
            review_request.committed_request_guard(
                m.reserve_request(committed(), head_oid="other-head", reserved_at=T0, snapshot=snap(time=T0, head="other-head"))
            )
        guard = review_request.committed_request_guard(committed())
        self.assertEqual(guard["head_oid"], H)
        self.assertEqual(guard["state"], m.RESERVED)

    def test_invalidated_guard_is_not_a_committed_request(self) -> None:
        used = m.invalidate_reserved_request(
            committed(), head_oid=H, at=T1, reason="head_changed"
        )
        with self.assertRaises(RuntimeError):
            review_request.committed_request_guard(used)


class RequestExecutionTests(unittest.TestCase):
    def test_execution_consumes_no_second_round_or_reservation(self) -> None:
        events: list[str] = []
        sequence = [snap(time=T1), snap(time=T2)]
        fixture = committed()
        original_reserve = review_request.model.reserve_request

        def failing_reserve(*args, **kwargs):
            events.append("reserved")
            raise AssertionError("the executor must not reserve again")

        def observer() -> dict:
            events.append("observed")
            return sequence.pop(0)

        def commenter(subject_id: str, body: str) -> dict:
            events.append("posted")
            return {"node_id": "c1", "created_at": T1, "url": "https://x/c1"}

        review_request.model.reserve_request = failing_reserve
        try:
            outcome = run(
                fixture,
                observer=observer,
                commenter=commenter,
                persist=lambda record: events.append("persisted"),
            )
        finally:
            review_request.model.reserve_request = original_reserve
        self.assertEqual(events, ["observed", "posted", "observed", "persisted"])
        self.assertEqual(outcome["outcome"], "window_open")
        guard = m.guard_for_head(outcome["campaign"], H)
        self.assertEqual(guard["state"], m.GUARD_ACTIVE)
        self.assertEqual(guard["request"]["node_id"], "c1")
        # Exactly the round consumed by the commitment, never a second one.
        self.assertEqual(outcome["campaign"]["rounds_used"], 1)

    def test_revalidation_invalidation_skips_post_and_keeps_round(self) -> None:
        posted = []
        sequence = [
            snap(
                time=T1,
                threads=[
                    {"id": "T1", "is_resolved": False, "root_login": CODEX},
                ],
            ),
        ]
        outcome = run(
            committed(),
            observer=lambda: sequence.pop(0),
            commenter=lambda *a: posted.append(a) or {},
        )
        self.assertEqual(outcome["outcome"], "invalidated")
        self.assertEqual(posted, [])
        self.assertEqual(outcome["campaign"]["rounds_used"], 1)
        self.assertEqual(outcome["campaign"]["status"], m.ACTIVE)
        self.assertFalse(outcome["retain_lock"])

    def test_head_change_at_revalidation_invalidates_without_post(self) -> None:
        outcome = run(
            committed(),
            observer=lambda: snap(time=T1, head="different"),
            commenter=lambda *a: (_ for _ in ()).throw(AssertionError("must not post")),
        )
        self.assertEqual(outcome["outcome"], "invalidated")
        self.assertEqual(
            m.guard_for_head(outcome["campaign"], H)["state"], m.INVALIDATED
        )

    def test_post_mutation_head_mismatch_terminates_manual_unbracketed(self) -> None:
        sequence = [snap(time=T1), snap(time=T2, head="changed")]
        outcome = run(
            committed(),
            observer=lambda: sequence.pop(0),
            commenter=lambda subject_id, body: {
                "node_id": "c1",
                "created_at": T1,
                "url": "https://x/c1",
            },
        )
        self.assertEqual(outcome["outcome"], "unbracketed")
        self.assertTrue(outcome["terminal"])
        self.assertFalse(outcome["retain_lock"])
        self.assertEqual(outcome["campaign"]["status"], m.MANUAL_INTERVENTION_REQUIRED)
        self.assertEqual(
            m.guard_for_head(outcome["campaign"], H)["state"], m.UNBRACKETED
        )

    def test_definitive_creation_failure_terminates_cleanly(self) -> None:
        sequence = [snap(time=T1), snap(time=T2)]

        def commenter(subject_id: str, body: str) -> dict:
            raise RuntimeError("422 nope")

        outcome = run(
            committed(),
            observer=lambda: sequence.pop(0),
            commenter=commenter,
        )
        self.assertEqual(outcome["outcome"], "creation_failed")
        self.assertTrue(outcome["terminal"])
        self.assertFalse(outcome["retain_lock"])
        self.assertEqual(outcome["campaign"]["status"], m.REQUEST_CREATION_FAILED)
        self.assertEqual(outcome["campaign"]["rounds_used"], 1)

    def test_unexpected_viewer_comment_after_failure_fails_closed(self) -> None:
        sequence = [
            snap(time=T1),
            snap(
                time=T2,
                comments=[
                    {
                        "id": "maybe-ours",
                        "login": "operator",
                        "created_at": T1,
                        "body": "@codex review",
                    }
                ],
            ),
        ]
        outcome = run(
            committed(),
            observer=lambda: sequence.pop(0),
            commenter=lambda *a: (_ for _ in ()).throw(RuntimeError("network gone")),
        )
        self.assertEqual(outcome["outcome"], "ambiguous")
        self.assertTrue(outcome["retain_lock"])
        self.assertEqual(outcome["campaign"]["status"], m.AMBIGUOUS_INTERRUPTION)

    def test_baseline_manual_comment_does_not_make_failure_ambiguous(self) -> None:
        manual = {
            "id": "manual-old",
            "login": "operator",
            "created_at": "2026-09-14T09:00:00Z",
            "body": "@codex review",
        }
        with_baseline = m.reserve_request(
            campaign(), head_oid=H, reserved_at=T0, snapshot=snap(time=T0, comments=[manual])
        )
        sequence = [snap(time=T1, comments=[manual]), snap(time=T2, comments=[manual])]
        outcome = run(
            with_baseline,
            observer=lambda: sequence.pop(0),
            commenter=lambda *a: (_ for _ in ()).throw(RuntimeError("403 forbidden")),
        )
        self.assertEqual(outcome["outcome"], "creation_failed")
        self.assertFalse(outcome["retain_lock"])

    def test_observation_failure_after_mutation_error_fails_closed(self) -> None:
        calls = iter([snap(time=T1)])

        def observer() -> dict:
            try:
                return next(calls)
            except StopIteration:
                raise RuntimeError("network down")

        outcome = run(
            committed(),
            observer=observer,
            commenter=lambda *a: (_ for _ in ()).throw(RuntimeError("network gone")),
        )
        self.assertEqual(outcome["outcome"], "ambiguous")
        self.assertTrue(outcome["retain_lock"])
        self.assertEqual(outcome["campaign"]["status"], m.AMBIGUOUS_INTERRUPTION)

    def test_incomplete_post_evidence_fails_closed(self) -> None:
        sequence = [snap(time=T1), snap(time=T2, complete=False)]
        outcome = run(
            committed(),
            observer=lambda: sequence.pop(0),
            commenter=lambda *a: (_ for _ in ()).throw(RuntimeError("500")),
        )
        self.assertEqual(outcome["outcome"], "ambiguous")
        self.assertTrue(outcome["retain_lock"])

    def test_ambiguity_stamps_use_the_committed_reservation_time(self) -> None:
        sequence = [snap(time=T1)]
        outcome = run(
            committed(),
            observer=lambda: sequence.pop(0),
            commenter=lambda *a: (_ for _ in ()).throw(RuntimeError("network gone")),
        )
        # Re-observation failed outright, so the ambiguity timestamp can only
        # come from the durable reservation, not from any replayed snapshot.
        self.assertEqual(outcome["campaign"]["terminal_at"], T0)


if __name__ == "__main__":
    unittest.main()
