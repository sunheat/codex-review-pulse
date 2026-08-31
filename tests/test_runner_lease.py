from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "codex-review-pulse" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from runner_lease import (  # noqa: E402
    acquire_lease,
    inspect_lease,
    release_lease,
    renew_lease,
)


NOW = datetime(2026, 8, 25, 0, 0, tzinfo=UTC)


class RunnerLeaseTests(unittest.TestCase):
    def test_two_runners_compete_and_only_one_acquires(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runner.lease.json"

            def acquire(owner: str) -> dict:
                return acquire_lease(
                    path,
                    repository="Owner/Repo",
                    pr_number=17,
                    owner_token=owner,
                    now=NOW,
                    duration_seconds=300,
                )

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(acquire, ("owner-a", "owner-b")))
            self.assertEqual(sum(result["acquired"] for result in results), 1)
            winner = next(result for result in results if result["acquired"])
            loser = next(result for result in results if not result["acquired"])
            self.assertEqual(
                loser["lease"]["owner_token"], winner["lease"]["owner_token"]
            )

    def test_non_owner_cannot_renew_or_release(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runner.lease.json"
            acquire_lease(
                path,
                repository="Owner/Repo",
                pr_number=17,
                owner_token="owner-a",
                now=NOW,
                duration_seconds=300,
            )
            with self.assertRaisesRegex(RuntimeError, "owner token mismatch"):
                renew_lease(
                    path,
                    repository="Owner/Repo",
                    pr_number=17,
                    owner_token="owner-b",
                    now=NOW + timedelta(seconds=1),
                    duration_seconds=300,
                )
            with self.assertRaisesRegex(RuntimeError, "owner token mismatch"):
                release_lease(
                    path,
                    repository="Owner/Repo",
                    pr_number=17,
                    owner_token="owner-b",
                )

    def test_owner_can_renew_and_release_with_compare_before_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runner.lease.json"
            acquired = acquire_lease(
                path,
                repository="Owner/Repo",
                pr_number=17,
                owner_token="owner-a",
                now=NOW,
                duration_seconds=300,
            )
            renewed = renew_lease(
                path,
                repository="Owner/Repo",
                pr_number=17,
                owner_token="owner-a",
                now=NOW + timedelta(seconds=20),
                duration_seconds=300,
            )
            self.assertEqual(renewed["acquired_at"], acquired["lease"]["acquired_at"])
            self.assertNotEqual(renewed["renewed_at"], acquired["lease"]["renewed_at"])
            release_lease(
                path,
                repository="Owner/Repo",
                pr_number=17,
                owner_token="owner-a",
            )
            self.assertFalse(path.exists())

    def test_renew_rejects_duration_outside_acquisition_bounds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runner.lease.json"
            acquire_lease(
                path,
                repository="Owner/Repo",
                pr_number=17,
                owner_token="owner-a",
                now=NOW,
                duration_seconds=300,
            )
            for duration in (29, 86401, 0, -1):
                with self.subTest(duration=duration):
                    with self.assertRaisesRegex(ValueError, "between 30 and 86400"):
                        renew_lease(
                            path,
                            repository="Owner/Repo",
                            pr_number=17,
                            owner_token="owner-a",
                            now=NOW + timedelta(seconds=1),
                            duration_seconds=duration,
                        )

    def test_stale_lease_is_recovered_without_pid_assumptions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runner.lease.json"
            acquire_lease(
                path,
                repository="Owner/Repo",
                pr_number=17,
                owner_token="crashed-owner",
                now=NOW,
                duration_seconds=30,
            )
            recovered = acquire_lease(
                path,
                repository="Owner/Repo",
                pr_number=17,
                owner_token="recovery-owner",
                now=NOW + timedelta(seconds=31),
                duration_seconds=300,
            )
            self.assertTrue(recovered["acquired"])
            self.assertTrue(recovered["stale_recovered"])
            self.assertEqual(recovered["lease"]["owner_token"], "recovery-owner")

    def test_inspect_is_read_only_and_does_not_create_lease_or_guard(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runner.lease.json"
            result = inspect_lease(
                path,
                repository="Owner/Repo",
                pr_number=17,
                now=NOW,
            )
            self.assertEqual(result["status"], "absent")
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_inspect_never_returns_owner_capability(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runner.lease.json"
            acquire_lease(
                path,
                repository="Owner/Repo",
                pr_number=17,
                owner_token="secret-owner-token",
                now=NOW,
                duration_seconds=300,
            )
            result = inspect_lease(
                path,
                repository="Owner/Repo",
                pr_number=17,
                now=NOW,
            )
            self.assertEqual(result["status"], "active")
            self.assertNotIn("owner_token", result)


if __name__ == "__main__":
    unittest.main()
