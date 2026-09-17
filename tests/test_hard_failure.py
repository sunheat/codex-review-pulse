"""Environment-gate hard-failure handoff (no network).

Covers the closed reason-code set, the pure ``hard_fail`` transition, and the
production-facing ``owned.py hard-fail`` handoff a scheduled delivery invokes:
budget forfeiture, the durable ``hard_failed`` terminal state, idle duplicate
and busy exits, stale-delivery protection, and independent persistence/release
failure reporting. These tests validate the concrete handoff and durable
transition only; they are not proof that a real ChatGPT Desktop denial payload
has been observed.
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "codex-review-pulse" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import campaign_model as model  # noqa: E402
import owned  # noqa: E402
import storage  # noqa: E402


CAMPAIGN_ID = "crp-20260914T120000Z-abc123"
T0 = "2026-09-14T12:00:00Z"
NOW = "2026-09-14T13:00:00Z"


def git(cwd: Path, *args: str) -> str:
    process = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True
    )
    if process.returncode != 0:
        raise RuntimeError(f"git {args} failed: {process.stderr.strip()}")
    return process.stdout.strip()


class HardFailureModelTests(unittest.TestCase):
    def base_campaign(self) -> dict:
        return model.new_campaign(
            campaign_id=CAMPAIGN_ID,
            repository="owner/repo",
            pull_request_number=7,
            created_at=T0,
            max_rounds=3,
            model="m",
            reasoning_level="low",
            interval_minutes=30,
            reviewer_logins=["chatgpt-codex-connector"],
            approval_logins=["chatgpt-codex-connector"],
        )

    def test_hard_fail_forfeits_budget_and_terminalizes(self) -> None:
        campaign = model.hard_fail(
            self.base_campaign(),
            at=NOW,
            reason="unsupported_execution_mode",
            detail="task mode metadata says ChatGPT Work",
        )
        self.assertEqual(campaign["status"], model.HARD_FAILED)
        self.assertEqual(campaign["rounds_used"], 3)
        self.assertEqual(
            campaign["status_detail"],
            "unsupported_execution_mode: task mode metadata says ChatGPT Work",
        )
        model.validate_campaign(
            campaign, repository="owner/repo", pull_request_number=7
        )
        self.assertTrue(model.is_terminal(campaign))
        self.assertFalse(model.is_rollover_eligible(campaign))

    def test_hard_fail_closes_an_open_request_window(self) -> None:
        snapshot = {
            "complete": True,
            "head_oid": "h1",
            "reactions": [],
            "reviews": [],
            "threads": [],
            "comments": [],
        }
        campaign = model.reserve_request(
            self.base_campaign(), head_oid="h1", reserved_at=T0, snapshot=snapshot
        )
        campaign = model.open_request_window(
            campaign,
            head_oid="h1",
            post_head_oid="h1",
            request_node_id="r1",
            request_created_at=NOW,
            request_url="u",
        )
        campaign = model.hard_fail(
            campaign,
            at=NOW,
            reason="insufficient_effective_access",
            detail="sandbox restricted",
        )
        self.assertEqual(campaign["status"], model.HARD_FAILED)
        self.assertEqual(campaign["rounds_used"], 3)
        self.assertEqual(
            model.guard_for_head(campaign, "h1")["state"], model.CLOSED
        )
        model.validate_campaign(
            campaign, repository="owner/repo", pull_request_number=7
        )

    def test_hard_fail_refuses_unknown_reasons_and_empty_detail(self) -> None:
        for reason in ("PermissionError", "access denied", "", "unsupported_execution_mode "):
            with self.assertRaises(ValueError):
                model.hard_fail(
                    self.base_campaign(), at=NOW, reason=reason, detail="d"
                )
        with self.assertRaises(ValueError):
            model.hard_fail(
                self.base_campaign(),
                at=NOW,
                reason="host_authorization_denied",
                detail="   ",
            )

    def test_hard_fail_refuses_reserved_ambiguity(self) -> None:
        snapshot = {
            "complete": True,
            "head_oid": "h1",
            "reactions": [],
            "reviews": [],
            "threads": [],
            "comments": [],
        }
        campaign = model.reserve_request(
            self.base_campaign(), head_oid="h1", reserved_at=T0, snapshot=snapshot
        )
        with self.assertRaises(RuntimeError):
            model.hard_fail(
                campaign,
                at=NOW,
                reason="host_authorization_denied",
                detail="d",
            )

    def test_hard_fail_caps_the_concise_detail(self) -> None:
        campaign = model.hard_fail(
            self.base_campaign(),
            at=NOW,
            reason="host_authorization_denied",
            detail="x" * 900,
        )
        self.assertEqual(
            campaign["status_detail"],
            "host_authorization_denied: " + "x" * model.HARD_FAILURE_DETAIL_LIMIT,
        )


class HandoffFixture(unittest.TestCase):
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
        self.save_campaign()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def save_campaign(self, campaign_id: str = CAMPAIGN_ID) -> None:
        campaign = model.new_campaign(
            campaign_id=campaign_id,
            repository="owner/repo",
            pull_request_number=7,
            created_at=T0,
            max_rounds=3,
            model="m",
            reasoning_level="low",
            interval_minutes=30,
            reviewer_logins=["chatgpt-codex-connector"],
            approval_logins=["chatgpt-codex-connector"],
        )
        storage.save_json(self.campaign_path(), campaign)

    def campaign_path(self) -> Path:
        return storage.campaign_path("owner/repo", 7, repository_path=self.repo)

    def campaign(self) -> dict:
        return storage.load_json(self.campaign_path())

    def lock_status(self) -> str:
        return storage.inspect_lock(
            "owner/repo", 7, repository_path=self.repo
        )["status"]

    def handoff(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "owned.py"),
                "hard-fail",
                "--repo",
                "owner/repo",
                "--pr",
                "7",
                *args,
            ],
            cwd=str(self.repo),
            capture_output=True,
            text=True,
        )


class HardFailureHandoffTests(HandoffFixture):
    """The production-facing scheduled-worker/skill handoff (CLI subprocess)."""

    def test_handoff_on_owned_campaign_forfeits_budget_and_terminalizes(self) -> None:
        result = self.handoff(
            "--campaign-id",
            CAMPAIGN_ID,
            "--reason",
            "unsupported_execution_mode",
            "--detail",
            "host identifies this delivery as ChatGPT Work",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["recorded"])
        self.assertEqual(payload["outcome"], "hard_failed")
        self.assertEqual(payload["status"], model.HARD_FAILED)
        self.assertEqual(payload["reason"], "unsupported_execution_mode")
        self.assertEqual(payload["rounds_used"], 3)
        self.assertEqual(payload["max_rounds"], 3)
        self.assertIn("unsupported_execution_mode", payload["status_detail"])
        self.assertEqual(payload["ownership"], "released")
        self.assertTrue(payload["scheduler_cleanup_authorized"])

        record = self.campaign()
        self.assertEqual(record["status"], model.HARD_FAILED)
        self.assertEqual(record["rounds_used"], record["config"]["max_rounds"])
        self.assertIn("unsupported_execution_mode", record["status_detail"])
        model.validate_campaign(
            record, repository="owner/repo", pull_request_number=7
        )
        self.assertEqual(self.lock_status(), "absent")

    def test_repeated_handoff_delivery_is_a_non_counting_idle_exit(self) -> None:
        first = self.handoff(
            "--campaign-id",
            CAMPAIGN_ID,
            "--reason",
            "unsupported_execution_mode",
            "--detail",
            "d",
        )
        self.assertTrue(json.loads(first.stdout)["recorded"])

        second = self.handoff(
            "--campaign-id",
            CAMPAIGN_ID,
            "--reason",
            "host_authorization_denied",
            "--detail",
            "duplicate delivery",
        )
        self.assertEqual(second.returncode, 0, second.stderr)
        payload = json.loads(second.stdout)
        self.assertFalse(payload["recorded"])
        self.assertEqual(payload["acquisition_status"], "campaign_terminal")
        record = self.campaign()
        self.assertEqual(record["status"], model.HARD_FAILED)
        self.assertIn("unsupported_execution_mode", record["status_detail"])
        self.assertEqual(self.lock_status(), "absent")

    def test_busy_lock_is_a_non_counting_idle_exit(self) -> None:
        acquired = storage.acquire_worker_lock(
            "owner/repo",
            7,
            campaign_id=CAMPAIGN_ID,
            acquired_at=T0,
            repository_path=self.repo,
        )
        self.assertTrue(acquired["acquired"])

        result = self.handoff(
            "--campaign-id",
            CAMPAIGN_ID,
            "--reason",
            "insufficient_effective_access",
            "--detail",
            "d",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["recorded"])
        self.assertEqual(payload["acquisition_status"], "busy")
        record = self.campaign()
        self.assertEqual(record["status"], model.ACTIVE)
        self.assertEqual(record["rounds_used"], 0)
        self.assertEqual(self.lock_status(), "active")

    def test_stale_delivery_cannot_modify_a_newer_campaign(self) -> None:
        result = self.handoff(
            "--campaign-id",
            "crp-20260914T120000Z-ffffff",
            "--reason",
            "unsupported_execution_mode",
            "--detail",
            "d",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["recorded"])
        self.assertEqual(payload["acquisition_status"], "campaign_identity_mismatch")
        record = self.campaign()
        self.assertEqual(record["status"], model.ACTIVE)
        self.assertEqual(record["campaign_id"], CAMPAIGN_ID)
        self.assertEqual(record["rounds_used"], 0)
        self.assertEqual(self.lock_status(), "absent")

    def test_generic_errors_are_refused_without_state_change(self) -> None:
        result = self.handoff(
            "--campaign-id",
            CAMPAIGN_ID,
            "--reason",
            "PermissionError",
            "--detail",
            "d",
        )
        self.assertNotEqual(result.returncode, 0)
        record = self.campaign()
        self.assertEqual(record["status"], model.ACTIVE)
        self.assertEqual(record["rounds_used"], 0)
        self.assertEqual(self.lock_status(), "absent")

    def test_malformed_campaign_state_is_preserved_fail_closed(self) -> None:
        self.campaign_path().write_text(
            json.dumps({"schema_version": 3, "campaign_id": CAMPAIGN_ID}),
            encoding="utf-8",
        )
        result = self.handoff(
            "--campaign-id",
            CAMPAIGN_ID,
            "--reason",
            "unsupported_execution_mode",
            "--detail",
            "d",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["recorded"])
        self.assertEqual(payload["acquisition_status"], "campaign_malformed")
        self.assertEqual(
            json.loads(self.campaign_path().read_text(encoding="utf-8")),
            {"schema_version": 3, "campaign_id": CAMPAIGN_ID},
        )
        self.assertEqual(self.lock_status(), "absent")


class HandoffFailureReportingTests(HandoffFixture):
    """Persistence and release failures are reported independently."""

    def call(self, **overrides) -> dict:
        arguments = {
            "repository": "owner/repo",
            "pr_number": 7,
            "campaign_id": CAMPAIGN_ID,
            "reason": "insufficient_effective_access",
            "detail": "d",
            "repository_path": self.repo,
            "now": lambda: NOW,
        }
        arguments.update(overrides)
        return owned.record_hard_failure(**arguments)

    def test_persistence_failure_reports_unconfirmed_without_success_claim(self) -> None:
        with mock.patch.object(
            storage, "transition_active_campaign", side_effect=OSError("write blocked")
        ):
            result = self.call()
        self.assertFalse(result["recorded"])
        self.assertEqual(result["outcome"], "persistence_unconfirmed")
        self.assertEqual(result["ownership"], "retained")
        self.assertFalse(result["scheduler_cleanup_authorized"])
        self.assertIn("write blocked", result["reason"])
        self.assertEqual(self.campaign()["status"], model.ACTIVE)
        self.assertEqual(self.campaign()["rounds_used"], 0)
        self.assertEqual(self.lock_status(), "active")

    def test_release_failure_keeps_the_durable_state_but_retains_ownership(self) -> None:
        with mock.patch.object(
            storage, "release_lock", side_effect=OSError("release blocked")
        ):
            result = self.call()
        self.assertTrue(result["recorded"])
        self.assertEqual(result["status"], model.HARD_FAILED)
        self.assertEqual(result["ownership"], "retained")
        self.assertFalse(result["scheduler_cleanup_authorized"])
        self.assertIn("release blocked", result["release_error"])
        record = self.campaign()
        self.assertEqual(record["status"], model.HARD_FAILED)
        self.assertEqual(record["rounds_used"], 3)
        self.assertEqual(self.lock_status(), "active")

    def test_boundary_refuses_unknown_reason_before_acquiring(self) -> None:
        with self.assertRaises(ValueError):
            self.call(reason="looks like a permission word")
        self.assertEqual(self.lock_status(), "absent")
        self.assertEqual(self.campaign()["rounds_used"], 0)


if __name__ == "__main__":
    unittest.main()
