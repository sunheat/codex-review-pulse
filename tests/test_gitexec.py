"""Real-Git execution regressions with executable positive controls."""
from __future__ import annotations

import ast
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills/codex-review-pulse/scripts"
sys.path.insert(0, str(SCRIPTS))
import gitexec
import gitlocal
import storage
import campaign_model as model


def raw(path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=path, capture_output=True,
                          encoding="utf-8", errors="surrogateescape")


def require(path: Path, *args: str) -> str:
    result = raw(path, *args)
    if result.returncode:
        raise RuntimeError(result.stderr)
    return result.stdout.strip()


class ExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        require(self.repo, "init", "-b", "main")
        require(self.repo, "config", "user.name", "Test")
        require(self.repo, "config", "user.email", "test@example.test")
        require(self.repo, "config", "commit.gpgSign", "false")
        (self.repo / "f").write_text("initial\n")
        require(self.repo, "add", ".")
        require(self.repo, "commit", "-m", "initial")
        self.marker = self.root / "EXECUTED"

    def script(self, body: str) -> Path:
        path = self.root / "sentinel"
        path.write_text(f'#!/bin/sh\ntouch "{self.marker.as_posix()}"\n{body}\n')
        path.chmod(0o755)
        return path

    def config(self, key: str, value: str) -> None:
        require(self.repo, "config", key, value)

    def reset_marker(self) -> None:
        self.assertTrue(self.marker.exists(), "sentinel must actually execute")
        self.marker.unlink()

    def test_fsmonitor_positive_control_and_shared_staging(self) -> None:
        hook = self.script('printf "token\\0"')
        self.config("core.fsmonitor", hook.as_posix())
        (self.repo / "f").write_text("modified\n")
        require(self.repo, "add", "-A")
        self.reset_marker()
        gitlocal.stage_complete_delta(self.repo)
        self.assertFalse(self.marker.exists())
        self.assertEqual(require(self.repo, "-c", "core.fsmonitor=false", "show", ":f"), "modified")

    def test_traditional_hooks_all_local_boundaries(self) -> None:
        hooks = self.root / "hooks"
        hooks.mkdir()
        for event in ("post-index-change", "reference-transaction", "pre-commit", "post-checkout"):
            hook = hooks / event
            hook.write_text(f'#!/bin/sh\ntouch "{self.marker.as_posix()}"\n')
            hook.chmod(0o755)
        self.config("core.hooksPath", hooks.as_posix())
        (self.repo / "f").write_text("modified\n")
        require(self.repo, "add", "-A")
        self.reset_marker()
        gitlocal.stage_complete_delta(self.repo)
        gitlocal.write_tree(self.repo)
        gitlocal.commit_index_hook_free(self.repo, message="protected")
        target = self.root / "worktree"
        gitexec.run_controller_git("worktree", "add", "--detach", str(target), "HEAD", cwd=self.repo)
        self.assertFalse(self.marker.exists())
        require(self.repo, "worktree", "remove", "--force", str(target))

    def test_configuration_hooks_execute_despite_empty_hook_directory(self) -> None:
        version = require(self.repo, "--version")
        numbers = tuple(int(n) for n in version.split()[2].split(".")[:2])
        if numbers < (2, 54):
            self.skipTest(f"configuration-defined hooks not available on {version}")
        hook = self.script("exit 0")
        self.config("hook.probe.event", "pre-commit")
        require(self.repo, "config", "--add", "hook.probe.event", "post-index-change")
        self.config("hook.probe.command", hook.as_posix())
        self.config("hook.probe.enabled", "true")
        empty = self.root / "empty-hooks"
        empty.mkdir()
        (self.repo / "f").write_text("modified\n")
        require(self.repo, "-c", f"core.hooksPath={empty.as_posix()}", "-c", "core.fsmonitor=false", "add", "-A")
        self.reset_marker()
        gitlocal.stage_complete_delta(self.repo)
        gitlocal.commit_index_hook_free(self.repo, message="protected")
        self.assertFalse(self.marker.exists())

    def configure_filter(self, location: str = "nested") -> None:
        hook = self.script("cat")
        self.config("filter.sentinel.clean", hook.as_posix())
        self.config("filter.sentinel.smudge", hook.as_posix())
        if location == "nested":
            directory = self.repo / "nested"
            directory.mkdir(exist_ok=True)
            (directory / ".gitattributes").write_text("*.txt filter=sentinel\n")
            (directory / "file.txt").write_text("content\n")
        elif location == "info":
            (self.repo / ".git/info/attributes").write_text("f filter=sentinel\n")
        elif location == "global":
            attrs = self.root / "attributes"
            attrs.write_text("f filter=sentinel\n")
            self.config("core.attributesFile", attrs.as_posix())

    def test_filter_positive_control_even_with_hooks_fsmonitor_disabled(self) -> None:
        self.configure_filter()
        empty = self.root / "empty-hooks"
        empty.mkdir()
        require(self.repo, "-c", f"core.hooksPath={empty.as_posix()}", "-c", "core.fsmonitor=false", "add", "-A")
        self.reset_marker()
        with self.assertRaisesRegex(gitexec.GitPolicyRefused, "content filter"):
            gitlocal.stage_complete_delta(self.repo)
        self.assertFalse(self.marker.exists())

    def test_checkout_preflight_uses_target_nested_index_attributes(self) -> None:
        self.configure_filter()
        require(self.repo, "add", "-A")
        require(self.repo, "commit", "-m", "filtered target")
        require(self.repo, "checkout", "HEAD~1")
        self.marker.unlink(missing_ok=True)
        target = self.root / "unsafe"
        result = raw(self.repo, "worktree", "add", "--detach", str(target), "main")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.reset_marker()
        require(self.repo, "worktree", "remove", "--force", str(target))
        with self.assertRaisesRegex(gitexec.GitPolicyRefused, "content filter"):
            gitexec.run_controller_git("worktree", "add", "--detach", str(target), "main", cwd=self.repo)
        self.assertFalse(self.marker.exists())
        self.assertFalse(target.exists())

    def test_info_global_and_speculatively_changed_attributes_refuse_staging(self) -> None:
        for location in ("info", "global"):
            with self.subTest(location=location):
                self.configure_filter(location)
                (self.repo / "f").write_text(location)
                with self.assertRaisesRegex(gitexec.GitPolicyRefused, "content filter"):
                    gitlocal.stage_complete_delta(self.repo)
                self.assertFalse(self.marker.exists())
                if location == "info":
                    (self.repo / ".git/info/attributes").unlink()
                else:
                    require(self.repo, "config", "--unset", "core.attributesFile")
        # A new nested attribute file introduced by speculative semantic work.
        self.configure_filter()
        with self.assertRaises(gitexec.GitPolicyRefused):
            gitlocal.stage_complete_delta(self.repo)
        self.assertEqual(require(self.repo, "show", "HEAD:f"), "initial")

    def test_literal_attribute_state_names_cannot_hide_filters(self) -> None:
        for name in ("unset", "unspecified"):
            with self.subTest(name=name):
                hook = self.script("cat")
                self.config(f"filter.{name}.clean", hook.as_posix())
                self.config(f"filter.{name}.smudge", hook.as_posix())
                (self.repo / ".gitattributes").write_text(f"f filter={name}\n")
                (self.repo / "f").write_text(f"filtered by {name}\n")
                with self.assertRaisesRegex(gitexec.GitPolicyRefused, "content filter"):
                    gitlocal.stage_complete_delta(self.repo)
                self.assertFalse(self.marker.exists())
                require(self.repo, "add", "-A")
                self.reset_marker()
                require(self.repo, "commit", "-m", f"literal {name} filter")
                self.marker.unlink(missing_ok=True)
                target = self.root / f"checkout-{name}"
                with self.assertRaisesRegex(gitexec.GitPolicyRefused, "content filter"):
                    gitexec.run_controller_git("worktree", "add", "--detach", str(target), "HEAD", cwd=self.repo)
                self.assertFalse(self.marker.exists())
                self.assertFalse(target.exists())

    def test_unsupported_features_do_not_block_storage_observation_or_release(self) -> None:
        self.configure_filter("info")
        self.config("commit.gpgsign", "true")
        self.config("push.gpgsign", "true")
        self.config("core.sshCommand", self.script("exit 1").as_posix())
        acquired = storage.acquire_setup_lock("owner/repo", 7, campaign_id="crp-20261021T000000Z-abc123",
                                             acquired_at="2026-10-21T00:00:00Z", repository_path=self.repo)
        self.assertTrue(storage.verify_owner("owner/repo", 7, acquired["owner_token"], repository_path=self.repo))
        storage.initialize_campaign(
            "owner/repo", 7, owner_token=acquired["owner_token"], repository_path=self.repo,
            campaign=model.new_campaign(
                campaign_id="crp-20261021T000000Z-abc123", repository="owner/repo",
                pull_request_number=7, created_at="2026-10-21T00:00:00Z", max_rounds=3,
                model="test", reasoning_level="medium", interval_minutes=30,
                reviewer_logins=["codex"], approval_logins=["codex"],
            ),
        )
        storage.release_lock("owner/repo", 7, owner_token=acquired["owner_token"], repository_path=self.repo)
        self.assertFalse(self.marker.exists())

    def test_signing_program_positive_control_then_explicit_refusal(self) -> None:
        hook = self.script("exit 1")
        self.config("gpg.program", hook.as_posix())
        self.config("commit.gpgsign", "true")
        (self.repo / "f").write_text("modified\n")
        require(self.repo, "add", "-A")
        self.assertNotEqual(raw(self.repo, "commit", "-m", "sign control").returncode, 0)
        self.reset_marker()
        with self.assertRaisesRegex(gitexec.GitPolicyRefused, "required signing"):
            gitlocal.commit_index_hook_free(self.repo, message="refused")
        self.assertFalse(self.marker.exists())
        self.assertEqual(require(self.repo, "log", "-1", "--format=%s"), "initial")

    def test_publication_scope_and_lfs_without_attributes(self) -> None:
        origin = self.root / "origin.git"
        require(self.repo, "init", "--bare", str(origin))
        require(self.repo, "remote", "add", "origin", str(origin))
        self.config("remote.origin.mirror", "true")
        self.config("push.followTags", "true")
        self.config("push.pushOption", "unexpected")
        self.config("push.recurseSubmodules", "on-demand")
        require(self.repo, "tag", "-a", "extra", "-m", "extra")
        require(self.repo, "branch", "unrelated")
        result = gitexec.run_controller_git("push", "--no-verify", "origin", "HEAD:refs/heads/main", cwd=self.repo)
        self.assertEqual(result.returncode, 0, result.stderr)
        refs = require(origin, "for-each-ref", "--format=%(refname)").splitlines()
        self.assertEqual(refs, ["refs/heads/main"])
        (self.repo / "pointer").write_text("version https://git-lfs.github.com/spec/v1\noid sha256:" + "a" * 64 + "\nsize 3\n")
        require(self.repo, "add", "pointer")
        require(self.repo, "commit", "-m", "tracked pointer without attributes")
        with self.assertRaisesRegex(gitexec.GitPolicyRefused, "LFS publication"):
            gitexec.preflight_publication(self.repo, "HEAD")
        with self.assertRaisesRegex(gitexec.GitPolicyRefused, "LFS publication"):
            gitexec.run_controller_git("push", "--no-verify", "origin", "HEAD:refs/heads/main", cwd=self.repo)
        self.assertNotEqual(require(origin, "rev-parse", "main"), require(self.repo, "rev-parse", "HEAD"))

    def test_multiple_push_urls_refuse_before_any_destination_changes(self) -> None:
        remotes = [self.root / "a.git", self.root / "b.git"]
        for remote in remotes:
            require(self.repo, "init", "--bare", str(remote))
        require(self.repo, "remote", "add", "origin", str(remotes[0]))
        for remote in remotes:
            require(self.repo, "config", "--add", "remote.origin.pushurl", str(remote))
        with self.assertRaisesRegex(gitexec.GitPolicyRefused, "exactly one"):
            gitexec.run_controller_git("push", "--no-verify", "origin", "HEAD:refs/heads/main", cwd=self.repo)
        for remote in remotes:
            self.assertEqual(require(remote, "for-each-ref"), "")

    def test_remote_destination_with_cr_cannot_redirect_push(self) -> None:
        import os
        if os.name == "nt":
            self.skipTest("Windows filesystem prohibits literal CR in filenames; POSIX CI exercises this")
        configured = self.root / "origin.git\r"
        sibling = self.root / "origin.git"
        for remote in (configured, sibling):
            require(self.repo, "init", "--bare", str(remote))
        require(self.repo, "remote", "add", "origin", str(configured))
        with self.assertRaisesRegex(gitexec.GitPolicyRefused, "destination"):
            gitexec.run_controller_git("push", "--no-verify", "origin", "HEAD:refs/heads/main", cwd=self.repo)
        for remote in (configured, sibling):
            self.assertEqual(require(remote, "for-each-ref"), "")

    def test_destination_parser_rejects_trailing_cr(self) -> None:
        def observed(command, **kwargs):
            return subprocess.CompletedProcess(command, 0, "/tmp/origin.git\r\n", "")
        with self.assertRaisesRegex(gitexec.GitPolicyRefused, "destination"):
            gitexec._destination(observed, "origin", push=False)

    def test_relative_local_destination_cannot_select_same_named_remote(self) -> None:
        configured = self.repo / "backup"
        substituted = self.root / "substituted.git"
        for remote in (configured, substituted):
            require(self.repo, "init", "--bare", str(remote))
        require(self.repo, "remote", "add", "origin", "backup")
        require(self.repo, "remote", "add", "backup", str(substituted))
        result = gitexec.run_controller_git("push", "--no-verify", "origin", "HEAD:refs/heads/main", cwd=self.repo)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(require(configured, "rev-parse", "main"), require(self.repo, "rev-parse", "HEAD"))
        self.assertEqual(require(substituted, "for-each-ref"), "")

    def test_automatic_maintenance_sentinel_and_protected_commit(self) -> None:
        import hashlib
        import zlib
        hook = self.script("exit 0")
        self.config("gc.recentObjectsHook", hook.as_posix())
        self.config("gc.cruftPacks", "true")
        # pre-auto-gc proves the implicit maintenance process really starts;
        # recentObjectsHook is separately exercised by explicit cruft GC.
        auto_hook = self.repo / ".git/hooks/pre-auto-gc"
        auto_hook.write_text(hook.read_text())
        auto_hook.chmod(0o755)
        self.config("gc.auto", "1")
        self.config("gc.autoPackLimit", "1")
        self.config("gc.autoDetach", "false")
        self.config("maintenance.autoDetach", "false")
        # Real unreachable loose blobs in the bucket Git samples for auto GC.
        # Write canonical Git objects directly so this fixture needs no 7,000
        # hash-object subprocesses and remains network/platform independent.
        def populate() -> None:
            count = 0
            for number in range(20000):
                content = str(number).encode()
                obj = b"blob " + str(len(content)).encode() + b"\0" + content
                oid = hashlib.sha1(obj).hexdigest()
                if oid.startswith("17"):
                    path = self.repo / ".git/objects" / oid[:2] / oid[2:]
                    path.parent.mkdir(exist_ok=True)
                    path.write_bytes(zlib.compress(obj))
                    import os
                    os.utime(path, (1, 1))
                    count += 1
                    if count == 30:
                        return
            self.fail("could not populate automatic GC sample")
        populate()
        require(self.repo, "gc", "--cruft")
        self.reset_marker()
        populate()
        require(self.repo, "gc", "--auto")
        self.reset_marker()
        populate()
        # A raw commit starts Git's implicit maintenance entrypoint. Trace is
        # authoritative process evidence even when no cruft helper is needed.
        with patch.dict("os.environ", {"GIT_TRACE": "1"}):
            control = raw(self.repo, "commit", "--allow-empty", "-m", "maintenance control")
        self.assertEqual(control.returncode, 0, control.stderr)
        self.assertIn("maintenance run", control.stderr)
        self.marker.unlink(missing_ok=True)
        populate()
        (self.repo / "f").write_text("protected change\n")
        gitlocal.stage_complete_delta(self.repo)
        commands = []
        def recorder(command, **kwargs):
            result = subprocess.run(command, **kwargs)
            commands.append(result.stderr.decode("utf-8", "surrogateescape")
                            if isinstance(result.stderr, bytes) else result.stderr)
            return result
        with patch.dict("os.environ", {"GIT_TRACE": "1"}):
            gitlocal.commit_index_hook_free(self.repo, message="no automatic maintenance", runner=recorder)
        self.assertFalse(any("maintenance run" in stderr for stderr in commands))
        self.assertFalse(self.marker.exists())

    def test_real_lfs_conversion_and_missing_object_publication_refusal(self) -> None:
        if raw(self.repo, "lfs", "version").returncode:
            self.skipTest("Git LFS unavailable; pointer fixture tests are not LFS integration evidence")
        require(self.repo, "lfs", "install", "--local", "--skip-smudge")
        (self.repo / ".gitattributes").write_text("*.bin filter=lfs diff=lfs merge=lfs -text\n")
        (self.repo / "payload.bin").write_bytes(b"real content, not a pointer")
        require(self.repo, "add", ".")
        pointer = raw(self.repo, "show", ":payload.bin").stdout
        self.assertTrue(pointer.startswith("version https://git-lfs.github.com/spec/v1"))
        (self.root / "pointer").write_text(pointer)
        self.assertEqual(raw(self.repo, "lfs", "pointer", "--check", "--file=" + str(self.root / "pointer")).returncode, 0)
        require(self.repo, "commit", "-m", "real LFS pointer")
        with self.assertRaisesRegex(gitexec.GitPolicyRefused, "content filter"):
            gitlocal.stage_complete_delta(self.repo)
        # Remove conversion attributes without replacing the already tracked
        # pointer: its LFS object is still required at any new destination.
        (self.repo / ".gitattributes").unlink()
        # Keep an actual tracked pointer after removing attributes. A full add
        # may otherwise stage the smudged payload as an ordinary blob on hosts
        # where LFS's timestamp/size cache behaves differently.
        (self.repo / "payload.bin").write_bytes(pointer.encode("utf-8"))
        require(self.repo, "add", "-A")
        self.assertEqual(raw(self.repo, "show", ":payload.bin").stdout, pointer)
        require(self.repo, "commit", "-m", "remove filter attributes")
        origin = self.root / "lfs-origin.git"
        require(self.repo, "init", "--bare", str(origin))
        require(self.repo, "remote", "add", "origin", str(origin))
        with self.assertRaisesRegex(gitexec.GitPolicyRefused, "LFS publication"):
            gitexec.preflight_publication(self.repo, "HEAD")
        self.assertEqual(require(origin, "for-each-ref"), "")

    def test_transport_helper_and_partial_clone_refusal_are_scoped(self) -> None:
        hook = self.script("exit 1")
        require(self.repo, "remote", "add", "origin", "ssh://example.invalid/repo")
        self.config("core.sshCommand", hook.as_posix())
        self.assertNotEqual(raw(self.repo, "ls-remote", "origin").returncode, 0)
        self.reset_marker()
        with self.assertRaisesRegex(gitexec.GitPolicyRefused, "transport helper"):
            gitlocal.remote_head(self.repo, "main")
        self.assertFalse(self.marker.exists())
        self.config("remote.origin.promisor", "true")
        with self.assertRaisesRegex(gitexec.GitPolicyRefused, "Partial-clone"):
            gitexec.run_controller_git("ls-tree", "HEAD", cwd=self.repo)
        self.assertEqual(storage.git_common_directory(self.repo), (self.repo / ".git").resolve())

    def test_repository_askpass_refused_before_transport(self) -> None:
        hook = self.script("exit 1")
        require(self.repo, "remote", "add", "origin", "https://example.invalid/repo")
        self.config("core.askPass", hook.as_posix())
        with self.assertRaisesRegex(gitexec.GitPolicyRefused, "transport helper"):
            gitexec.run_controller_git("ls-remote", "origin", cwd=self.repo)
        self.assertFalse(self.marker.exists())

    def test_included_configuration_hook_is_suppressed(self) -> None:
        version = require(self.repo, "--version")
        if tuple(int(n) for n in version.split()[2].split(".")[:2]) < (2, 54):
            self.skipTest(f"configuration-defined hooks not available on {version}")
        hook = self.script("exit 0")
        include = self.root / "included-config"
        include.write_text(f'[hook "included.with.dots"]\n event = pre-commit\n command = "{hook.as_posix()}"\n enabled = true\n')
        self.config("include.path", include.as_posix())
        require(self.repo, "commit", "--allow-empty", "-m", "included hook control")
        self.reset_marker()
        (self.repo / "f").write_text("modified")
        gitlocal.stage_complete_delta(self.repo)
        gitlocal.commit_index_hook_free(self.repo, message="included hook suppressed")
        self.assertFalse(self.marker.exists())

    def test_configured_reference_hook_fetch_and_push(self) -> None:
        version = require(self.repo, "--version")
        if tuple(int(n) for n in version.split()[2].split(".")[:2]) < (2, 54):
            self.skipTest(f"configuration-defined hooks not available on {version}")
        origin = self.root / "origin.git"
        require(self.repo, "init", "--bare", str(origin))
        require(self.repo, "remote", "add", "origin", str(origin))
        hook = self.script("exit 0")
        self.config("hook.network.event", "reference-transaction")
        require(self.repo, "config", "--add", "hook.network.event", "pre-push")
        self.config("hook.network.command", hook.as_posix())
        require(self.repo, "push", "origin", "HEAD:refs/heads/main")
        self.reset_marker()
        (self.repo / "f").write_text("advanced")
        gitlocal.stage_complete_delta(self.repo)
        gitlocal.commit_index_hook_free(self.repo, message="advanced")
        pushed = gitexec.run_controller_git("push", "--no-verify", "origin", "HEAD:refs/heads/main", cwd=self.repo)
        self.assertEqual(pushed.returncode, 0, pushed.stderr)
        self.assertFalse(self.marker.exists())
        # Delete tracking ref, then fetch must perform a real reference update.
        require(self.repo, "update-ref", "-d", "refs/remotes/origin/main")
        self.marker.unlink(missing_ok=True)
        require(self.repo, "fetch", "origin")
        self.reset_marker()
        require(self.repo, "update-ref", "-d", "refs/remotes/origin/main")
        self.marker.unlink(missing_ok=True)
        fetched = gitexec.run_controller_git("fetch", "origin", cwd=self.repo)
        self.assertEqual(fetched.returncode, 0, fetched.stderr)
        self.assertFalse(self.marker.exists())

    def test_effective_attribute_precedence_and_cached_fallback(self) -> None:
        self.configure_filter("info")
        # Higher-priority info attribute can explicitly unset a global filter.
        global_attrs = self.root / "global-attributes"
        global_attrs.write_text("f filter=sentinel\n")
        self.config("core.attributesFile", str(global_attrs))
        info = self.repo / ".git/info/attributes"
        info.write_text("f -filter\n")
        (self.repo / "f").write_text("safe")
        gitlocal.stage_complete_delta(self.repo)
        self.assertFalse(self.marker.exists())
        require(self.repo, "config", "--unset", "core.attributesFile")
        info.unlink()
        # Indexed nested attributes still apply when the worktree copy is gone.
        self.configure_filter()
        require(self.repo, "add", "-A")
        self.reset_marker()
        (self.repo / "nested/.gitattributes").unlink()
        with self.assertRaisesRegex(gitexec.GitPolicyRefused, "content filter"):
            gitlocal.stage_complete_delta(self.repo)
        self.assertFalse(self.marker.exists())

    def test_conditional_configuration_and_relative_attributes_refuse_checkout(self) -> None:
        target = self.root / "worktree"
        relative_attrs = self.repo / "attributes"
        relative_attrs.write_text("f filter=sentinel\n")
        self.config("core.attributesFile", "attributes")
        with self.assertRaisesRegex(gitexec.GitPolicyRefused, "Relative attributesFile"):
            gitexec.run_controller_git("worktree", "add", "--detach", str(target), "HEAD", cwd=self.repo)
        require(self.repo, "config", "--unset", "core.attributesFile")
        include = self.root / "conditional-config"
        include.write_text("[core]\n fsmonitor = false\n")
        self.config("includeIf.onbranch:main.path", str(include))
        with self.assertRaisesRegex(gitexec.GitPolicyRefused, "Conditional Git includes"):
            gitexec.run_controller_git("worktree", "add", "--detach", str(target), "HEAD", cwd=self.repo)
        self.assertFalse(target.exists())
        self.assertEqual(storage.git_common_directory(self.repo), (self.repo / ".git").resolve())

    def test_publication_raw_blob_batch_preserves_crlf_and_unknown_bytes(self) -> None:
        origin = self.root / "origin.git"
        require(self.repo, "init", "--bare", str(origin))
        require(self.repo, "remote", "add", "origin", str(origin))
        self.config("core.autocrlf", "false")
        (self.repo / "binary").write_bytes(b"\xff\x00\r\nUTF-8: \xe4\xb8\xad\r\n")
        (self.repo / "text").write_bytes(b"normal\r\ntext\r\n")
        gitlocal.stage_complete_delta(self.repo)
        tree = gitlocal.write_tree(self.repo)
        gitexec.preflight_publication(self.repo, tree)
        gitlocal.commit_index_hook_free(self.repo, message="byte framing")
        result = gitexec.run_controller_git("push", "--no-verify", "origin", "HEAD:refs/heads/main", cwd=self.repo)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_cr_path_identity_cannot_bypass_filter_preflight(self) -> None:
        import os
        if os.name == "nt":
            self.skipTest("Windows filesystem prohibits literal CR in filenames; POSIX CI exercises this")
        hook = self.script("cat")
        self.config("filter.sentinel.clean", str(hook))
        self.config("filter.sentinel.smudge", str(hook))
        filename = "file\r.txt"
        (self.repo / filename).write_text("filtered")
        (self.repo / ".gitattributes").write_text('"file\\r.txt" filter=sentinel\n')
        require(self.repo, "add", "-A")
        self.reset_marker()
        with self.assertRaisesRegex(gitexec.GitPolicyRefused, "content filter"):
            gitlocal.stage_complete_delta(self.repo)
        self.assertFalse(self.marker.exists())
        require(self.repo, "commit", "-m", "CR path target")
        target = self.root / "checkout"
        require(self.repo, "worktree", "add", "--detach", str(target), "HEAD")
        self.reset_marker()
        require(self.repo, "worktree", "remove", "--force", str(target))
        with self.assertRaisesRegex(gitexec.GitPolicyRefused, "content filter"):
            gitexec.run_controller_git("worktree", "add", "--detach", str(target), "HEAD", cwd=self.repo)
        self.assertFalse(self.marker.exists())
        self.assertFalse(target.exists())

    def test_nul_output_retains_literal_cr_and_surrogate_bytes(self) -> None:
        commands = []
        def executor(command, **kwargs):
            commands.append((command, kwargs))
            if "config" in command:
                return subprocess.CompletedProcess(command, 0, b"", b"")
            return subprocess.CompletedProcess(command, 0, b"file\r\xff.txt\0", b"diagnostic\r\n")
        result = gitexec.run_controller_git("ls-files", "-z", cwd=self.repo, executor=executor)
        self.assertEqual(result.stdout.encode("utf-8", "surrogateescape"), b"file\r\xff.txt\0")
        self.assertEqual(result.stderr, "diagnostic\r\n")
        self.assertTrue(all("encoding" not in kwargs for _, kwargs in commands))

    def test_process_injection_is_below_common_policy_and_preserves_results(self) -> None:
        commands = []
        def executor(command, **kwargs):
            commands.append((command, kwargs))
            return subprocess.run(command, **kwargs)
        with patch.dict("os.environ", {"GIT_INDEX_FILE": str(self.root / "wrong-index")}):
            result = gitexec.run_controller_git("rev-parse", "absent-ref", cwd=self.repo, executor=executor)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unknown revision", result.stderr)
        for command, kwargs in commands:
            self.assertIn("core.fsmonitor=false", command)
            self.assertIn("maintenance.auto=false", command)
            self.assertTrue(any(item.startswith("core.hooksPath=") for item in command))
            self.assertNotIn("GIT_INDEX_FILE", kwargs["env"])
            if "--null" not in command:
                self.assertEqual(kwargs["errors"], "surrogateescape")

    def test_no_direct_git_process_bypass_in_active_controller(self) -> None:
        # AST call-site inventory, not brittle source substring matching.
        # github_api owns the separate gh process boundary, never Git.
        allowed = {"gitexec.py", "github_api.py"}
        violations = []
        for path in SCRIPTS.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    if any(alias.name == "subprocess" for alias in node.names) and path.name not in allowed | {"gitlocal.py"}:
                        violations.append((path.name, node.lineno))
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    if isinstance(node.func.value, ast.Name) and node.func.value.id == "subprocess" and path.name not in allowed:
                        violations.append((path.name, node.lineno))
        self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main()
