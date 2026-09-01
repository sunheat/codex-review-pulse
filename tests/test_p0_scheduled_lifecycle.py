from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "codex-review-pulse" / "scripts"
TESTS = ROOT / "tests"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(TESTS))

from recurring_model import clear_failure_latch, evaluate_recurring_action  # noqa: E402
from runner_lease import acquire_lease, assert_lease_owner  # noqa: E402
from test_heartbeat_tick import (  # noqa: E402
    NOW,
    checkout_ok,
    create_contract,
    create_installation,
    git_init,
    observation,
    plan_tick,
    verified_runtime,
)
from heartbeat_tick import complete_tick  # noqa: E402


class P0ScheduledLifecycleTests(unittest.TestCase):
    def test_hardened_same_wake_plan_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory_name:
            repository = Path(directory_name) / "repo"
            repository.mkdir()
            git_init(repository)
            installation = Path(directory_name) / "installed" / "codex-review-pulse"
            create_installation(installation)
            contract_path = create_contract(repository, installation)
            first = plan_tick(
                contract_path=contract_path,
                repository_path=repository,
                observation=observation(),
                now=NOW,
                owner_token="owner-a",
                wake_id="host-wake-1",
                pause_heartbeat=lambda: True,
                checkout_inspector=checkout_ok,
                runtime_script_path=verified_runtime(installation),
            )
            second = plan_tick(
                contract_path=contract_path,
                repository_path=repository,
                observation=observation(server_time="2026-08-25T00:01:00+00:00"),
                now="2026-08-25T00:01:00+00:00",
                owner_token="owner-a",
                wake_id="host-wake-1",
                pause_heartbeat=lambda: True,
                checkout_inspector=checkout_ok,
                runtime_script_path=verified_runtime(installation),
            )
            self.assertEqual(first["wake_count"], 1)
            self.assertTrue(second["duplicate_wake"])
            self.assertEqual(second["wake_count"], 1)

    def test_hardened_pause_blocks_a_followup_wake_without_clear_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory_name:
            repository = Path(directory_name) / "repo"
            repository.mkdir()
            git_init(repository)
            installation = Path(directory_name) / "installed" / "codex-review-pulse"
            create_installation(installation)
            contract_path = create_contract(repository, installation)
            blocked = plan_tick(
                contract_path=contract_path,
                repository_path=repository,
                observation=observation(),
                now=NOW,
                owner_token="owner-a",
                wake_id="host-wake-1",
                pause_heartbeat=lambda: True,
                checkout_inspector=lambda *args, **kwargs: {"ok": False, "errors": ["drift"]},
                runtime_script_path=verified_runtime(installation),
            )
            self.assertEqual(blocked["next_action"], "PAUSE_BLOCKED")
            replay = plan_tick(
                contract_path=contract_path,
                repository_path=repository,
                observation=observation(),
                now="2026-08-25T00:01:00+00:00",
                owner_token="owner-a",
                wake_id="host-wake-1",
                pause_heartbeat=lambda: True,
                checkout_inspector=checkout_ok,
                runtime_script_path=verified_runtime(installation),
            )
            self.assertTrue(replay["duplicate_wake"])
            self.assertEqual(replay["wake_count"], 1)

    def test_hardened_completion_sets_completion_relative_next_wake(self) -> None:
        with tempfile.TemporaryDirectory() as directory_name:
            repository = Path(directory_name) / "repo"
            repository.mkdir()
            git_init(repository)
            installation = Path(directory_name) / "installed" / "codex-review-pulse"
            create_installation(installation)
            contract_path = create_contract(repository, installation)
            planned = plan_tick(
                contract_path=contract_path,
                repository_path=repository,
                observation=observation(),
                now=NOW,
                owner_token="owner-a",
                wake_id="host-wake-1",
                pause_heartbeat=lambda: True,
                checkout_inspector=checkout_ok,
                runtime_script_path=verified_runtime(installation),
            )
            self.assertEqual(planned["next_action"], "WAIT_REVIEW")
            self.assertEqual(planned["lease"]["status"], "retained")
            completed = complete_tick(
                contract_path=contract_path,
                repository_path=repository,
                owner_token="owner-a",
                wake_id="host-wake-1",
                final_observation=observation(),
                now="2026-08-25T00:46:00+00:00",
                mutation_occurred=False,
                runtime_script_path=verified_runtime(installation),
            )
            self.assertEqual(completed["next_not_before"], "2026-08-25T00:56:00+00:00")
            self.assertEqual(completed["scheduled_task_disposition"], "PAUSED")

    def test_expiring_hardened_lease_fails_closed_after_thirty_minutes(self) -> None:
        with tempfile.TemporaryDirectory() as directory_name:
            lease = Path(directory_name) / "lease.json"
            acquired = acquire_lease(
                lease,
                repository="Owner/Repo",
                pr_number=17,
                owner_token="owner-a",
                now=NOW,
                duration_seconds=1800,
            )
            self.assertTrue(acquired["acquired"])
            with self.assertRaisesRegex(RuntimeError, "expired"):
                assert_lease_owner(
                    lease,
                    repository="Owner/Repo",
                    pr_number=17,
                    owner_token="owner-a",
                    now="2026-08-26T00:30:01+00:00",
                )
            decision = evaluate_recurring_action(
                contract={"wait_policy": {"minimum_stable_observations": 2, "minimum_server_wait_seconds": 600}},
                observation={"lease_status": "lost", "auth_ok": True, "api_ok": True},
                state={},
                now="2026-08-26T00:30:01+00:00",
            )
            self.assertEqual(decision["next_action"], "PAUSE_CONCURRENT")

    def test_plain_recovery_id_cannot_clear_latch(self) -> None:
        state = {"failure_latch": {"reason_code": "pause"}}
        with self.assertRaisesRegex(ValueError, "Verified"):
            clear_failure_latch(state, recovery_authorization_id="scheduled-agent-made-this")


if __name__ == "__main__":
    unittest.main()
