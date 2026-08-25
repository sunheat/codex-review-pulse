from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "codex-review-pulse" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from checkpoint_store import save_checkpoint  # noqa: E402
from manage_pilot_install import MANIFEST_NAME, installation_path  # noqa: E402
from pilot_preflight import build_preflight  # noqa: E402
from state_model import empty_checkpoint  # noqa: E402


EXPECTED_COMMIT = "a" * 40


def command_runner(command: list[str]) -> subprocess.CompletedProcess[str]:
    if command[:3] == ["gh", "auth", "status"]:
        return subprocess.CompletedProcess(command, 0, "authenticated\n", "")
    if "--is-inside-work-tree" in command:
        return subprocess.CompletedProcess(command, 0, "true\n", "")
    if command[-2:] == ["rev-parse", "HEAD"]:
        return subprocess.CompletedProcess(command, 0, "HEAD1\n", "")
    if "status" in command and "--porcelain=v1" in command:
        return subprocess.CompletedProcess(command, 0, "", "")
    if command[-4:] == ["remote", "get-url", "--all", "origin"]:
        return subprocess.CompletedProcess(
            command, 0, "https://github.com/Owner/Repo.git\n", ""
        )
    if command[-5:] == ["remote", "get-url", "--push", "--all", "origin"]:
        return subprocess.CompletedProcess(
            command, 0, "https://github.com/Owner/Repo.git\n", ""
        )
    return subprocess.CompletedProcess(command, 0, "version 1\n", "")


def which(name: str) -> str:
    return f"C:/tools/{name}.exe"


def create_installation(root: Path, *, commit: str = EXPECTED_COMMIT, version: str = "0.3.0") -> None:
    target = installation_path(root)
    target.mkdir(parents=True)
    content = b"---\nname: codex-review-pulse\ndescription: Test.\n---\n"
    (target / "SKILL.md").write_bytes(content)
    manifest = {
        "schema_version": 1,
        "skill_name": "codex-review-pulse",
        "skill_version": version,
        "source_commit": commit,
        "source_repository": "C:/source",
        "files": {"SKILL.md": hashlib.sha256(content).hexdigest()},
    }
    (target / MANIFEST_NAME).write_text(
        json.dumps(manifest), encoding="utf-8"
    )


class FakeSnapshot:
    def __init__(self, *, mixed_head: bool = False) -> None:
        self.meta_calls = 0
        self.mixed_head = mixed_head
        self.calls: list[str] = []

    def __call__(
        self, query: str, owner: str, repo: str, number: int, cursor: str | None
    ) -> dict:
        self.calls.append(query)
        if "nameWithOwner" in query:
            self.meta_calls += 1
            head = "HEAD1"
            if self.mixed_head and self.meta_calls == 2:
                head = "HEAD2"
            return {
                "data": {
                    "repository": {
                        "nameWithOwner": "Owner/Repo",
                        "pullRequest": {
                            "number": 17,
                            "url": "https://github.com/Owner/Repo/pull/17",
                            "title": "Pilot",
                            "state": "OPEN",
                            "isDraft": False,
                            "headRefOid": head,
                            "headRepository": {"nameWithOwner": "Owner/Repo"},
                        },
                    }
                }
            }
        if "reviewThreads(" in query:
            connection_name = "reviewThreads"
            nodes = []
        elif "reactions(" in query:
            connection_name = "reactions"
            nodes = []
        elif "reviews(" in query:
            connection_name = "reviews"
            nodes = []
        else:
            raise AssertionError("Unexpected GraphQL query")
        return {
            "data": {
                "repository": {
                    "pullRequest": {
                        connection_name: {
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "nodes": nodes,
                        }
                    }
                }
            }
        }


def run_preflight(
    directory: Path,
    *,
    graphql_call=None,
    runner=command_runner,
    state_file: Path | None = None,
) -> dict:
    install_root = directory / "installed"
    create_installation(install_root)
    return build_preflight(
        repository="Owner/Repo",
        pr_number=17,
        repository_path=directory,
        reviewer_logins=None,
        approval_logins=None,
        state_file=state_file or directory / "state.json",
        install_root=install_root,
        expected_skill_version="0.3.0",
        expected_source_commit=EXPECTED_COMMIT,
        single_runner_confirmed=True,
        runtime_skill_path=installation_path(install_root),
        command_runner=runner,
        which=which,
        graphql_call=graphql_call or FakeSnapshot(),
    )


class PilotPreflightTests(unittest.TestCase):
    def test_preflight_does_not_modify_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory_name:
            directory = Path(directory_name)
            state_file = directory / "state.json"
            save_checkpoint(state_file, empty_checkpoint("Owner/Repo", 17))
            before = state_file.read_bytes()
            before_mtime = state_file.stat().st_mtime_ns
            result = run_preflight(directory, state_file=state_file)
            self.assertTrue(result["ready_for_supervised_pilot"])
            self.assertFalse(result["checkpoint_write_performed"])
            self.assertEqual(state_file.read_bytes(), before)
            self.assertEqual(state_file.stat().st_mtime_ns, before_mtime)
            self.assertFalse(state_file.with_name("state.lease.json").exists())
            self.assertTrue(result["runner_lease"]["inspection_only"])

    def test_mixed_head_snapshot_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory_name:
            result = run_preflight(
                Path(directory_name), graphql_call=FakeSnapshot(mixed_head=True)
            )
            self.assertFalse(result["ready_for_supervised_pilot"])
            self.assertEqual(result["failure_phase"], "github_snapshot")
            self.assertIn("head advanced", result["github_snapshot"]["error"])

    def test_fake_gh_auth_failure_is_structured_and_stops_before_api(self) -> None:
        calls: list[list[str]] = []

        def failed_auth(command: list[str]) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            if command[:3] == ["gh", "auth", "status"]:
                return subprocess.CompletedProcess(command, 1, "", "not logged in")
            return subprocess.CompletedProcess(command, 0, "version 1", "")

        api = FakeSnapshot()
        with tempfile.TemporaryDirectory() as directory_name:
            result = run_preflight(
                Path(directory_name), graphql_call=api, runner=failed_auth
            )
        self.assertEqual(result["failure_phase"], "github_authentication")
        self.assertEqual(api.calls, [])
        self.assertIn(["gh", "auth", "status"], calls)

    def test_fake_github_api_failure_is_structured(self) -> None:
        def failed_api(
            query: str, owner: str, repo: str, number: int, cursor: str | None
        ) -> dict:
            raise RuntimeError("simulated GraphQL failure")

        with tempfile.TemporaryDirectory() as directory_name:
            result = run_preflight(Path(directory_name), graphql_call=failed_api)
        self.assertEqual(result["failure_phase"], "github_snapshot")
        self.assertIn("simulated GraphQL failure", result["github_snapshot"]["error"])

    def test_unrelated_local_checkout_blocks_readiness(self) -> None:
        def wrong_checkout(command: list[str]) -> subprocess.CompletedProcess[str]:
            if command[-2:] == ["rev-parse", "HEAD"]:
                return subprocess.CompletedProcess(command, 0, "WRONG_HEAD\n", "")
            if command[-4:] == ["remote", "get-url", "--all", "origin"]:
                return subprocess.CompletedProcess(
                    command, 0, "https://github.com/Other/Repo.git\n", ""
                )
            if command[-5:] == ["remote", "get-url", "--push", "--all", "origin"]:
                return subprocess.CompletedProcess(
                    command, 0, "https://github.com/Other/Repo.git\n", ""
                )
            return command_runner(command)

        with tempfile.TemporaryDirectory() as directory_name:
            result = run_preflight(Path(directory_name), runner=wrong_checkout)
        self.assertFalse(result["ready_for_supervised_pilot"])
        self.assertIn("local_checkout_verification_failed", result["blockers"])
        self.assertIn(
            "target_checkout_head_mismatch", result["local_checkout"]["errors"]
        )
        self.assertIn(
            "target_checkout_origin_fetch_mismatch",
            result["local_checkout"]["errors"],
        )
        self.assertIn(
            "target_checkout_origin_push_mismatch",
            result["local_checkout"]["errors"],
        )

    def test_preflight_must_run_from_verified_installation(self) -> None:
        with tempfile.TemporaryDirectory() as directory_name:
            directory = Path(directory_name)
            install_root = directory / "installed"
            create_installation(install_root)
            result = build_preflight(
                repository="Owner/Repo",
                pr_number=17,
                repository_path=directory,
                reviewer_logins=None,
                approval_logins=None,
                state_file=directory / "state.json",
                install_root=install_root,
                expected_skill_version="0.3.0",
                expected_source_commit=EXPECTED_COMMIT,
                single_runner_confirmed=True,
                runtime_skill_path=ROOT / "skills" / "codex-review-pulse",
                command_runner=command_runner,
                which=which,
                graphql_call=FakeSnapshot(),
            )
        self.assertFalse(result["ready_for_supervised_pilot"])
        self.assertIn(
            "preflight_not_running_from_verified_installation", result["blockers"]
        )

    def test_non_git_repository_path_returns_structured_blockers(self) -> None:
        def non_git_checkout(command: list[str]) -> subprocess.CompletedProcess[str]:
            if "--is-inside-work-tree" in command:
                return subprocess.CompletedProcess(command, 128, "", "not a worktree")
            return command_runner(command)

        with tempfile.TemporaryDirectory() as directory_name:
            directory = Path(directory_name)
            install_root = directory / "installed"
            create_installation(install_root)
            result = build_preflight(
                repository="Owner/Repo",
                pr_number=17,
                repository_path=directory,
                reviewer_logins=None,
                approval_logins=None,
                state_file=None,
                install_root=install_root,
                expected_skill_version="0.3.0",
                expected_source_commit=EXPECTED_COMMIT,
                single_runner_confirmed=True,
                runtime_skill_path=installation_path(install_root),
                command_runner=non_git_checkout,
                which=which,
                graphql_call=FakeSnapshot(),
            )
        self.assertFalse(result["ready_for_supervised_pilot"])
        self.assertFalse(result["local_checkout"]["ok"])
        self.assertIn("local_checkout_verification_failed", result["blockers"])
        self.assertIn("checkpoint_invalid", result["blockers"])
        self.assertIn("Unable to resolve checkpoint path", result["checkpoint"]["error"])


if __name__ == "__main__":
    unittest.main()
