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
    """The publication primitives behind the deterministic finalizer."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.env = GitEnvironment(Path(self._tmp.name))
        self.repo = "owner/repo"
        self.pr = 7
        acquired = storage.acquire_setup_lock(
            self.repo, self.pr,
            campaign_id="crp-20260914T120000Z-abc123",
            acquired_at="2026-09-14T12:00:00Z",
            repository_path=self.env.clone,
        )
        self.token = acquired["owner_token"]
        # Ordinary mutation authority requires a valid matching active campaign.
        storage.initialize_campaign(
            self.repo, self.pr,
            owner_token=self.token,
            campaign=model.new_campaign(
                campaign_id="crp-20260914T120000Z-abc123",
                repository=self.repo,
                pull_request_number=self.pr,
                created_at="2026-09-14T12:00:00Z",
                max_rounds=6,
                model="a-model",
                reasoning_level="medium",
                interval_minutes=30,
                reviewer_logins=["chatgpt-codex-connector"],
                approval_logins=["chatgpt-codex-connector"],
            ),
            repository_path=self.env.clone,
        )
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

    def stage_tree_and_commit(self, *, message: str = "remediation: fix") -> str:
        gitlocal.stage_complete_delta(self.worktree)
        return gitlocal.commit_index_hook_free(self.worktree, message=message)

    def test_stage_write_tree_captures_the_complete_non_ignored_delta(self) -> None:
        (self.worktree / "file.txt").write_text("v2\n", encoding="utf-8")
        (self.worktree / "added.txt").write_text("new\n", encoding="utf-8")
        gitlocal.stage_complete_delta(self.worktree)
        tree = gitlocal.write_tree(self.worktree)
        base = gitlocal.head_tree_oid(self.worktree)
        self.assertNotEqual(tree, base)
        names = require(
            self.worktree, "ls-tree", "-r", "--name-only", tree
        ).split()
        self.assertIn("added.txt", names)

    def test_staging_does_not_execute_post_index_change_hook(self) -> None:
        hooks = self.env.clone / "configured-hooks"
        hooks.mkdir()
        hook = hooks / "post-index-change"
        hook.write_text(
            "#!/bin/sh\ntouch POST_INDEX_CHANGE_HOOK_RAN\n", encoding="utf-8"
        )
        hook.chmod(0o755)
        require(self.env.clone, "config", "core.hooksPath", str(hooks))

        (self.worktree / "file.txt").write_text("v2\n", encoding="utf-8")
        gitlocal.stage_complete_delta(self.worktree)

        self.assertFalse((self.worktree / "POST_INDEX_CHANGE_HOOK_RAN").exists())
        self.assertEqual(
            require(self.worktree, "show", ":file.txt"), "v2"
        )

    def test_write_tree_does_not_execute_post_index_change_hook(self) -> None:
        (self.worktree / "file.txt").write_text("v2\n", encoding="utf-8")
        gitlocal.stage_complete_delta(self.worktree)
        hooks = self.env.clone / "configured-hooks"
        hooks.mkdir()
        hook = hooks / "post-index-change"
        hook.write_text(
            "#!/bin/sh\ntouch POST_INDEX_CHANGE_HOOK_RAN\n", encoding="utf-8"
        )
        hook.chmod(0o755)
        require(self.env.clone, "config", "core.hooksPath", str(hooks))

        tree = gitlocal.write_tree(self.worktree)

        self.assertEqual(require(self.worktree, "show", f"{tree}:file.txt"), "v2")
        self.assertFalse((self.worktree / "POST_INDEX_CHANGE_HOOK_RAN").exists())

    def test_fetch_does_not_execute_reference_transaction_hook(self) -> None:
        other = Path(self._tmp.name) / "other"
        require(self.env.clone, "clone", str(self.env.origin), str(other))
        require(other, "config", "user.email", "test@example.test")
        require(other, "config", "user.name", "Test User")
        (other / "file.txt").write_text("v2\n", encoding="utf-8")
        require(other, "add", "file.txt")
        require(other, "commit", "-m", "advance remote")
        require(other, "push", "origin", "main")
        advanced = require(other, "rev-parse", "HEAD")

        hooks = self.env.clone / "configured-hooks"
        hooks.mkdir()
        hook = hooks / "reference-transaction"
        hook.write_text(
            "#!/bin/sh\ntouch REFERENCE_TRANSACTION_HOOK_RAN\n",
            encoding="utf-8",
        )
        hook.chmod(0o755)
        require(self.env.clone, "config", "core.hooksPath", str(hooks))

        gitlocal.fetch(
            self.env.clone,
            repository=self.repo,
            pr_number=self.pr,
            owner_token=self.token,
        )

        self.assertEqual(require(self.env.clone, "rev-parse", "origin/main"), advanced)
        self.assertFalse((self.env.clone / "REFERENCE_TRANSACTION_HOOK_RAN").exists())

    def test_ignored_files_are_excluded_from_the_delta(self) -> None:
        (self.worktree / "skip.log").write_text("noise\n", encoding="utf-8")
        (self.worktree / ".gitignore").write_text("skip.log\n", encoding="utf-8")
        gitlocal.stage_complete_delta(self.worktree)
        tree = gitlocal.write_tree(self.worktree)
        names = require(
            self.worktree, "ls-tree", "-r", "--name-only", tree
        ).split()
        self.assertNotIn("skip.log", names)
        self.assertIn(".gitignore", names)

    def test_empty_delta_write_tree_equals_head_tree(self) -> None:
        gitlocal.stage_complete_delta(self.worktree)
        self.assertEqual(
            gitlocal.write_tree(self.worktree), gitlocal.head_tree_oid(self.worktree)
        )

    def test_hook_free_commit_ignores_configured_hooks(self) -> None:
        hooks = self.env.clone / "configured-hooks"
        hooks.mkdir()
        (hooks / "pre-commit").write_text(
            "#!/bin/sh\ntouch HOOK-RAN\nexit 1\n", encoding="utf-8"
        )
        require(self.env.clone, "config", "core.hooksPath", str(hooks))
        (self.worktree / "file.txt").write_text("v2\n", encoding="utf-8")
        commit = self.stage_tree_and_commit()
        self.assertNotEqual(commit, self.head1)
        self.assertFalse((self.env.clone / "HOOK-RAN").exists())
        self.assertFalse((self.worktree / "HOOK-RAN").exists())
        self.assertEqual(
            require(self.worktree, "rev-parse", f"{commit}^{{tree}}"),
            gitlocal.write_tree(self.worktree),
        )

    def test_worktree_creation_does_not_run_tracked_post_checkout_hook(self) -> None:
        hooks = self.env.clone / ".githooks"
        hooks.mkdir()
        hook = hooks / "post-checkout"
        hook.write_text("#!/bin/sh\ntouch HOOK_RAN\n", encoding="utf-8")
        hook.chmod(0o755)
        require(self.env.clone, "add", ".githooks/post-checkout")
        require(self.env.clone, "commit", "-m", "add configured hook")
        require(self.env.clone, "config", "core.hooksPath", ".githooks")
        head = require(self.env.clone, "rev-parse", "HEAD")
        added = gitlocal.add_worktree(
            self.repo, self.pr, head,
            owner_token=self.token,
            repository_path=self.env.clone, name="hook-check",
        )
        target = Path(added["path"])
        try:
            self.assertFalse((target / "HOOK_RAN").exists())
        finally:
            require(self.env.clone, "worktree", "remove", "--force", str(target))

    def test_push_publication_commit_pushes_once_and_confirms(self) -> None:
        (self.worktree / "file.txt").write_text("v2\n", encoding="utf-8")
        local = self.stage_tree_and_commit()
        result = gitlocal.push_publication_commit(
            worktree=self.worktree,
            branch="main",
            expected_head=self.head1,
            local_commit=local,
            repository=self.repo,
            pr_number=self.pr,
            owner_token=self.token,
            repository_path=self.env.clone,
        )
        self.assertTrue(result["published"])
        self.assertEqual(result["status"], "pushed")
        self.assertEqual(
            gitlocal.remote_head(self.env.clone, "main"), local
        )
        log = require(self.env.clone, "log", "--format=%s", "-1", local)
        self.assertEqual(log, "remediation: fix")

    def test_publication_does_not_execute_pre_push_hook(self) -> None:
        hooks = self.env.clone / "configured-hooks"
        hooks.mkdir()
        hook = hooks / "pre-push"
        hook.write_text(
            "#!/bin/sh\ntouch PRE_PUSH_HOOK_RAN\nexit 1\n", encoding="utf-8"
        )
        hook.chmod(0o755)
        require(self.env.clone, "config", "core.hooksPath", str(hooks))

        (self.worktree / "file.txt").write_text("v2\n", encoding="utf-8")
        local = self.stage_tree_and_commit()
        result = gitlocal.push_publication_commit(
            worktree=self.worktree,
            branch="main",
            expected_head=self.head1,
            local_commit=local,
            repository=self.repo,
            pr_number=self.pr,
            owner_token=self.token,
            repository_path=self.env.clone,
        )

        self.assertTrue(result["published"])
        self.assertEqual(gitlocal.remote_head(self.env.clone, "main"), local)
        self.assertFalse((self.worktree / "PRE_PUSH_HOOK_RAN").exists())

    def test_publication_does_not_execute_reference_transaction_hook(self) -> None:
        (self.worktree / "file.txt").write_text("v2\n", encoding="utf-8")
        local = self.stage_tree_and_commit()
        hooks = self.env.clone / "configured-hooks"
        hooks.mkdir()
        hook = hooks / "reference-transaction"
        hook.write_text(
            "#!/bin/sh\ntouch REFERENCE_TRANSACTION_HOOK_RAN\n",
            encoding="utf-8",
        )
        hook.chmod(0o755)
        require(self.env.clone, "config", "core.hooksPath", str(hooks))
        result = gitlocal.push_publication_commit(
            worktree=self.worktree,
            branch="main",
            expected_head=self.head1,
            local_commit=local,
            repository=self.repo,
            pr_number=self.pr,
            owner_token=self.token,
            repository_path=self.env.clone,
        )

        self.assertTrue(result["published"])
        self.assertEqual(gitlocal.remote_head(self.env.clone, "main"), local)
        self.assertFalse((self.worktree / "REFERENCE_TRANSACTION_HOOK_RAN").exists())

    def test_push_command_never_uses_force(self) -> None:
        recorded: list[list[str]] = []
        real_git = gitlocal.git

        def recorder(*args, cwd=None):  # type: ignore[no-untyped-def]
            recorded.append(list(args))
            return real_git(*args, cwd=cwd)

        (self.worktree / "file.txt").write_text("v2\n", encoding="utf-8")
        local = self.stage_tree_and_commit()
        result = gitlocal.push_publication_commit(
            worktree=self.worktree,
            branch="main",
            expected_head=self.head1,
            local_commit=local,
            repository=self.repo,
            pr_number=self.pr,
            owner_token=self.token,
            repository_path=self.env.clone,
            runner=recorder,
        )
        self.assertTrue(result["published"])
        push_args = next(args for args in recorded if "push" in args)
        self.assertNotIn("--force", push_args)
        self.assertNotIn("--force-with-lease", push_args)
        self.assertIn("HEAD:refs/heads/main", push_args)

    def test_remote_advancement_aborts_the_push(self) -> None:
        # Someone else lands a commit on the PR branch.
        (self.env.clone / "file.txt").write_text("v3\n", encoding="utf-8")
        require(self.env.clone, "add", "file.txt")
        require(self.env.clone, "commit", "-m", "other work")
        require(self.env.clone, "push", "origin", "main")
        advanced = gitlocal.remote_head(self.env.clone, "main")

        (self.worktree / "file.txt").write_text("v2\n", encoding="utf-8")
        local = self.stage_tree_and_commit()
        result = gitlocal.push_publication_commit(
            worktree=self.worktree,
            branch="main",
            expected_head=self.head1,
            local_commit=local,
            repository=self.repo,
            pr_number=self.pr,
            owner_token=self.token,
            repository_path=self.env.clone,
        )
        self.assertFalse(result["published"])
        self.assertEqual(result["status"], "remote_head_advanced_after_commit")
        self.assertEqual(result["remote_head"], advanced)

    def test_push_requires_ownership(self) -> None:
        (self.worktree / "file.txt").write_text("v2\n", encoding="utf-8")
        local = self.stage_tree_and_commit()
        with self.assertRaises(RuntimeError):
            gitlocal.push_publication_commit(
                worktree=self.worktree,
                branch="main",
                expected_head=self.head1,
                local_commit=local,
                repository=self.repo,
                pr_number=self.pr,
                owner_token="wrong-token",
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
