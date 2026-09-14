"""End-to-end CLI glue for the deterministic delivery skeleton (no network)."""

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


def git(cwd: Path, *args: str) -> str:
    process = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True
    )
    if process.returncode != 0:
        raise RuntimeError(f"git {args} failed: {process.stderr.strip()}")
    return process.stdout.strip()


SNAPSHOT = {
    "complete": True,
    "server_time": "2026-09-14T12:00:00Z",
    "pr_state": "OPEN",
    "head_oid": "H1",
    "head_ref_name": "feature",
    "node_id": "PR1",
    "viewer": "operator",
    "repository": "owner/repo",
    "pr_number": 7,
    "threads": [],
    "reactions": [],
    "reviews": [],
    "comments": [],
}


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
        self.snapshot_file = self.repo / "snapshot.json"
        self.snapshot_file.write_text(json.dumps(SNAPSHOT), encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def cli(self, script: str, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(SCRIPTS / script), *args],
            cwd=str(self.repo),
            capture_output=True,
            text=True,
        )

    def test_delivery_skeleton_acquire_decide_terminate_release(self) -> None:
        acquired = self.cli(
            "lock.py", "acquire", "--repo", "owner/repo", "--pr", "7",
            "--generate-campaign-id", "--acquired-at", SNAPSHOT["server_time"],
        )
        self.assertEqual(acquired.returncode, 0, acquired.stderr)
        acquire_payload = json.loads(acquired.stdout)
        campaign_id = acquire_payload["campaign_id"]
        token = acquire_payload["owner_token"]
        self.assertTrue(campaign_id.startswith("crp-20260914T120000Z-"))

        # A second delivery is blocked without consuming anything.
        blocked = self.cli(
            "lock.py", "acquire", "--repo", "owner/repo", "--pr", "7",
            "--campaign-id", campaign_id,
            "--acquired-at", SNAPSHOT["server_time"],
        )
        self.assertEqual(blocked.returncode, 2)

        init = self.cli(
            "campaign.py", "init", "--repo", "owner/repo", "--pr", "7",
            "--snapshot", "snapshot.json", "--max-rounds", "2",
            "--model", "m", "--reasoning-level", "low",
            "--interval-minutes", "30", "--owner-token", token,
        )
        self.assertEqual(init.returncode, 0, init.stderr)
        initialized = json.loads(init.stdout)["campaign"]
        self.assertEqual(initialized["campaign_id"], campaign_id)
        self.assertEqual(initialized["rounds_used"], 0)

        self.cli(
            "campaign.py", "sync-head", "--repo", "owner/repo", "--pr", "7",
            "--head-oid", "H1", "--server-time", SNAPSHOT["server_time"],
            "--owner-token", token,
        )

        directive = self.cli(
            "decide.py", "--repo", "owner/repo", "--pr", "7",
            "--snapshot", "snapshot.json",
        )
        self.assertEqual(directive.returncode, 0, directive.stderr)
        self.assertEqual(json.loads(directive.stdout)["action"], "request_review")

        # A wrong-identity token cannot drive the campaign.
        bad = self.cli(
            "campaign.py", "consume-round", "--repo", "owner/repo", "--pr", "7",
            "--owner-token", "wrong",
        )
        self.assertNotEqual(bad.returncode, 0)

        consumed = self.cli(
            "campaign.py", "consume-round", "--repo", "owner/repo", "--pr", "7",
            "--owner-token", token,
        )
        self.assertEqual(consumed.returncode, 0, consumed.stderr)
        self.assertEqual(json.loads(consumed.stdout)["campaign"]["rounds_used"], 1)

        terminated = self.cli(
            "campaign.py", "terminate", "--repo", "owner/repo", "--pr", "7",
            "--status", "manual_intervention_required",
            "--at", "2026-09-14T12:05:00Z", "--owner-token", token,
        )
        self.assertEqual(terminated.returncode, 0, terminated.stderr)

        # The next stale delivery sees a terminal campaign at show time.
        show = self.cli("campaign.py", "show", "--repo", "owner/repo", "--pr", "7")
        self.assertEqual(
            json.loads(show.stdout)["status"], "manual_intervention_required"
        )

        released = self.cli(
            "lock.py", "release", "--repo", "owner/repo", "--pr", "7",
            "--owner-token", token,
        )
        self.assertEqual(released.returncode, 0, released.stderr)
        inspect = self.cli("lock.py", "inspect", "--repo", "owner/repo", "--pr", "7")
        self.assertEqual(json.loads(inspect.stdout)["status"], "absent")

    def test_abort_removes_unstarted_campaign_and_lock(self) -> None:
        acquired = self.cli(
            "lock.py", "acquire", "--repo", "owner/repo", "--pr", "8",
            "--generate-campaign-id", "--acquired-at", SNAPSHOT["server_time"],
        )
        token = json.loads(acquired.stdout)["owner_token"]
        self.cli(
            "campaign.py", "init", "--repo", "owner/repo", "--pr", "8",
            "--snapshot", "snapshot.json", "--max-rounds", "3",
            "--model", "m", "--reasoning-level", "medium",
            "--interval-minutes", "15", "--owner-token", token,
        )
        aborted = self.cli(
            "campaign.py", "abort", "--repo", "owner/repo", "--pr", "8",
            "--owner-token", token,
        )
        self.assertEqual(aborted.returncode, 0, aborted.stderr)
        inspect = self.cli("lock.py", "inspect", "--repo", "owner/repo", "--pr", "8")
        self.assertEqual(json.loads(inspect.stdout)["status"], "absent")


if __name__ == "__main__":
    unittest.main()
