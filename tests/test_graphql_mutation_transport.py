from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "codex-review-pulse" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import campaign_model as m  # noqa: E402
import externalize  # noqa: E402
import github_api  # noqa: E402
import review_request  # noqa: E402
import storage  # noqa: E402


REQUEST_ERROR_STDOUT = '{"errors": [{"message": "syntax error, unexpected IDENT"}]}'
SUCCESS_ADD_COMMENT_STDOUT = (
    '{"data": {"addComment": {"commentEdge": {"node": {'
    '"id": "C1", "createdAt": "2026-09-14T12:00:00Z", '
    '"url": "https://example.test/c1", "body": "@codex review"}}}}}'
)
SUCCESS_RESOLVE_STDOUT = (
    '{"data": {"resolveReviewThread": {"thread": {"id": "T1", "isResolved": true}}}}'
)
FORBIDDEN_STDOUT = (
    '{"data": {"addComment": null}, "errors": [{"type": "FORBIDDEN", '
    '"path": ["addComment"], "message": "Resource not accessible by integration"}]}'
)


def fake_process(*, returncode: int, stdout: str, stderr: str = "") -> SimpleNamespace:
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def run_mutation(stdout: str, *, returncode: int = 0, stderr: str = "") -> dict:
    with gh_only_fake(returncode=returncode, stdout=stdout, stderr=stderr) as mocked:
        try:
            return github_api.graphql_mutation("mutation { noop }")
        finally:
            assert mocked.call_count == 1


def gh_only_fake(*, returncode: int, stdout: str, stderr: str = ""):
    from unittest.mock import MagicMock

    real_run = github_api.subprocess.run
    calls: list = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if (
            isinstance(command, list)
            and len(command) >= 3
            and command[:3] == ["gh", "api", "graphql"]
        ):
            return fake_process(returncode=returncode, stdout=stdout, stderr=stderr)
        return real_run(command, **kwargs)

    mocked = MagicMock(side_effect=fake_run, wraps=fake_run)
    return patch.object(github_api.subprocess, "run", mocked)


def assert_ordinary_runtime_error(test: unittest.TestCase, func) -> Exception:
    with test.assertRaises(RuntimeError) as caught:
        func()
    test.assertNotIsInstance(caught.exception, github_api.GithubRejectionError)
    return caught.exception


class PreExecutionRejectionTests(unittest.TestCase):
    def test_request_error_allows_json_whitespace_and_valid_locations(self) -> None:
        stdout = '\n {"errors":[{"message":"bad","locations":[{"line":1,"column":2}]}]}\t\n'
        for returncode in (0, 1):
            with self.subTest(returncode=returncode):
                with self.assertRaises(github_api.GithubRejectionError):
                    run_mutation(stdout, returncode=returncode)

    def test_nonzero_exit_with_request_error_is_proven_rejection(self) -> None:
        with gh_only_fake(
            returncode=1, stdout=REQUEST_ERROR_STDOUT, stderr="gh: error"
        ) as mocked:
            with self.assertRaises(github_api.GithubRejectionError) as caught:
                github_api.graphql_mutation("mutation { noop }")
        self.assertEqual(mocked.call_count, 1)
        self.assertIn("syntax error", str(caught.exception))

    def test_zero_exit_with_same_request_error_is_proven_rejection(self) -> None:
        with gh_only_fake(returncode=0, stdout=REQUEST_ERROR_STDOUT):
            with self.assertRaises(github_api.GithubRejectionError):
                github_api.graphql_mutation("mutation { noop }")

    def test_request_error_stdout_has_no_data_key(self) -> None:
        self.assertNotIn("data", json.loads(REQUEST_ERROR_STDOUT))


class TransportUncertaintyTests(unittest.TestCase):
    def test_nonzero_exit_empty_stdout_is_ambiguous(self) -> None:
        assert_ordinary_runtime_error(
            self, lambda: run_mutation("", returncode=1, stderr="boom")
        )

    def test_zero_exit_empty_stdout_is_ambiguous(self) -> None:
        assert_ordinary_runtime_error(self, lambda: run_mutation("", returncode=0))

    def test_malformed_stdout_is_ambiguous(self) -> None:
        assert_ordinary_runtime_error(
            self, lambda: run_mutation("not json{{{", returncode=1)
        )
        assert_ordinary_runtime_error(
            self, lambda: run_mutation("not json{{{", returncode=0)
        )

    def test_nonzero_exit_success_looking_json_is_ambiguous(self) -> None:
        assert_ordinary_runtime_error(
            self, lambda: run_mutation(SUCCESS_ADD_COMMENT_STDOUT, returncode=1)
        )

    def test_zero_exit_success_is_returned_normally(self) -> None:
        payload = run_mutation(SUCCESS_ADD_COMMENT_STDOUT, returncode=0)
        self.assertIn("data", payload)

    def test_trailing_garbage_is_ambiguous(self) -> None:
        assert_ordinary_runtime_error(
            self, lambda: run_mutation(REQUEST_ERROR_STDOUT + " garbage", returncode=0)
        )
        assert_ordinary_runtime_error(
            self, lambda: run_mutation(REQUEST_ERROR_STDOUT + " garbage", returncode=1)
        )

    def test_concatenated_documents_are_ambiguous(self) -> None:
        joined = REQUEST_ERROR_STDOUT + '{"data": null}'
        assert_ordinary_runtime_error(self, lambda: run_mutation(joined, returncode=0))
        assert_ordinary_runtime_error(self, lambda: run_mutation(joined, returncode=1))

    def test_unexpected_root_is_ambiguous(self) -> None:
        for stdout in ("[1, 2]", '"text"', "42", "null"):
            with self.subTest(stdout=stdout):
                assert_ordinary_runtime_error(
                    self, lambda s=stdout: run_mutation(s, returncode=0)
                )

    def test_stderr_forbidden_without_payload_proof_is_ambiguous(self) -> None:
        assert_ordinary_runtime_error(
            self, lambda: run_mutation("", returncode=1, stderr="FORBIDDEN: denied")
        )
        assert_ordinary_runtime_error(
            self,
            lambda: run_mutation('{"data": null}', returncode=1, stderr="FORBIDDEN"),
        )


class ExecutionResultAmbiguityTests(unittest.TestCase):
    def test_data_null_with_errors_is_ambiguous(self) -> None:
        stdout = '{"data": null, "errors": [{"message": "bad"}]}'
        assert_ordinary_runtime_error(self, lambda: run_mutation(stdout, returncode=0))
        assert_ordinary_runtime_error(self, lambda: run_mutation(stdout, returncode=1))

    def test_partial_data_with_errors_is_ambiguous(self) -> None:
        stdout = '{"data": {"addComment": null}, "errors": [{"message": "bad"}]}'
        assert_ordinary_runtime_error(self, lambda: run_mutation(stdout, returncode=0))
        assert_ordinary_runtime_error(self, lambda: run_mutation(stdout, returncode=1))

    def test_execution_path_error_is_ambiguous(self) -> None:
        stdout = (
            '{"data": {"addComment": null}, "errors": [{'
            '"message": "example", '
            '"path": ["addComment", "commentEdge", "node", "id"]}]}'
        )
        assert_ordinary_runtime_error(self, lambda: run_mutation(stdout, returncode=0))
        assert_ordinary_runtime_error(self, lambda: run_mutation(stdout, returncode=1))

    def test_realistic_forbidden_execution_result_stays_ambiguous(self) -> None:
        assert_ordinary_runtime_error(
            self, lambda: run_mutation(FORBIDDEN_STDOUT, returncode=0)
        )
        assert_ordinary_runtime_error(
            self, lambda: run_mutation(FORBIDDEN_STDOUT, returncode=1)
        )

    def test_null_path_key_still_disproves_pre_execution(self) -> None:
        stdout = '{"errors": [{"message": "m", "path": null}]}'
        assert_ordinary_runtime_error(self, lambda: run_mutation(stdout, returncode=0))

    def test_unknown_top_level_metadata_stays_ambiguous(self) -> None:
        stdout = '{"errors": [{"message": "m"}], "extensions": {}}'
        assert_ordinary_runtime_error(self, lambda: run_mutation(stdout, returncode=0))


class InvalidErrorStructureTests(unittest.TestCase):
    def test_malformed_or_unknown_error_metadata_is_ambiguous(self) -> None:
        for metadata in (
            {"locations": "unknown"},
            {"locations": [None]},
            {"locations": [{"line": 0, "column": 1}]},
            {"locations": [{"line": True, "column": 1}]},
            {"locations": [{"line": 1}]},
            {"type": []},
            {"unexpected": {"executed": True}},
        ):
            stdout = json.dumps({"errors": [{"message": "bad", **metadata}]})
            for returncode in (0, 1):
                with self.subTest(metadata=metadata, returncode=returncode):
                    assert_ordinary_runtime_error(
                        self, lambda: run_mutation(stdout, returncode=returncode)
                    )

    def test_empty_errors_list_is_ambiguous(self) -> None:
        assert_ordinary_runtime_error(
            self, lambda: run_mutation('{"errors": []}', returncode=0)
        )

    def test_string_errors_is_ambiguous(self) -> None:
        assert_ordinary_runtime_error(
            self, lambda: run_mutation('{"errors": "forbidden"}', returncode=0)
        )

    def test_non_object_error_entry_is_ambiguous(self) -> None:
        assert_ordinary_runtime_error(
            self, lambda: run_mutation('{"errors": ["bad"]}', returncode=0)
        )
        assert_ordinary_runtime_error(
            self, lambda: run_mutation('{"errors": [42]}', returncode=0)
        )

    def test_error_missing_string_message_is_ambiguous(self) -> None:
        for stdout in (
            '{"errors": [{}]}',
            '{"errors": [{"message": 42}]}',
            '{"errors": [{"message": null}]}',
        ):
            with self.subTest(stdout=stdout):
                assert_ordinary_runtime_error(
                    self, lambda s=stdout: run_mutation(s, returncode=0)
                )


class StrictParsingTests(unittest.TestCase):
    def test_duplicate_top_level_errors_key_is_rejected(self) -> None:
        stdout = '{"errors": [{"message": "a"}], "errors": [{"message": "b"}]}'
        assert_ordinary_runtime_error(self, lambda: run_mutation(stdout, returncode=0))
        assert_ordinary_runtime_error(self, lambda: run_mutation(stdout, returncode=1))

    def test_duplicate_nested_key_is_rejected(self) -> None:
        stdout = '{"errors": [{"message": "a", "message": "b"}]}'
        assert_ordinary_runtime_error(self, lambda: run_mutation(stdout, returncode=0))

    def test_duplicate_data_key_is_rejected(self) -> None:
        stdout = '{"data": null, "data": null, "errors": [{"message": "m"}]}'
        assert_ordinary_runtime_error(self, lambda: run_mutation(stdout, returncode=0))

    def test_nan_constant_is_rejected(self) -> None:
        assert_ordinary_runtime_error(
            self, lambda: run_mutation('{"data": NaN}', returncode=0)
        )
        assert_ordinary_runtime_error(
            self, lambda: run_mutation('{"data": NaN}', returncode=1)
        )

    def test_infinity_constants_are_rejected(self) -> None:
        for stdout in ('{"data": Infinity}', '{"data": -Infinity}'):
            with self.subTest(stdout=stdout):
                assert_ordinary_runtime_error(
                    self, lambda s=stdout: run_mutation(s, returncode=0)
                )
                assert_ordinary_runtime_error(
                    self, lambda s=stdout: run_mutation(s, returncode=1)
                )


class MutationWrapperTests(unittest.TestCase):
    def test_add_comment_success_through_real_wrapper(self) -> None:
        with gh_only_fake(returncode=0, stdout=SUCCESS_ADD_COMMENT_STDOUT) as mocked:
            result = github_api.add_comment("PR_1", "@codex review")
        self.assertEqual(mocked.call_count, 1)
        self.assertEqual(result["node_id"], "C1")

    def test_add_comment_rejection_through_real_wrapper(self) -> None:
        with gh_only_fake(returncode=1, stdout=REQUEST_ERROR_STDOUT):
            with self.assertRaises(github_api.GithubRejectionError):
                github_api.add_comment("PR_1", "@codex review")

    def test_add_comment_execution_error_is_ambiguous(self) -> None:
        with gh_only_fake(returncode=0, stdout=FORBIDDEN_STDOUT):
            assert_ordinary_runtime_error(
                self, lambda: github_api.add_comment("PR_1", "@codex review")
            )

    def test_resolve_thread_success_through_real_wrapper(self) -> None:
        with gh_only_fake(returncode=0, stdout=SUCCESS_RESOLVE_STDOUT) as mocked:
            result = github_api.resolve_thread("T1")
        self.assertEqual(mocked.call_count, 1)
        self.assertEqual(result, {"id": "T1", "isResolved": True})

    def test_resolve_thread_rejection_through_real_wrapper(self) -> None:
        with gh_only_fake(returncode=0, stdout=REQUEST_ERROR_STDOUT):
            with self.assertRaises(github_api.GithubRejectionError):
                github_api.resolve_thread("T1")

    def test_resolve_thread_execution_error_is_ambiguous(self) -> None:
        stdout = (
            '{"data": {"resolveReviewThread": null}, '
            '"errors": [{"message": "m", "path": ["resolveReviewThread"]}]}'
        )
        with gh_only_fake(returncode=0, stdout=stdout):
            assert_ordinary_runtime_error(self, lambda: github_api.resolve_thread("T1"))


CODEX = "chatgpt-codex-connector"
H = "head-oid"
T0 = "2026-09-14T12:00:00Z"
T1 = "2026-09-14T12:00:05Z"
T2 = "2026-09-14T12:00:10Z"
CAMPAIGN_ID = "crp-20260914T120000Z-abc123"


def snap(*, time: str, head: str = H, comments: list | None = None) -> dict:
    return {
        "complete": True, "server_time": time, "pr_state": "OPEN",
        "head_oid": head, "node_id": "pr-node-1", "viewer": "operator",
        "threads": [], "reactions": [], "reviews": [], "comments": comments or [],
    }


def git(cwd: Path, *args: str) -> None:
    process = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    if process.returncode != 0:
        raise RuntimeError(f"git {args} failed: {process.stderr.strip()}")


class OwnedRequestRepository:
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
            campaign_id=CAMPAIGN_ID, acquired_at=T0, repository_path=self.path,
        )
        self.token = acquired["owner_token"]
        storage.initialize_campaign(
            "owner/repo", 7, owner_token=self.token,
            campaign=m.new_campaign(
                campaign_id=CAMPAIGN_ID, repository="owner/repo",
                pull_request_number=7, created_at=T0, max_rounds=6,
                model="a-model", reasoning_level="medium", interval_minutes=30,
                reviewer_logins=[CODEX], approval_logins=[CODEX],
            ),
            repository_path=self.path,
        )

    def commit_request(self) -> None:
        path = storage.campaign_path("owner/repo", 7, repository_path=self.path)
        record = storage.load_json(path)
        record = m.reserve_request(record, head_oid=H, reserved_at=T0, snapshot=snap(time=T0))
        storage.save_json(path, record)

    def campaign_on_disk(self) -> dict:
        return storage.load_json(
            storage.campaign_path("owner/repo", 7, repository_path=self.path)
        )

    def lock_status(self) -> str:
        return storage.inspect_lock("owner/repo", 7, repository_path=self.path)["status"]


class HigherLevelClassificationTests(unittest.TestCase):
    def test_thread_resolution_ambiguous_classification(self) -> None:
        fixture_dir = tempfile.TemporaryDirectory()
        try:
            root = Path(fixture_dir.name)
            repo = root / "repo"
            repo.mkdir()
            git(repo, "init", "-b", "main")
            git(repo, "config", "user.email", "t@e.test")
            git(repo, "config", "user.name", "T")
            (repo / "f").write_text("x", encoding="utf-8")
            git(repo, "add", "f")
            git(repo, "commit", "-m", "init")
            acquired = storage.acquire_setup_lock(
                "owner/repo", 7,
                campaign_id=CAMPAIGN_ID, acquired_at=T0, repository_path=repo,
            )
            campaign = m.consume_round(
                m.new_campaign(
                    campaign_id=CAMPAIGN_ID, repository="owner/repo",
                    pull_request_number=7, created_at=T0, max_rounds=6,
                    model="a-model", reasoning_level="medium",
                    interval_minutes=30, reviewer_logins=[CODEX],
                    approval_logins=[CODEX],
                ),
                kind="remediation",
            )
            storage.initialize_campaign(
                "owner/repo", 7,
                owner_token=acquired["owner_token"], campaign=campaign,
                repository_path=repo,
            )
            thread_snapshot = {
                "complete": True, "server_time": T1, "repository": "owner/repo",
                "pr_number": 7, "pr_state": "OPEN", "head_oid": H,
                "head_ref_name": "feature", "head_repository": "owner/repo",
                "node_id": "PR_1", "viewer": "operator",
                "threads": [{
                    "id": "T1", "is_resolved": False, "is_outdated": False,
                    "path": "src/a.py", "root_comment_id": "T1-c1",
                    "root_login": CODEX, "body": "Fix this.",
                    "root_updated_at": T0, "url": "https://example.test/T1",
                }],
                "reactions": [], "reviews": [], "comments": [],
            }
            frozen_path = root / "frozen.json"
            frozen_path.write_text(json.dumps(thread_snapshot), encoding="utf-8")
            with gh_only_fake(returncode=0, stdout=FORBIDDEN_STDOUT):
                result = externalize.resolve_review_thread(
                    repository="owner/repo", pr_number=7,
                    owner_token=acquired["owner_token"], snapshot_path=frozen_path,
                    thread_id="T1", expected_head=H, outcome="no_fix_required",
                    campaign_id=CAMPAIGN_ID, rounds_used=1, repository_path=repo,
                    fetch_snapshot=lambda: dict(thread_snapshot),
                    remote_head_call=lambda ref: H, list_issues=lambda: [],
                    viewer_call=lambda: "operator",
                )
            self.assertEqual(result["classification"], "ambiguous")
        finally:
            fixture_dir.cleanup()


class OwnershipConsequenceChainTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.fixture = OwnedRequestRepository(Path(self._tmp.name))
        self.fixture.commit_request()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_chain(self, *, stdout: str, returncode: int) -> dict:
        sequence = [snap(time=T1), snap(time=T2)]
        with gh_only_fake(returncode=returncode, stdout=stdout) as mocked:
            result = review_request.run_committed_request(
                repository="owner/repo", pr_number=7,
                owner_token=self.fixture.token, repository_path=self.fixture.path,
                observer=lambda: sequence.pop(0),
            )
        gh_calls = [
            call for call in mocked.call_args_list
            if call.args and call.args[0][:3] == ["gh", "api", "graphql"]
        ]
        self.assertEqual(len(gh_calls), 1)
        return result

    def test_proven_rejection_releases_lock(self) -> None:
        result = self.run_chain(stdout=REQUEST_ERROR_STDOUT, returncode=1)
        self.assertEqual(result["outcome"], "creation_failed")
        self.assertEqual(result["ownership"], "released")
        self.assertEqual(self.fixture.lock_status(), "absent")

    def test_ambiguous_response_retains_lock(self) -> None:
        result = self.run_chain(stdout=FORBIDDEN_STDOUT, returncode=0)
        self.assertEqual(result["outcome"], "ambiguous")
        self.assertEqual(result["ownership"], "retained")
        self.assertEqual(self.fixture.lock_status(), "active")


if __name__ == "__main__":
    unittest.main()
