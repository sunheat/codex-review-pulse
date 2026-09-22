"""End-to-end CLI glue for the narrowed v2 product surface (no network).

The product mutation paths live behind the deterministic owned boundaries
(``owned.py``). This file exercises the remaining campaign/lock CLI surface:
read-only inspection, owner-authorized local operations, and the recovery
boundaries.
"""

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

import campaign_model as model  # noqa: E402
import storage  # noqa: E402


def git(cwd: Path, *args: str) -> str:
    process = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True
    )
    if process.returncode != 0:
        raise RuntimeError(f"git {args} failed: {process.stderr.strip()}")
    return process.stdout.strip()


CAMPAIGN_ID = "crp-20260914T120000Z-abc123"
ACQUIRED_AT = "2026-09-14T12:00:00Z"


class WorkerCliFlowTests(unittest.TestCase):
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

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def cli(self, script: str, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(SCRIPTS / script), *args],
            cwd=str(self.repo),
            capture_output=True,
            text=True,
        )

    def save_campaign(self, pr: int = 7, **kwargs) -> dict:
        campaign = model.new_campaign(
            campaign_id=CAMPAIGN_ID,
            repository="owner/repo",
            pull_request_number=pr,
            created_at=ACQUIRED_AT,
            max_rounds=2,
            model="m",
            reasoning_level="low",
            interval_minutes=30,
            reviewer_logins=["chatgpt-codex-connector"],
            approval_logins=["chatgpt-codex-connector"],
            **kwargs,
        )
        storage.save_json(
            storage.campaign_path("owner/repo", pr, repository_path=self.repo),
            campaign,
        )
        return campaign

    def test_old_product_mutation_bypasses_are_not_exposed(self) -> None:
        for command in ("init", "consume-round", "sync-head", "terminate"):
            result = self.cli("campaign.py", command, "--help")
            self.assertNotEqual(result.returncode, 0)
        help_text = self.cli("campaign.py", "--help").stdout
        for command in ("init", "consume-round", "sync-head", "terminate"):
            self.assertNotIn(command, help_text)

    def test_show_reports_campaign_state_and_fails_closed(self) -> None:
        missing = self.cli("campaign.py", "show", "--repo", "owner/repo", "--pr", "7")
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("does not exist", missing.stderr)

        self.save_campaign()
        shown = self.cli("campaign.py", "show", "--repo", "owner/repo", "--pr", "7")
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertEqual(
            json.loads(shown.stdout)["campaign_id"], CAMPAIGN_ID
        )

    def test_setup_acquire_then_cancel_setup_releases_the_lock(self) -> None:
        acquired = self.cli(
            "lock.py", "acquire", "--repo", "owner/repo", "--pr", "7",
            "--campaign-id", CAMPAIGN_ID, "--acquired-at", ACQUIRED_AT,
            "--purpose", "setup",
        )
        self.assertEqual(acquired.returncode, 0, acquired.stderr)
        token = json.loads(acquired.stdout)["owner_token"]

        cancelled = self.cli(
            "campaign.py", "cancel-setup", "--repo", "owner/repo", "--pr", "7",
            "--owner-token", token, "--expected-campaign-id", CAMPAIGN_ID,
        )
        self.assertEqual(cancelled.returncode, 0, cancelled.stderr)
        inspect = self.cli("lock.py", "inspect", "--repo", "owner/repo", "--pr", "7")
        self.assertEqual(json.loads(inspect.stdout)["status"], "absent")

    def test_abort_removes_unstarted_campaign_and_lock(self) -> None:
        acquired = self.cli(
            "lock.py", "acquire", "--repo", "owner/repo", "--pr", "8",
            "--campaign-id", CAMPAIGN_ID, "--acquired-at", ACQUIRED_AT,
            "--purpose", "setup",
        )
        token = json.loads(acquired.stdout)["owner_token"]
        self.save_campaign(pr=8)
        aborted = self.cli(
            "campaign.py", "abort", "--repo", "owner/repo", "--pr", "8",
            "--owner-token", token,
        )
        self.assertEqual(aborted.returncode, 0, aborted.stderr)
        inspect = self.cli("lock.py", "inspect", "--repo", "owner/repo", "--pr", "8")
        self.assertEqual(json.loads(inspect.stdout)["status"], "absent")

    def test_retired_campaign_cannot_be_recreated_by_stale_deliveries(self) -> None:
        acquired = self.cli(
            "lock.py", "acquire", "--repo", "owner/repo", "--pr", "9",
            "--campaign-id", CAMPAIGN_ID, "--acquired-at", ACQUIRED_AT,
            "--purpose", "setup",
        )
        token = json.loads(acquired.stdout)["owner_token"]
        self.save_campaign(pr=9)

        retired = self.cli(
            "campaign.py", "retire", "--repo", "owner/repo", "--pr", "9",
            "--expected-campaign-id", CAMPAIGN_ID, "--retained-lock",
            "--user-authorized-retirement",
        )
        self.assertEqual(retired.returncode, 0, retired.stderr)

        stale = self.cli(
            "lock.py", "acquire", "--repo", "owner/repo", "--pr", "9",
            "--campaign-id", CAMPAIGN_ID, "--acquired-at", ACQUIRED_AT,
            "--purpose", "worker",
        )
        self.assertEqual(stale.returncode, 2)
        self.assertEqual(json.loads(stale.stdout)["status"], "campaign_absent")

        unauthorized = self.cli(
            "campaign.py", "retire", "--repo", "owner/repo", "--pr", "9",
            "--expected-campaign-id", CAMPAIGN_ID, "--retained-lock",
        )
        self.assertNotEqual(unauthorized.returncode, 0)

    def test_terminal_campaign_releases_normally(self) -> None:
        self.save_campaign(pr=10)
        acquired = self.cli(
            "lock.py", "acquire", "--repo", "owner/repo", "--pr", "10",
            "--campaign-id", CAMPAIGN_ID, "--acquired-at", ACQUIRED_AT,
            "--purpose", "worker",
        )
        self.assertEqual(acquired.returncode, 0, acquired.stderr)
        token = json.loads(acquired.stdout)["owner_token"]
        campaign = storage.load_json(
            storage.campaign_path("owner/repo", 10, repository_path=self.repo)
        )
        terminal = model.terminate(
            campaign, status=model.SUCCEEDED, at="2026-09-14T13:00:00Z"
        )
        storage.save_json(
            storage.campaign_path("owner/repo", 10, repository_path=self.repo),
            terminal,
        )
        released = self.cli(
            "lock.py", "release", "--repo", "owner/repo", "--pr", "10",
            "--owner-token", token,
        )
        self.assertEqual(released.returncode, 0, released.stderr)
        inspect = self.cli("lock.py", "inspect", "--repo", "owner/repo", "--pr", "10")
        self.assertEqual(json.loads(inspect.stdout)["status"], "absent")

    def test_finalize_exhaustion_cli_is_no_longer_exposed(self) -> None:
        # The old model-visible finalize-exhaustion command was removed with
        # the legacy long-lock remediation path: the deterministic remediation
        # finalizer applies the direct exhaustion transition inside the same
        # invocation that commits the final remediation round.
        result = self.cli(
            "owned.py", "finalize-exhaustion", "--repo", "owner/repo", "--pr", "11",
            "--owner-token", "t",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid choice", result.stderr)


if __name__ == "__main__":
    unittest.main()
