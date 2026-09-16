"""Phase 1 guarded authority/state kernel tests.

Covers guarded initialization, active-campaign ordinary authority, guarded
local transition serialization, same-owner stale-write protection, ordinary
release, setup cancellation, specialized unused-campaign abort, the fixed
rollover identity transition (including the deliberate partial-rollover
state), explicit recovery identity protection, and explicit user-authorized
campaign retirement. Network-free.
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "codex-review-pulse" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import campaign_model as model  # noqa: E402
import storage  # noqa: E402


CAMPAIGN_ID = "crp-20260914T120000Z-abc123"
C2_ID = "crp-20260915T120000Z-def456"
ACQUIRED_AT = "2026-09-14T12:00:00Z"
T1 = "2026-09-14T13:00:00Z"


def git(cwd: Path, *args: str) -> str:
    process = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True
    )
    if process.returncode != 0:
        raise RuntimeError(f"git {args} failed: {process.stderr.strip()}")
    return process.stdout.strip()


def make_campaign(
    campaign_id: str = CAMPAIGN_ID,
    *,
    pr: int = 7,
    rounds_used: int = 0,
    max_rounds: int = 6,
    status: str | None = None,
    created_at: str = "2026-09-14T12:00:00Z",
) -> dict:
    data = model.new_campaign(
        campaign_id=campaign_id,
        repository="owner/repo",
        pull_request_number=pr,
        created_at=created_at,
        max_rounds=max_rounds,
        model="a-model",
        reasoning_level="medium",
        interval_minutes=30,
        reviewer_logins=["chatgpt-codex-connector"],
        approval_logins=["chatgpt-codex-connector"],
    )
    data["rounds_used"] = rounds_used
    if status is not None and status != model.ACTIVE:
        data = model.terminate(data, status=status, at=T1)
    return data


def run_blocked(operation):
    """Run ``operation`` in a thread and return (thread, outcome) while the
    caller holds the canonical guard, asserting it blocks."""
    outcome: list[object] = []

    def target() -> None:
        try:
            outcome.append(("ok", operation()))
        except BaseException as error:  # surface thread failures to the test
            outcome.append(("error", error))

    thread = threading.Thread(target=target)
    thread.start()
    thread.join(0.4)
    return thread, outcome


class AuthorityTests(unittest.TestCase):
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

    # ------------------------------------------------------------------
    # Fixture helpers

    def campaign_file(self, pr: int = 7) -> Path:
        return storage.campaign_path("owner/repo", pr, repository_path=self.repo)

    def lock_file(self, pr: int = 7) -> Path:
        return storage.lock_path("owner/repo", pr, repository_path=self.repo)

    def save_campaign(self, record: dict, pr: int = 7) -> Path:
        storage.save_json(self.campaign_file(pr), record)
        return self.campaign_file(pr)

    def setup_owned(self, pr: int = 7, campaign_id: str = CAMPAIGN_ID) -> str:
        """Acquire setup ownership and initialize a valid active campaign."""
        acquired = storage.acquire_setup_lock(
            "owner/repo", pr,
            campaign_id=campaign_id,
            acquired_at=ACQUIRED_AT,
            repository_path=self.repo,
        )
        storage.initialize_campaign(
            "owner/repo", pr,
            owner_token=acquired["owner_token"],
            campaign=make_campaign(campaign_id, pr=pr),
            repository_path=self.repo,
        )
        return acquired["owner_token"]

    def lock_status(self, pr: int = 7) -> str:
        return storage.inspect_lock("owner/repo", pr, repository_path=self.repo)[
            "status"
        ]

    # ------------------------------------------------------------------
    # Guarded initialization

    def test_initialization_requires_owner_lock_and_absence(self) -> None:
        acquired = storage.acquire_setup_lock(
            "owner/repo", 7,
            campaign_id=CAMPAIGN_ID,
            acquired_at=ACQUIRED_AT,
            repository_path=self.repo,
        )
        token = acquired["owner_token"]
        campaign = make_campaign()
        with self.assertRaises(RuntimeError):
            storage.initialize_campaign(
                "owner/repo", 7,
                owner_token="wrong",
                campaign=campaign,
                repository_path=self.repo,
            )
        # Identity mismatch between the proposed campaign and the lock.
        with self.assertRaises(RuntimeError):
            storage.initialize_campaign(
                "owner/repo", 7,
                owner_token=token,
                campaign=make_campaign(C2_ID),
                repository_path=self.repo,
            )
        result = storage.initialize_campaign(
            "owner/repo", 7,
            owner_token=token,
            campaign=campaign,
            repository_path=self.repo,
        )
        self.assertTrue(result["initialized"])
        # A second initialization refuses; the record is never overwritten.
        with self.assertRaises(RuntimeError):
            storage.initialize_campaign(
                "owner/repo", 7,
                owner_token=token,
                campaign=campaign,
                repository_path=self.repo,
            )

    def test_initialization_serializes_with_setup_cancellation(self) -> None:
        acquired = storage.acquire_setup_lock(
            "owner/repo", 7,
            campaign_id=CAMPAIGN_ID,
            acquired_at=ACQUIRED_AT,
            repository_path=self.repo,
        )
        token = acquired["owner_token"]
        target = self.lock_file()
        with storage._guard(target):
            thread, outcome = run_blocked(
                lambda: storage.initialize_campaign(
                    "owner/repo", 7,
                    owner_token=token,
                    campaign=make_campaign(),
                    repository_path=self.repo,
                )
            )
            self.assertTrue(thread.is_alive())
        thread.join(5)
        self.assertEqual(outcome[0][0], "ok")
        self.assertTrue(self.campaign_file().exists())
        # Init won first, so cancellation now refuses.
        with self.assertRaises(RuntimeError):
            storage.cancel_setup(
                "owner/repo", 7,
                owner_token=token,
                expected_campaign_id=CAMPAIGN_ID,
                repository_path=self.repo,
            )

    def test_cancellation_winning_first_prevents_later_initialization(self) -> None:
        acquired = storage.acquire_setup_lock(
            "owner/repo", 7,
            campaign_id=CAMPAIGN_ID,
            acquired_at=ACQUIRED_AT,
            repository_path=self.repo,
        )
        token = acquired["owner_token"]
        result = storage.cancel_setup(
            "owner/repo", 7,
            owner_token=token,
            expected_campaign_id=CAMPAIGN_ID,
            repository_path=self.repo,
        )
        self.assertTrue(result["cancelled"])
        self.assertEqual(self.lock_status(), "absent")
        with self.assertRaises(RuntimeError):
            storage.initialize_campaign(
                "owner/repo", 7,
                owner_token=token,
                campaign=make_campaign(),
                repository_path=self.repo,
            )
        self.assertFalse(self.campaign_file().exists())

    def test_initialization_refuses_after_recovery(self) -> None:
        acquired = storage.acquire_setup_lock(
            "owner/repo", 7,
            campaign_id=CAMPAIGN_ID,
            acquired_at=ACQUIRED_AT,
            repository_path=self.repo,
        )
        storage.recover_lock(
            "owner/repo", 7,
            user_authorized_recovery=True,
            expected_campaign_id=CAMPAIGN_ID,
            repository_path=self.repo,
        )
        with self.assertRaises(RuntimeError):
            storage.initialize_campaign(
                "owner/repo", 7,
                owner_token=acquired["owner_token"],
                campaign=make_campaign(),
                repository_path=self.repo,
            )
        self.assertFalse(self.campaign_file().exists())

    # ------------------------------------------------------------------
    # Active-campaign ordinary authority

    def test_matching_active_campaign_grants_ordinary_authority(self) -> None:
        token = self.setup_owned()
        metadata = storage.ensure_active_campaign_owner(
            "owner/repo", 7, token, repository_path=self.repo
        )
        self.assertEqual(metadata["campaign_id"], CAMPAIGN_ID)

    def test_absent_campaign_fails_ordinary_authority(self) -> None:
        acquired = storage.acquire_setup_lock(
            "owner/repo", 7,
            campaign_id=CAMPAIGN_ID,
            acquired_at=ACQUIRED_AT,
            repository_path=self.repo,
        )
        with self.assertRaises(RuntimeError):
            storage.ensure_active_campaign_owner(
                "owner/repo", 7, acquired["owner_token"], repository_path=self.repo
            )

    def test_setup_lock_with_absent_campaign_cannot_authorize_mutation(self) -> None:
        # Explicit Phase 1 invariant: a setup owner before initialization has
        # no ordinary product mutation authority.
        self.test_absent_campaign_fails_ordinary_authority()

    def test_malformed_and_unsupported_campaign_fail_authority(self) -> None:
        acquired = storage.acquire_setup_lock(
            "owner/repo", 7,
            campaign_id=CAMPAIGN_ID,
            acquired_at=ACQUIRED_AT,
            repository_path=self.repo,
        )
        token = acquired["owner_token"]
        self.save_campaign({"campaign_id": CAMPAIGN_ID, "schema_version": 99})
        with self.assertRaises(ValueError):
            storage.ensure_active_campaign_owner(
                "owner/repo", 7, token, repository_path=self.repo
            )

    def test_terminal_campaign_fails_ordinary_authority(self) -> None:
        token = self.setup_owned()
        self.save_campaign(
            make_campaign(status=model.MANUAL_INTERVENTION_REQUIRED)
        )
        with self.assertRaises(RuntimeError):
            storage.ensure_active_campaign_owner(
                "owner/repo", 7, token, repository_path=self.repo
            )

    # ------------------------------------------------------------------
    # Guarded local transition serialization

    def test_local_transitions_occur_under_one_guard(self) -> None:
        token = self.setup_owned()
        target = self.lock_file()
        for transition in (
            lambda c: model.consume_round(c, kind="remediation"),
            lambda c: model.supersede_active_guards(
                c, current_head_oid="H1", at=T1
            ),
            lambda c: model.terminate(
                c, status=model.ROUNDS_EXHAUSTED, at=T1
            ),
        ):
            with storage._guard(target):
                thread, outcome = run_blocked(
                    lambda: storage.transition_active_campaign(
                        "owner/repo", 7,
                        owner_token=token,
                        transition=transition,
                        repository_path=self.repo,
                    )
                )
                self.assertTrue(thread.is_alive())
            thread.join(5)
            self.assertEqual(outcome[0][0], "ok")
        campaign = storage.load_json(self.campaign_file())
        self.assertEqual(campaign["status"], model.ROUNDS_EXHAUSTED)
        self.assertEqual(campaign["rounds_used"], 1)

    def test_transition_refuses_terminal_campaign(self) -> None:
        token = self.setup_owned()
        self.save_campaign(make_campaign(status=model.SUCCEEDED))
        with self.assertRaises(RuntimeError):
            storage.transition_active_campaign(
                "owner/repo", 7,
                owner_token=token,
                transition=lambda c: model.consume_round(c, kind="remediation"),
                repository_path=self.repo,
            )

    # ------------------------------------------------------------------
    # Same-owner stale-write protection

    def shared_owner_with_c0(self) -> tuple[str, dict]:
        token = self.setup_owned()
        c0 = storage.load_json(self.campaign_file())
        return token, c0

    def test_stale_same_owner_transition_is_rejected(self) -> None:
        token, c0 = self.shared_owner_with_c0()
        # Owner A durably produces C1.
        c1 = storage.apply_campaign_transition_if_current(
            "owner/repo", 7,
            owner_token=token,
            expected_source=c0,
            transition=lambda c: model.consume_round(c, kind="remediation"),
            repository_path=self.repo,
        )
        self.assertEqual(c1["rounds_used"], 1)
        # Owner B still holds a C0-derived view; its write must be rejected
        # and the durable C1 preserved.
        with self.assertRaises(storage.StaleCampaignError):
            storage.apply_campaign_transition_if_current(
                "owner/repo", 7,
                owner_token=token,
                expected_source=c0,
                transition=lambda c: model.consume_round(c, kind="remediation"),
                repository_path=self.repo,
            )
        current = storage.load_json(self.campaign_file())
        self.assertEqual(current["rounds_used"], 1)

    def test_stale_write_cannot_erase_a_request_guard(self) -> None:
        token, c0 = self.shared_owner_with_c0()
        snapshot = {
            "complete": True,
            "head_oid": "H1",
            "reactions": [],
            "reviews": [],
            "threads": [],
            "comments": [],
        }
        c1 = storage.apply_campaign_transition_if_current(
            "owner/repo", 7,
            owner_token=token,
            expected_source=c0,
            transition=lambda c: model.reserve_request(
                c, head_oid="H1", reserved_at=T1, snapshot=snapshot
            ),
            repository_path=self.repo,
        )
        self.assertEqual(len(c1["guards"]), 1)
        with self.assertRaises(storage.StaleCampaignError):
            storage.apply_campaign_transition_if_current(
                "owner/repo", 7,
                owner_token=token,
                expected_source=c0,
                transition=lambda c: model.consume_round(c, kind="remediation"),
                repository_path=self.repo,
            )
        current = storage.load_json(self.campaign_file())
        self.assertEqual(len(current["guards"]), 1)

    def test_stale_write_cannot_regress_rounds_or_reopen_terminal(self) -> None:
        token = self.setup_owned()
        consumed = storage.transition_active_campaign(
            "owner/repo", 7,
            owner_token=token,
            transition=lambda c: model.consume_round(c, kind="remediation"),
            repository_path=self.repo,
        )
        terminal = storage.transition_active_campaign(
            "owner/repo", 7,
            owner_token=token,
            transition=lambda c: model.terminate(
                c, status=model.ROUNDS_EXHAUSTED, at=T1
            ),
            repository_path=self.repo,
        )
        # A stale writer holding the pre-terminal snapshot cannot reopen the
        # terminal campaign or regress the round counter.
        with self.assertRaises(storage.StaleCampaignError):
            storage.apply_campaign_transition_if_current(
                "owner/repo", 7,
                owner_token=token,
                expected_source=consumed,
                transition=lambda c: model.terminate(
                    c, status=model.SUCCEEDED, at=T1
                ),
                repository_path=self.repo,
            )
        current = storage.load_json(self.campaign_file())
        self.assertEqual(current["status"], model.ROUNDS_EXHAUSTED)
        self.assertEqual(current["rounds_used"], 1)

    # ------------------------------------------------------------------
    # Setup cancellation

    def test_cancel_setup_removes_matching_unused_setup_lock(self) -> None:
        acquired = storage.acquire_setup_lock(
            "owner/repo", 7,
            campaign_id=CAMPAIGN_ID,
            acquired_at=ACQUIRED_AT,
            repository_path=self.repo,
        )
        result = storage.cancel_setup(
            "owner/repo", 7,
            owner_token=acquired["owner_token"],
            expected_campaign_id=CAMPAIGN_ID,
            repository_path=self.repo,
        )
        self.assertTrue(result["cancelled"])
        self.assertEqual(self.lock_status(), "absent")

    def test_cancel_setup_refusals(self) -> None:
        acquired = storage.acquire_setup_lock(
            "owner/repo", 7,
            campaign_id=CAMPAIGN_ID,
            acquired_at=ACQUIRED_AT,
            repository_path=self.repo,
        )
        token = acquired["owner_token"]
        # Wrong token refuses.
        with self.assertRaises(RuntimeError):
            storage.cancel_setup(
                "owner/repo", 7,
                owner_token="wrong",
                expected_campaign_id=CAMPAIGN_ID,
                repository_path=self.repo,
            )
        # Wrong identity refuses.
        with self.assertRaises(RuntimeError):
            storage.cancel_setup(
                "owner/repo", 7,
                owner_token=token,
                expected_campaign_id=C2_ID,
                repository_path=self.repo,
            )
        # Invalid lock refuses.
        target = self.lock_file()
        payload = json.loads(target.read_text(encoding="utf-8"))
        payload["schema_version"] = True
        target.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(ValueError):
            storage.cancel_setup(
                "owner/repo", 7,
                owner_token=token,
                expected_campaign_id=CAMPAIGN_ID,
                repository_path=self.repo,
            )
        self.assertEqual(self.lock_status(), "invalid")

    def test_cancel_setup_refuses_once_campaign_exists(self) -> None:
        token = self.setup_owned()
        with self.assertRaises(RuntimeError):
            storage.cancel_setup(
                "owner/repo", 7,
                owner_token=token,
                expected_campaign_id=CAMPAIGN_ID,
                repository_path=self.repo,
            )
        self.assertEqual(self.lock_status(), "active")
        self.assertTrue(self.campaign_file().exists())

    def test_ordinary_release_is_not_weakened_for_setup_cancellation(self) -> None:
        # Campaign absence must go through cancellation, not ordinary release.
        acquired = storage.acquire_setup_lock(
            "owner/repo", 7,
            campaign_id=CAMPAIGN_ID,
            acquired_at=ACQUIRED_AT,
            repository_path=self.repo,
        )
        with self.assertRaises(RuntimeError):
            storage.release_lock(
                "owner/repo", 7,
                acquired["owner_token"],
                repository_path=self.repo,
            )
        self.assertEqual(self.lock_status(), "active")

    # ------------------------------------------------------------------
    # Specialized unused-campaign abort

    def test_abort_removes_unused_active_campaign_then_lock(self) -> None:
        token = self.setup_owned()
        result = storage.abort_unused_campaign(
            "owner/repo", 7, owner_token=token, repository_path=self.repo
        )
        self.assertTrue(result["aborted"])
        self.assertFalse(self.campaign_file().exists())
        self.assertEqual(self.lock_status(), "absent")

    def test_abort_refusals(self) -> None:
        # Nonzero rounds refuse.
        token = self.setup_owned()
        storage.transition_active_campaign(
            "owner/repo", 7,
            owner_token=token,
            transition=lambda c: model.consume_round(c, kind="remediation"),
            repository_path=self.repo,
        )
        with self.assertRaises(RuntimeError):
            storage.abort_unused_campaign(
                "owner/repo", 7, owner_token=token, repository_path=self.repo
            )
        # Guards refuse.
        self.setup_owned(pr=8)
        token8 = storage.load_json(self.lock_file(8))["owner_token"]
        snapshot = {
            "complete": True,
            "head_oid": "H1",
            "reactions": [],
            "reviews": [],
            "threads": [],
            "comments": [],
        }
        storage.transition_active_campaign(
            "owner/repo", 8,
            owner_token=token8,
            transition=lambda c: model.reserve_request(
                c, head_oid="H1", reserved_at=T1, snapshot=snapshot
            ),
            repository_path=self.repo,
        )
        with self.assertRaises(RuntimeError):
            storage.abort_unused_campaign(
                "owner/repo", 8, owner_token=token8, repository_path=self.repo
            )
        # Terminal campaigns refuse.
        self.save_campaign(make_campaign(status=model.SUCCEEDED, pr=8), pr=8)
        with self.assertRaises(RuntimeError):
            storage.abort_unused_campaign(
                "owner/repo", 8, owner_token=token8, repository_path=self.repo
            )

    def test_interrupted_abort_state_remains_fail_closed(self) -> None:
        token = self.setup_owned()
        # Crash after campaign deletion but before lock deletion.
        self.campaign_file().unlink()
        self.assertEqual(self.lock_status(), "active")
        # A worker delivery is blocked by the surviving lock (lock-first) and
        # the owner cannot release without the campaign record.
        result = storage.acquire_worker_lock(
            "owner/repo", 7,
            campaign_id=CAMPAIGN_ID,
            acquired_at=ACQUIRED_AT,
            repository_path=self.repo,
        )
        self.assertEqual(result["status"], "busy")
        with self.assertRaises(RuntimeError):
            storage.release_lock(
                "owner/repo", 7, token, repository_path=self.repo
            )
        self.assertEqual(self.lock_status(), "active")

    # ------------------------------------------------------------------
    # Fixed local rollover identity transition

    def rollover_ready(self, pr: int = 7) -> str:
        """Terminal fully-consumed C1 with the rollover lock acquired."""
        self.save_campaign(
            make_campaign(pr=pr, rounds_used=6, status=model.ROUNDS_EXHAUSTED), pr=pr
        )
        acquired = storage.acquire_rollover_lock(
            "owner/repo", pr,
            campaign_id=CAMPAIGN_ID,
            acquired_at=ACQUIRED_AT,
            repository_path=self.repo,
        )
        self.assertTrue(acquired["acquired"])
        return acquired["owner_token"]

    def test_rollover_identity_transition_lock_first_campaign_second(self) -> None:
        token = self.rollover_ready()
        proposed = make_campaign(C2_ID, created_at="2026-09-15T12:00:00Z")
        result = storage.rollover_campaign_identity(
            "owner/repo", 7,
            owner_token=token,
            proposed_campaign=proposed,
            repository_path=self.repo,
        )
        self.assertEqual(result["previous_campaign_id"], CAMPAIGN_ID)
        self.assertEqual(result["campaign_id"], C2_ID)
        lock = storage.load_json(self.lock_file())
        campaign = storage.load_json(self.campaign_file())
        self.assertEqual(lock["campaign_id"], C2_ID)
        # The owner token is preserved across the identity transition.
        self.assertEqual(lock["owner_token"], token)
        self.assertEqual(campaign["campaign_id"], C2_ID)
        self.assertEqual(campaign["status"], model.ACTIVE)

    def test_rollover_refuses_non_rollover_source_or_invalid_c2(self) -> None:
        token = self.rollover_ready()
        with self.assertRaises(RuntimeError):
            storage.rollover_campaign_identity(
                "owner/repo", 7,
                owner_token=token,
                proposed_campaign=make_campaign(CAMPAIGN_ID),  # same identity
                repository_path=self.repo,
            )
        with self.assertRaises(ValueError):
            storage.rollover_campaign_identity(
                "owner/repo", 7,
                owner_token=token,
                proposed_campaign={"campaign_id": C2_ID, "junk": True},
                repository_path=self.repo,
            )

    def test_interruption_after_lock_replacement_fails_closed(self) -> None:
        token = self.rollover_ready()
        proposed = make_campaign(C2_ID, created_at="2026-09-15T12:00:00Z")
        original_save = storage.save_json
        calls = {"n": 0}

        def failing_save(path, payload):
            calls["n"] += 1
            if calls["n"] == 1:
                return original_save(path, payload)  # lock identity written
            raise RuntimeError("crash before campaign replacement")

        storage.save_json = failing_save
        try:
            with self.assertRaises(RuntimeError):
                storage.rollover_campaign_identity(
                    "owner/repo", 7,
                    owner_token=token,
                    proposed_campaign=proposed,
                    repository_path=self.repo,
                )
        finally:
            storage.save_json = original_save
        # The deliberate partial state: lock=C2, campaign=terminal C1.
        self.assertEqual(storage.load_json(self.lock_file())["campaign_id"], C2_ID)
        self.assertEqual(
            storage.load_json(self.campaign_file())["campaign_id"], CAMPAIGN_ID
        )
        # Ordinary acquisition is blocked by the still-held C2 lock first.
        worker = storage.acquire_worker_lock(
            "owner/repo", 7,
            campaign_id=CAMPAIGN_ID,
            acquired_at=ACQUIRED_AT,
            repository_path=self.repo,
        )
        self.assertEqual(worker["status"], "busy")
        # Ordinary release rejects the campaign/lock identity mismatch.
        with self.assertRaises(RuntimeError):
            storage.release_lock(
                "owner/repo", 7, token, repository_path=self.repo
            )
        # A repeated rollover attempt also rejects the mismatch.
        with self.assertRaises(RuntimeError):
            storage.rollover_campaign_identity(
                "owner/repo", 7,
                owner_token=token,
                proposed_campaign=proposed,
                repository_path=self.repo,
            )
        # Recovery expects the C2 lock identity; C1 remains unchanged.
        recovered = storage.recover_lock(
            "owner/repo", 7,
            user_authorized_recovery=True,
            expected_campaign_id=C2_ID,
            repository_path=self.repo,
        )
        self.assertTrue(recovered["recovered"])
        self.assertEqual(self.lock_status(), "absent")
        self.assertEqual(
            storage.load_json(self.campaign_file())["campaign_id"], CAMPAIGN_ID
        )
        self.assertEqual(
            storage.load_json(self.campaign_file())["status"],
            model.ROUNDS_EXHAUSTED,
        )
        # With the lock gone, a stale C1 worker delivery sees the terminal
        # campaign and cannot acquire.
        worker = storage.acquire_worker_lock(
            "owner/repo", 7,
            campaign_id=CAMPAIGN_ID,
            acquired_at=ACQUIRED_AT,
            repository_path=self.repo,
        )
        self.assertEqual(worker["status"], "campaign_terminal")
        # The surviving C1 can roll over again after re-acquisition.
        retry_token = self.rollover_ready()
        result = storage.rollover_campaign_identity(
            "owner/repo", 7,
            owner_token=retry_token,
            proposed_campaign=proposed,
            repository_path=self.repo,
        )
        self.assertEqual(result["campaign_id"], C2_ID)

    def test_reverse_mismatch_is_also_rejected(self) -> None:
        # Defensive fixture: lock=C1, campaign=C2 (the mirror of the partial
        # rollover state). No production path writes it, and ordinary paths
        # reject it.
        self.save_campaign(
            make_campaign(pr=7, rounds_used=6, status=model.ROUNDS_EXHAUSTED)
        )
        token = self.rollover_ready()
        self.save_campaign(make_campaign(C2_ID, created_at="2026-09-15T12:00:00Z"))
        # A competitor delivery is first blocked by the held lock itself.
        worker = storage.acquire_worker_lock(
            "owner/repo", 7,
            campaign_id=C2_ID,
            acquired_at=ACQUIRED_AT,
            repository_path=self.repo,
        )
        self.assertEqual(worker["status"], "busy")
        # The owner's own release refuses the campaign/lock identity mismatch.
        with self.assertRaises(RuntimeError):
            storage.release_lock(
                "owner/repo", 7, token, repository_path=self.repo
            )

    def test_clean_rollover_rejection_releases_the_c1_lock(self) -> None:
        # No identity transition began: identity-safe release of terminal C1
        # must work so a rollover failure does not poison ownership.
        token = self.rollover_ready()
        storage.release_lock(
            "owner/repo", 7, token, repository_path=self.repo
        )
        self.assertEqual(self.lock_status(), "absent")
        self.assertTrue(self.campaign_file().exists())

    # ------------------------------------------------------------------
    # Explicit user-authorized campaign retirement

    def test_retained_lock_retirement_removes_campaign_then_lock(self) -> None:
        token = self.setup_owned()
        result = storage.retire_campaign_retaining_lock(
            "owner/repo", 7,
            expected_campaign_id=CAMPAIGN_ID,
            user_authorized_retirement=True,
            repository_path=self.repo,
        )
        self.assertEqual(result["mode"], "retained-lock")
        self.assertFalse(self.campaign_file().exists())
        self.assertEqual(self.lock_status(), "absent")

    def test_retirement_requires_explicit_authorization(self) -> None:
        self.setup_owned()
        with self.assertRaises(RuntimeError):
            storage.retire_campaign_retaining_lock(
                "owner/repo", 7,
                expected_campaign_id=CAMPAIGN_ID,
                user_authorized_retirement=False,
                repository_path=self.repo,
            )
        with self.assertRaises(RuntimeError):
            storage.retire_campaign_without_lock(
                "owner/repo", 7,
                expected_campaign_id=CAMPAIGN_ID,
                user_authorized_retirement=False,
                repository_path=self.repo,
            )
        self.assertTrue(self.campaign_file().exists())

    def test_retirement_requires_exact_expected_identity(self) -> None:
        self.setup_owned()
        with self.assertRaises(RuntimeError):
            storage.retire_campaign_retaining_lock(
                "owner/repo", 7,
                expected_campaign_id=C2_ID,
                user_authorized_retirement=True,
                repository_path=self.repo,
            )
        self.assertTrue(self.campaign_file().exists())

    def test_active_interrupted_and_fully_consumed_campaigns_can_retire(self) -> None:
        # Active fully-consumed campaign the user chooses not to continue.
        token = self.setup_owned()
        storage.transition_active_campaign(
            "owner/repo", 7,
            owner_token=token,
            transition=lambda c: model.consume_round(c, kind="remediation"),
            repository_path=self.repo,
        )
        result = storage.retire_campaign_retaining_lock(
            "owner/repo", 7,
            expected_campaign_id=CAMPAIGN_ID,
            user_authorized_retirement=True,
            repository_path=self.repo,
        )
        self.assertEqual(result["mode"], "retained-lock")
        # Active interrupted campaign with guards.
        self.setup_owned(pr=8)
        token8 = storage.load_json(self.lock_file(8))["owner_token"]
        snapshot = {
            "complete": True,
            "head_oid": "H1",
            "reactions": [],
            "reviews": [],
            "threads": [],
            "comments": [],
        }
        storage.transition_active_campaign(
            "owner/repo", 8,
            owner_token=token8,
            transition=lambda c: model.reserve_request(
                c, head_oid="H1", reserved_at=T1, snapshot=snapshot
            ),
            repository_path=self.repo,
        )
        result = storage.retire_campaign_retaining_lock(
            "owner/repo", 8,
            expected_campaign_id=CAMPAIGN_ID,
            user_authorized_retirement=True,
            repository_path=self.repo,
        )
        self.assertEqual(result["mode"], "retained-lock")
        self.assertEqual(self.lock_status(8), "absent")

    def test_abnormal_terminal_and_early_succeeded_campaigns_can_retire(self) -> None:
        self.save_campaign(
            make_campaign(rounds_used=6, status=model.AMBIGUOUS_INTERRUPTION)
        )
        result = storage.retire_campaign_without_lock(
            "owner/repo", 7,
            expected_campaign_id=CAMPAIGN_ID,
            user_authorized_retirement=True,
            repository_path=self.repo,
        )
        self.assertEqual(result["mode"], "lock-absent")
        # Early succeeded campaign with unused rounds cannot auto-roll over
        # but can be explicitly retired.
        self.save_campaign(make_campaign(status=model.SUCCEEDED))
        acquired = storage.acquire_setup_lock(
            "owner/repo", 7,
            campaign_id=C2_ID,
            acquired_at=ACQUIRED_AT,
            repository_path=self.repo,
        )
        self.assertEqual(acquired["acquired"], False)
        self.assertEqual(acquired["status"], "campaign_exists")
        result = storage.retire_campaign_without_lock(
            "owner/repo", 7,
            expected_campaign_id=CAMPAIGN_ID,
            user_authorized_retirement=True,
            repository_path=self.repo,
        )
        self.assertEqual(result["mode"], "lock-absent")

    def test_consumed_campaigns_require_retirement_not_specialized_abort(self) -> None:
        token = self.setup_owned()
        storage.transition_active_campaign(
            "owner/repo", 7,
            owner_token=token,
            transition=lambda c: model.consume_round(c, kind="remediation"),
            repository_path=self.repo,
        )
        with self.assertRaises(RuntimeError):
            storage.abort_unused_campaign(
                "owner/repo", 7, owner_token=token, repository_path=self.repo
            )
        result = storage.retire_campaign_retaining_lock(
            "owner/repo", 7,
            expected_campaign_id=CAMPAIGN_ID,
            user_authorized_retirement=True,
            repository_path=self.repo,
        )
        self.assertEqual(result["mode"], "retained-lock")

    def test_malformed_campaign_is_preserved_and_cannot_be_retired(self) -> None:
        storage.acquire_setup_lock(
            "owner/repo", 7,
            campaign_id=CAMPAIGN_ID,
            acquired_at=ACQUIRED_AT,
            repository_path=self.repo,
        )
        self.save_campaign({"campaign_id": CAMPAIGN_ID, "schema_version": 99})
        with self.assertRaises(ValueError):
            storage.retire_campaign_retaining_lock(
                "owner/repo", 7,
                expected_campaign_id=CAMPAIGN_ID,
                user_authorized_retirement=True,
                repository_path=self.repo,
            )
        with self.assertRaises(ValueError):
            storage.retire_campaign_without_lock(
                "owner/repo", 7,
                expected_campaign_id=CAMPAIGN_ID,
                user_authorized_retirement=True,
                repository_path=self.repo,
            )
        # State preserved for inspection; the lock also remains untouched.
        self.assertTrue(self.campaign_file().exists())
        self.assertEqual(self.lock_status(), "active")

    def test_invalid_or_mismatched_lock_cannot_bypass_retirement(self) -> None:
        self.setup_owned()
        target = self.lock_file()
        payload = json.loads(target.read_text(encoding="utf-8"))
        payload["schema_version"] = 99
        target.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(RuntimeError):
            storage.retire_campaign_retaining_lock(
                "owner/repo", 7,
                expected_campaign_id=CAMPAIGN_ID,
                user_authorized_retirement=True,
                repository_path=self.repo,
            )
        self.assertTrue(self.campaign_file().exists())
        # Mismatched campaign/lock identity.
        self.setup_owned(pr=8)
        self.save_campaign(make_campaign(C2_ID, pr=8), pr=8)
        with self.assertRaises(RuntimeError):
            storage.retire_campaign_retaining_lock(
                "owner/repo", 8,
                expected_campaign_id=C2_ID,
                user_authorized_retirement=True,
                repository_path=self.repo,
            )
        # Partial-transition mismatch (lock=C2, campaign=C1).
        self.rollover_ready(pr=9)
        self.save_json_c2_lock(pr=9)
        with self.assertRaises(RuntimeError):
            storage.retire_campaign_retaining_lock(
                "owner/repo", 9,
                expected_campaign_id=CAMPAIGN_ID,
                user_authorized_retirement=True,
                repository_path=self.repo,
            )
        self.assertEqual(self.lock_status(9), "active")

    def save_json_c2_lock(self, pr: int) -> None:
        payload = storage.load_json(self.lock_file(pr))
        payload["campaign_id"] = C2_ID
        storage.save_json(self.lock_file(pr), payload)

    def test_lock_absent_retirement_refuses_when_a_lock_exists(self) -> None:
        self.setup_owned()
        with self.assertRaises(RuntimeError):
            storage.retire_campaign_without_lock(
                "owner/repo", 7,
                expected_campaign_id=CAMPAIGN_ID,
                user_authorized_retirement=True,
                repository_path=self.repo,
            )
        self.assertTrue(self.campaign_file().exists())
        self.assertEqual(self.lock_status(), "active")

    def test_after_recovery_lock_absent_retirement_works(self) -> None:
        self.setup_owned()
        storage.recover_lock(
            "owner/repo", 7,
            user_authorized_recovery=True,
            expected_campaign_id=CAMPAIGN_ID,
            repository_path=self.repo,
        )
        result = storage.retire_campaign_without_lock(
            "owner/repo", 7,
            expected_campaign_id=CAMPAIGN_ID,
            user_authorized_retirement=True,
            repository_path=self.repo,
        )
        self.assertEqual(result["mode"], "lock-absent")
        self.assertFalse(self.campaign_file().exists())

    def test_after_retirement_authority_is_gone_and_setup_is_possible(self) -> None:
        self.setup_owned()
        storage.retire_campaign_retaining_lock(
            "owner/repo", 7,
            expected_campaign_id=CAMPAIGN_ID,
            user_authorized_retirement=True,
            repository_path=self.repo,
        )
        # A stale worker delivery observes campaign absence and creates no
        # lock; it cannot regain product authority.
        result = storage.acquire_worker_lock(
            "owner/repo", 7,
            campaign_id=CAMPAIGN_ID,
            acquired_at=ACQUIRED_AT,
            repository_path=self.repo,
        )
        self.assertEqual(result["status"], "campaign_absent")
        self.assertEqual(self.lock_status(), "absent")
        # A later launcher may create a new campaign through normal setup.
        fresh = storage.acquire_setup_lock(
            "owner/repo", 7,
            campaign_id=C2_ID,
            acquired_at="2026-09-15T12:00:00Z",
            repository_path=self.repo,
        )
        self.assertTrue(fresh["acquired"])
        storage.initialize_campaign(
            "owner/repo", 7,
            owner_token=fresh["owner_token"],
            campaign=make_campaign(C2_ID, created_at="2026-09-15T12:00:00Z"),
            repository_path=self.repo,
        )
        # Retiring the new C2 campaign never restores or reconstructs C1.
        storage.retire_campaign_retaining_lock(
            "owner/repo", 7,
            expected_campaign_id=C2_ID,
            user_authorized_retirement=True,
            repository_path=self.repo,
        )
        self.assertFalse(self.campaign_file().exists())
        self.assertEqual(self.lock_status(), "absent")

    # ------------------------------------------------------------------
    # CLI glue for human recovery operations

    def cli(self, script: str, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(SCRIPTS / script), *args],
            cwd=str(self.repo),
            capture_output=True,
            text=True,
        )

    def test_cli_cancel_setup(self) -> None:
        acquired = self.cli(
            "lock.py", "acquire", "--repo", "owner/repo", "--pr", "7",
            "--campaign-id", CAMPAIGN_ID,
            "--acquired-at", ACQUIRED_AT,
        )
        token = json.loads(acquired.stdout)["owner_token"]
        cancelled = self.cli(
            "campaign.py", "cancel-setup", "--repo", "owner/repo", "--pr", "7",
            "--owner-token", token, "--expected-campaign-id", CAMPAIGN_ID,
        )
        self.assertEqual(cancelled.returncode, 0, cancelled.stderr)
        self.assertEqual(self.lock_status(), "absent")

    def test_cli_retire_requires_authorization_flag(self) -> None:
        self.setup_owned()
        missing = self.cli(
            "campaign.py", "retire", "--repo", "owner/repo", "--pr", "7",
            "--expected-campaign-id", CAMPAIGN_ID, "--retained-lock",
        )
        self.assertNotEqual(missing.returncode, 0)
        retired = self.cli(
            "campaign.py", "retire", "--repo", "owner/repo", "--pr", "7",
            "--expected-campaign-id", CAMPAIGN_ID,
            "--retained-lock", "--user-authorized-retirement",
        )
        self.assertEqual(retired.returncode, 0, retired.stderr)
        self.assertFalse(self.campaign_file().exists())
        self.assertEqual(self.lock_status(), "absent")


if __name__ == "__main__":
    unittest.main()
