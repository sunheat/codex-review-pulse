"""Deterministic owned campaign creation and rollover boundaries (no network).

The owned helpers fetch their own fresh authoritative evidence; tests inject
``fetch_snapshot`` so every rule is proven without any scheduler, adapter, or
network service.
"""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import unittest.mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "codex-review-pulse" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import campaign_model as model  # noqa: E402
import owned  # noqa: E402
import storage  # noqa: E402


def git(cwd: Path, *args: str) -> str:
    process = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True
    )
    if process.returncode != 0:
        raise RuntimeError(f"git {args} failed: {process.stderr.strip()}")
    return process.stdout.strip()


CODEX = "chatgpt-codex-connector"
CAMPAIGN_ID = "crp-20260914T120000Z-abc123"
ACQUIRED_AT = "2026-09-14T12:00:00Z"
SNAPSHOT_TIME = "2026-09-14T12:05:00Z"
H1 = "h1-oid"
CONFIG = dict(
    max_rounds=6,
    model_name="a-model",
    reasoning_level="medium",
    interval_minutes=30,
)


def snapshot(
    *,
    time: str = SNAPSHOT_TIME,
    head: str = H1,
    pr_state: str = "OPEN",
    reactions: list | None = None,
    complete: bool = True,
    repository: str = "owner/repo",
    head_repository: str | None = "owner/repo",
) -> dict:
    return {
        "complete": complete,
        "server_time": time,
        "repository": repository,
        "pr_number": 7,
        "pr_state": pr_state,
        "head_oid": head,
        "head_ref_name": "feature",
        "head_repository": head_repository,
        "node_id": "pr-node-1",
        "viewer": "operator",
        "threads": [],
        "reactions": reactions or [],
        "reviews": [],
        "comments": [],
    }


def reaction(content: str, rid: str, *, login: str = CODEX, created_at: str = "2026-09-14T09:00:00Z") -> dict:
    return {"id": rid, "content": content, "login": login, "created_at": created_at}


class OwnedBoundaryFixture(unittest.TestCase):
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
        self.repository_path = self.repo

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def lock_status(self, pr: int = 7) -> str:
        return storage.inspect_lock(
            "owner/repo", pr, repository_path=self.repository_path
        )["status"]

    def campaign_record(self, pr: int = 7) -> dict | None:
        return storage.load_json(
            storage.campaign_path("owner/repo", pr, repository_path=self.repository_path)
        )

    def acquire_setup(self, pr: int = 7, campaign_id: str = CAMPAIGN_ID) -> str:
        result = storage.acquire_setup_lock(
            "owner/repo",
            pr,
            campaign_id=campaign_id,
            acquired_at=ACQUIRED_AT,
            repository_path=self.repository_path,
        )
        self.assertTrue(result["acquired"])
        return result["owner_token"]

    def make_campaign(self, pr: int = 7, campaign_id: str = CAMPAIGN_ID) -> dict:
        return model.new_campaign(
            campaign_id=campaign_id,
            repository="owner/repo",
            pull_request_number=pr,
            created_at=ACQUIRED_AT,
            max_rounds=6,
            model="a-model",
            reasoning_level="medium",
            interval_minutes=30,
            reviewer_logins=[CODEX],
            approval_logins=[CODEX],
        )


class CreationAuthorityTests(OwnedBoundaryFixture):
    def create(self, snap, *, token: str | None = None, pr: int = 7) -> dict:
        return owned.create_campaign_owned(
            repository="owner/repo",
            pr_number=pr,
            owner_token=token or self.acquire_setup(pr),
            fetch_snapshot=lambda: snap,
            repository_path=self.repository_path,
            **CONFIG,
        )

    def test_creation_fetches_owned_evidence_and_derives_everything_from_it(self) -> None:
        snap = snapshot(
            reactions=[
                reaction(model.EYES, "eyes-2"),
                reaction(model.EYES, "eyes-1"),
                reaction(model.THUMBS_UP, "up-1"),
                reaction(model.EYES, "human-eyes", login="a-human"),
            ]
        )
        result = self.create(snap)
        self.assertTrue(result["created"])
        record = self.campaign_record()
        model.validate_campaign(record, repository="owner/repo", pull_request_number=7)
        # The campaign identity is the one preallocated on the setup lock.
        self.assertEqual(record["campaign_id"], CAMPAIGN_ID)
        # created_at comes from the owned snapshot, independently of the
        # timestamp embedded in the campaign id.
        self.assertEqual(record["created_at"], SNAPSHOT_TIME)
        self.assertNotEqual(record["created_at"], ACQUIRED_AT)
        # Canonical sorted unique applicable reaction IDs only.
        self.assertEqual(
            record["creation_baseline"]["reaction_ids"],
            ["eyes-1", "eyes-2", "up-1"],
        )
        self.assertEqual(result["head_oid"], H1)
        self.assertEqual(result["server_time"], SNAPSHOT_TIME)

    def test_caller_cannot_select_the_creation_snapshot_or_its_evidence(self) -> None:
        with self.assertRaises(TypeError):
            owned.create_campaign_owned(  # type: ignore[call-arg]
                repository="owner/repo",
                pr_number=7,
                owner_token=self.acquire_setup(),
                snapshot=snapshot(),  # no snapshot parameter exists
                created_at="2026-01-01T00:00:00Z",
                repository_path=self.repository_path,
                **CONFIG,
            )

    def test_handled_preinit_failure_cancels_the_clean_setup(self) -> None:
        cases = [
            snapshot(complete=False),
            snapshot(pr_state="MERGED"),
            snapshot(head_repository="someone-else/repo"),
            snapshot(repository="other/repo"),
            snapshot(head=""),
        ]
        for snap in cases:
            with self.subTest(snap=snap):
                token = self.acquire_setup()
                result = self.create(snap, token=token)
                self.assertEqual(
                    result,
                    {
                        "created": False,
                        "cancelled": True,
                        "reason": result["reason"],
                    },
                )
                self.assertTrue(result["reason"])
                self.assertEqual(self.lock_status(), "absent")
                self.assertIsNone(self.campaign_record())

    def test_fetch_failure_cancels_the_clean_setup(self) -> None:
        def broken() -> dict:
            raise RuntimeError("network down")

        token = self.acquire_setup()
        result = owned.create_campaign_owned(
            repository="owner/repo",
            pr_number=7,
            owner_token=token,
            fetch_snapshot=broken,
            repository_path=self.repository_path,
            **CONFIG,
        )
        self.assertFalse(result["created"])
        self.assertTrue(result["cancelled"])
        self.assertEqual(self.lock_status(), "absent")
        self.assertIsNone(self.campaign_record())

    def test_existing_campaign_refuses_without_cancellation(self) -> None:
        token = self.acquire_setup()
        storage.initialize_campaign(
            "owner/repo",
            7,
            owner_token=token,
            campaign=self.make_campaign(),
            repository_path=self.repository_path,
        )
        with self.assertRaises(owned.OwnedBoundaryError):
            self.create(snapshot(), token=token)
        # The existing campaign and its protection are untouched.
        self.assertIsNotNone(self.campaign_record())
        self.assertEqual(self.lock_status(), "active")

    def test_concurrent_campaign_appearance_fails_closed(self) -> None:
        token = self.acquire_setup()
        original = storage.initialize_campaign

        def racing(*args, **kwargs):
            # The campaign appears between the helper's absence check and the
            # guarded initialization; cancellation is no longer applicable.
            campaign = self.make_campaign()
            campaign["campaign_id"] = "crp-20260914T130000Z-def456"
            storage.save_json(
                storage.campaign_path(
                    "owner/repo", 7, repository_path=self.repository_path
                ),
                campaign,
            )
            return original(*args, **kwargs)

        with unittest.mock.patch.object(storage, "initialize_campaign", racing):
            result = owned.create_campaign_owned(
                repository="owner/repo",
                pr_number=7,
                owner_token=token,
                fetch_snapshot=lambda: snapshot(),
                repository_path=self.repository_path,
                **CONFIG,
            )
        self.assertFalse(result["created"])
        self.assertTrue(result["fail_closed"])
        self.assertIsNotNone(self.campaign_record())
        self.assertEqual(self.lock_status(), "active")

    def test_pure_constructor_stays_testable_without_persistence(self) -> None:
        campaign = model.new_campaign(
            campaign_id=CAMPAIGN_ID,
            repository="owner/repo",
            pull_request_number=7,
            created_at=ACQUIRED_AT,
            max_rounds=6,
            model="m",
            reasoning_level="low",
            interval_minutes=30,
            reviewer_logins=[CODEX],
            approval_logins=[CODEX],
            creation_baseline=["eyes-1"],
        )
        self.assertEqual(campaign["creation_baseline"], {"reaction_ids": ["eyes-1"]})
        model.validate_campaign(campaign, repository="owner/repo", pull_request_number=7)


class RolloverTests(OwnedBoundaryFixture):
    def prepare_terminal_c1(self, pr: int = 7) -> str:
        campaign = self.make_campaign(pr=pr)
        while campaign["rounds_used"] < campaign["config"]["max_rounds"]:
            campaign = model.consume_round(campaign, kind="remediation")
        campaign = model.terminate(
            campaign, status=model.ROUNDS_EXHAUSTED, at="2026-09-14T14:00:00Z"
        )
        storage.save_json(
            storage.campaign_path("owner/repo", pr, repository_path=self.repository_path),
            campaign,
        )
        result = storage.acquire_rollover_lock(
            "owner/repo",
            pr,
            campaign_id=CAMPAIGN_ID,
            acquired_at="2026-09-14T14:05:00Z",
            repository_path=self.repository_path,
        )
        self.assertTrue(result["acquired"])
        return result["owner_token"]

    def roll(self, snap, *, token: str, pr: int = 7) -> dict:
        return owned.prepare_rollover_owned(
            repository="owner/repo",
            pr_number=pr,
            owner_token=token,
            fetch_snapshot=lambda: snap,
            repository_path=self.repository_path,
            **CONFIG,
        )

    def test_rollover_uses_fresh_evidence_and_one_exact_identity(self) -> None:
        token = self.prepare_terminal_c1()
        snap = snapshot(
            time="2026-09-14T15:00:00Z",
            head="h9-oid",
            reactions=[reaction(model.EYES, "old-eyes")],
        )
        result = self.roll(snap, token=token)
        self.assertTrue(result["rolled_over"])
        self.assertEqual(result["previous_campaign_id"], CAMPAIGN_ID)
        c2_id = result["campaign_id"]
        self.assertTrue(model.CAMPAIN_ID_RE.fullmatch(c2_id))
        self.assertNotEqual(c2_id, CAMPAIGN_ID)
        # The exact chosen C2 identity is used by lock, campaign record, and result.
        record = self.campaign_record()
        self.assertEqual(record["campaign_id"], c2_id)
        self.assertEqual(record["created_at"], "2026-09-14T15:00:00Z")
        self.assertEqual(
            record["creation_baseline"], {"reaction_ids": ["old-eyes"]}
        )
        lock_payload = storage.load_json(
            storage.lock_path("owner/repo", 7, repository_path=self.repository_path)
        )
        self.assertEqual(lock_payload["campaign_id"], c2_id)
        self.assertEqual(result["head_oid"], "h9-oid")

    def test_pretransition_failure_leaves_c1_and_releases(self) -> None:
        token = self.prepare_terminal_c1()
        result = self.roll(snapshot(complete=False), token=token)
        self.assertFalse(result["rolled_over"])
        self.assertTrue(result["released"])
        self.assertTrue(result["reason"])
        record = self.campaign_record()
        self.assertEqual(record["campaign_id"], CAMPAIGN_ID)
        self.assertEqual(record["status"], model.ROUNDS_EXHAUSTED)
        self.assertEqual(self.lock_status(), "absent")

    def test_identity_collision_is_a_clean_rejection(self) -> None:
        token = self.prepare_terminal_c1()
        with unittest.mock.patch.object(
            model, "new_campaign_id", lambda created_at: CAMPAIGN_ID
        ):
            result = self.roll(snapshot(), token=token)
        self.assertFalse(result["rolled_over"])
        self.assertTrue(result["released"])
        self.assertEqual(self.campaign_record()["campaign_id"], CAMPAIGN_ID)
        self.assertEqual(self.lock_status(), "absent")

    def test_uncertain_transition_failure_preserves_fail_closed_state(self) -> None:
        token = self.prepare_terminal_c1()

        def exploding_transition(*args, **kwargs):
            raise RuntimeError("disk gone")

        with unittest.mock.patch.object(
            storage, "rollover_campaign_identity", exploding_transition
        ):
            result = self.roll(snapshot(), token=token)
        self.assertFalse(result["rolled_over"])
        self.assertTrue(result["fail_closed"])
        # C1 and the lock stay in place for explicit recovery; nothing rolled back.
        self.assertEqual(self.campaign_record()["campaign_id"], CAMPAIGN_ID)
        self.assertEqual(self.lock_status(), "active")

    def test_non_matching_ownership_refuses(self) -> None:
        self.prepare_terminal_c1()
        with self.assertRaises(RuntimeError):
            owned.prepare_rollover_owned(
                repository="owner/repo",
                pr_number=7,
                owner_token="0" * 64,
                fetch_snapshot=lambda: snapshot(),
                repository_path=self.repository_path,
                **CONFIG,
            )


if __name__ == "__main__":
    unittest.main()
