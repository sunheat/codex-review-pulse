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

import admission  # noqa: E402
import campaign_model as model  # noqa: E402
import github_api  # noqa: E402
import storage  # noqa: E402


def git(cwd: Path, *args: str) -> str:
    process = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True
    )
    if process.returncode != 0:
        raise RuntimeError(f"git {args} failed: {process.stderr.strip()}")
    return process.stdout.strip()


def complete_snapshot(head: str = "H1") -> dict:
    return {
        "complete": True,
        "server_time": "2026-09-14T12:00:00Z",
        "pr_state": "OPEN",
        "head_oid": head,
        "head_ref_name": "feature",
        "head_repository": "owner/repo",
        "node_id": "PR1",
        "viewer": "operator",
        "repository": "owner/repo",
        "pr_number": 7,
        "threads": [],
        "reactions": [],
        "reviews": [],
        "comments": [],
    }


def campaign(status: str = model.ACTIVE) -> dict:
    data = model.new_campaign(
        campaign_id="crp-20260914T120000Z-abc123",
        repository="owner/repo",
        pull_request_number=7,
        created_at="2026-09-14T12:00:00Z",
        max_rounds=6,
        model="a-model",
        reasoning_level="medium",
        interval_minutes=30,
        reviewer_logins=["chatgpt-codex-connector"],
        approval_logins=["chatgpt-codex-connector"],
    )
    if status != model.ACTIVE:
        data = model.terminate(data, status=status, at="2026-09-14T12:05:00Z")
    return data


class AdmissionTests(unittest.TestCase):
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
        self._original_fetch = admission.github_api.fetch_snapshot
        admission.github_api.fetch_snapshot = lambda *a, **k: complete_snapshot()

    def tearDown(self) -> None:
        admission.github_api.fetch_snapshot = self._original_fetch
        self._tmp.cleanup()

    def save_campaign(self, record: dict | None) -> None:
        if record is None:
            return
        storage.save_json(
            storage.campaign_path("owner/repo", 7, repository_path=self.repo),
            record,
        )

    def envelope(self) -> dict:
        return admission.run_admission(
            "owner/repo", 7, repository_path=self.repo
        )

    def test_absent_campaign_with_complete_observation(self) -> None:
        envelope = self.envelope()
        self.assertTrue(envelope["snapshot_complete"])
        self.assertEqual(envelope["campaign_record"], "absent")
        self.assertEqual(envelope["lock_status"], "absent")
        self.assertEqual(envelope["head_oid"], "H1")
        self.assertEqual(envelope["head_repository"], "owner/repo")
        snapshot_file = Path(envelope["snapshot_path"])
        self.assertTrue(snapshot_file.exists())
        self.assertEqual(
            json.loads(snapshot_file.read_text(encoding="utf-8")),
            complete_snapshot(),
        )

    def test_active_campaign_is_reported_with_identity(self) -> None:
        self.save_campaign(campaign())
        envelope = self.envelope()
        self.assertEqual(envelope["campaign_record"], "active")
        self.assertEqual(envelope["campaign_id"], "crp-20260914T120000Z-abc123")
        self.assertEqual(envelope["campaign_status"], model.ACTIVE)
        self.assertEqual(envelope["rounds_used"], 0)

    def test_terminal_campaign_is_reported_without_mutation(self) -> None:
        self.save_campaign(campaign(status=model.SUCCEEDED))
        envelope = self.envelope()
        self.assertEqual(envelope["campaign_record"], "terminal")
        self.assertEqual(envelope["campaign_status"], model.SUCCEEDED)

    def test_malformed_campaign_fails_closed(self) -> None:
        bad = campaign()
        bad["schema_version"] = 99
        self.save_campaign(bad)
        with self.assertRaises(ValueError):
            self.envelope()

    def test_incomplete_observation_is_reported_not_absence(self) -> None:
        admission.github_api.fetch_snapshot = lambda *a, **k: {
            **complete_snapshot(),
            "complete": False,
        }
        envelope = self.envelope()
        self.assertFalse(envelope["snapshot_complete"])
        self.assertEqual(envelope["campaign_record"], "absent")

    def test_observation_failure_propagates_and_writes_nothing(self) -> None:
        def broken(*args, **kwargs):
            raise RuntimeError("gh offline")

        admission.github_api.fetch_snapshot = broken
        with self.assertRaisesRegex(RuntimeError, "gh offline"):
            self.envelope()


if __name__ == "__main__":
    unittest.main()
