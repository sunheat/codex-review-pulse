from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "codex-review-pulse" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import campaign_model as m  # noqa: E402
import github_api  # noqa: E402
import review_request  # noqa: E402
import storage  # noqa: E402


CODEX = "chatgpt-codex-connector"
H = "head-oid"
T0 = "2026-09-14T12:00:00Z"
T1 = "2026-09-14T12:00:05Z"
T2 = "2026-09-14T12:00:10Z"
CAMPAIGN_ID = "crp-20260914T120000Z-abc123"


def campaign() -> dict:
    return m.new_campaign(
        campaign_id=CAMPAIGN_ID,
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


def git(cwd: Path, *args: str) -> None:
    process = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True
    )
    if process.returncode != 0:
        raise RuntimeError(f"git {args} failed: {process.stderr.strip()}")


class OwnedRequestRepository:
    """A git-backed repository with an active campaign and a committed request."""

    def __init__(self, root: Path) -> None:
        self.path = root / "repo"
        self.path.mkdir()
        git(self.path, "init", "-b", "main")
        git(self.path, "config", "user.email", "t@e.test")
        git(self.path, "config", "user.name", "T")
        (self.path / "f").write_text("x", encoding="utf-8")
        git(self.path, "add", "f")
        git(self.path, "commit", "-m", "init")
        acquired = storage.acquire_setup_lock(
            "owner/repo", 7,
            campaign_id=CAMPAIGN_ID,
            acquired_at=T0,
            repository_path=self.path,
        )
        self.token = acquired["owner_token"]
        storage.initialize_campaign(
            "owner/repo", 7,
            owner_token=self.token,
            campaign=campaign(),
            repository_path=self.path,
        )

    def commit_request(self) -> None:
        """Durably commit the request round and per-head allowance (Phase 2)."""
        path = storage.campaign_path("owner/repo", 7, repository_path=self.path)
        record = storage.load_json(path)
        record = m.reserve_request(record, head_oid=H, reserved_at=T0, snapshot=snap(time=T0))
        storage.save_json(path, record)

    def campaign_on_disk(self) -> dict:
        return storage.load_json(
            storage.campaign_path("owner/repo", 7, repository_path=self.path)
        )

    def lock_status(self) -> str:
        return storage.inspect_lock(
            "owner/repo", 7, repository_path=self.path
        )["status"]


class CommittedGuardHandoffTests(unittest.TestCase):
    def test_exactly_one_committed_guard_is_required(self) -> None:
        with self.assertRaises(RuntimeError):
            review_request.committed_request_guard(campaign())
        base = m.reserve_request(campaign(), head_oid=H, reserved_at=T0, snapshot=snap(time=T0))
        double = m.reserve_request(
            base, head_oid="other-head", reserved_at=T0,
            snapshot=snap(time=T0, head="other-head"),
        )
        with self.assertRaises(RuntimeError):
            review_request.committed_request_guard(double)
        guard = review_request.committed_request_guard(base)
        self.assertEqual(guard["head_oid"], H)
        self.assertEqual(guard["state"], m.RESERVED)

    def test_invalidated_guard_is_not_a_committed_request(self) -> None:
        committed = m.reserve_request(campaign(), head_oid=H, reserved_at=T0, snapshot=snap(time=T0))
        used = m.invalidate_reserved_request(committed, head_oid=H, at=T1, reason="head_changed")
        with self.assertRaises(RuntimeError):
            review_request.committed_request_guard(used)


class RequestBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.fixture = OwnedRequestRepository(Path(self._tmp.name))
        self.fixture.commit_request()
        self._original_ensure = storage.ensure_active_campaign_owner

    def tearDown(self) -> None:
        storage.ensure_active_campaign_owner = self._original_ensure
        self._tmp.cleanup()

    def run_boundary(self, *, observer, commenter) -> dict:
        return review_request.run_committed_request(
            repository="owner/repo",
            pr_number=7,
            owner_token=self.fixture.token,
            repository_path=self.fixture.path,
            observer=observer,
            commenter=commenter,
        )

    def test_execution_consumes_no_second_round_or_reservation(self) -> None:
        events: list[str] = []

        def failing_reserve(*args, **kwargs):
            events.append("reserved")
            raise AssertionError("the boundary must not reserve again")

        sequence = [snap(time=T1), snap(time=T2)]

        def observer() -> dict:
            events.append("observed")
            return sequence.pop(0)

        def commenter(subject_id: str, body: str) -> dict:
            events.append("posted")
            return {"node_id": "c1", "created_at": T1, "url": "https://x/c1"}

        original_reserve = m.reserve_request
        m.reserve_request = failing_reserve
        try:
            result = self.run_boundary(observer=observer, commenter=commenter)
        finally:
            m.reserve_request = original_reserve
        # Final ownership revalidation happens immediately before the POST.
        self.assertEqual(events, ["observed", "posted", "observed"])
        self.assertEqual(result["outcome"], "window_open")
        self.assertEqual(result["rounds_used"], 1)
        self.assertEqual(result["ownership"], "released")
        self.assertEqual(self.fixture.campaign_on_disk()["rounds_used"], 1)
        self.assertEqual(self.fixture.lock_status(), "absent")

    def test_final_ownership_revalidation_precedes_the_post(self) -> None:
        events: list[str] = []
        real_ensure = storage.ensure_active_campaign_owner

        def counting_ensure(*args, **kwargs):
            events.append("authority")
            return real_ensure(*args, **kwargs)

        storage.ensure_active_campaign_owner = counting_ensure
        try:
            result = self.run_boundary(
                observer=lambda: (events.append("observed"), snap(time=T1))[1],
                commenter=lambda *a: (events.append("posted"), {"node_id": "c1", "created_at": T1, "url": ""})[1],
            )
        finally:
            storage.ensure_active_campaign_owner = self._original_ensure
        # The handoff establishment counts as the first authority call; the
        # final revalidation must run after it and immediately before the POST.
        self.assertEqual(events[:4], ["authority", "observed", "authority", "posted"])
        self.assertEqual(result["outcome"], "window_open")

    def test_authority_loss_before_the_post_fails_closed_without_posting(self) -> None:
        calls = {"count": 0}
        real_ensure = storage.ensure_active_campaign_owner

        def failing_second_ensure(*args, **kwargs):
            calls["count"] += 1
            if calls["count"] >= 2:
                raise RuntimeError("authority vanished")
            return real_ensure(*args, **kwargs)

        storage.ensure_active_campaign_owner = failing_second_ensure
        try:
            result = self.run_boundary(
                observer=lambda: snap(time=T1),
                commenter=lambda *a: (_ for _ in ()).throw(AssertionError("must not post")),
            )
        finally:
            storage.ensure_active_campaign_owner = self._original_ensure
        self.assertEqual(result["outcome"], "local_fail_closed")
        self.assertEqual(result["ownership"], "retained")
        self.assertEqual(self.fixture.lock_status(), "active")
        self.assertEqual(
            self.fixture.campaign_on_disk()["guards"][0]["state"], m.RESERVED
        )

    def test_stale_campaign_write_is_refused_and_fails_closed(self) -> None:
        def foreign_writer(subject_id: str, body: str) -> dict:
            # A same-token stale source overwriting newer campaign state must
            # be refused by the guarded persistence primitive.
            path = storage.campaign_path("owner/repo", 7, repository_path=self.fixture.path)
            record = storage.load_json(path)
            record["rounds_used"] = 2
            storage.save_json(path, record)
            return {"node_id": "c1", "created_at": T1, "url": "https://x/c1"}

        result = self.run_boundary(
            observer=lambda: snap(time=T1),
            commenter=foreign_writer,
        )
        self.assertEqual(result["outcome"], "local_fail_closed")
        self.assertEqual(result["ownership"], "retained")
        self.assertEqual(self.fixture.lock_status(), "active")

    def test_revalidation_invalidation_skips_post_and_releases(self) -> None:
        posted = []
        result = self.run_boundary(
            observer=lambda: snap(
                time=T1,
                threads=[{"id": "T1", "is_resolved": False, "root_login": CODEX}],
            ),
            commenter=lambda *a: posted.append(a) or {},
        )
        self.assertEqual(result["outcome"], "invalidated")
        self.assertEqual(posted, [])
        self.assertEqual(result["rounds_used"], 1)
        self.assertEqual(result["campaign_status"], m.ACTIVE)
        self.assertEqual(result["ownership"], "released")
        self.assertEqual(self.fixture.lock_status(), "absent")
        self.assertEqual(
            self.fixture.campaign_on_disk()["guards"][0]["state"], m.INVALIDATED
        )

    def test_head_change_at_revalidation_invalidates_without_post(self) -> None:
        result = self.run_boundary(
            observer=lambda: snap(time=T1, head="different"),
            commenter=lambda *a: (_ for _ in ()).throw(AssertionError("must not post")),
        )
        self.assertEqual(result["outcome"], "invalidated")
        self.assertEqual(
            self.fixture.campaign_on_disk()["guards"][0]["state"], m.INVALIDATED
        )
        self.assertEqual(self.fixture.lock_status(), "absent")

    def test_post_mutation_head_mismatch_terminates_unbracketed_and_releases(self) -> None:
        sequence = [snap(time=T1), snap(time=T2, head="changed")]
        result = self.run_boundary(
            observer=lambda: sequence.pop(0),
            commenter=lambda subject_id, body: {
                "node_id": "c1", "created_at": T1, "url": "https://x/c1",
            },
        )
        self.assertEqual(result["outcome"], "unbracketed")
        self.assertTrue(result["terminal"])
        self.assertEqual(result["ownership"], "released")
        self.assertEqual(result["scheduler_cleanup_authorized"], True)
        self.assertEqual(self.fixture.campaign_on_disk()["status"], m.MANUAL_INTERVENTION_REQUIRED)
        self.assertEqual(self.fixture.lock_status(), "absent")

    def test_server_rejection_with_no_comment_is_definitive_failure(self) -> None:
        sequence = [snap(time=T1), snap(time=T2)]
        result = self.run_boundary(
            observer=lambda: sequence.pop(0),
            commenter=lambda *a: (_ for _ in ()).throw(
                github_api.GithubRejectionError("GitHub GraphQL errors: forbidden")
            ),
        )
        self.assertEqual(result["outcome"], "creation_failed")
        self.assertTrue(result["terminal"])
        self.assertEqual(result["ownership"], "released")
        self.assertEqual(self.fixture.campaign_on_disk()["status"], m.REQUEST_CREATION_FAILED)
        self.assertEqual(self.fixture.lock_status(), "absent")

    def test_transport_failure_with_no_comment_is_ambiguous_and_retains(self) -> None:
        sequence = [snap(time=T1), snap(time=T2)]
        result = self.run_boundary(
            observer=lambda: sequence.pop(0),
            commenter=lambda *a: (_ for _ in ()).throw(RuntimeError("network gone")),
        )
        # A timeout or nonzero exit alone is never definitive: the mutation
        # may still complete, so ownership is retained fail closed.
        self.assertEqual(result["outcome"], "ambiguous")
        self.assertEqual(result["ownership"], "retained")
        self.assertEqual(self.fixture.campaign_on_disk()["status"], m.AMBIGUOUS_INTERRUPTION)
        self.assertEqual(self.fixture.lock_status(), "active")

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
        result = self.run_boundary(
            observer=lambda: sequence.pop(0),
            commenter=lambda *a: (_ for _ in ()).throw(RuntimeError("network gone")),
        )
        self.assertEqual(result["outcome"], "ambiguous")
        self.assertEqual(result["ownership"], "retained")
        self.assertEqual(self.fixture.campaign_on_disk()["status"], m.AMBIGUOUS_INTERRUPTION)

    def test_baseline_manual_comment_with_rejection_is_definitive(self) -> None:
        manual = {
            "id": "manual-old",
            "login": "operator",
            "created_at": "2026-09-14T09:00:00Z",
            "body": "@codex review",
        }
        # Replace the committed record with one whose durable baseline
        # already contains the manual request comment.
        path = storage.campaign_path("owner/repo", 7, repository_path=self.fixture.path)
        record = m.reserve_request(
            campaign(), head_oid=H, reserved_at=T0, snapshot=snap(time=T0, comments=[manual])
        )
        storage.save_json(path, record)
        sequence = [
            snap(time=T1, comments=[manual]),
            snap(time=T2, comments=[manual]),
        ]
        result = self.run_boundary(
            observer=lambda: sequence.pop(0),
            commenter=lambda *a: (_ for _ in ()).throw(
                github_api.GithubRejectionError("GitHub GraphQL errors: forbidden")
            ),
        )
        self.assertEqual(result["outcome"], "creation_failed")
        self.assertEqual(result["ownership"], "released")
        self.assertEqual(self.fixture.lock_status(), "absent")

    def test_observation_failure_after_mutation_error_fails_closed(self) -> None:
        calls = iter([snap(time=T1)])

        def observer() -> dict:
            try:
                return next(calls)
            except StopIteration:
                raise RuntimeError("network down")

        result = self.run_boundary(
            observer=observer,
            commenter=lambda *a: (_ for _ in ()).throw(RuntimeError("network gone")),
        )
        self.assertEqual(result["outcome"], "ambiguous")
        self.assertEqual(result["ownership"], "retained")
        # Re-observation failed outright, so the ambiguity timestamp can only
        # come from the durable reservation, not from any replayed snapshot.
        self.assertEqual(self.fixture.campaign_on_disk()["terminal_at"], T0)

    def test_incomplete_post_evidence_fails_closed(self) -> None:
        sequence = [snap(time=T1), snap(time=T2, complete=False)]
        result = self.run_boundary(
            observer=lambda: sequence.pop(0),
            commenter=lambda *a: (_ for _ in ()).throw(RuntimeError("500")),
        )
        self.assertEqual(result["outcome"], "ambiguous")
        self.assertEqual(result["ownership"], "retained")

    def test_release_failure_is_reported_as_release_unconfirmed(self) -> None:
        real_release = storage.release_lock

        def failing_release(*args, **kwargs):
            raise RuntimeError("release failed")

        storage.release_lock = failing_release
        try:
            result = self.run_boundary(
                observer=lambda: snap(time=T1),
                commenter=lambda *a: {"node_id": "c1", "created_at": T1, "url": ""},
            )
        finally:
            storage.release_lock = real_release
        self.assertEqual(result["ownership"], "release_unconfirmed")
        self.assertEqual(result["outcome"], "local_fail_closed")
        self.assertEqual(self.fixture.lock_status(), "active")


if __name__ == "__main__":
    unittest.main()
