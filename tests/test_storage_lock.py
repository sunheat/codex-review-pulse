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


CAMPAIGN_ID = "crp-20260914T120000Z-abc123"
OTHER_CAMPAIGN_ID = "crp-20260914T130000Z-def456"
ACQUIRED_AT = "2026-09-14T12:00:00Z"


def make_campaign(
    campaign_id: str = CAMPAIGN_ID,
    *,
    pr: int = 7,
    rounds_used: int = 0,
    max_rounds: int = 6,
    status: str | None = None,
) -> dict:
    data = model.new_campaign(
        campaign_id=campaign_id,
        repository="owner/repo",
        pull_request_number=pr,
        created_at="2026-09-14T12:00:00Z",
        max_rounds=max_rounds,
        model="a-model",
        reasoning_level="medium",
        interval_minutes=30,
        reviewer_logins=["chatgpt-codex-connector"],
        approval_logins=["chatgpt-codex-connector"],
    )
    data["rounds_used"] = rounds_used
    if status is not None and status != model.ACTIVE:
        data = model.terminate(data, status=status, at="2026-09-14T14:00:00Z")
    return data


def run_blocked(operation) -> object:
    """Run ``operation`` in a thread while asserting it blocks on the guard.

    The caller must already hold the canonical sidecar guard.
    """
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


class StorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = TemporaryRepository(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def save_campaign(self, record: dict | None, pr: int = 7) -> Path:
        path = storage.campaign_path("owner/repo", pr, repository_path=self.repo.main)
        if record is not None:
            storage.save_json(path, record)
        return path

    # ------------------------------------------------------------------
    # Shared state fundamentals

    def test_state_is_shared_between_worktrees(self) -> None:
        via_main = storage.lock_path("Owner/Repo", 7, repository_path=self.repo.main)
        via_linked = storage.lock_path("owner/repo", 7, repository_path=self.repo.linked)
        self.assertEqual(via_main.resolve(), via_linked.resolve())
        # The artifacts live inside the shared Git metadata directory, never in
        # tracked worktree content.
        relative_to_main = via_main.resolve().relative_to(self.repo.main.resolve())
        self.assertEqual(relative_to_main.parts[0], ".git")

    def test_distinct_prs_get_distinct_locks(self) -> None:
        a = storage.lock_path("owner/repo", 1, repository_path=self.repo.main)
        b = storage.lock_path("owner/repo", 2, repository_path=self.repo.main)
        self.assertNotEqual(a, b)

    def test_malformed_json_document_fails_closed(self) -> None:
        target = storage.campaign_path("owner/repo", 7, repository_path=self.repo.main)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("[1, 2]", encoding="utf-8")
        with self.assertRaises(ValueError):
            storage.load_json(target)

    # ------------------------------------------------------------------
    # Permanent-lock semantic validation

    def acquire_setup(self, campaign_id: str = CAMPAIGN_ID) -> dict:
        return storage.acquire_setup_lock(
            "owner/repo", 7,
            campaign_id=campaign_id,
            acquired_at=ACQUIRED_AT,
            repository_path=self.repo.main,
        )

    def corrupt_lock(self, mutate) -> None:
        target = storage.lock_path("owner/repo", 7, repository_path=self.repo.main)
        payload = json.loads(target.read_text(encoding="utf-8"))
        mutate(payload)
        target.write_text(json.dumps(payload), encoding="utf-8")

    def test_valid_generated_lock_is_accepted(self) -> None:
        result = self.acquire_setup()
        self.assertTrue(result["acquired"])
        status = storage.inspect_lock("owner/repo", 7, repository_path=self.repo.linked)
        self.assertEqual(status["status"], "active")
        self.assertEqual(status["campaign_id"], CAMPAIGN_ID)

    def test_boolean_schema_version_is_rejected(self) -> None:
        self.acquire_setup()
        # Python would otherwise accept True as schema version 1.
        self.corrupt_lock(lambda p: p.update(schema_version=True))
        status = storage.inspect_lock("owner/repo", 7, repository_path=self.repo.main)
        self.assertEqual(status["status"], "invalid")

    def test_boolean_pr_number_is_rejected(self) -> None:
        self.acquire_setup()
        self.corrupt_lock(lambda p: p.update(pull_request_number=True))
        self.assertEqual(
            storage.inspect_lock("owner/repo", 7, repository_path=self.repo.main)[
                "status"
            ],
            "invalid",
        )

    def test_malformed_lock_campaign_id_is_rejected(self) -> None:
        self.acquire_setup()
        self.corrupt_lock(lambda p: p.update(campaign_id="not-a-campaign-id"))
        self.assertEqual(
            storage.inspect_lock("owner/repo", 7, repository_path=self.repo.main)[
                "status"
            ],
            "invalid",
        )

    def test_malformed_owner_token_is_rejected(self) -> None:
        self.acquire_setup()
        self.corrupt_lock(lambda p: p.update(owner_token="XYZ-not-hex"))
        self.assertEqual(
            storage.inspect_lock("owner/repo", 7, repository_path=self.repo.main)[
                "status"
            ],
            "invalid",
        )

    def test_naive_acquired_at_is_rejected(self) -> None:
        self.acquire_setup()
        self.corrupt_lock(lambda p: p.update(acquired_at="2026-09-14T12:00:00"))
        self.assertEqual(
            storage.inspect_lock("owner/repo", 7, repository_path=self.repo.main)[
                "status"
            ],
            "invalid",
        )

    def test_garbage_acquired_at_is_rejected(self) -> None:
        self.acquire_setup()
        self.corrupt_lock(lambda p: p.update(acquired_at="not-a-timestamp"))
        self.assertEqual(
            storage.inspect_lock("owner/repo", 7, repository_path=self.repo.main)[
                "status"
            ],
            "invalid",
        )

    def test_invalid_lock_is_reported_invalid_not_busy_and_blocks(self) -> None:
        target = storage.lock_path("owner/repo", 7, repository_path=self.repo.main)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("", encoding="utf-8")
        self.assertEqual(
            storage.inspect_lock("owner/repo", 7, repository_path=self.repo.main)[
                "status"
            ],
            "invalid",
        )
        for acquire in (
            storage.acquire_setup_lock,
            storage.acquire_worker_lock,
            storage.acquire_rollover_lock,
        ):
            result = acquire(
                "owner/repo", 7,
                campaign_id=CAMPAIGN_ID,
                acquired_at=ACQUIRED_AT,
                repository_path=self.repo.linked,
            )
            self.assertEqual(result["acquired"], False)
            self.assertEqual(result["status"], "invalid")

    def test_corrupt_lock_still_blocks(self) -> None:
        target = storage.lock_path("owner/repo", 7, repository_path=self.repo.main)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{not json", encoding="utf-8")
        self.assertEqual(
            storage.inspect_lock("owner/repo", 7, repository_path=self.repo.linked)[
                "status"
            ],
            "invalid",
        )
        result = storage.acquire_setup_lock(
            "owner/repo", 7,
            campaign_id=CAMPAIGN_ID,
            acquired_at=ACQUIRED_AT,
            repository_path=self.repo.linked,
        )
        self.assertEqual(result["status"], "invalid")

    # ------------------------------------------------------------------
    # Stable public inspection

    def test_inspect_never_exposes_token(self) -> None:
        self.acquire_setup()
        status = storage.inspect_lock("owner/repo", 7, repository_path=self.repo.linked)
        self.assertNotIn("owner_token", status)
        self.assertEqual(status["campaign_id"], CAMPAIGN_ID)

    def test_public_inspection_serializes_with_lock_creation(self) -> None:
        target = storage.lock_path("owner/repo", 7, repository_path=self.repo.main)
        metadata = storage.lock_metadata(
            repository="owner/repo",
            pr_number=7,
            campaign_id=CAMPAIGN_ID,
            owner_token=storage.new_owner_token(),
            acquired_at=ACQUIRED_AT,
        )
        rendered = json.dumps(metadata, indent=2, sort_keys=True) + "\n"
        with storage._guard(target):
            # Simulate the O_EXCL-created lock file before metadata writing
            # completed: the file is temporarily truncated JSON. A guarded
            # reader must wait, then observe the valid lock, never a transient
            # corrupt classification.
            with target.open("w", encoding="utf-8") as stream:
                stream.write(rendered[:-10])
                stream.flush()
                thread, outcome = run_blocked(
                    lambda: storage.inspect_lock(
                        "owner/repo", 7, repository_path=self.repo.main
                    )
                )
                self.assertTrue(thread.is_alive())
                stream.write(rendered[-10:])
                stream.flush()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(outcome[0][0], "ok")
        self.assertEqual(outcome[0][1]["status"], "active")

    def test_internal_guarded_callers_do_not_deadlock(self) -> None:
        # Acquisition itself inspects the lock inside the guard via the
        # internal helper; success proves no re-acquisition deadlock.
        result = self.acquire_setup()
        self.assertTrue(result["acquired"])
        worker = storage.acquire_worker_lock(
            "owner/repo", 7,
            campaign_id=CAMPAIGN_ID,
            acquired_at=ACQUIRED_AT,
            repository_path=self.repo.main,
        )
        self.assertEqual(worker["status"], "busy")

    # ------------------------------------------------------------------
    # Lock-first acquisition classification

    def test_valid_lock_is_busy_for_all_purposes_despite_campaign_state(self) -> None:
        self.acquire_setup()
        # Malformed campaign state must not even be diagnosed: the valid lock
        # wins with an immediate busy.
        self.save_campaign({"campaign_id": "junk", "status": "weird"})
        for acquire in (
            storage.acquire_setup_lock,
            storage.acquire_worker_lock,
            storage.acquire_rollover_lock,
        ):
            result = acquire(
                "owner/repo", 7,
                campaign_id=CAMPAIGN_ID,
                acquired_at=ACQUIRED_AT,
                repository_path=self.repo.linked,
            )
            self.assertEqual(result["status"], "busy")
            self.assertEqual(result["lock"]["campaign_id"], CAMPAIGN_ID)

    def test_setup_lock_with_absent_campaign_is_busy_not_campaign_absent(self) -> None:
        self.acquire_setup()
        result = storage.acquire_worker_lock(
            "owner/repo", 7,
            campaign_id=CAMPAIGN_ID,
            acquired_at=ACQUIRED_AT,
            repository_path=self.repo.linked,
        )
        self.assertEqual(result["status"], "busy")

    def test_only_lock_absence_reaches_the_campaign_predicate(self) -> None:
        # With no lock at all and no campaign, the worker reports absence.
        result = storage.acquire_worker_lock(
            "owner/repo", 7,
            campaign_id=CAMPAIGN_ID,
            acquired_at=ACQUIRED_AT,
            repository_path=self.repo.main,
        )
        self.assertEqual(result["status"], "campaign_absent")

    def test_predicate_and_creation_are_serialized_by_one_guard(self) -> None:
        target = storage.lock_path("owner/repo", 7, repository_path=self.repo.main)
        with storage._guard(target):
            thread, outcome = run_blocked(
                lambda: storage.acquire_setup_lock(
                    "owner/repo", 7,
                    campaign_id=CAMPAIGN_ID,
                    acquired_at=ACQUIRED_AT,
                    repository_path=self.repo.main,
                )
            )
            self.assertTrue(thread.is_alive())
            # The campaign appears while the competitor waits on the guard;
            # the predicate must be re-evaluated inside the same critical
            # section, so the delayed setup cannot acquire.
            self.save_campaign(make_campaign())
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(outcome[0][0], "ok")
        self.assertEqual(outcome[0][1]["status"], "campaign_exists")
        self.assertEqual(
            storage.inspect_lock("owner/repo", 7, repository_path=self.repo.main)[
                "status"
            ],
            "absent",
        )

    def test_worker_predicate_observes_campaign_created_behind_the_guard(self) -> None:
        target = storage.lock_path("owner/repo", 7, repository_path=self.repo.main)
        with storage._guard(target):
            thread, outcome = run_blocked(
                lambda: storage.acquire_worker_lock(
                    "owner/repo", 7,
                    campaign_id=CAMPAIGN_ID,
                    acquired_at=ACQUIRED_AT,
                    repository_path=self.repo.main,
                )
            )
            self.assertTrue(thread.is_alive())
            self.save_campaign(make_campaign())
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(outcome[0][0], "ok")
        self.assertTrue(outcome[0][1]["acquired"])

    # ------------------------------------------------------------------
    # Purpose-specific acquisition

    def test_delayed_setup_cannot_acquire_after_campaign_appears(self) -> None:
        self.save_campaign(make_campaign())
        result = storage.acquire_setup_lock(
            "owner/repo", 7,
            campaign_id=CAMPAIGN_ID,
            acquired_at=ACQUIRED_AT,
            repository_path=self.repo.main,
        )
        self.assertEqual(result["acquired"], False)
        self.assertEqual(result["status"], "campaign_exists")

    def test_worker_predicate_and_creation_are_one_guarded_decision(self) -> None:
        self.save_campaign(make_campaign())
        result = storage.acquire_worker_lock(
            "owner/repo", 7,
            campaign_id=CAMPAIGN_ID,
            acquired_at=ACQUIRED_AT,
            repository_path=self.repo.main,
        )
        self.assertTrue(result["acquired"])
        self.assertEqual(
            storage.inspect_lock("owner/repo", 7, repository_path=self.repo.main)[
                "campaign_id"
            ],
            CAMPAIGN_ID,
        )

    def test_active_fully_consumed_worker_campaign_may_acquire(self) -> None:
        self.save_campaign(make_campaign(rounds_used=6, max_rounds=6))
        result = storage.acquire_worker_lock(
            "owner/repo", 7,
            campaign_id=CAMPAIGN_ID,
            acquired_at=ACQUIRED_AT,
            repository_path=self.repo.main,
        )
        self.assertTrue(result["acquired"])

    def test_terminal_worker_campaign_may_not_acquire(self) -> None:
        self.save_campaign(make_campaign(status=model.SUCCEEDED))
        result = storage.acquire_worker_lock(
            "owner/repo", 7,
            campaign_id=CAMPAIGN_ID,
            acquired_at=ACQUIRED_AT,
            repository_path=self.repo.main,
        )
        self.assertEqual(result["status"], "campaign_terminal")

    def test_stale_identity_worker_campaign_may_not_acquire(self) -> None:
        self.save_campaign(make_campaign(campaign_id=OTHER_CAMPAIGN_ID))
        result = storage.acquire_worker_lock(
            "owner/repo", 7,
            campaign_id=CAMPAIGN_ID,
            acquired_at=ACQUIRED_AT,
            repository_path=self.repo.main,
        )
        self.assertEqual(result["status"], "campaign_identity_mismatch")
        self.assertEqual(result["record_campaign_id"], OTHER_CAMPAIGN_ID)

    def test_malformed_worker_campaign_may_not_acquire(self) -> None:
        self.save_campaign({"campaign_id": CAMPAIGN_ID, "schema_version": 99})
        result = storage.acquire_worker_lock(
            "owner/repo", 7,
            campaign_id=CAMPAIGN_ID,
            acquired_at=ACQUIRED_AT,
            repository_path=self.repo.main,
        )
        self.assertEqual(result["status"], "campaign_malformed")

    def test_malformed_campaign_id_input_is_rejected_before_any_lock(self) -> None:
        with self.assertRaises(ValueError):
            storage.acquire_setup_lock(
                "owner/repo", 7,
                campaign_id="garbage",
                acquired_at=ACQUIRED_AT,
                repository_path=self.repo.main,
            )
        self.assertEqual(
            storage.inspect_lock("owner/repo", 7, repository_path=self.repo.main)[
                "status"
            ],
            "absent",
        )

    # ------------------------------------------------------------------
    # Rollover acquisition

    def save_terminal(self, status: str, *, rounds_used: int = 6, max_rounds: int = 6) -> None:
        self.save_campaign(
            make_campaign(rounds_used=rounds_used, max_rounds=max_rounds, status=status)
        )

    def test_allowlisted_fully_consumed_terminal_c1_may_acquire_rollover(self) -> None:
        for status in model.ROLLOVER_TERMINAL_STATUSES:
            self.save_terminal(status)
            result = storage.acquire_rollover_lock(
                "owner/repo", 7,
                campaign_id=CAMPAIGN_ID,
                acquired_at=ACQUIRED_AT,
                repository_path=self.repo.main,
            )
            self.assertTrue(result["acquired"], status)
            storage.release_lock(
                "owner/repo", 7,
                result["owner_token"],
                repository_path=self.repo.main,
            )

    def test_active_fully_consumed_c1_may_not_acquire_rollover(self) -> None:
        self.save_campaign(make_campaign(rounds_used=6, max_rounds=6))
        result = storage.acquire_rollover_lock(
            "owner/repo", 7,
            campaign_id=CAMPAIGN_ID,
            acquired_at=ACQUIRED_AT,
            repository_path=self.repo.main,
        )
        self.assertEqual(result["status"], "campaign_active")

    def test_terminal_with_unused_rounds_may_not_acquire_rollover(self) -> None:
        self.save_terminal(model.SUCCEEDED, rounds_used=1, max_rounds=6)
        result = storage.acquire_rollover_lock(
            "owner/repo", 7,
            campaign_id=CAMPAIGN_ID,
            acquired_at=ACQUIRED_AT,
            repository_path=self.repo.main,
        )
        self.assertEqual(result["status"], "campaign_not_rolloverable")

    def test_abnormal_terminal_c1_may_not_acquire_rollover(self) -> None:
        for status in (
            model.REQUEST_CREATION_FAILED,
            model.MANUAL_INTERVENTION_REQUIRED,
            model.AMBIGUOUS_INTERRUPTION,
            model.TARGET_UNAVAILABLE,
        ):
            self.save_terminal(status)
            result = storage.acquire_rollover_lock(
                "owner/repo", 7,
                campaign_id=CAMPAIGN_ID,
                acquired_at=ACQUIRED_AT,
                repository_path=self.repo.main,
            )
            self.assertEqual(result["status"], "campaign_not_rolloverable", status)

    # ------------------------------------------------------------------
    # Release

    def test_owner_release_and_reacquire(self) -> None:
        self.save_campaign(make_campaign())
        first = storage.acquire_worker_lock(
            "owner/repo", 7,
            campaign_id=CAMPAIGN_ID,
            acquired_at=ACQUIRED_AT,
            repository_path=self.repo.main,
        )
        self.assertTrue(first["acquired"])
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
        second = storage.acquire_worker_lock(
            "owner/repo", 7,
            campaign_id=CAMPAIGN_ID,
            acquired_at="2026-09-14T12:05:00Z",
            repository_path=self.repo.linked,
        )
        self.assertTrue(second["acquired"])

    def test_release_of_terminal_campaign_is_permitted(self) -> None:
        self.save_campaign(make_campaign())
        acquired = storage.acquire_worker_lock(
            "owner/repo", 7,
            campaign_id=CAMPAIGN_ID,
            acquired_at=ACQUIRED_AT,
            repository_path=self.repo.main,
        )
        self.assertTrue(acquired["acquired"])
        storage.transition_active_campaign(
            "owner/repo", 7,
            owner_token=acquired["owner_token"],
            transition=lambda c: model.terminate(
                c, status=model.SUCCEEDED, at="2026-09-14T13:00:00Z"
            ),
            repository_path=self.repo.main,
        )
        storage.release_lock(
            "owner/repo", 7, acquired["owner_token"], repository_path=self.repo.main
        )
        self.assertEqual(
            storage.inspect_lock("owner/repo", 7, repository_path=self.repo.main)[
                "status"
            ],
            "absent",
        )

    def test_release_without_campaign_refuses_and_preserves_lock(self) -> None:
        acquired = self.acquire_setup()
        with self.assertRaises(RuntimeError):
            storage.release_lock(
                "owner/repo", 7, acquired["owner_token"], repository_path=self.repo.main
            )
        self.assertEqual(
            storage.inspect_lock("owner/repo", 7, repository_path=self.repo.main)[
                "status"
            ],
            "active",
        )

    def test_release_with_malformed_campaign_refuses_and_preserves_lock(self) -> None:
        acquired = self.acquire_setup()
        self.save_campaign({"campaign_id": CAMPAIGN_ID, "schema_version": 99})
        with self.assertRaises(ValueError):
            storage.release_lock(
                "owner/repo", 7, acquired["owner_token"], repository_path=self.repo.main
            )
        self.assertEqual(
            storage.inspect_lock("owner/repo", 7, repository_path=self.repo.main)[
                "status"
            ],
            "active",
        )

    def test_release_with_foreign_campaign_refuses_and_preserves_lock(self) -> None:
        acquired = self.acquire_setup()
        self.save_campaign(make_campaign(campaign_id=OTHER_CAMPAIGN_ID))
        with self.assertRaises(RuntimeError):
            storage.release_lock(
                "owner/repo", 7, acquired["owner_token"], repository_path=self.repo.main
            )
        self.assertEqual(
            storage.inspect_lock("owner/repo", 7, repository_path=self.repo.main)[
                "status"
            ],
            "active",
        )

    # ------------------------------------------------------------------
    # Recovery

    def test_recovery_requires_explicit_authorization_and_matches_campaign(self) -> None:
        self.acquire_setup()
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
                expected_campaign_id=OTHER_CAMPAIGN_ID,
                repository_path=self.repo.linked,
            )
        result = storage.recover_lock(
            "owner/repo", 7,
            user_authorized_recovery=True,
            expected_campaign_id=CAMPAIGN_ID,
            repository_path=self.repo.main,
        )
        self.assertTrue(result["recovered"])

    def test_recovery_of_valid_lock_requires_expected_identity(self) -> None:
        self.acquire_setup()
        with self.assertRaises(RuntimeError):
            storage.recover_lock(
                "owner/repo", 7,
                user_authorized_recovery=True,
                repository_path=self.repo.main,
            )
        self.assertEqual(
            storage.inspect_lock("owner/repo", 7, repository_path=self.repo.main)[
                "status"
            ],
            "active",
        )

    def test_recovery_of_invalid_parseable_lock_uses_raw_identity_guard(self) -> None:
        self.acquire_setup()
        self.corrupt_lock(lambda p: p.update(schema_version=99))
        raw_id = storage.load_json(
            storage.lock_path("owner/repo", 7, repository_path=self.repo.main)
        )["campaign_id"]
        self.assertEqual(raw_id, CAMPAIGN_ID)
        # Omitted expected identity must not clear a raw-readable lock.
        with self.assertRaises(RuntimeError):
            storage.recover_lock(
                "owner/repo", 7,
                user_authorized_recovery=True,
                repository_path=self.repo.main,
            )
        # Raw identity is opaque mismatch material only: a different campaign
        # refuses even though the lock is invalid.
        with self.assertRaises(RuntimeError):
            storage.recover_lock(
                "owner/repo", 7,
                user_authorized_recovery=True,
                expected_campaign_id=OTHER_CAMPAIGN_ID,
                repository_path=self.repo.main,
            )
        result = storage.recover_lock(
            "owner/repo", 7,
            user_authorized_recovery=True,
            expected_campaign_id=CAMPAIGN_ID,
            repository_path=self.repo.main,
        )
        self.assertEqual(result["previous_status"], "invalid")
        self.assertEqual(
            storage.inspect_lock("owner/repo", 7, repository_path=self.repo.main)[
                "status"
            ],
            "absent",
        )

    def test_unreadable_lock_refuses_with_expected_id_and_recovers_without(self) -> None:
        target = storage.lock_path("owner/repo", 7, repository_path=self.repo.main)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{not json", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            storage.recover_lock(
                "owner/repo", 7,
                user_authorized_recovery=True,
                expected_campaign_id=CAMPAIGN_ID,
                repository_path=self.repo.main,
            )
        result = storage.recover_lock(
            "owner/repo", 7,
            user_authorized_recovery=True,
            repository_path=self.repo.main,
        )
        self.assertTrue(result["recovered"])

    def test_absent_recovery_is_a_harmless_no_op(self) -> None:
        result = storage.recover_lock(
            "owner/repo", 7,
            user_authorized_recovery=True,
            repository_path=self.repo.main,
        )
        self.assertEqual(result, {"recovered": False, "status": "absent"})

    def test_recovery_preserves_campaign_record(self) -> None:
        acquired = self.acquire_setup()
        campaign_file = self.save_campaign(make_campaign())
        storage.release_lock(
            "owner/repo", 7, acquired["owner_token"], repository_path=self.repo.main
        )
        storage.recover_lock(
            "owner/repo", 7,
            user_authorized_recovery=True,
            expected_campaign_id=CAMPAIGN_ID,
            repository_path=self.repo.linked,
        )
        self.assertTrue(campaign_file.exists())

    # ------------------------------------------------------------------
    # CLI glue

    def _cli(self, *args: str) -> subprocess.CompletedProcess:
        script = SCRIPTS / "lock.py"
        return subprocess.run(
            [sys.executable, str(script), *args],
            cwd=str(self.repo.main),
            capture_output=True,
            text=True,
        )

    def test_stale_delivery_for_other_campaign_cannot_acquire(self) -> None:
        self.save_campaign(make_campaign(campaign_id=OTHER_CAMPAIGN_ID))
        process = self._cli(
            "acquire", "--repo", "owner/repo", "--pr", "7",
            "--campaign-id", CAMPAIGN_ID,
            "--acquired-at", ACQUIRED_AT,
        )
        self.assertEqual(process.returncode, 2)
        self.assertEqual(json.loads(process.stdout)["status"], "campaign_exists")
        self.assertEqual(
            storage.inspect_lock("owner/repo", 7, repository_path=self.repo.main)[
                "status"
            ],
            "absent",
        )

    def test_matching_setup_can_acquire_via_cli(self) -> None:
        process = self._cli(
            "acquire", "--repo", "owner/repo", "--pr", "7",
            "--generate-campaign-id", "--acquired-at", ACQUIRED_AT,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        payload = json.loads(process.stdout)
        self.assertTrue(payload["acquired"])
        self.assertEqual(
            storage.inspect_lock("owner/repo", 7, repository_path=self.repo.linked)[
                "status"
            ],
            "active",
        )


if __name__ == "__main__":
    unittest.main()
