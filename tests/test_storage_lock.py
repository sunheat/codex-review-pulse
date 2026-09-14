from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "codex-review-pulse" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import storage  # noqa: E402


def git(cwd: Path, *args: str) -> str:
    process = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True
    )
    if process.returncode != 0:
        raise RuntimeError(f"git {args} failed: {process.stderr.strip()}")
    return process.stdout.strip()


class TemporaryRepository:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.main = root / "main"
        self.linked = root / "linked"
        self.main.mkdir()
        git(self.main, "init", "-b", "main")
        git(self.main, "config", "user.email", "test@example.test")
        git(self.main, "config", "user.name", "Test User")
        (self.main / "README.md").write_text("hello", encoding="utf-8")
        git(self.main, "add", "README.md")
        git(self.main, "commit", "-m", "initial")
        git(self.main, "worktree", "add", str(self.linked), "-b", "other", "HEAD")


class StorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = TemporaryRepository(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_state_is_shared_between_worktrees(self) -> None:
        via_main = storage.lock_path(
            "Owner/Repo", 7, repository_path=self.repo.main
        )
        via_linked = storage.lock_path(
            "owner/repo", 7, repository_path=self.repo.linked
        )
        self.assertEqual(via_main.resolve(), via_linked.resolve())
        # The artifacts live inside the shared Git metadata directory, never in
        # tracked worktree content.
        relative_to_main = via_main.resolve().relative_to(self.repo.main.resolve())
        self.assertEqual(relative_to_main.parts[0], ".git")

    def test_only_one_worker_acquires_and_second_is_blocked(self) -> None:
        first = storage.acquire_lock(
            "owner/repo", 7,
            campaign_id="crp-20260914T120000-abc123",
            acquired_at="2026-09-14T12:00:00Z",
            repository_path=self.repo.main,
        )
        self.assertTrue(first["acquired"])
        with self.assertRaises(storage.LockHeld) as context:
            storage.acquire_lock(
                "owner/repo", 7,
                campaign_id="crp-20260914T120000-def456",
                acquired_at="2026-09-14T12:01:00Z",
                repository_path=self.repo.linked,
            )
        self.assertEqual(context.exception.status, "active")
        # The blocked delivery consumed nothing and cannot act as owner.
        with self.assertRaises(RuntimeError):
            storage.verify_owner(
                "owner/repo", 7, "wrong-token",
                repository_path=self.repo.main,
            )

    def test_incomplete_lock_still_blocks(self) -> None:
        target = storage.lock_path(
            "owner/repo", 7, repository_path=self.repo.main
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("", encoding="utf-8")
        status = storage.inspect_lock(
            "owner/repo", 7, repository_path=self.repo.linked
        )
        self.assertEqual(status["status"], "invalid")
        with self.assertRaises(storage.LockHeld):
            storage.acquire_lock(
                "owner/repo", 7,
                campaign_id="crp-20260914T120000-abc123",
                acquired_at="2026-09-14T12:00:00Z",
                repository_path=self.repo.main,
            )

    def test_corrupt_lock_still_blocks(self) -> None:
        target = storage.lock_path(
            "owner/repo", 7, repository_path=self.repo.main
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{not json", encoding="utf-8")
        self.assertEqual(
            storage.inspect_lock("owner/repo", 7, repository_path=self.repo.main)[
                "status"
            ],
            "invalid",
        )
        with self.assertRaises(storage.LockHeld):
            storage.acquire_lock(
                "owner/repo", 7,
                campaign_id="crp-20260914T120000-abc123",
                acquired_at="2026-09-14T12:00:00Z",
                repository_path=self.repo.linked,
            )

    def test_inspect_never_exposes_token(self) -> None:
        storage.acquire_lock(
            "owner/repo", 7,
            campaign_id="crp-20260914T120000-abc123",
            acquired_at="2026-09-14T12:00:00Z",
            repository_path=self.repo.main,
        )
        status = storage.inspect_lock(
            "owner/repo", 7, repository_path=self.repo.linked
        )
        self.assertNotIn("owner_token", status)
        self.assertEqual(status["campaign_id"], "crp-20260914T120000-abc123")

    def test_owner_release_and_reacquire(self) -> None:
        first = storage.acquire_lock(
            "owner/repo", 7,
            campaign_id="crp-20260914T120000-abc123",
            acquired_at="2026-09-14T12:00:00Z",
            repository_path=self.repo.main,
        )
        with self.assertRaises(RuntimeError):
            storage.release_lock(
                "owner/repo", 7, "wrong", repository_path=self.repo.linked
            )
        storage.release_lock(
            "owner/repo", 7, first["owner_token"], repository_path=self.repo.main
        )
        self.assertEqual(
            storage.inspect_lock("owner/repo", 7, repository_path=self.repo.main)[
                "status"
            ],
            "absent",
        )
        second = storage.acquire_lock(
            "owner/repo", 7,
            campaign_id="crp-20260914T120000-abc123",
            acquired_at="2026-09-14T12:05:00Z",
            repository_path=self.repo.linked,
        )
        self.assertTrue(second["acquired"])

    def test_recovery_requires_explicit_authorization_and_matches_campaign(self) -> None:
        storage.acquire_lock(
            "owner/repo", 7,
            campaign_id="crp-20260914T120000-abc123",
            acquired_at="2026-09-14T12:00:00Z",
            repository_path=self.repo.main,
        )
        with self.assertRaises(RuntimeError):
            storage.recover_lock(
                "owner/repo", 7,
                user_authorized_recovery=False,
                repository_path=self.repo.main,
            )
        with self.assertRaises(RuntimeError):
            storage.recover_lock(
                "owner/repo", 7,
                user_authorized_recovery=True,
                expected_campaign_id="crp-other",
                repository_path=self.repo.linked,
            )
        result = storage.recover_lock(
            "owner/repo", 7,
            user_authorized_recovery=True,
            expected_campaign_id="crp-20260914T120000-abc123",
            repository_path=self.repo.main,
        )
        self.assertTrue(result["recovered"])

    def test_recovery_preserves_campaign_record(self) -> None:
        acquired = storage.acquire_lock(
            "owner/repo", 7,
            campaign_id="crp-20260914T120000-abc123",
            acquired_at="2026-09-14T12:00:00Z",
            repository_path=self.repo.main,
        )
        campaign_file = storage.campaign_path(
            "owner/repo", 7, repository_path=self.repo.main
        )
        storage.save_json(campaign_file, {"kept": True})
        storage.release_lock(
            "owner/repo", 7, acquired["owner_token"], repository_path=self.repo.main
        )
        storage.recover_lock(
            "owner/repo", 7, user_authorized_recovery=True,
            repository_path=self.repo.linked,
        )
        self.assertTrue(campaign_file.exists())

    def test_distinct_prs_get_distinct_locks(self) -> None:
        a = storage.lock_path("owner/repo", 1, repository_path=self.repo.main)
        b = storage.lock_path("owner/repo", 2, repository_path=self.repo.main)
        self.assertNotEqual(a, b)

    def test_malformed_json_document_fails_closed(self) -> None:
        target = storage.campaign_path(
            "owner/repo", 7, repository_path=self.repo.main
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("[1, 2]", encoding="utf-8")
        with self.assertRaises(ValueError):
            storage.load_json(target)

    def _cli(self, *args: str) -> subprocess.CompletedProcess:
        script = ROOT / "skills" / "codex-review-pulse" / "scripts" / "lock.py"
        return subprocess.run(
            [sys.executable, str(script), *args],
            cwd=str(self.repo.main),
            capture_output=True,
            text=True,
        )

    def test_stale_delivery_for_other_campaign_cannot_acquire(self) -> None:
        record = storage.campaign_path(
            "owner/repo", 7, repository_path=self.repo.main
        )
        record.parent.mkdir(parents=True, exist_ok=True)
        storage.save_json(
            record, {"campaign_id": "crp-20260914T120000Z-newnew"}
        )
        process = self._cli(
            "acquire", "--repo", "owner/repo", "--pr", "7",
            "--campaign-id", "crp-20260914T120000Z-oldold",
            "--acquired-at", "2026-09-14T12:00:00Z",
        )
        self.assertEqual(process.returncode, 2)
        self.assertIn("campaign_identity_mismatch", process.stdout)
        self.assertEqual(
            storage.inspect_lock(
                "owner/repo", 7, repository_path=self.repo.main
            )["status"],
            "absent",
        )

    def test_matching_campaign_can_acquire_via_cli(self) -> None:
        record = storage.campaign_path(
            "owner/repo", 7, repository_path=self.repo.main
        )
        record.parent.mkdir(parents=True, exist_ok=True)
        storage.save_json(
            record, {"campaign_id": "crp-20260914T120000Z-abc123"}
        )
        process = self._cli(
            "acquire", "--repo", "owner/repo", "--pr", "7",
            "--campaign-id", "crp-20260914T120000Z-abc123",
            "--acquired-at", "2026-09-14T12:00:00Z",
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(
            storage.inspect_lock(
                "owner/repo", 7, repository_path=self.repo.linked
            )["status"],
            "active",
        )


if __name__ == "__main__":
    unittest.main()
