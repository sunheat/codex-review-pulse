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


class RequestAttemptTests(unittest.TestCase):
    def test_round_and_guard_are_committed_before_the_mutation(self) -> None:
        events: list[str] = []
        original_reserve = review_request.model.reserve_request

        def tracking_reserve(campaign_data, **kwargs):
            result = original_reserve(campaign_data, **kwargs)
            events.append("reserved")
            return result

        review_request.model.reserve_request = tracking_reserve
        try:
            def commenter(subject_id: str, body: str) -> dict:
                events.append("posted")
                return {"node_id": "c1", "created_at": T1, "url": "https://x/c1"}

            sequence = [snap(time=T1), snap(time=T2)]
            outcome = review_request.execute_request_attempt(
                repository="owner/repo",
                pr_number=7,
                campaign=campaign(),
                admission_snapshot=snap(time=T0),
                persist=lambda record: None,
                observer=lambda: sequence.pop(0),
                commenter=commenter,
            )
        finally:
            review_request.model.reserve_request = original_reserve
        self.assertEqual(events, ["reserved", "posted"])
        self.assertEqual(outcome["outcome"], "window_open")
        guard = m.guard_for_head(outcome["campaign"], H)
        self.assertEqual(guard["state"], m.GUARD_ACTIVE)
        self.assertEqual(guard["request"]["node_id"], "c1")
        self.assertEqual(outcome["campaign"]["rounds_used"], 1)

    def test_reservation_is_persisted_before_any_external_call(self) -> None:
        events: list[str] = []
        sequence = [snap(time=T1), snap(time=T2)]

        def observer() -> dict:
            events.append("observed")
            return sequence.pop(0)

        def commenter(subject_id: str, body: str) -> dict:
            events.append("posted")
            return {"node_id": "c1", "created_at": T1, "url": "https://x/c1"}

        def persist(record: dict) -> None:
            guard = m.guard_for_head(record, H)
            events.append(
                "persisted-reserved"
                if record["rounds_used"] == 1 and guard["state"] == m.RESERVED
                else "persisted-window"
            )

        outcome = review_request.execute_request_attempt(
            repository="owner/repo",
            pr_number=7,
            campaign=campaign(),
            admission_snapshot=snap(time=T0),
            observer=observer,
            commenter=commenter,
            persist=persist,
        )
        self.assertEqual(
            events,
            ["persisted-reserved", "observed", "posted", "observed", "persisted-window"],
        )
        self.assertEqual(outcome["outcome"], "window_open")

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

        def observer() -> dict:
            return sequence.pop(0)

        outcome = review_request.execute_request_attempt(
            repository="owner/repo",
            pr_number=7,
            campaign=campaign(),
            admission_snapshot=snap(time=T0),
            persist=lambda record: None,
            observer=observer,
            commenter=lambda *a: posted.append(a) or {},
        )
        self.assertEqual(outcome["outcome"], "invalidated")
        self.assertEqual(posted, [])
        self.assertEqual(outcome["campaign"]["rounds_used"], 1)
        self.assertEqual(outcome["campaign"]["status"], m.ACTIVE)
        self.assertFalse(outcome["retain_lock"])

    def test_head_change_at_revalidation_invalidates_without_post(self) -> None:
        outcome = review_request.execute_request_attempt(
            repository="owner/repo",
            pr_number=7,
            campaign=campaign(),
            admission_snapshot=snap(time=T0),
            persist=lambda record: None,
            observer=lambda: snap(time=T1, head="different"),
            commenter=lambda *a: (_ for _ in ()).throw(AssertionError("must not post")),
        )
        self.assertEqual(outcome["outcome"], "invalidated")
        self.assertEqual(
            m.guard_for_head(outcome["campaign"], H)["state"], m.INVALIDATED
        )

    def test_post_mutation_head_mismatch_terminates_manual_unbracketed(self) -> None:
        sequence = [snap(time=T1), snap(time=T2, head="changed")]
        outcome = review_request.execute_request_attempt(
            repository="owner/repo",
            pr_number=7,
            campaign=campaign(),
            admission_snapshot=snap(time=T0),
            persist=lambda record: None,
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

        outcome = review_request.execute_request_attempt(
            repository="owner/repo",
            pr_number=7,
            campaign=campaign(),
            admission_snapshot=snap(time=T0),
            persist=lambda record: None,
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
        outcome = review_request.execute_request_attempt(
            repository="owner/repo",
            pr_number=7,
            campaign=campaign(),
            admission_snapshot=snap(time=T0),
            persist=lambda record: None,
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
        admission = snap(time=T0, comments=[manual])
        sequence = [snap(time=T1, comments=[manual]), snap(time=T2, comments=[manual])]
        outcome = review_request.execute_request_attempt(
            repository="owner/repo",
            pr_number=7,
            campaign=campaign(),
            admission_snapshot=admission,
            observer=lambda: sequence.pop(0),
            commenter=lambda *a: (_ for _ in ()).throw(RuntimeError("403 forbidden")),
            persist=lambda record: None,
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

        outcome = review_request.execute_request_attempt(
            repository="owner/repo",
            pr_number=7,
            campaign=campaign(),
            admission_snapshot=snap(time=T0),
            persist=lambda record: None,
            observer=observer,
            commenter=lambda *a: (_ for _ in ()).throw(RuntimeError("network gone")),
        )
        self.assertEqual(outcome["outcome"], "ambiguous")
        self.assertTrue(outcome["retain_lock"])
        self.assertEqual(outcome["campaign"]["status"], m.AMBIGUOUS_INTERRUPTION)

    def test_incomplete_post_evidence_fails_closed(self) -> None:
        sequence = [snap(time=T1), snap(time=T2, complete=False)]
        outcome = review_request.execute_request_attempt(
            repository="owner/repo",
            pr_number=7,
            campaign=campaign(),
            admission_snapshot=snap(time=T0),
            persist=lambda record: None,
            observer=lambda: sequence.pop(0),
            commenter=lambda *a: (_ for _ in ()).throw(RuntimeError("500")),
        )
        self.assertEqual(outcome["outcome"], "ambiguous")
        self.assertTrue(outcome["retain_lock"])


if __name__ == "__main__":
    unittest.main()
