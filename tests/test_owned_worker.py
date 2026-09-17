"""The deterministic owned-worker decision boundary (no network).

Covers the durable-local preflight, owned S1 observation, decision ordering,
effective-action commitment, bounded S2 terminal confirmation, and local
ownership disposition. ``fetch_snapshot`` is injected; no scheduler, adapter,
or network service exists.
"""

from __future__ import annotations

from pathlib import Path
import json
from copy import deepcopy
import subprocess
import sys
import tempfile
import unittest
import unittest.mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "codex-review-pulse" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import campaign_model as model  # noqa: E402
import owned  # noqa: E402
import storage  # noqa: E402


def git(cwd: Path, *args: str) -> str:
    process = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True
    )
    if process.returncode != 0:
        raise RuntimeError(f"git {args} failed: {process.stderr.strip()}")
    return process.stdout.strip()


CODEX = "chatgpt-codex-connector"
CAMPAIGN_ID = "crp-20260914T120000Z-abc123"
T0 = "2026-09-14T12:00:00Z"
T_EARLY = "2026-09-14T12:05:00Z"
T_GRACE = "2026-09-14T12:35:00Z"
T_AFTER = "2026-09-14T12:40:00Z"
H1 = "h1-oid"
H2 = "h2-oid"


def snapshot(
    *,
    time: str = T_EARLY,
    head: str = H1,
    pr_state: str = "OPEN",
    threads: list | None = None,
    reactions: list | None = None,
    reviews: list | None = None,
    comments: list | None = None,
    complete: bool = True,
) -> dict:
    return {
        "complete": complete,
        "server_time": time,
        "repository": "owner/repo",
        "pr_number": 7,
        "pr_state": pr_state,
        "head_oid": head,
        "head_ref_name": "feature",
        "head_repository": "owner/repo",
        "node_id": "pr-node-1",
        "viewer": "operator",
        "threads": threads or [],
        "reactions": reactions or [],
        "reviews": reviews or [],
        "comments": comments or [],
    }


def thread(thread_id: str, *, login: str = CODEX) -> dict:
    return {
        "id": thread_id,
        "is_resolved": False,
        "root_login": login,
        "path": "x.py",
        "body": "change",
    }


def reaction(content: str, rid: str, *, created_at: str = T_EARLY) -> dict:
    return {
        "id": rid,
        "content": content,
        "login": CODEX,
        "created_at": created_at,
    }


def review(*, state: str = "APPROVED", review_id: str = "rv1", head: str = H1, submitted_at: str = T_EARLY) -> dict:
    return {
        "id": review_id,
        "state": state,
        "login": CODEX,
        "commit_oid": head,
        "submitted_at": submitted_at,
    }


class WorkerDecisionFixture(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name) / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-b", "main")
        git(self.repo, "config", "user.email", "t@e.test")
        git(self.repo, "config", "user.name", "T")
        (self.repo / "f.txt").write_text("x", encoding="utf-8")
        git(self.repo, "add", "f.txt")
        git(self.repo, "commit", "-m", "init")
        self.repository_path = self.repo
        self.campaign = model.new_campaign(
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
        storage.save_json(self.campaign_path(), self.campaign)
        result = storage.acquire_worker_lock(
            "owner/repo",
            7,
            campaign_id=CAMPAIGN_ID,
            acquired_at=T0,
            repository_path=self.repository_path,
        )
        self.assertTrue(result["acquired"])
        self.token = result["owner_token"]
        self.fetches: list[dict] = []

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # -- helpers ----------------------------------------------------------

    def campaign_path(self) -> Path:
        return storage.campaign_path(
            "owner/repo", 7, repository_path=self.repository_path
        )

    def on_disk(self) -> dict:
        record = storage.load_json(self.campaign_path())
        model.validate_campaign(record, repository="owner/repo", pull_request_number=7)
        return record

    def fetch(self) -> dict:
        return self.fetches.pop(0)

    def decide(self, *, token: str | None = None) -> dict:
        return owned.run_worker_decision(
            repository="owner/repo",
            pr_number=7,
            owner_token=token or self.token,
            fetch_snapshot=self.fetch,
            repository_path=self.repository_path,
        )

    def lock_status(self) -> str:
        return storage.inspect_lock(
            "owner/repo", 7, repository_path=self.repository_path
        )["status"]

    def open_window(self, *, head: str = H1, opened_at: str = T0) -> None:
        self.campaign = model.reserve_request(
            self.campaign,
            head_oid=head,
            reserved_at=opened_at,
            snapshot=snapshot(time=opened_at, head=head),
        )
        self.campaign = model.open_request_window(
            self.campaign,
            head_oid=head,
            post_head_oid=head,
            request_node_id="req1",
            request_created_at=opened_at,
            request_url="https://example.test/req1",
        )
        storage.save_json(self.campaign_path(), self.campaign)


class PreflightTests(WorkerDecisionFixture):
    def test_reserved_guard_on_any_head_terminalizes_before_any_observation(self) -> None:
        # RESERVED on an older head while the delivery would observe H1.
        self.campaign = model.reserve_request(
            self.campaign, head_oid=H2, reserved_at=T0, snapshot=snapshot(head=H2)
        )
        # An unrelated active window on the observed head must not hide it.
        self.campaign = model.reserve_request(
            self.campaign, head_oid=H1, reserved_at=T0, snapshot=snapshot(time=T0)
        )
        self.campaign = model.open_request_window(
            self.campaign,
            head_oid=H1,
            post_head_oid=H1,
            request_node_id="req1",
            request_created_at=T0,
            request_url="u",
        )
        storage.save_json(self.campaign_path(), self.campaign)
        rounds_before = self.campaign["rounds_used"]

        def must_not_fetch() -> dict:
            raise AssertionError("no S1 may be fetched to prove a local result")

        outcome = owned.run_worker_decision(
            repository="owner/repo",
            pr_number=7,
            owner_token=self.token,
            fetch_snapshot=must_not_fetch,
            repository_path=self.repository_path,
        )
        self.assertEqual(outcome["outcome"], "terminal_retained")
        self.assertEqual(outcome["status"], model.AMBIGUOUS_INTERRUPTION)
        self.assertEqual(outcome["terminal_basis"], "durable_local")
        self.assertEqual(outcome["guard_head"], H2)
        self.assertFalse(outcome["round_committed"])
        self.assertEqual(outcome["ownership"], "retained")
        self.assertFalse(outcome["scheduler_cleanup_authorized"])
        record = self.on_disk()
        self.assertEqual(record["status"], model.AMBIGUOUS_INTERRUPTION)
        # The RESERVED diagnostic guard stays; the unrelated window is closed.
        states = sorted(guard["state"] for guard in record["guards"])
        self.assertEqual(states, [model.CLOSED, model.RESERVED])
        self.assertEqual(record["rounds_used"], rounds_before)
        self.assertEqual(self.lock_status(), "active")

    def test_reserved_ambiguity_denies_cleanup_even_for_terminal_campaign(self) -> None:
        self.campaign = model.reserve_request(
            self.campaign, head_oid=H1, reserved_at=T0, snapshot=snapshot(time=T0)
        )
        storage.save_json(self.campaign_path(), self.campaign)
        outcome = self.decide()
        self.assertEqual(outcome["outcome"], "terminal_retained")
        self.assertFalse(outcome["scheduler_cleanup_authorized"])


class ObservationTests(WorkerDecisionFixture):
    def test_failed_s1_causes_no_mutation_and_releases_cleanly(self) -> None:
        def broken() -> dict:
            raise RuntimeError("network down")

        outcome = owned.run_worker_decision(
            repository="owner/repo",
            pr_number=7,
            owner_token=self.token,
            fetch_snapshot=broken,
            repository_path=self.repository_path,
        )
        self.assertEqual(outcome["outcome"], "observation_failed_released")
        self.assertFalse(outcome["round_committed"])
        self.assertEqual(outcome["ownership"], "released")
        self.assertFalse(outcome["scheduler_cleanup_authorized"])
        record = self.on_disk()
        self.assertEqual(record["status"], model.ACTIVE)
        self.assertEqual(record["rounds_used"], 0)
        self.assertEqual(self.lock_status(), "absent")

    def test_incomplete_s1_is_not_authoritative_and_releases(self) -> None:
        self.fetches = [snapshot(complete=False)]
        outcome = self.decide()
        self.assertEqual(outcome["outcome"], "observation_failed_released")
        self.assertEqual(self.on_disk()["status"], model.ACTIVE)
        self.assertEqual(self.lock_status(), "absent")

    def test_s1_runs_with_the_sidecar_guard_released(self) -> None:
        # Taking the canonical guard inside the network call proves the owned
        # boundary does not hold it across observation.
        def guarded_fetch() -> dict:
            storage.inspect_lock(
                "owner/repo", 7, repository_path=self.repository_path
            )
            return snapshot(threads=[thread("T1")])

        outcome = owned.run_worker_decision(
            repository="owner/repo",
            pr_number=7,
            owner_token=self.token,
            fetch_snapshot=guarded_fetch,
            repository_path=self.repository_path,
        )
        self.assertEqual(outcome["outcome"], "remediation_committed")

    def test_stale_s0_cannot_authorize_remediation_after_fresh_resolution(self) -> None:
        # Admission (S0) may have seen feedback, but the boundary observes S1
        # itself: with no threads at S1, the fresh evidence wins.
        self.fetches = [snapshot()]
        outcome = self.decide()
        self.assertEqual(outcome["outcome"], "request_committed")
        self.assertEqual(self.on_disk()["rounds_used"], 1)

    def test_open_request_window_blocks_a_second_request(self) -> None:
        self.open_window()
        self.fetches = [snapshot(time=T_EARLY)]
        outcome = self.decide()
        self.assertEqual(outcome["outcome"], "wait_released")
        record = self.on_disk()
        self.assertEqual(record["rounds_used"], 1)
        self.assertEqual(len(record["guards"]), 1)
        self.assertEqual(self.lock_status(), "absent")

    def test_release_failure_never_reports_released(self) -> None:
        self.fetches = [snapshot(reactions=[reaction(model.EYES, "e1")])]
        with unittest.mock.patch.object(
            storage,
            "release_lock",
            side_effect=RuntimeError("unlink refused"),
        ):
            outcome = self.decide()
        self.assertEqual(outcome["outcome"], "local_fail_closed")
        self.assertEqual(outcome["ownership"], "retained")
        self.assertEqual(self.lock_status(), "active")

    def test_caller_cannot_substitute_snapshots_or_dispositions(self) -> None:
        with self.assertRaises(TypeError):
            owned.run_worker_decision(  # type: ignore[call-arg]
                repository="owner/repo",
                pr_number=7,
                owner_token=self.token,
                snapshot=snapshot(),
                terminal_status=model.SUCCEEDED,
                terminal_basis="durable_local",
                release=True,
                fetch_snapshot=self.fetch,
                repository_path=self.repository_path,
            )


class OrderingTests(WorkerDecisionFixture):
    def test_sync_head_uses_s1_and_decision_uses_post_sync_campaign(self) -> None:
        # The window belongs to H1; S1 observes head H2. Sync-head supersedes
        # the stale window, and the committed request is reserved for H2.
        self.open_window(head=H1, opened_at=T0)
        self.fetches = [snapshot(head=H2, time=T_AFTER)]
        outcome = self.decide()
        self.assertEqual(outcome["outcome"], "request_committed")
        self.assertEqual(outcome["guard_head"], H2)
        record = self.on_disk()
        self.assertEqual(
            model.guard_for_head(record, H1)["state"], model.SUPERSEDED
        )
        self.assertEqual(
            model.guard_for_head(record, H2)["state"], model.RESERVED
        )

    def test_stale_expected_source_is_never_overwritten(self) -> None:
        # The campaign changes on disk after the post-sync campaign was
        # captured but before terminal persistence: CAS must refuse and
        # preserve the newer record.
        self.open_window(head=H1, opened_at=T0)
        original = storage.transition_active_campaign

        def sync_then_mutate(*args, **kwargs):
            result = original(*args, **kwargs)
            mutated = deepcopy(result)
            mutated["config"]["interval_minutes"] = 45
            storage.save_json(self.campaign_path(), mutated)
            return result

        self.fetches = [snapshot(time=T_GRACE), snapshot(time=T_AFTER)]
        with unittest.mock.patch.object(
            storage, "transition_active_campaign", sync_then_mutate
        ):
            outcome = self.decide()
        self.assertEqual(outcome["outcome"], "local_fail_closed")
        record = self.on_disk()
        self.assertEqual(record["status"], model.ACTIVE)
        self.assertEqual(record["config"]["interval_minutes"], 45)
        self.assertEqual(self.lock_status(), "active")


class CommitmentTests(WorkerDecisionFixture):
    def test_remediation_is_returned_only_after_one_durable_round(self) -> None:
        self.fetches = [snapshot(threads=[thread("T1"), thread("T2")])]
        outcome = self.decide()
        self.assertEqual(outcome["outcome"], "remediation_committed")
        self.assertTrue(outcome["round_committed"])
        self.assertEqual(outcome["rounds_used"], 1)
        self.assertEqual(
            [t["id"] for t in outcome["threads"]], ["T1", "T2"]
        )
        self.assertEqual(outcome["expected_head"], H1)
        self.assertEqual(outcome["ownership"], "retained")
        self.assertFalse(outcome["scheduler_cleanup_authorized"])
        # The frozen batch is not persisted; only the round is durable.
        self.assertEqual(self.on_disk()["rounds_used"], 1)
        self.assertEqual(self.on_disk()["guards"], [])
        snapshot_on_disk = json.loads(
            Path(outcome["snapshot_path"]).read_text(encoding="utf-8")
        )
        self.assertEqual(snapshot_on_disk["head_oid"], H1)
        Path(outcome["snapshot_path"]).unlink()

    def test_request_commitment_reserves_exactly_once(self) -> None:
        self.fetches = [snapshot()]
        outcome = self.decide()
        self.assertEqual(outcome["outcome"], "request_committed")
        self.assertTrue(outcome["round_committed"])
        self.assertEqual(outcome["rounds_used"], 1)
        self.assertEqual(outcome["guard_state"], model.RESERVED)
        self.assertEqual(outcome["guard_head"], H1)
        self.assertEqual(outcome["ownership"], "retained")
        self.assertFalse(outcome["scheduler_cleanup_authorized"])
        record = self.on_disk()
        self.assertEqual(record["rounds_used"], 1)
        self.assertEqual(
            model.guard_for_head(record, H1)["state"], model.RESERVED
        )

    def test_request_committed_handoff_needs_no_new_tokens(self) -> None:
        self.fetches = [snapshot()]
        outcome = self.decide()
        self.assertEqual(
            set(outcome.keys()),
            {
                "outcome",
                "campaign_id",
                "guard_head",
                "guard_state",
                "round_committed",
                "rounds_used",
                "ownership",
                "scheduler_cleanup_authorized",
            },
        )
        # The durable handoff identity is campaign + owner lock + exact guard.
        self.assertEqual(outcome["campaign_id"], CAMPAIGN_ID)
        self.assertEqual(outcome["guard_head"], H1)

    def test_commitment_after_stale_source_change_fails_closed(self) -> None:
        original = storage.transition_active_campaign

        def sync_then_mutate(*args, **kwargs):
            result = original(*args, **kwargs)
            mutated = deepcopy(result)
            mutated["config"]["interval_minutes"] = 45
            storage.save_json(self.campaign_path(), mutated)
            return result

        self.fetches = [snapshot()]
        with unittest.mock.patch.object(
            storage, "transition_active_campaign", sync_then_mutate
        ):
            outcome = self.decide()
        self.assertEqual(outcome["outcome"], "local_fail_closed")
        record = self.on_disk()
        self.assertEqual(record["status"], model.ACTIVE)
        self.assertEqual(record["rounds_used"], 0)
        self.assertEqual(record["guards"], [])
        self.assertEqual(record["config"]["interval_minutes"], 45)

    def test_unknown_directive_fails_closed(self) -> None:
        self.fetches = [snapshot()]
        with unittest.mock.patch.object(
            model,
            "decide",
            return_value={"action": "mystery_directive"},
        ):
            outcome = self.decide()
        self.assertEqual(outcome["outcome"], "local_fail_closed")
        self.assertEqual(self.on_disk()["rounds_used"], 0)


class TerminalConfirmationTests(WorkerDecisionFixture):
    def test_observed_approval_is_confirmed_by_s2_and_released(self) -> None:
        self.fetches = [
            snapshot(time=T_EARLY, reviews=[review(review_id="rv1")]),
            snapshot(time=T_AFTER, reviews=[review(review_id="rv1")]),
        ]
        outcome = self.decide()
        self.assertEqual(outcome["outcome"], "terminal_released")
        self.assertEqual(outcome["status"], model.SUCCEEDED)
        self.assertEqual(outcome["terminal_basis"], "observation")
        self.assertEqual(outcome["status_detail"], "approval:rv1")
        self.assertTrue(outcome["scheduler_cleanup_authorized"])
        self.assertEqual(outcome["ownership"], "released")
        record = self.on_disk()
        self.assertEqual(record["status"], model.SUCCEEDED)
        self.assertEqual(record["terminal_at"], T_AFTER)
        self.assertEqual(self.lock_status(), "absent")

    def test_fresher_s2_detail_is_persisted_not_caller_text(self) -> None:
        self.fetches = [
            snapshot(time=T_EARLY, reviews=[review(review_id="rv1")]),
            snapshot(time=T_AFTER, reviews=[review(review_id="rv2")]),
        ]
        outcome = self.decide()
        self.assertEqual(outcome["status_detail"], "approval:rv2")

    def test_contradicting_s2_does_not_terminalize(self) -> None:
        self.fetches = [
            snapshot(time=T_EARLY, reviews=[review(review_id="rv1")]),
            snapshot(time=T_AFTER, threads=[thread("T9")]),
        ]
        outcome = self.decide()
        self.assertEqual(outcome["outcome"], "observation_failed_released")
        record = self.on_disk()
        self.assertEqual(record["status"], model.ACTIVE)
        self.assertEqual(record["rounds_used"], 0)
        self.assertEqual(self.lock_status(), "absent")

    def test_incomplete_or_headmoved_s2_does_not_terminalize(self) -> None:
        for bad_s2 in (
            snapshot(time=T_AFTER, complete=False),
            snapshot(
                time=T_AFTER,
                head=H2,
                reviews=[review(review_id="rv1", head=H2)],
            ),
        ):
            with self.subTest(complete=bad_s2["complete"]):
                self.setUp()
                self.fetches = [
                    snapshot(time=T_EARLY, reviews=[review(review_id="rv1")]),
                    bad_s2,
                ]
                outcome = self.decide()
                self.assertEqual(outcome["outcome"], "observation_failed_released")
                self.assertEqual(self.on_disk()["status"], model.ACTIVE)
                self.tearDown()

    def test_failed_s2_confirmation_consumes_no_round(self) -> None:
        self.fetches = [
            snapshot(time=T_EARLY, reviews=[review(review_id="rv1")]),
            snapshot(time=T_AFTER, reactions=[reaction(model.EYES, "late-eyes")]),
        ]
        outcome = self.decide()
        self.assertEqual(outcome["outcome"], "observation_failed_released")
        self.assertFalse(outcome["round_committed"])
        self.assertEqual(self.on_disk()["rounds_used"], 0)

    def test_target_unavailable_does_not_require_head_consistency(self) -> None:
        self.fetches = [
            snapshot(time=T_EARLY, pr_state="MERGED"),
            snapshot(time=T_AFTER, head=H2, pr_state="MERGED"),
        ]
        outcome = self.decide()
        self.assertEqual(outcome["outcome"], "terminal_released")
        self.assertEqual(outcome["status"], model.TARGET_UNAVAILABLE)
        self.assertEqual(outcome["terminal_basis"], "observation")
        self.assertTrue(outcome["scheduler_cleanup_authorized"])

    def test_grace_elapsed_unresponsive_is_confirmed_then_released(self) -> None:
        self.open_window(head=H1, opened_at=T0)
        self.fetches = [
            snapshot(time=T_GRACE),
            snapshot(time=T_AFTER),
        ]
        outcome = self.decide()
        self.assertEqual(outcome["outcome"], "terminal_released")
        self.assertEqual(outcome["status"], model.CODEX_REVIEW_SERVICE_UNRESPONSIVE)
        self.assertTrue(outcome["scheduler_cleanup_authorized"])
        self.assertEqual(self.lock_status(), "absent")

    def test_exactly_one_s2_fetch_only_for_observation_derived_terminals(self) -> None:
        self.open_window(head=H1, opened_at=T0)
        self.fetches = [snapshot(time=T_GRACE), snapshot(time=T_AFTER)]
        outcome = self.decide()
        self.assertEqual(len(self.fetches), 0)
        self.assertEqual(outcome["outcome"], "terminal_released")


class FinalizationTests(WorkerDecisionFixture):
    """Same-delivery exhaustion finalization after a successful final remediation."""

    def finalize(self) -> dict:
        return owned.finalize_exhaustion_owned(
            repository="owner/repo",
            pr_number=7,
            owner_token=self.token,
            repository_path=self.repository_path,
        )

    def test_successful_final_remediation_finalizes_in_same_delivery(self) -> None:
        # Active at max-1 rounds; the boundary commits the final remediation
        # round; after the batch completes successfully the same delivery
        # terminalizes rounds_exhausted and releases without needing another
        # scheduled delivery merely to discover exhaustion.
        self.campaign["config"]["max_rounds"] = 2
        self.campaign["rounds_used"] = 1
        storage.save_json(self.campaign_path(), self.campaign)
        self.fetches = [snapshot(threads=[thread("T1")])]
        committed = self.decide()
        self.assertEqual(committed["outcome"], "remediation_committed")
        self.assertEqual(committed["rounds_used"], 2)
        self.assertEqual(self.on_disk()["status"], model.ACTIVE)

        finalization = self.finalize()
        self.assertTrue(finalization["finalized"])
        self.assertEqual(finalization["outcome"], "rounds_exhausted_finalized")
        self.assertEqual(finalization["status"], model.ROUNDS_EXHAUSTED)
        self.assertEqual(finalization["rounds_used"], 2)
        self.assertEqual(finalization["ownership"], "released")
        self.assertTrue(finalization["scheduler_cleanup_authorized"])
        record = self.on_disk()
        self.assertEqual(record["status"], model.ROUNDS_EXHAUSTED)
        self.assertEqual(record["rounds_used"], record["config"]["max_rounds"])
        self.assertIsNotNone(record["terminal_at"])
        self.assertEqual(self.lock_status(), "absent")

    def test_finalization_refuses_while_request_window_is_outstanding(self) -> None:
        # A fully-consumed campaign with an open current-head request window
        # stays active for non-counting lifecycle observation.
        self.campaign["config"]["max_rounds"] = 1
        self.open_window()
        self.assertEqual(self.campaign["rounds_used"], 1)
        finalization = self.finalize()
        self.assertFalse(finalization["finalized"])
        self.assertEqual(finalization["outcome"], "not_applicable")
        self.assertIn("window", finalization["reason"])
        record = self.on_disk()
        self.assertEqual(record["status"], model.ACTIVE)
        self.assertEqual(self.lock_status(), "active")

    def test_finalization_refuses_while_budget_remains(self) -> None:
        self.fetches = [snapshot(threads=[thread("T1")])]
        committed = self.decide()
        self.assertEqual(committed["outcome"], "remediation_committed")
        self.assertEqual(committed["rounds_used"], 1)
        self.assertEqual(self.on_disk()["config"]["max_rounds"], 6)
        finalization = self.finalize()
        self.assertFalse(finalization["finalized"])
        self.assertEqual(finalization["outcome"], "not_applicable")
        self.assertEqual(self.on_disk()["status"], model.ACTIVE)
        self.assertEqual(self.lock_status(), "active")

    def test_finalization_refuses_an_already_terminal_campaign(self) -> None:
        self.campaign["config"]["max_rounds"] = 2
        self.campaign["rounds_used"] = 2
        self.campaign = model.terminate(
            self.campaign, status=model.SUCCEEDED, at=T_EARLY
        )
        storage.save_json(self.campaign_path(), self.campaign)
        finalization = self.finalize()
        self.assertFalse(finalization["finalized"])
        self.assertEqual(finalization["outcome"], "not_applicable")
        self.assertIn("already terminal", finalization["reason"])
        self.assertEqual(self.on_disk()["status"], model.SUCCEEDED)
        self.assertEqual(self.lock_status(), "active")

    def test_finalization_fails_closed_on_reserved_guard(self) -> None:
        self.campaign["config"]["max_rounds"] = 1
        self.campaign = model.reserve_request(
            self.campaign, head_oid=H1, reserved_at=T0, snapshot=snapshot(time=T0)
        )
        storage.save_json(self.campaign_path(), self.campaign)
        finalization = self.finalize()
        self.assertFalse(finalization["finalized"])
        self.assertEqual(finalization["outcome"], "local_fail_closed")
        self.assertEqual(finalization["ownership"], "retained")
        self.assertFalse(finalization["scheduler_cleanup_authorized"])
        self.assertEqual(self.on_disk()["status"], model.ACTIVE)
        self.assertEqual(self.lock_status(), "active")

    def test_terminalized_campaign_is_never_lost_on_release_failure(self) -> None:
        self.campaign["config"]["max_rounds"] = 2
        self.campaign["rounds_used"] = 1
        storage.save_json(self.campaign_path(), self.campaign)
        self.fetches = [snapshot(threads=[thread("T1")])]
        self.assertEqual(self.decide()["outcome"], "remediation_committed")
        with unittest.mock.patch.object(
            storage,
            "release_lock",
            side_effect=RuntimeError("unlink refused"),
        ):
            finalization = self.finalize()
        self.assertTrue(finalization["finalized"])
        self.assertEqual(finalization["outcome"], "release_unconfirmed")
        self.assertEqual(finalization["ownership"], "retained")
        self.assertFalse(finalization["scheduler_cleanup_authorized"])
        record = self.on_disk()
        self.assertEqual(record["status"], model.ROUNDS_EXHAUSTED)
        self.assertEqual(self.lock_status(), "active")

    def test_caller_cannot_select_the_terminal_result(self) -> None:
        with self.assertRaises(TypeError):
            owned.finalize_exhaustion_owned(  # type: ignore[call-arg]
                repository="owner/repo",
                pr_number=7,
                owner_token=self.token,
                status=model.SUCCEEDED,
                terminal_at=T_EARLY,
                repository_path=self.repository_path,
            )


if __name__ == "__main__":
    unittest.main()
