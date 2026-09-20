"""Behavioral tests for the deterministic remediation subprotocol (Phase 1).

Covers the eight required regression clusters: preparation disappearance and
clean restart, closed remediation routing and legacy-path removal, malformed
or stale semantic units, campaign-source CAS and the concurrent proposal
loser, complete-tree binding and hook safety, the commitment point and
ordered mutation, definitive versus ambiguous failure, and the lost
finalizer-result incident class. Git publication runs against a real local
bare remote; every GitHub transport is injected. No network.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "codex-review-pulse" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import campaign_model as model  # noqa: E402
import externalize  # noqa: E402
import gitlocal  # noqa: E402
import github_api  # noqa: E402
import owned  # noqa: E402
import remediation  # noqa: E402
import storage  # noqa: E402


CODEX = "chatgpt-codex-connector"
VIEWER = "operator"
CAMPAIGN_ID = "crp-20260914T120000Z-abc123"
T0 = "2026-09-14T12:00:00Z"


def git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    process = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True
    )
    if check and process.returncode != 0:
        raise RuntimeError(f"git {args} failed: {process.stderr.strip()}")
    return process


def rev_parse(cwd: Path, *args: str) -> str:
    return git(cwd, "rev-parse", *args).stdout.strip()


def raw_thread(
    thread_id: str,
    *,
    resolved: bool = False,
    author: str | None = CODEX,
    body: str = "Fix this boundary check.",
    path: str = "f.txt",
    updated_at: str = T0,
) -> dict:
    return {
        "id": thread_id,
        "is_resolved": resolved,
        "is_outdated": False,
        "path": path,
        "root_comment_id": f"{thread_id}-rc1",
        "root_login": author,
        "body": body,
        "root_updated_at": updated_at,
        "url": f"https://example.test/{thread_id}",
    }


def disposition(
    thread_id: str,
    outcome: str,
    *,
    mode: str | None = None,
    title: str = "Deferred: boundary check",
    body: str = "Deferred follow-up for the boundary check.",
    rationale: str = "Already addressed on the prepared head; verified.",
) -> dict:
    if outcome == "fix_now":
        return {"thread_id": thread_id, "outcome": "fix_now", "mode": mode}
    if outcome == "fix_later":
        return {
            "thread_id": thread_id,
            "outcome": "fix_later",
            "issue_title": title,
            "issue_body": body,
        }
    return {
        "thread_id": thread_id,
        "outcome": "no_fix_required",
        "rationale": rationale,
    }


def proposal_json(*dispositions: dict) -> str:
    return json.dumps(
        {
            "kind": remediation.PROPOSAL_KIND,
            "schema_version": remediation.PROPOSAL_SCHEMA_VERSION,
            "dispositions": list(dispositions),
        }
    )


class RemediationFixture(unittest.TestCase):
    """A real local clone with a bare origin and one active campaign."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        origin = root / "origin.git"
        seed = root / "seed"
        self.clone = root / "clone"
        git(root, "init", "--bare", str(origin))
        seed.mkdir()
        git(seed, "init", "-b", "main")
        git(seed, "config", "user.email", "t@e.test")
        git(seed, "config", "user.name", "T")
        (seed / ".gitignore").write_text("ignored.log\n", encoding="utf-8")
        (seed / "f.txt").write_text("v1\n", encoding="utf-8")
        git(seed, "add", ".")
        git(seed, "commit", "-m", "initial")
        git(seed, "remote", "add", "origin", str(origin))
        git(seed, "push", "origin", "main")
        git(origin, "symbolic-ref", "HEAD", "refs/heads/main")
        git(seed, "clone", str(origin), str(self.clone))
        git(self.clone, "config", "user.email", "t@e.test")
        git(self.clone, "config", "user.name", "T")
        self.h1 = rev_parse(self.clone, "HEAD")

        acquired = storage.acquire_setup_lock(
            "owner/repo", 7,
            campaign_id=CAMPAIGN_ID,
            acquired_at=T0,
            repository_path=self.clone,
        )
        storage.initialize_campaign(
            "owner/repo", 7,
            owner_token=acquired["owner_token"],
            campaign=model.new_campaign(
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
            ),
            repository_path=self.clone,
        )
        storage.release_lock(
            "owner/repo", 7, acquired["owner_token"], repository_path=self.clone
        )
        self.threads: list[dict] = [raw_thread("T1")]
        self.packet_path: str | None = None
        self.worktree: Path | None = None
        # The fake GitHub issue tracker: created deferred issues become
        # visible to later provenance searches, mirroring the real platform.
        self.issues: list[dict] = []
        self.created_issues: list[tuple[str, str]] = []

    def _create_issue(self, title: str, body: str) -> str:
        self.created_issues.append((title, body))
        number = 12 + len(self.issues)
        url = f"https://example.test/owner/repo/issues/{number}"
        self.issues.append(
            {"number": number, "url": url, "body": body, "author": {"login": VIEWER}}
        )
        return url

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # -- helpers ------------------------------------------------------------

    def campaign_path(self) -> Path:
        return storage.campaign_path("owner/repo", 7, repository_path=self.clone)

    def on_disk(self) -> dict:
        record = storage.load_json(self.campaign_path())
        model.validate_campaign(record, repository="owner/repo", pull_request_number=7)
        return record

    def lock_status(self) -> str:
        return storage.inspect_lock(
            "owner/repo", 7, repository_path=self.clone
        )["status"]

    def remote_head(self) -> str:
        return gitlocal.remote_head(self.clone, "main")

    def snapshot(self, *, head: str | None = None, threads: list | None = None) -> dict:
        return {
            "complete": True,
            "server_time": T0,
            "repository": "owner/repo",
            "pr_number": 7,
            "pr_state": "OPEN",
            "head_oid": head if head is not None else self.h1,
            "head_ref_name": "main",
            "head_repository": "owner/repo",
            "node_id": "PR_1",
            "viewer": VIEWER,
            "threads": self.threads if threads is None else threads,
            "reactions": [],
            "reviews": [],
            "comments": [],
        }

    def live_github(self) -> dict:
        """A fake GitHub observation that mirrors the real remote head."""
        return self.snapshot(head=self.remote_head())

    def acquire_worker(self) -> str:
        acquired = storage.acquire_worker_lock(
            "owner/repo", 7,
            campaign_id=CAMPAIGN_ID,
            acquired_at=T0,
            repository_path=self.clone,
        )
        self.assertTrue(acquired["acquired"])
        return acquired["owner_token"]

    def prepare(self, *, threads: list | None = None, head: str | None = None) -> dict:
        """Run one owned-worker decision that selects remediation."""
        self.threads = threads if threads is not None else self.threads
        s1 = self.snapshot(head=head, threads=self.threads)
        token = self.acquire_worker()
        outcome = owned.run_worker_decision(
            repository="owner/repo",
            pr_number=7,
            owner_token=token,
            fetch_snapshot=lambda: s1,
            fetch_remote=lambda: None,
            repository_path=self.clone,
        )
        if outcome.get("outcome") == "remediation_prepared":
            self.packet_path = outcome["packet_path"]
            self.worktree = Path(outcome["worktree"])
        return outcome

    def packet(self) -> dict:
        assert self.packet_path is not None
        return storage.load_json(self.packet_path)

    def finalize(
        self,
        *,
        proposal_text: str | None = None,
        packet_path: str | Path | None = None,
        fetch=None,
        list_issues=None,
        create_issue=None,
        viewer_call=None,
        resolve_call=None,
        remote_head_call=None,
        runner=None,
        proposal_origin: Path | None = None,
    ) -> dict:
        if proposal_text is None:
            proposal_text = proposal_json(disposition("T1", "no_fix_required"))

        result = remediation.finalize_remediation(
            repository="owner/repo",
            pr_number=7,
            packet_path=packet_path or self.packet_path,
            proposal_text=proposal_text,
            proposal_origin_path=proposal_origin,
            repository_path=self.clone,
            fetch_snapshot=fetch or self.live_github,
            list_issues=list_issues or (lambda: list(self.issues)),
            create_issue=create_issue or self._create_issue,
            viewer_call=viewer_call or (lambda: VIEWER),
            resolve_call=resolve_call or self.resolving()[1],
            remote_head_call=remote_head_call,
            runner=runner,
        )
        result["_created_issues"] = self.created_issues
        return result

    def resolving(self) -> tuple[list[str], object]:
        resolved: list[str] = []

        def resolve_call(thread_id: str) -> dict:
            resolved.append(thread_id)
            return {"id": thread_id, "isResolved": True}

        return resolved, resolve_call


# ---------------------------------------------------------------------------
# Cluster 1: preparation disappearance and clean restart


class PreparationDisappearanceTests(RemediationFixture):
    def test_preparation_creates_only_disposable_state(self) -> None:
        outcome = self.prepare()
        self.assertEqual(outcome["outcome"], "remediation_prepared")
        self.assertEqual(outcome["action"], "run_semantic_remediation")
        self.assertFalse(outcome["round_committed"])
        self.assertEqual(outcome["ownership"], "released")
        self.assertEqual(self.on_disk()["rounds_used"], 0)
        self.assertEqual(self.on_disk()["status"], model.ACTIVE)
        self.assertEqual(self.lock_status(), "absent")
        # Disposable artifacts exist: the packet, the frozen batch snapshot,
        # and the isolated worktree under the campaign state root.
        packet = self.packet()
        self.assertTrue(Path(self.packet_path).exists())
        self.assertTrue(Path(packet["batch_snapshot_path"]).exists())
        self.assertTrue(self.worktree.exists())
        root = storage.worktree_root(
            "owner/repo", 7, repository_path=self.clone
        )
        self.assertTrue(str(self.worktree.resolve()).startswith(str(root.resolve())))

    def test_model_disappearance_leaves_no_campaign_mutation(self) -> None:
        self.prepare()
        # The "model disappears" case is this test simply stopping here: no
        # finalizer runs. Nothing durable changed besides disposable state.
        self.assertEqual(self.on_disk()["rounds_used"], 0)
        self.assertEqual(self.on_disk()["guards"], [])
        self.assertEqual(self.lock_status(), "absent")
        self.assertEqual(self.remote_head(), self.h1)

    def test_abandoned_worktree_does_not_block_fresh_preparation(self) -> None:
        first = self.prepare()
        self.assertEqual(first["outcome"], "remediation_prepared")
        second = self.prepare()
        self.assertEqual(second["outcome"], "remediation_prepared")
        self.assertNotEqual(first["worktree"], second["worktree"])
        self.assertEqual(self.on_disk()["rounds_used"], 0)

    def test_failed_fetch_releases_and_consumes_no_round(self) -> None:
        s1 = self.snapshot()
        token = self.acquire_worker()
        outcome = owned.run_worker_decision(
            repository="owner/repo",
            pr_number=7,
            owner_token=token,
            fetch_snapshot=lambda: s1,
            fetch_remote=lambda: (_ for _ in ()).throw(RuntimeError("fetch down")),
            repository_path=self.clone,
        )
        self.assertEqual(outcome["outcome"], "remediation_preparation_refused")
        self.assertEqual(outcome["ownership"], "released")
        self.assertFalse(outcome["round_committed"])
        self.assertEqual(self.on_disk()["rounds_used"], 0)
        self.assertEqual(self.lock_status(), "absent")


# ---------------------------------------------------------------------------
# Cluster 2: closed remediation routing and legacy-path removal


class ClosedRoutingTests(RemediationFixture):
    def test_preparation_returns_no_owner_token(self) -> None:
        outcome = self.prepare()
        rendered = json.dumps(outcome)
        self.assertNotIn("owner_token", rendered)
        packet = self.packet()
        self.assertNotIn("owner_token", json.dumps(packet))

    def test_packet_binds_controller_owned_authority(self) -> None:
        self.prepare()
        packet = self.packet()
        self.assertEqual(packet["campaign_id"], CAMPAIGN_ID)
        self.assertEqual(packet["repository"], "owner/repo")
        self.assertEqual(packet["pr_number"], 7)
        self.assertEqual(packet["prepared_head"], self.h1)
        self.assertEqual(packet["head_ref_name"], "main")
        self.assertEqual(
            packet["source_digest"],
            model.campaign_source_digest(self.on_disk()),
        )
        self.assertEqual(
            [t["id"] for t in packet["targets"]], ["T1"]
        )
        frozen = packet["targets"][0]
        self.assertEqual(frozen["root_comment_id"], "T1-rc1")
        self.assertEqual(frozen["root_author"], CODEX)

    def test_finalizer_accepts_no_model_owner_token(self) -> None:
        parameters = inspect.signature(
            remediation.finalize_remediation
        ).parameters
        self.assertNotIn("owner_token", parameters)

    def test_legacy_long_lock_path_is_unreachable(self) -> None:
        externalize_source = (SCRIPTS / "externalize.py").read_text(encoding="utf-8")
        self.assertNotIn("def publish_fix_now", externalize_source)
        self.assertNotIn("build_parser", externalize_source)
        owned_help = subprocess.run(
            [sys.executable, str(SCRIPTS / "owned.py"), "--help"],
            capture_output=True, text=True,
        ).stdout
        self.assertNotIn("finalize-exhaustion", owned_help)
        references = ROOT / "skills" / "codex-review-pulse" / "references"
        worker = (references / "worker.md").read_text(encoding="utf-8")
        self.assertNotIn("publish-fix-now", worker)
        self.assertNotIn("ensure-issue", worker)

    def test_malformed_proposal_does_not_fall_back_to_legacy_path(self) -> None:
        self.prepare()
        (self.worktree / "f.txt").write_text("v2\n", encoding="utf-8")
        result = self.finalize(proposal_text="{not json")
        self.assertEqual(result["outcome"], "remediation_refused")
        self.assertFalse(result["round_committed"])
        self.assertEqual(self.on_disk()["rounds_used"], 0)
        self.assertEqual(self.lock_status(), "absent")
        # The legacy path did not consume the round or hold the lock.
        self.assertEqual(self.remote_head(), self.h1)


# ---------------------------------------------------------------------------
# Cluster 3: malformed or stale semantic unit


class MalformedProposalTests(RemediationFixture):
    def refusals(self) -> None:
        self.assertEqual(self.on_disk()["rounds_used"], 0)
        self.assertEqual(self.lock_status(), "absent")

    def test_missing_prepared_target_refuses(self) -> None:
        self.prepare(threads=[raw_thread("T1"), raw_thread("T2")])
        result = self.finalize(proposal_text=proposal_json(disposition("T1", "no_fix_required")))
        self.assertEqual(result["outcome"], "remediation_refused")
        self.assertIn("coverage", result["reason"])
        self.refusals()

    def test_extra_target_refuses(self) -> None:
        self.prepare(threads=[raw_thread("T1")])
        result = self.finalize(
            proposal_text=proposal_json(
                disposition("T1", "no_fix_required"),
                disposition("T9", "no_fix_required"),
            )
        )
        self.assertEqual(result["outcome"], "remediation_refused")
        self.refusals()

    def test_duplicate_target_refuses(self) -> None:
        self.prepare(threads=[raw_thread("T1")])
        result = self.finalize(
            proposal_text=proposal_json(
                disposition("T1", "no_fix_required"),
                disposition("T1", "no_fix_required"),
            )
        )
        self.assertEqual(result["outcome"], "remediation_refused")
        self.refusals()

    def test_unknown_outcome_refuses(self) -> None:
        self.prepare(threads=[raw_thread("T1")])
        result = self.finalize(
            proposal_text=proposal_json(
                {"thread_id": "T1", "outcome": "maybe_someday"}
            )
        )
        self.assertEqual(result["outcome"], "remediation_refused")
        self.refusals()

    def test_proposal_cannot_supply_authoritative_fields(self) -> None:
        self.prepare(threads=[raw_thread("T1")])
        forged = json.dumps(
            {
                "kind": remediation.PROPOSAL_KIND,
                "schema_version": remediation.PROPOSAL_SCHEMA_VERSION,
                "dispositions": [disposition("T1", "no_fix_required")],
                "prepared_head": "attacker-head",
                "source_digest": "0" * 64,
                "worktree_path": "/elsewhere",
                "campaign_id": "crp-20200101T000000Z-ffffff",
            }
        )
        result = self.finalize(proposal_text=forged)
        self.assertEqual(result["outcome"], "remediation_refused")
        self.refusals()

    def test_missing_packet_refuses_without_acquisition(self) -> None:
        self.prepare()
        result = self.finalize(packet_path=self.clone / "missing-packet.json")
        self.assertEqual(result["outcome"], "remediation_refused")
        self.assertFalse(result["round_committed"])
        self.refusals()

    def test_foreign_packet_refuses(self) -> None:
        self.prepare()
        packet = self.packet()
        packet["repository"] = "other/repo"
        foreign = self.clone / "foreign.json"
        storage.save_json(foreign, packet)
        result = self.finalize(packet_path=foreign)
        self.assertEqual(result["outcome"], "remediation_refused")
        self.refusals()

    def test_proposal_file_inside_worktree_refuses(self) -> None:
        self.prepare(threads=[raw_thread("T1")])
        inside = self.worktree / "proposal.json"
        inside.write_text(proposal_json(disposition("T1", "no_fix_required")), encoding="utf-8")
        result = self.finalize(proposal_origin=inside)
        self.assertEqual(result["outcome"], "remediation_refused")
        self.refusals()


class StaleProposalTests(RemediationFixture):
    def stale(self, result: dict) -> None:
        self.assertEqual(
            result["outcome"], "remediation_stale", json.dumps(result, indent=2)
        )
        self.assertFalse(result["round_committed"])
        self.assertEqual(self.on_disk()["rounds_used"], 0)
        self.assertEqual(self.lock_status(), "absent")
        self.assertEqual(self.on_disk()["status"], model.ACTIVE)

    def test_head_change_makes_whole_proposal_stale(self) -> None:
        self.prepare()
        result = self.finalize(fetch=lambda: self.snapshot(head="advanced-oid"))
        self.stale(result)

    def test_target_evidence_change_makes_whole_proposal_stale(self) -> None:
        self.prepare()
        edited = [raw_thread("T1", body="The comment was edited.")]
        result = self.finalize(fetch=lambda: self.snapshot(threads=edited))
        self.stale(result)

    def test_externally_resolved_target_makes_whole_proposal_stale(self) -> None:
        self.prepare()
        resolved = [raw_thread("T1", resolved=True)]
        result = self.finalize(fetch=lambda: self.snapshot(threads=resolved))
        self.stale(result)

    def test_new_feedback_outside_the_batch_neither_enlarges_nor_invalidates(self) -> None:
        self.prepare(threads=[raw_thread("T1")])
        # Applicable feedback appeared after preparation: it waits for a later
        # fresh delivery; it is not absorbed and does not invalidate the
        # prepared proposal.
        observed = [raw_thread("T1"), raw_thread("T2", body="New sibling feedback.")]
        resolved, resolve_call = self.resolving()
        result = self.finalize(
            fetch=lambda: self.snapshot(threads=observed),
            resolve_call=resolve_call,
        )
        self.assertEqual(
            result["outcome"], "remediation_completed", json.dumps(result, indent=2)
        )
        self.assertEqual(resolved, ["T1"])
        self.assertNotIn("T2", result["resolutions"])
        self.assertEqual(self.on_disk()["rounds_used"], 1)
        self.assertEqual(self.lock_status(), "absent")

    def test_campaign_change_without_head_movement_makes_proposal_stale(self) -> None:
        self.prepare()
        # An authoritative campaign transition that never moves the head:
        # a concurrent delivery reserved its request on another head.
        def reserve(record: dict) -> dict:
            return model.reserve_request(
                record,
                head_oid="other-head-oid",
                reserved_at=T0,
                snapshot=self.snapshot(head="other-head-oid"),
            )

        concurrent_token = self.acquire_worker()
        storage.transition_active_campaign(
            "owner/repo", 7, owner_token=concurrent_token,
            transition=reserve, repository_path=self.clone,
        )
        # The concurrent delivery finished its reservation and released.
        storage.release_lock(
            "owner/repo", 7, concurrent_token, repository_path=self.clone
        )
        result = self.finalize()
        self.assertEqual(
            result["outcome"], "remediation_stale", json.dumps(result, indent=2)
        )
        self.assertFalse(result["round_committed"])
        # The concurrent delivery's round is the only consumed round.
        self.assertEqual(self.on_disk()["rounds_used"], 1)
        self.assertEqual(self.lock_status(), "absent")


# ---------------------------------------------------------------------------
# Cluster 4: campaign CAS and the concurrent proposal loser


class CampaignCasTests(RemediationFixture):
    def test_two_proposals_from_one_source_cannot_both_commit(self) -> None:
        first = self.prepare()
        self.assertEqual(first["outcome"], "remediation_prepared")
        second = self.prepare()
        self.assertEqual(second["outcome"], "remediation_prepared")

        winner = self.finalize(packet_path=first["packet_path"])
        self.assertEqual(winner["outcome"], "remediation_completed")
        self.assertEqual(self.on_disk()["rounds_used"], 1)
        self.assertEqual(self.remote_head(), self.h1)

        loser = self.finalize(packet_path=second["packet_path"])
        self.assertEqual(loser["outcome"], "remediation_stale")
        self.assertEqual(self.on_disk()["rounds_used"], 1)
        self.assertEqual(self.lock_status(), "absent")
        self.assertEqual(self.remote_head(), self.h1)

    def test_round_commitment_changes_the_source_witness(self) -> None:
        self.prepare()
        before = model.campaign_source_digest(self.on_disk())
        packet = self.packet()
        self.assertEqual(packet["source_digest"], before)
        result = self.finalize()
        self.assertEqual(result["outcome"], "remediation_completed")
        after = model.campaign_source_digest(self.on_disk())
        self.assertNotEqual(before, after)


# ---------------------------------------------------------------------------
# Cluster 5: complete-tree binding and hook safety


class TreeBindingTests(RemediationFixture):
    def test_publication_carries_the_complete_non_ignored_delta(self) -> None:
        self.prepare()
        (self.worktree / "f.txt").write_text("v2\n", encoding="utf-8")
        (self.worktree / "new.txt").write_text("added\n", encoding="utf-8")
        (self.worktree / "ignored.log").write_text("noise\n", encoding="utf-8")
        result = self.finalize(
            proposal_text=proposal_json(
                disposition("T1", "fix_now", mode=externalize.FIX_NOW_PROSPECTIVE)
            )
        )
        self.assertEqual(result["outcome"], "remediation_completed")
        published = self.remote_head()
        self.assertNotEqual(published, self.h1)
        names = git(
            self.clone, "ls-tree", "-r", "--name-only", published
        ).stdout.split()
        self.assertIn("f.txt", names)
        self.assertIn("new.txt", names)
        self.assertNotIn("ignored.log", names)

    def test_no_publication_proposal_requires_clean_worktree(self) -> None:
        self.prepare()
        (self.worktree / "f.txt").write_text("v2\n", encoding="utf-8")
        result = self.finalize(
            proposal_text=proposal_json(disposition("T1", "no_fix_required"))
        )
        self.assertEqual(result["outcome"], "remediation_refused")
        self.assertIn("no disposition", result["reason"])
        self.assertFalse(result["round_committed"])
        self.assertEqual(self.remote_head(), self.h1)
        self.assertEqual(self.lock_status(), "absent")

    def test_publication_requires_non_empty_delta(self) -> None:
        self.prepare()
        result = self.finalize(
            proposal_text=proposal_json(
                disposition("T1", "fix_now", mode=externalize.FIX_NOW_PROSPECTIVE)
            )
        )
        self.assertEqual(result["outcome"], "remediation_refused")
        self.assertIn("empty", result["reason"])
        self.assertFalse(result["round_committed"])
        self.assertEqual(self.remote_head(), self.h1)

    def test_arbitrary_git_hooks_never_run_during_authoritative_commit(self) -> None:
        hooks = self.clone / "configured-hooks"
        hooks.mkdir()
        (hooks / "pre-commit").write_text(
            "#!/bin/sh\ntouch HOOK-RAN\nexit 1\n", encoding="utf-8"
        )
        git(self.clone, "config", "core.hooksPath", str(hooks))
        self.prepare()
        (self.worktree / "f.txt").write_text("v2\n", encoding="utf-8")
        result = self.finalize(
            proposal_text=proposal_json(
                disposition("T1", "fix_now", mode=externalize.FIX_NOW_PROSPECTIVE)
            )
        )
        self.assertEqual(result["outcome"], "remediation_completed")
        self.assertFalse((self.clone / "HOOK-RAN").exists())
        self.assertFalse((self.worktree / "HOOK-RAN").exists())

    def test_conflict_state_refuses_publication(self) -> None:
        self.prepare()
        # Simulate unsupported repository state in the worktree.
        git_dir = git(self.worktree, "rev-parse", "--git-dir").stdout.strip()
        merge_head = Path(git_dir)
        if not merge_head.is_absolute():
            merge_head = self.worktree / merge_head
        (merge_head / "MERGE_HEAD").write_text("x\n", encoding="utf-8")
        (self.worktree / "f.txt").write_text("v2\n", encoding="utf-8")
        result = self.finalize(
            proposal_text=proposal_json(
                disposition("T1", "fix_now", mode=externalize.FIX_NOW_PROSPECTIVE)
            )
        )
        self.assertEqual(result["outcome"], "remediation_refused")
        self.assertIn("conflict state", result["reason"])
        self.assertFalse(result["round_committed"])

    def test_already_present_fix_now_resolves_without_publication(self) -> None:
        self.prepare()
        result = self.finalize(
            proposal_text=proposal_json(
                disposition("T1", "fix_now", mode=externalize.FIX_NOW_ALREADY_PRESENT)
            )
        )
        self.assertEqual(result["outcome"], "remediation_completed")
        self.assertIsNone(result["published_head"])
        self.assertEqual(self.remote_head(), self.h1)
        self.assertEqual(result["resolutions"]["T1"]["classification"], "confirmed_success")


# ---------------------------------------------------------------------------
# Cluster 6: commitment point and ordered mutation


class CommitmentOrderTests(RemediationFixture):
    def test_round_commitment_precedes_every_external_mutation(self) -> None:
        self.prepare(threads=[raw_thread("T1"), raw_thread("T2")])
        (self.worktree / "f.txt").write_text("v2\n", encoding="utf-8")
        events: list[str] = []
        real_commit = storage.apply_campaign_transition_if_current

        def recording_commit(*args, **kwargs):
            events.append("round_commitment")
            return real_commit(*args, **kwargs)

        real_push = gitlocal.push_publication_commit

        def recording_push(**kwargs):
            events.append("push")
            return real_push(**kwargs)

        def recording_issue(title: str, body: str) -> str:
            events.append("issue")
            return self._create_issue(title, body)

        resolved, _resolve_probe = self.resolving()

        def recording_resolve(thread_id: str) -> dict:
            events.append(f"resolve:{thread_id}")
            resolved.append(thread_id)
            return {"id": thread_id, "isResolved": True}

        original_commit = storage.apply_campaign_transition_if_current
        original_push = gitlocal.push_publication_commit
        storage.apply_campaign_transition_if_current = recording_commit
        gitlocal.push_publication_commit = recording_push
        try:
            result = self.finalize(
                proposal_text=proposal_json(
                    disposition("T1", "fix_now", mode=externalize.FIX_NOW_PROSPECTIVE),
                    disposition("T2", "fix_later"),
                ),
                resolve_call=recording_resolve,
                create_issue=recording_issue,
            )
        finally:
            storage.apply_campaign_transition_if_current = original_commit
            gitlocal.push_publication_commit = original_push

        self.assertEqual(
            result["outcome"], "remediation_completed", json.dumps(result, indent=2)
        )
        self.assertEqual(
            events,
            ["round_commitment", "push", "issue", "resolve:T1", "resolve:T2"],
        )
        self.assertEqual(resolved, ["T1", "T2"])
        # Exactly one commit and one push: the remote carries one new commit.
        count = git(
            self.clone, "rev-list", "--count", f"{self.h1}..{self.remote_head()}"
        ).stdout.strip()
        self.assertEqual(count, "1")

    def test_issue_confirmed_before_dependent_resolution(self) -> None:
        self.prepare(threads=[raw_thread("T1")])
        resolved, resolve_call = self.resolving()
        result = self.finalize(
            proposal_text=proposal_json(disposition("T1", "fix_later")),
            resolve_call=resolve_call,
        )
        self.assertEqual(result["outcome"], "remediation_completed")
        self.assertEqual(
            result["issues"]["T1"]["classification"], "confirmed_success"
        )
        self.assertEqual(resolved, ["T1"])


# ---------------------------------------------------------------------------
# Cluster 7: definitive versus ambiguous failure


class DefinitiveFailureTests(RemediationFixture):
    def test_definitive_publication_failure_stops_the_external_suffix(self) -> None:
        self.prepare(threads=[raw_thread("T1"), raw_thread("T2")])
        (self.worktree / "f.txt").write_text("v2\n", encoding="utf-8")

        def failing_push(**kwargs):
            return {
                "published": False,
                "status": "push_failed_clean",
                "local_commit": "local-1",
                "remote_head": self.h1,
            }

        created: list[str] = []
        resolved, resolve_call = self.resolving()
        original_push = gitlocal.push_publication_commit
        gitlocal.push_publication_commit = failing_push
        try:
            result = self.finalize(
                proposal_text=proposal_json(
                    disposition("T1", "fix_now", mode=externalize.FIX_NOW_PROSPECTIVE),
                    disposition("T2", "fix_later"),
                ),
                resolve_call=resolve_call,
                create_issue=lambda t, b: (created.append(t) or "url"),
            )
        finally:
            gitlocal.push_publication_commit = original_push
        self.assertEqual(result["outcome"], "remediation_failed_definitive")
        self.assertTrue(result["round_committed"])
        self.assertEqual(self.on_disk()["rounds_used"], 1)
        self.assertEqual(self.on_disk()["status"], model.ACTIVE)
        self.assertEqual(created, [])
        self.assertEqual(resolved, [])
        self.assertEqual(result["ownership"], "released")
        self.assertEqual(self.lock_status(), "absent")

    def test_direct_exhaustion_applies_after_definitive_final_failure(self) -> None:
        # One allowed round; a definitive publication failure still leaves the
        # campaign durably exhausted, not indefinitely active.
        record = self.on_disk()
        record["config"]["max_rounds"] = 1
        storage.save_json(self.campaign_path(), record)
        self.threads = [raw_thread("T1")]
        outcome = self.prepare()
        self.assertEqual(outcome["outcome"], "remediation_prepared")
        (self.worktree / "f.txt").write_text("v2\n", encoding="utf-8")

        original_push = gitlocal.push_publication_commit

        def failing_push(**kwargs):
            return {
                "published": False,
                "status": "push_failed_clean",
                "local_commit": "local-1",
                "remote_head": self.h1,
            }

        gitlocal.push_publication_commit = failing_push
        try:
            result = self.finalize(
                proposal_text=proposal_json(
                    disposition("T1", "fix_now", mode=externalize.FIX_NOW_PROSPECTIVE)
                ),
                resolve_call=lambda tid: {"id": tid, "isResolved": True},
            )
        finally:
            gitlocal.push_publication_commit = original_push
        self.assertEqual(result["outcome"], "remediation_failed_definitive")
        self.assertTrue(result.get("exhaustion_finalized"))
        self.assertEqual(result["ownership"], "released")
        self.assertTrue(result["scheduler_cleanup_authorized"])
        self.assertEqual(self.on_disk()["status"], model.ROUNDS_EXHAUSTED)
        self.assertEqual(self.lock_status(), "absent")

    def test_confirmed_prefix_is_preserved_when_suffix_stops(self) -> None:
        self.prepare(threads=[raw_thread("T1"), raw_thread("T2")])
        resolved, resolve_call = self.resolving()
        calls = {"n": 0}

        def second_resolution_fails(thread_id: str) -> dict:
            calls["n"] += 1
            if thread_id == "T2":
                raise github_api.GithubRejectionError("GitHub GraphQL errors: denied")
            return {"id": thread_id, "isResolved": True}

        result = self.finalize(
            proposal_text=proposal_json(
                disposition("T1", "no_fix_required"),
                disposition("T2", "no_fix_required"),
            ),
            resolve_call=second_resolution_fails,
        )
        self.assertEqual(result["outcome"], "remediation_failed_definitive")
        # The confirmed prefix remains authoritative and is not repeated.
        self.assertEqual(
            result["resolutions"]["T1"]["classification"], "confirmed_success"
        )
        self.assertEqual(
            result["resolutions"]["T2"]["classification"], "definitive_failure"
        )
        self.assertTrue(result["round_committed"])
        self.assertEqual(self.on_disk()["rounds_used"], 1)
        self.assertEqual(result["ownership"], "released")


class AmbiguousFailureTests(RemediationFixture):
    def test_ambiguous_issue_creation_stops_suffix_and_retains_ownership(self) -> None:
        self.prepare(threads=[raw_thread("T1"), raw_thread("T2")])
        resolved, resolve_call = self.resolving()

        def failing_issue(title: str, body: str) -> str:
            raise RuntimeError("connection reset")

        result = self.finalize(
            proposal_text=proposal_json(
                disposition("T1", "no_fix_required"),
                disposition("T2", "fix_later"),
            ),
            create_issue=failing_issue,
            resolve_call=resolve_call,
        )
        self.assertEqual(result["outcome"], "remediation_failed_ambiguous")
        self.assertTrue(result["round_committed"])
        self.assertEqual(result["ownership"], "retained")
        self.assertFalse(result["scheduler_cleanup_authorized"])
        self.assertEqual(self.on_disk()["rounds_used"], 1)
        self.assertEqual(self.on_disk()["status"], model.ACTIVE)
        self.assertEqual(self.lock_status(), "active")
        self.assertEqual(resolved, [])

    def test_ambiguity_is_never_overwritten_by_exhaustion(self) -> None:
        record = self.on_disk()
        record["config"]["max_rounds"] = 1
        storage.save_json(self.campaign_path(), record)
        self.threads = [raw_thread("T1")]
        self.prepare()

        def failing_resolve(thread_id: str) -> dict:
            raise RuntimeError("network gone")

        result = self.finalize(
            proposal_text=proposal_json(disposition("T1", "no_fix_required")),
            resolve_call=failing_resolve,
        )
        self.assertEqual(result["outcome"], "remediation_failed_ambiguous")
        self.assertEqual(self.on_disk()["status"], model.ACTIVE)
        self.assertEqual(self.on_disk()["rounds_used"], 1)
        self.assertEqual(self.lock_status(), "active")
        self.assertFalse(result["scheduler_cleanup_authorized"])


# ---------------------------------------------------------------------------
# Cluster 8: lost finalizer result


class LostResultTests(RemediationFixture):
    def test_completed_transaction_survives_lost_stdout(self) -> None:
        self.prepare(threads=[raw_thread("T1")])
        (self.worktree / "f.txt").write_text("v2\n", encoding="utf-8")
        # The incident class: the finalizer completed but its structured
        # result was never received, parsed, retained, or understood by the
        # calling model. Discard the informational result entirely.
        _ = self.finalize(
            proposal_text=proposal_json(
                disposition("T1", "fix_now", mode=externalize.FIX_NOW_PROSPECTIVE)
            )
        )
        # The authoritative facts are already durable and disposed:
        self.assertNotEqual(self.remote_head(), self.h1)
        self.assertEqual(self.on_disk()["rounds_used"], 1)
        self.assertEqual(self.on_disk()["status"], model.ACTIVE)
        self.assertEqual(self.lock_status(), "absent")
        self.assertFalse(self.worktree.exists())

    def test_final_exhaustion_survives_lost_stdout(self) -> None:
        record = self.on_disk()
        record["config"]["max_rounds"] = 1
        storage.save_json(self.campaign_path(), record)
        self.threads = [raw_thread("T1")]
        self.prepare()
        _ = self.finalize(
            proposal_text=proposal_json(disposition("T1", "no_fix_required"))
        )
        self.assertEqual(self.on_disk()["status"], model.ROUNDS_EXHAUSTED)
        self.assertEqual(self.lock_status(), "absent")

    def test_finalizer_cli_reports_a_pre_mutation_refusal_without_network(self) -> None:
        # CLI glue: a malformed proposal is refused from packet/proposal
        # validation alone, before acquisition or any GitHub transport, and
        # the subprocess reports a structured, closed result.
        self.prepare()
        proposal = self.clone / "proposal.json"
        proposal.write_text("{not json", encoding="utf-8")
        process = subprocess.run(
            [
                sys.executable, str(SCRIPTS / "remediation.py"), "finalize",
                "--repo", "owner/repo", "--pr", "7",
                "--repository-path", str(self.clone),
                "--packet", str(self.packet_path),
                "--proposal-file", str(proposal),
            ],
            cwd=str(self.clone),
            capture_output=True,
            text=True,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        result = json.loads(process.stdout)
        self.assertEqual(result["outcome"], "remediation_refused")
        self.assertEqual(result["ownership"], "not_acquired")
        self.assertFalse(result["round_committed"])
        self.assertEqual(self.lock_status(), "absent")


if __name__ == "__main__":
    unittest.main()
