from __future__ import annotations

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
import storage  # noqa: E402


CODEX = "chatgpt-codex-connector"
VIEWER = "operator"
CAMPAIGN_ID = "crp-20260914T120000Z-abc123"
H1 = "h1-head-oid"
H2 = "h2-head-oid"
T0 = "2026-09-14T12:00:00Z"
T1 = "2026-09-14T12:05:00Z"


def git(cwd: Path, *args: str) -> None:
    process = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True
    )
    if process.returncode != 0:
        raise RuntimeError(f"git {args} failed: {process.stderr.strip()}")


def thread(
    tid: str = "T1",
    *,
    resolved: bool = False,
    path: str = "src/a.py",
    root_comment: str | None = None,
    author: str | None = CODEX,
    body: str = "Fix this boundary check.",
    updated_at: str = T0,
) -> dict:
    return {
        "id": tid,
        "is_resolved": resolved,
        "is_outdated": False,
        "path": path,
        "root_comment_id": root_comment or f"{tid}-c1",
        "root_login": author,
        "body": body,
        "root_updated_at": updated_at,
        "url": f"https://example.test/{tid}",
    }


def snapshot(
    *,
    head: str = H1,
    threads: list | None = None,
    pr_state: str = "OPEN",
    repository: str = "owner/repo",
    pr_number: int = 7,
    complete: bool = True,
) -> dict:
    return {
        "complete": complete,
        "server_time": T1,
        "repository": repository,
        "pr_number": pr_number,
        "pr_state": pr_state,
        "head_oid": head,
        "head_ref_name": "feature",
        "head_repository": repository,
        "node_id": "PR_1",
        "viewer": VIEWER,
        "threads": threads if threads is not None else [thread()],
        "reactions": [],
        "reviews": [],
        "comments": [],
    }


class OwnedRepository:
    """A git-backed repository with an active campaign that consumed one round."""

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
        campaign = model.consume_round(
            model.new_campaign(
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
            kind="remediation",
        )
        storage.initialize_campaign(
            "owner/repo", 7,
            owner_token=self.token,
            campaign=campaign,
            repository_path=self.path,
        )
        handle = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        handle.close()
        self._snapshot_file = Path(handle.name)
        self.snapshot_path = self._snapshot_file

    def write_frozen(self, snap: dict) -> Path:
        storage.save_json(self.snapshot_path, snap)
        return self.snapshot_path


class ExternalizeTests(unittest.TestCase):
    """Shared fixture: an owned repository and a frozen S1 snapshot."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.fixture = OwnedRepository(Path(self._tmp.name))
        self.frozen = snapshot()
        self.snapshot_path = self.fixture.write_frozen(self.frozen)
        self.fingerprint = externalize.deferred_issue_fingerprint(
            repository="owner/repo", pr_number=7, triage_head=H1,
            target=externalize._frozen_thread(self.frozen, "T1"),
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()
        self.fixture._snapshot_file.unlink(missing_ok=True)

    # -- shared callers -----------------------------------------------------

    def ensure_issue(self, *, created_error: Exception | None = None, **overrides):
        created: list[tuple[str, str]] = []

        def create_issue(title: str, body: str) -> str:
            created.append((title, body))
            if created_error is not None:
                raise created_error
            return "https://example.test/owner/repo/issues/12"

        kwargs = dict(
            repository="owner/repo",
            pr_number=7,
            owner_token=self.fixture.token,
            snapshot_path=self.snapshot_path,
            thread_id="T1",
            triage_head=H1,
            title="Fix later: boundary check",
            body="Deferred: the boundary check needs a follow-up.",
            campaign_id=CAMPAIGN_ID,
            rounds_used=1,
            repository_path=self.fixture.path,
            fetch_snapshot=lambda: snapshot(),
            list_issues=lambda: [],
            create_issue=create_issue,
            viewer_call=lambda: VIEWER,
        )
        kwargs.update(overrides)
        return externalize.ensure_deferred_issue(**kwargs), created

    def resolve(self, *, recheck: dict | None = None, **overrides):
        resolved: list[str] = []
        resolve_error = overrides.pop("resolve_error", None)
        current = overrides.pop("current", None)

        def resolve_call(tid: str) -> dict:
            resolved.append(tid)
            if resolve_error is not None:
                raise resolve_error
            return {"id": tid, "isResolved": True}

        validation = current or snapshot()
        recheck_snapshot = recheck or snapshot()
        fetch = overrides.pop("fetch_snapshot", None)
        if fetch is None:
            state = {"first": True}

            def fetch() -> dict:
                if state["first"]:
                    state["first"] = False
                    return validation
                return recheck_snapshot

        kwargs = dict(
            repository="owner/repo",
            pr_number=7,
            owner_token=self.fixture.token,
            snapshot_path=self.snapshot_path,
            thread_id="T1",
            expected_head=H1,
            outcome="no_fix_required",
            campaign_id=CAMPAIGN_ID,
            rounds_used=1,
            repository_path=self.fixture.path,
            fetch_snapshot=fetch,
            remote_head_call=lambda ref: H1,
            list_issues=lambda: [],
            viewer_call=lambda: VIEWER,
            resolve_call=resolve_call,
        )
        kwargs.update(overrides)
        return externalize.resolve_review_thread(**kwargs), resolved


# ---------------------------------------------------------------------------
# Frozen-evidence authority


class FrozenEvidenceAuthorityTests(ExternalizeTests):
    def test_missing_frozen_evidence_grants_no_mutation_authority(self) -> None:
        with self.assertRaises(externalize.FrozenEvidenceError):
            self.ensure_issue(snapshot_path=self.fixture.path / "missing.json")

    def test_human_author_thread_grants_no_authority(self) -> None:
        self.fixture.write_frozen(snapshot(threads=[thread(author="human-reviewer")]))
        with self.assertRaises(externalize.FrozenEvidenceError):
            self.resolve()

    def test_unknown_author_thread_grants_no_authority(self) -> None:
        self.fixture.write_frozen(snapshot(threads=[thread(author=None)]))
        with self.assertRaises(externalize.FrozenEvidenceError):
            self.resolve()

    def test_model_cannot_replace_frozen_identity_fields(self) -> None:
        # The boundary derives every identity field from the snapshot; the
        # caller supplies only thread IDs, heads, and semantic inputs.
        import inspect

        for boundary in (
            externalize.ensure_deferred_issue,
            externalize.resolve_review_thread,
        ):
            parameters = inspect.signature(boundary).parameters
            for forbidden in (
                "root_comment_id", "root_author", "root_updated_at",
                "frozen_body", "body_fingerprint", "classification",
            ):
                self.assertNotIn(forbidden, parameters, boundary.__name__)


# ---------------------------------------------------------------------------
# Ownership and handoff authority


class MutationAuthorityTests(ExternalizeTests):
    def test_wrong_owner_token_blocks_mutation(self) -> None:
        with self.assertRaises(RuntimeError):
            self.resolve(owner_token="not-the-owner")

    def test_changed_campaign_identity_blocks_mutation(self) -> None:
        with self.assertRaises(RuntimeError):
            self.resolve(campaign_id="crp-20260914T120000Z-ffffee")

    def test_changed_round_count_blocks_remediation_mutation(self) -> None:
        with self.assertRaises(RuntimeError):
            self.resolve(rounds_used=2)

    def test_terminal_campaign_blocks_ordinary_mutation(self) -> None:
        path = storage.campaign_path("owner/repo", 7, repository_path=self.fixture.path)
        record = storage.load_json(path)
        storage.save_json(
            path,
            model.terminate(record, status=model.SUCCEEDED, at=T1),
        )
        with self.assertRaises(RuntimeError):
            self.resolve()


# ---------------------------------------------------------------------------
# Fix-now submodes (tree-authority verification only)


class FixNowSubmodeTests(ExternalizeTests):
    def test_prospective_tree_submode_is_the_default(self) -> None:
        result, resolved = self.resolve(
            outcome="fix_now",
            expected_head=H2,
            current=snapshot(head=H2),
            published_commit=H2,
            remote_head_call=lambda ref: H2,
        )
        self.assertEqual(result["classification"], "confirmed_success")
        self.assertEqual(resolved, ["T1"])

    def test_already_present_submode_requires_the_prepared_head(self) -> None:
        result, resolved = self.resolve(
            outcome="fix_now",
            fix_now_mode=externalize.FIX_NOW_ALREADY_PRESENT,
            expected_head=H2,
            current=snapshot(head=H2),
            remote_head_call=lambda ref: H2,
        )
        self.assertEqual(result["classification"], "refused")
        self.assertIn("must target the prepared head", result["reason"])
        self.assertEqual(resolved, [])

    def test_already_present_submode_requires_the_remote_prepared_head(self) -> None:
        result, resolved = self.resolve(
            outcome="fix_now",
            fix_now_mode=externalize.FIX_NOW_ALREADY_PRESENT,
            remote_head_call=lambda ref: "other-oid",
        )
        self.assertEqual(result["classification"], "refused")
        self.assertIn("prepared head", result["reason"])
        self.assertEqual(resolved, [])

    def test_already_present_submode_forbids_a_publication_commit(self) -> None:
        result, resolved = self.resolve(
            outcome="fix_now",
            fix_now_mode=externalize.FIX_NOW_ALREADY_PRESENT,
            published_commit=H2,
        )
        self.assertEqual(result["classification"], "refused")
        self.assertIn("must not claim a publication commit", result["reason"])
        self.assertEqual(resolved, [])

    def test_already_present_submode_resolves_against_the_prepared_head(self) -> None:
        result, resolved = self.resolve(
            outcome="fix_now",
            fix_now_mode=externalize.FIX_NOW_ALREADY_PRESENT,
            remote_head_call=lambda ref: H1,
        )
        self.assertEqual(result["classification"], "confirmed_success")
        self.assertEqual(resolved, ["T1"])

    def test_unknown_fix_now_submode_is_a_programmer_error(self) -> None:
        with self.assertRaises(RuntimeError):
            self.resolve(
                outcome="fix_now",
                fix_now_mode="vibes_based",
                remote_head_call=lambda ref: H1,
            )

    def test_fix_now_mode_on_non_fix_now_outcome_is_a_programmer_error(self) -> None:
        with self.assertRaises(RuntimeError):
            self.resolve(
                outcome="no_fix_required",
                fix_now_mode=externalize.FIX_NOW_ALREADY_PRESENT,
            )


# ---------------------------------------------------------------------------
# Deferred-issue identity and boundary


class IssueIdentityTests(ExternalizeTests):
    def test_fingerprint_changes_with_expected_head(self) -> None:
        target = externalize._frozen_thread(self.frozen, "T1")
        first = externalize.deferred_issue_fingerprint(
            repository="owner/repo", pr_number=7, triage_head=H1, target=target
        )
        second = externalize.deferred_issue_fingerprint(
            repository="owner/repo", pr_number=7, triage_head=H2, target=target
        )
        self.assertNotEqual(first, second)
        self.assertTrue(first.startswith("crpf1-"))

    def test_fingerprint_changes_with_any_frozen_field(self) -> None:
        base = externalize._frozen_thread(self.frozen, "T1")
        reference = externalize.deferred_issue_fingerprint(
            repository="owner/repo", pr_number=7, triage_head=H1, target=base
        )
        for mutation in (
            {"root_comment_id": "other"},
            {"root_author": "someone-else"},
            {"body": "edited"},
            {"root_updated_at": "2026-09-14T23:59:59Z"},
            {"path": "src/b.py"},
            {"id": "T9"},
        ):
            changed = {**base, **mutation}
            fingerprint = externalize.deferred_issue_fingerprint(
                repository="owner/repo", pr_number=7, triage_head=H1, target=changed
            )
            self.assertNotEqual(fingerprint, reference, mutation)


class EnsureDeferredIssueTests(ExternalizeTests):
    def marker(self) -> str:
        return externalize.deferred_issue_marker("owner/repo", 7, "T1")

    def test_exact_marker_fingerprint_author_and_open_reuses(self) -> None:
        issue = {
            "number": 42,
            "url": "https://example.test/issues/42",
            "body": (
                f"text\n{self.marker()}\n"
                f"<!-- codex-review-pulse-fingerprint: {self.fingerprint} -->"
            ),
            "author": {"login": VIEWER},
        }
        result, created = self.ensure_issue(list_issues=lambda: [issue])
        self.assertEqual(result["classification"], "confirmed_success")
        self.assertEqual(result["action"], "reused")
        self.assertEqual(result["issue_number"], 42)
        self.assertEqual(created, [])

    def test_title_similarity_alone_does_not_reuse(self) -> None:
        result, created = self.ensure_issue(
            list_issues=lambda: [{
                "number": 9,
                "url": "https://example.test/issues/9",
                "body": "Deferred: the boundary check needs a follow-up.",
                "author": {"login": VIEWER},
            }],
        )
        self.assertEqual(result["action"], "created")
        self.assertEqual(len(created), 1)
        self.assertIn(self.marker(), created[0][1])
        self.assertIn(self.fingerprint, created[0][1])

    def test_wrong_fingerprint_does_not_reuse(self) -> None:
        other_head = externalize.deferred_issue_fingerprint(
            repository="owner/repo", pr_number=7, triage_head=H2,
            target=externalize._frozen_thread(self.frozen, "T1"),
        )
        result, created = self.ensure_issue(
            list_issues=lambda: [{
                "number": 9,
                "url": "https://example.test/issues/9",
                "body": f"{self.marker()}\n"
                        f"<!-- codex-review-pulse-fingerprint: {other_head} -->",
                "author": {"login": VIEWER},
            }],
        )
        self.assertEqual(result["action"], "created")

    def test_wrong_author_does_not_reuse(self) -> None:
        result, created = self.ensure_issue(
            list_issues=lambda: [{
                "number": 9,
                "url": "https://example.test/issues/9",
                "body": f"{self.marker()}\n"
                        f"<!-- codex-review-pulse-fingerprint: {self.fingerprint} -->",
                "author": {"login": "someone-else"},
            }],
        )
        self.assertEqual(result["action"], "created")

    def test_missing_author_does_not_reuse(self) -> None:
        result, created = self.ensure_issue(
            list_issues=lambda: [{
                "number": 9,
                "url": "https://example.test/issues/9",
                "body": f"{self.marker()}\n"
                        f"<!-- codex-review-pulse-fingerprint: {self.fingerprint} -->",
                "author": None,
            }],
        )
        self.assertEqual(result["action"], "created")

    def test_creation_requires_current_head_to_match_triage_head(self) -> None:
        result, created = self.ensure_issue(
            fetch_snapshot=lambda: snapshot(head=H2),
        )
        self.assertEqual(result["classification"], "refused")
        self.assertIn("head", result["reason"])
        self.assertEqual(created, [])

    def test_changed_target_evidence_blocks_issue_creation(self) -> None:
        result, created = self.ensure_issue(
            fetch_snapshot=lambda: snapshot(threads=[thread(updated_at="2026-09-15T00:00:00Z")]),
        )
        self.assertEqual(result["classification"], "refused")
        self.assertEqual(created, [])

    def test_failed_provenance_search_refuses_without_creation(self) -> None:
        def broken_list() -> list:
            raise RuntimeError("search unavailable")

        result, created = self.ensure_issue(list_issues=broken_list)
        self.assertEqual(result["classification"], "refused")
        self.assertEqual(created, [])

    def test_transport_error_with_no_visible_issue_is_ambiguous(self) -> None:
        result, created = self.ensure_issue(created_error=RuntimeError("network gone"))
        self.assertEqual(result["classification"], "ambiguous")
        self.assertEqual(result["ownership"], "retain")
        self.assertEqual(len(created), 1)

    def test_transport_error_with_visible_issue_is_confirmed_creation(self) -> None:
        issue = {
            "number": 12,
            "url": "https://example.test/issues/12",
            "body": (
                f"{self.marker()}\n"
                f"<!-- codex-review-pulse-fingerprint: {self.fingerprint} -->"
            ),
            "author": {"login": VIEWER},
        }
        state = {"first": True}

        def flaky_list() -> list:
            if state["first"]:
                state["first"] = False
                return []
            return [issue]

        result, created = self.ensure_issue(
            created_error=RuntimeError("connection reset"),
            list_issues=flaky_list,
        )
        self.assertEqual(result["classification"], "confirmed_success")
        self.assertEqual(result["action"], "created")
        self.assertEqual(result["issue_number"], 12)
        self.assertEqual(len(created), 1)

    def test_reobservation_failure_after_creation_error_is_ambiguous(self) -> None:
        state = {"count": 0}

        def flaky_list() -> list:
            state["count"] += 1
            if state["count"] == 1:
                return []
            raise RuntimeError("search down")

        result, created = self.ensure_issue(
            created_error=RuntimeError("network gone"),
            list_issues=flaky_list,
        )
        self.assertEqual(result["classification"], "ambiguous")
        self.assertEqual(result["ownership"], "retain")

    def test_final_ownership_check_precedes_creation(self) -> None:
        calls = {"ensure": 0}
        real_ensure = storage.ensure_active_campaign_owner

        def counting_ensure(*args, **kwargs):
            calls["ensure"] += 1
            if calls["ensure"] >= 2:
                raise RuntimeError("authority vanished before creation")
            return real_ensure(*args, **kwargs)

        storage.ensure_active_campaign_owner = counting_ensure
        try:
            with self.assertRaises(RuntimeError):
                self.ensure_issue()
        finally:
            storage.ensure_active_campaign_owner = real_ensure
        self.assertEqual(calls["ensure"], 2)


# ---------------------------------------------------------------------------
# Independent thread-resolution boundary


class ResolveReviewThreadTests(ExternalizeTests):
    def test_no_fix_resolution_is_confirmed(self) -> None:
        result, resolved = self.resolve()
        self.assertEqual(result["classification"], "confirmed_success")
        self.assertFalse(result["already_resolved"])
        self.assertEqual(resolved, ["T1"])

    def test_already_resolved_target_is_confirmed_without_mutation(self) -> None:
        result, resolved = self.resolve(
            current=snapshot(threads=[thread(resolved=True)]),
        )
        self.assertEqual(result["classification"], "confirmed_success")
        self.assertTrue(result["already_resolved"])
        self.assertEqual(resolved, [])

    def test_missing_target_refuses_without_mutation(self) -> None:
        result, resolved = self.resolve(current=snapshot(threads=[]))
        self.assertEqual(result["classification"], "refused")
        self.assertIn("no longer present", result["reason"])
        self.assertEqual(resolved, [])

    def test_changed_target_blocks_only_that_target(self) -> None:
        result, resolved = self.resolve(
            current=snapshot(threads=[thread(body="Edited comment.")]),
        )
        self.assertEqual(result["classification"], "refused")
        self.assertEqual(resolved, [])

    def test_missing_sibling_does_not_block_unchanged_target(self) -> None:
        # The frozen batch holds T1 and T2, but only T1 is still present on
        # the pull request. Independent resolution must still proceed.
        self.fixture.write_frozen(
            snapshot(threads=[thread("T1"), thread("T2", path="src/b.py")])
        )
        result, resolved = self.resolve()
        self.assertEqual(result["classification"], "confirmed_success")
        self.assertEqual(resolved, ["T1"])

    def test_fix_now_requires_the_confirmed_publication(self) -> None:
        result, resolved = self.resolve(
            outcome="fix_now", expected_head=H2, current=snapshot(head=H2),
        )
        self.assertEqual(result["classification"], "refused")
        self.assertIn("published commit", result["reason"])
        self.assertEqual(resolved, [])

    def test_fix_now_rejects_the_pre_publication_head_as_publication(self) -> None:
        result, resolved = self.resolve(
            outcome="fix_now",
            expected_head=H2,
            current=snapshot(head=H2),
            published_commit=H1,
        )
        self.assertEqual(result["classification"], "refused")
        self.assertIn("advance", result["reason"])
        self.assertEqual(resolved, [])

    def test_fix_now_rejects_a_mismatched_published_commit(self) -> None:
        result, resolved = self.resolve(
            outcome="fix_now",
            expected_head=H2,
            published_commit="totally-different",
        )
        self.assertEqual(result["classification"], "refused")
        self.assertEqual(resolved, [])

    def test_fix_now_rejects_when_remote_head_does_not_match(self) -> None:
        result, resolved = self.resolve(
            outcome="fix_now",
            expected_head=H2,
            published_commit=H2,
            remote_head_call=lambda ref: H1,
        )
        self.assertEqual(result["classification"], "refused")
        self.assertEqual(resolved, [])

    def test_fix_now_resolves_after_confirmed_publication(self) -> None:
        result, resolved = self.resolve(
            outcome="fix_now",
            expected_head=H2,
            current=snapshot(head=H2),
            published_commit=H2,
            remote_head_call=lambda ref: H2,
        )
        self.assertEqual(result["classification"], "confirmed_success")
        self.assertEqual(resolved, ["T1"])

    def test_fix_later_requires_the_issue_reference(self) -> None:
        result, resolved = self.resolve(outcome="fix_later")
        self.assertEqual(result["classification"], "refused")
        self.assertIn("issue", result["reason"])
        self.assertEqual(resolved, [])

    def test_fix_later_rejects_an_unproven_issue_number(self) -> None:
        result, resolved = self.resolve(outcome="fix_later", issue_number=99)
        self.assertEqual(result["classification"], "refused")
        self.assertEqual(resolved, [])

    def test_fix_later_resolves_with_a_trustworthy_issue(self) -> None:
        issues = [{
            "number": 42,
            "url": "https://example.test/issues/42",
            "body": (
                f"{externalize.deferred_issue_marker('owner/repo', 7, 'T1')}\n"
                f"<!-- codex-review-pulse-fingerprint: {self.fingerprint} -->"
            ),
            "author": {"login": VIEWER},
        }]
        result, resolved = self.resolve(
            outcome="fix_later", issue_number=42, list_issues=lambda: issues,
        )
        self.assertEqual(result["classification"], "confirmed_success")
        self.assertEqual(resolved, ["T1"])

    def test_server_rejection_with_unresolved_target_is_definitive_failure(self) -> None:
        result, resolved = self.resolve(
            resolve_error=externalize.github_api.GithubRejectionError(
                "GitHub GraphQL errors: no"
            ),
        )
        self.assertEqual(result["classification"], "definitive_failure")
        self.assertEqual(resolved, ["T1"])

    def test_transport_error_with_unresolved_target_is_ambiguous(self) -> None:
        result, resolved = self.resolve(resolve_error=RuntimeError("network gone"))
        self.assertEqual(result["classification"], "ambiguous")
        self.assertEqual(result["ownership"], "retain")
        self.assertEqual(resolved, ["T1"])

    def test_transport_error_with_resolved_target_is_confirmed(self) -> None:
        result, resolved = self.resolve(
            resolve_error=RuntimeError("network gone"),
            recheck=snapshot(threads=[thread(resolved=True)]),
        )
        self.assertEqual(result["classification"], "confirmed_success")
        self.assertEqual(resolved, ["T1"])

    def test_failed_reobservation_after_error_is_ambiguous(self) -> None:
        state = {"first": True}

        def broken_fetch() -> dict:
            if state["first"]:
                state["first"] = False
                return snapshot()
            raise RuntimeError("network down")

        result, resolved = self.resolve(
            resolve_error=RuntimeError("network gone"),
            fetch_snapshot=broken_fetch,
        )
        self.assertEqual(result["classification"], "ambiguous")
        self.assertEqual(result["ownership"], "retain")

    def test_unknown_outcome_is_refused(self) -> None:
        with self.assertRaises(RuntimeError):
            self.resolve(outcome="maybe_someday")


# ---------------------------------------------------------------------------
# Caller target-selection refusals (never authority failures)


class TargetSelectionRefusalTests(ExternalizeTests):
    """Caller target-selection mistakes refuse pre-mutation, structured.

    Incident context: a worker once selected a thread outside the committed
    batch and received an unstructured FrozenEvidenceError, treating a target
    mistake as an unrecoverable authority failure and retaining the lock
    forever. A structured refusal proves no mutation began. (The batch
    membership itself is now enforced one-for-one by the deterministic
    remediation finalizer; see test_remediation.py.)
    """

    def test_outside_batch_issue_target_refuses_without_creation(self) -> None:
        result, created = self.ensure_issue(thread_id="T9")
        self.assertEqual(result["classification"], "refused")
        self.assertIn("not part of the committed batch", result["reason"])
        self.assertEqual(created, [])

    def test_outside_batch_resolve_target_refuses_without_resolution(self) -> None:
        result, resolved = self.resolve(thread_id="T9")
        self.assertEqual(result["classification"], "refused")
        self.assertIn("not part of the committed batch", result["reason"])
        self.assertEqual(resolved, [])

    def test_missing_frozen_snapshot_remains_fail_closed(self) -> None:
        with self.assertRaises(externalize.FrozenEvidenceError):
            self.ensure_issue(snapshot_path=self.fixture.path / "missing.json")

    def test_incomplete_frozen_snapshot_remains_fail_closed(self) -> None:
        broken = self.frozen
        broken["complete"] = False
        self.fixture.write_frozen(broken)
        with self.assertRaises(externalize.FrozenEvidenceError):
            self.resolve()

    def test_foreign_frozen_snapshot_remains_fail_closed(self) -> None:
        self.fixture.write_frozen(snapshot(repository="other/repo"))
        with self.assertRaises(externalize.FrozenEvidenceError):
            self.resolve()
        with self.assertRaises(externalize.FrozenEvidenceError):
            self.ensure_issue()

    def test_malformed_frozen_identity_remains_fail_closed(self) -> None:
        broken = thread()
        del broken["root_comment_id"]
        self.fixture.write_frozen(snapshot(threads=[broken]))
        with self.assertRaises(externalize.FrozenEvidenceError):
            self.resolve()

    def test_resolved_record_inside_batch_is_an_invariant_failure(self) -> None:
        # A repaired batch projection never contains a resolved record; finding
        # one is corrupt frozen evidence, not a caller typo.
        self.fixture.write_frozen(snapshot(threads=[thread(resolved=True)]))
        with self.assertRaises(externalize.FrozenEvidenceError):
            self.resolve()

    def test_non_applicable_author_inside_batch_is_an_invariant_failure(self) -> None:
        self.fixture.write_frozen(snapshot(threads=[thread(author="human-reviewer")]))
        with self.assertRaises(externalize.FrozenEvidenceError):
            self.ensure_issue()

    def test_invalid_outcome_stays_a_programmer_error(self) -> None:
        with self.assertRaises(RuntimeError):
            self.resolve(outcome="maybe_someday")

    def test_freshly_resolved_target_follows_already_resolved_behavior(self) -> None:
        result, resolved = self.resolve(
            current=snapshot(threads=[thread(resolved=True)]),
        )
        self.assertEqual(result["classification"], "confirmed_success")
        self.assertTrue(result["already_resolved"])
        self.assertEqual(resolved, [])


# ---------------------------------------------------------------------------
# No model-visible CLI remains on the internal mutation library


class NoCliTests(unittest.TestCase):
    """The legacy model-sequenced externalization CLI is removed.

    Incident context: an argparse defect once rejected every documented
    externalization command and pushed a worker into bypassing the CLI via
    direct imports. Phase 1 removed the model-visible CLI entirely: the
    deterministic remediation finalizer is the single authoritative path and
    calls these helpers as internal library boundaries.
    """

    def test_externalize_defines_no_parser_or_publish_boundary(self) -> None:
        import externalize

        self.assertFalse(hasattr(externalize, "build_parser"))
        self.assertFalse(hasattr(externalize, "publish_fix_now"))
        source = (
            Path(externalize.__file__).read_text(encoding="utf-8")
        )
        self.assertNotIn("add_parser", source)
        self.assertNotIn("def main(", source)

    def test_externalize_selftest_entrypoint_still_works(self) -> None:
        process = subprocess.run(
            [sys.executable, str(SCRIPTS / "externalize.py"), "--selftest"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(process.stdout.strip(), "externalize ok")


# ---------------------------------------------------------------------------
# Legacy raw mutation surfaces stay closed


class LegacySurfaceTests(unittest.TestCase):
    ROOT = Path(__file__).resolve().parents[1]
    SCRIPTS = ROOT / "skills" / "codex-review-pulse" / "scripts"

    def test_gitlocal_cli_no_longer_exposes_raw_publish(self) -> None:
        source = (self.SCRIPTS / "gitlocal.py").read_text(encoding="utf-8")
        self.assertNotIn('"publish"', source)

    def test_github_api_cli_no_longer_exposes_raw_product_mutations(self) -> None:
        source = (self.SCRIPTS / "github_api.py").read_text(encoding="utf-8")
        for subcommand in ('"resolve-thread"', '"ensure-issue"'):
            self.assertNotIn(subcommand, source)

    def test_legacy_helper_functions_are_gone(self) -> None:
        source = (self.SCRIPTS / "github_api.py").read_text(encoding="utf-8")
        for name in (
            "def verify_and_resolve_thread",
            "def ensure_deferred_issue",
            "def fetch_all_thread_heads",
        ):
            self.assertNotIn(name, source)


if __name__ == "__main__":
    unittest.main()

