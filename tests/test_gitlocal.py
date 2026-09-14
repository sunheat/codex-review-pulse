from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "codex-review-pulse" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import gitlocal  # noqa: E402
import storage  # noqa: E402


def git(cwd: Path, *args: str, input_text: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), input=input_text,
        capture_output=True, text=True,
    )


def require(cwd: Path, *args: str) -> str:
    process = git(cwd, *args)
    if process.returncode != 0:
        raise RuntimeError(f"git {args} failed: {process.stderr.strip()}")
    return process.stdout.strip()


class GitEnvironment:
    def __init__(self, root: Path) -> None:
        origin = root / "origin.git"
        seed = root / "seed"
        self.clone = root / "clone"
        subprocess.run(["git", "init", "--bare", str(origin)], check=True, capture_output=True)
        seed.mkdir()
        require(seed, "init", "-b", "main")
        require(seed, "config", "user.email", "test@example.test")
        require(seed, "config", "user.name", "Test User")
        (seed / "file.txt").write_text("v1\n", encoding="utf-8")
        require(seed, "add", "file.txt")
        require(seed, "commit", "-m", "initial")
        require(seed, "remote", "add", "origin", str(origin))
        require(seed, "push", "origin", "main")
        subprocess.run(
            ["git", "-C", str(origin), "symbolic-ref", "HEAD", "refs/heads/main"],
            check=True,
            capture_output=True,
        )
        require(seed, "clone", str(origin), str(self.clone))
        require(self.clone, "config", "user.email", "test@example.test")
        require(self.clone, "config", "user.name", "Test User")
        self.origin = origin


class PublishTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.env = GitEnvironment(Path(self._tmp.name))
        self.repo = "owner/repo"
        self.pr = 7
        acquired = storage.acquire_lock(
            self.repo, self.pr,
            campaign_id="crp-20260914T120000Z-abc123",
            acquired_at="2026-09-14T12:00:00Z",
            repository_path=self.env.clone,
        )
        self.token = acquired["owner_token"]
        gitlocal.fetch(
            self.env.clone,
            repository=self.repo,
            pr_number=self.pr,
            owner_token=self.token,
        )
        self.head1 = gitlocal.remote_head(self.env.clone, "main")
        added = gitlocal.add_worktree(
            self.repo, self.pr, self.head1,
            owner_token=self.token,
            repository_path=self.env.clone, name="batch1",
        )
        self.worktree = Path(added["path"])

    def tearDown(self) -> None:
        git(self.env.clone, "worktree", "remove", "--force", str(self.worktree))
        self._tmp.cleanup()

    def test_publishes_one_commit_and_confirms_remote_head(self) -> None:
        (self.worktree / "file.txt").write_text("v2\n", encoding="utf-8")
        result = gitlocal.publish_batch(
            worktree=self.worktree,
            branch="main",
            paths=["file.txt"],
            commit_message="remediation: fix",
            repository=self.repo,
            pr_number=self.pr,
            owner_token=self.token,
            expected_head=self.head1,
            repository_path=self.env.clone,
        )
        self.assertTrue(result["published"])
        self.assertEqual(result["status"], "pushed")
        self.assertEqual(
            gitlocal.remote_head(self.env.clone, "main"), result["commit"]
        )
        log = require(self.env.clone, "log", "--format=%s", "-1", result["commit"])
        self.assertEqual(log, "remediation: fix")

    def test_no_changes_creates_no_commit(self) -> None:
        before = gitlocal.remote_head(self.env.clone, "main")
        result = gitlocal.publish_batch(
            worktree=self.worktree,
            branch="main",
            paths=["file.txt"],
            commit_message="should not happen",
            repository=self.repo,
            pr_number=self.pr,
            owner_token=self.token,
            expected_head=before,
            repository_path=self.env.clone,
        )
        self.assertFalse(result["published"])
        self.assertEqual(result["status"], "no_changes")
        self.assertEqual(gitlocal.remote_head(self.env.clone, "main"), before)

    def test_remote_advancement_aborts_publication(self) -> None:
        # Someone else lands a commit on the PR branch.
        (self.env.clone / "file.txt").write_text("v3\n", encoding="utf-8")
        require(self.env.clone, "add", "file.txt")
        require(self.env.clone, "commit", "-m", "other work")
        require(self.env.clone, "push", "origin", "main")
        advanced = gitlocal.remote_head(self.env.clone, "main")

        (self.worktree / "file.txt").write_text("v2\n", encoding="utf-8")
        result = gitlocal.publish_batch(
            worktree=self.worktree,
            branch="main",
            paths=["file.txt"],
            commit_message="remediation: fix",
            repository=self.repo,
            pr_number=self.pr,
            owner_token=self.token,
            expected_head=self.head1,
            repository_path=self.env.clone,
        )
        self.assertFalse(result["published"])
        self.assertEqual(result["status"], "remote_head_advanced")
        self.assertEqual(result["remote_head"], advanced)

    def test_push_command_never_uses_force(self) -> None:
        recorded: list[list[str]] = []
        real_git = gitlocal.git

        def recorder(*args, cwd=None):  # type: ignore[no-untyped-def]
            recorded.append(list(args))
            return real_git(*args, cwd=cwd)

        (self.worktree / "file.txt").write_text("v2\n", encoding="utf-8")
        result = gitlocal.publish_batch(
            worktree=self.worktree,
            branch="main",
            paths=["file.txt"],
            commit_message="remediation: fix",
            repository=self.repo,
            pr_number=self.pr,
            owner_token=self.token,
            expected_head=self.head1,
            repository_path=self.env.clone,
            runner=recorder,
        )
        self.assertTrue(result["published"])
        push_args = next(args for args in recorded if args and args[0] == "push")
        self.assertNotIn("--force", push_args)
        self.assertNotIn("--force-with-lease", push_args)
        self.assertIn("HEAD:refs/heads/main", push_args)

    def test_publish_refuses_paths_outside_worktree(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "escapes the worktree"):
            gitlocal.publish_batch(
                worktree=self.worktree,
                branch="main",
                paths=["../evil.txt"],
                commit_message="x",
                repository=self.repo,
                pr_number=self.pr,
                owner_token=self.token,
                expected_head=self.head1,
                repository_path=self.env.clone,
            )

    def test_publish_requires_ownership(self) -> None:
        (self.worktree / "file.txt").write_text("v2\n", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            gitlocal.publish_batch(
                worktree=self.worktree,
                branch="main",
                paths=["file.txt"],
                commit_message="x",
                repository=self.repo,
                pr_number=self.pr,
                owner_token="wrong-token",
                expected_head=self.head1,
                repository_path=self.env.clone,
            )

    def test_worktree_lives_under_shared_state_dir(self) -> None:
        resolved = self.worktree.resolve()
        parts = resolved.parts
        self.assertIn("codex-review-pulse", parts)
        self.assertIn("v2", parts)

    def test_remove_refuses_unscoped_worktree(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "outside the v2 state directory"):
            gitlocal.remove_worktree(
                self.env.clone,
                repository=self.repo,
                pr_number=self.pr,
                owner_token=self.token,
                repository_path=self.env.clone,
            )

    def test_dirty_worktree_is_not_removed_without_force(self) -> None:
        (self.worktree / "file.txt").write_text("dirty\n", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            gitlocal.remove_worktree(
                self.worktree,
                repository=self.repo,
                pr_number=self.pr,
                owner_token=self.token,
                repository_path=self.env.clone,
                force=False,
            )


if __name__ == "__main__":
    unittest.main()
