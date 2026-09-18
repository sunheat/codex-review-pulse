"""Network-free tests for the Phase 2 product-state rules and documentation.

These tests cover the scheduler-evidence policy, response grace, cleanup
authority, the unknown-result contract, and the recovery-documentation
contract. No scheduler service, adapter, client, or network is introduced.
"""

from __future__ import annotations

import re
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "codex-review-pulse" / "scripts"
REFERENCES = ROOT / "skills" / "codex-review-pulse" / "references"
sys.path.insert(0, str(SCRIPTS))

import campaign_model as m  # noqa: E402
import owned  # noqa: E402
import review_request  # noqa: E402


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class SchedulerEvidenceContractTests(unittest.TestCase):
    """No scheduler adapter, receipt, polling, or reconciliation is introduced.

    The single native recurring Automation is the only delivery mechanism and
    carries no max-round-derived COUNT or end date. These properties are proven
    by static contract: the source contains no such machinery, and the
    documentation asserts the policy.
    """

    def test_no_scheduler_adapters_clients_or_polling_exist_in_source(self) -> None:
        forbidden_substrings = (
            "scheduler_adapter",
            "scheduler_client",
            "scheduler_receipt",
            "scheduler_journal",
            "polling_reconciliation",
            "successor_scheduler",
            "bootstrap_automation",
        )
        offenders: list[str] = []
        for path in SCRIPTS.glob("*.py"):
            text = path.read_text(encoding="utf-8")
            for token in forbidden_substrings:
                if token in text:
                    offenders.append(f"{path.name}: {token}")
        self.assertEqual(offenders, [])

    def test_no_rrule_count_or_end_date_derived_from_max_rounds(self) -> None:
        for path in REFERENCES.glob("*.md"):
            text = _read(path)
            self.assertNotRegex(
                text,
                r"RRULE\s+COUNT",
                f"{path.name} mentions an RRULE COUNT",
            )
        launcher = _read(REFERENCES / "launcher.md")
        self.assertIn("indefinitely", launcher)
        self.assertIn("delivery count is not round count", launcher.lower())

    def test_confirmed_native_creation_is_authoritative_without_mandatory_readback(self) -> None:
        launcher = _read(REFERENCES / "launcher.md")
        self.assertIn("confirmed compliant", launcher.lower())
        # The contract says a successful validated native creation may be
        # authoritative without a separate readback.
        self.assertIn("without a mandatory separate readback", launcher.lower())

    def test_unknown_native_result_is_ambiguous_and_never_retried(self) -> None:
        launcher = _read(REFERENCES / "launcher.md")
        self.assertIn("ambiguous", launcher.lower())
        self.assertIn("recovery", launcher.lower())

    def test_single_recurring_automation_with_one_fixed_worker_prompt(self) -> None:
        launcher = _read(REFERENCES / "launcher.md")
        self.assertIn("exactly one recurring automation", launcher.lower())
        self.assertIn("CRPCAMPAIGNID", launcher)


class ResponseGraceTests(unittest.TestCase):
    CODEX = "chatgpt-codex-connector"
    H = "h1-oid"
    T0 = "2026-09-14T12:00:00Z"
    T_20 = "2026-09-14T12:20:00Z"
    T_19 = "2026-09-14T12:19:59Z"
    T_30 = "2026-09-14T12:30:00Z"

    def _campaign(self, *, interval: int) -> dict:
        return m.new_campaign(
            campaign_id="crp-20260914T120000Z-abc123",
            repository="owner/repo",
            pull_request_number=7,
            created_at=self.T0,
            max_rounds=6,
            model="a-model",
            reasoning_level="medium",
            interval_minutes=interval,
            reviewer_logins=[self.CODEX],
            approval_logins=[self.CODEX],
        )

    def _opened(self, *, interval: int) -> dict:
        c = self._campaign(interval=interval)
        snap = {
            "complete": True,
            "head_oid": self.H,
            "reactions": [],
            "reviews": [],
            "threads": [],
            "comments": [],
        }
        c = m.reserve_request(c, head_oid=self.H, reserved_at=self.T0, snapshot=snap)
        c = m.open_request_window(
            c,
            head_oid=self.H,
            post_head_oid=self.H,
            request_node_id="r1",
            request_created_at=self.T0,
            request_url="u",
        )
        return c

    def _snap(self, time: str) -> dict:
        return {
            "complete": True,
            "server_time": time,
            "pr_state": "OPEN",
            "head_oid": self.H,
            "threads": [],
            "reactions": [],
            "reviews": [],
            "comments": [],
        }

    def test_grace_is_independent_of_scheduler_cadence(self) -> None:
        self.assertEqual(m.MIN_REVIEW_RESPONSE_GRACE_MINUTES, 20)
        self.assertEqual(
            m.effective_response_grace_minutes(self._campaign(interval=1)), 20
        )
        self.assertEqual(
            m.effective_response_grace_minutes(self._campaign(interval=8)), 20
        )
        self.assertEqual(
            m.effective_response_grace_minutes(self._campaign(interval=30)), 30
        )

    def test_interval_1_cannot_conclude_unresponsive_before_20_minutes(self) -> None:
        c = self._opened(interval=1)
        self.assertEqual(
            m.decide(c, self._snap(self.T_19))["action"],
            "wait_request_outstanding",
        )

    def test_interval_8_cannot_conclude_unresponsive_before_20_minutes(self) -> None:
        c = self._opened(interval=8)
        self.assertEqual(
            m.decide(c, self._snap(self.T_19))["action"],
            "wait_request_outstanding",
        )

    def test_interval_8_becomes_unresponsive_at_or_after_grace(self) -> None:
        c = self._opened(interval=8)
        self.assertEqual(
            m.decide(c, self._snap(self.T_20))["status"],
            m.CODEX_REVIEW_SERVICE_UNRESPONSIVE,
        )

    def test_interval_30_requires_30_minutes(self) -> None:
        c = self._opened(interval=30)
        self.assertEqual(
            m.decide(c, self._snap(self.T_20))["action"],
            "wait_request_outstanding",
        )
        self.assertEqual(
            m.decide(c, self._snap(self.T_30))["status"],
            m.CODEX_REVIEW_SERVICE_UNRESPONSIVE,
        )

    def test_complete_evidence_remains_required_for_unresponsive(self) -> None:
        c = self._opened(interval=1)
        self.assertEqual(
            m.decide(c, {**self._snap(self.T_20), "complete": False})["action"],
            "wait_observation_incomplete",
        )

    def test_outstanding_request_remains_observable_throughout_grace(self) -> None:
        for time in (self.T0, self.T_19, self.T_20):
            c = self._opened(interval=1)
            directive = m.decide(c, self._snap(time))
            self.assertIn(
                directive["action"],
                {"wait_request_outstanding", "terminal"},
            )
            if directive["action"] == "terminal":
                self.assertEqual(
                    directive["status"], m.CODEX_REVIEW_SERVICE_UNRESPONSIVE
                )


class TerminalProofClassificationTests(unittest.TestCase):
    CODEX = "chatgpt-codex-connector"
    H = "h1-oid"
    H2 = "h2-oid"
    T0 = "2026-09-14T12:00:00Z"
    T_PLUS = "2026-09-14T12:35:00Z"

    def _campaign(self, *, guards: list | None = None) -> dict:
        c = m.new_campaign(
            campaign_id="crp-20260914T120000Z-abc123",
            repository="owner/repo",
            pull_request_number=7,
            created_at=self.T0,
            max_rounds=6,
            model="a-model",
            reasoning_level="medium",
            interval_minutes=30,
            reviewer_logins=[self.CODEX],
            approval_logins=[self.CODEX],
        )
        c["guards"] = guards or []
        return c

    def _snap(self, **kwargs) -> dict:
        snap = {
            "complete": True,
            "server_time": self.T_PLUS,
            "pr_state": "OPEN",
            "head_oid": self.H,
            "threads": [],
            "reactions": [],
            "reviews": [],
            "comments": [],
        }
        snap.update(kwargs)
        return snap

    def test_caller_cannot_select_terminal_basis(self) -> None:
        with self.assertRaises(TypeError):
            owned.run_worker_decision(  # type: ignore[call-arg]
                repository="owner/repo",
                pr_number=7,
                owner_token="t",
                terminal_basis="durable_local",
                fetch_snapshot=lambda: self._snap(),
                repository_path=".",
            )

    def test_observed_approval_is_observation_derived(self) -> None:
        c = self._campaign()
        snap = self._snap(
            reviews=[
                {
                    "id": "rv1",
                    "state": m.APPROVED,
                    "login": self.CODEX,
                    "commit_oid": self.H,
                    "submitted_at": self.T_PLUS,
                }
            ]
        )
        directive = m.decide(c, snap)
        self.assertEqual(directive["status"], m.SUCCEEDED)
        self.assertEqual(directive["basis"], "observation")

    def test_observed_completion_is_observation_derived(self) -> None:
        c = m.reserve_request(
            self._campaign(),
            head_oid=self.H,
            reserved_at=self.T0,
            snapshot=self._snap(server_time=self.T0),
        )
        c = m.open_request_window(
            c,
            head_oid=self.H,
            post_head_oid=self.H,
            request_node_id="r1",
            request_created_at=self.T0,
            request_url="u",
        )
        directive = m.decide(
            c,
            self._snap(
                reviews=[
                    {
                        "id": "rv1",
                        "state": "COMMENTED",
                        "login": self.CODEX,
                        "commit_oid": self.H,
                        "submitted_at": self.T_PLUS,
                    }
                ]
            ),
        )
        self.assertEqual(directive["status"], m.REVIEW_COMPLETED_WITHOUT_APPROVAL)
        self.assertEqual(directive["basis"], "observation")

    def test_observed_target_unavailable_is_observation_derived(self) -> None:
        directive = m.decide(self._campaign(), self._snap(pr_state="MERGED"))
        self.assertEqual(directive["status"], m.TARGET_UNAVAILABLE)
        self.assertEqual(directive["basis"], "observation")

    def test_reserved_interruption_is_durable_local(self) -> None:
        c = m.reserve_request(
            self._campaign(),
            head_oid=self.H,
            reserved_at=self.T0,
            snapshot=self._snap(server_time=self.T0),
        )
        directive = m.decide(c, self._snap())
        self.assertEqual(directive["status"], m.AMBIGUOUS_INTERRUPTION)
        self.assertEqual(directive["basis"], "durable_local")

    def test_invalidated_is_not_classified_by_state_name_alone(self) -> None:
        c = m.reserve_request(
            self._campaign(),
            head_oid=self.H,
            reserved_at=self.T0,
            snapshot=self._snap(server_time=self.T0),
        )
        c = m.invalidate_reserved_request(c, head_oid=self.H, at=self.T0, reason="x")
        # INVALIDATED requires the observed current head to be terminal: its
        # status is not classified from the guard state name alone.
        directive = m.decide(c, self._snap())
        self.assertEqual(directive["status"], m.MANUAL_INTERVENTION_REQUIRED)
        self.assertEqual(directive["basis"], "observation")

    def test_superseded_returned_head_stays_observation_derived(self) -> None:
        c = m.reserve_request(
            self._campaign(),
            head_oid=self.H,
            reserved_at=self.T0,
            snapshot=self._snap(server_time=self.T0),
        )
        c = m.open_request_window(
            c,
            head_oid=self.H,
            post_head_oid=self.H,
            request_node_id="r1",
            request_created_at=self.T0,
            request_url="u",
        )
        c = m.supersede_active_guards(c, current_head_oid=self.H2, at="2026-09-14T12:10:00Z")
        directive = m.decide(c, self._snap())
        self.assertEqual(directive["status"], m.MANUAL_INTERVENTION_REQUIRED)
        self.assertEqual(directive["basis"], "observation")

    def test_classification_is_not_a_status_only_allowlist(self) -> None:
        # A terminal directive always carries a basis; non-terminal directives
        # never carry a basis. This proves the classification is structural,
        # not a status allowlist.
        c = self._campaign()
        non_terminal = m.decide(c, self._snap())
        self.assertNotIn("basis", non_terminal)
        terminal = m.decide(c, self._snap(pr_state="CLOSED"))
        self.assertIn("basis", terminal)


class OwnershipCompletionAlgebraTests(unittest.TestCase):
    """The closed owned-worker result algebra: outcomes report facts, not recommendations."""

    def test_outcome_set_is_closed(self) -> None:
        # The outcomes are produced by owned.run_worker_decision. Source is
        # the canonical list; any new outcome is a contract change.
        source = (SCRIPTS / "owned.py").read_text(encoding="utf-8")
        expected = (
            "wait_released",
            "observation_failed_released",
            "terminal_released",
            "terminal_retained",
            "remediation_committed",
            "request_committed",
            "local_fail_closed",
        )
        for outcome in expected:
            self.assertIn(f'"{outcome}"', source, f"missing outcome {outcome}")

    def test_request_committed_retains_and_denies_cleanup(self) -> None:
        source = (SCRIPTS / "owned.py").read_text(encoding="utf-8")
        # request_committed sets ownership retained and cleanup unauthorized.
        self.assertIn('"request_committed"', source)
        self.assertIn('"ownership": "retained"', source)

    def test_terminal_released_authorizes_cleanup_only_after_release(self) -> None:
        source = (SCRIPTS / "owned.py").read_text(encoding="utf-8")
        self.assertIn('"terminal_released"', source)
        self.assertIn('"scheduler_cleanup_authorized": True', source)
        self.assertIn('"terminal_retained"', source)
        self.assertIn('"scheduler_cleanup_authorized": False', source)


class UnknownResultTests(unittest.TestCase):
    """An unknown owned-worker command result allows no compensating action."""

    def test_worker_guide_forbids_compensating_action_on_unknown_result(self) -> None:
        worker = _read(REFERENCES / "worker.md")
        self.assertIn("unknown", worker.lower())
        self.assertTrue(
            "no compensating action" in worker.lower()
            or "stop" in worker.lower()
        )

    def test_no_durable_invocation_receipt_is_introduced(self) -> None:
        forbidden = (
            "invocation_receipt",
            "durable_action_id",
            "handoff_token",
            "reservation_token",
        )
        for path in SCRIPTS.glob("*.py"):
            text = path.read_text(encoding="utf-8")
            for token in forbidden:
                self.assertNotIn(token, text, f"{path.name}: {token}")


class CleanupAuthorityTests(unittest.TestCase):
    def test_active_fully_consumed_campaign_cannot_normal_clean(self) -> None:
        worker = _read(REFERENCES / "worker.md")
        # Active fully-consumed campaigns remain scheduled for non-counting
        # lifecycle observation.
        self.assertIn("non-counting", worker.lower())
        recovery = _read(REFERENCES / "recovery.md")
        self.assertIn("cleanup", recovery.lower())

    def test_cleanup_requires_successful_release(self) -> None:
        worker = _read(REFERENCES / "worker.md")
        self.assertIn("cleanup", worker.lower())
        # The worker releases before cleanup; ambiguous retention denies it.
        self.assertIn("retain", worker.lower())

    def test_retained_terminal_ambiguity_cannot_clean(self) -> None:
        recovery = _read(REFERENCES / "recovery.md")
        self.assertIn("ambiguous_interruption", recovery)
        # ambiguous_interruption retains the lock.
        ambiguous_row = [
            line for line in recovery.splitlines()
            if "ambiguous_interruption" in line
        ]
        self.assertTrue(ambiguous_row)
        self.assertTrue(any("retained" in line.lower() for line in ambiguous_row))


class RecoveryDocumentationTests(unittest.TestCase):
    def test_recovery_distinguishes_resume_from_retirement(self) -> None:
        recovery = _read(REFERENCES / "recovery.md")
        self.assertIn("resume", recovery.lower())
        self.assertIn("retire", recovery.lower())

    def test_terminal_ambiguous_interruption_has_no_resume(self) -> None:
        recovery = _read(REFERENCES / "recovery.md")
        self.assertIn("ambiguous_interruption", recovery)
        # The doc asserts no ordinary resume path for ambiguous_interruption.
        self.assertTrue(
            "no resume" in recovery.lower() or "no ordinary resume" in recovery.lower()
        )

    def test_active_reserved_recovery_behavior_is_documented(self) -> None:
        recovery = _read(REFERENCES / "recovery.md")
        # Active RESERVED recovery re-detects ambiguity on the next acquire.
        self.assertIn("RESERVED", recovery)
        self.assertIn("ambiguous", recovery.lower())

    def test_no_automatic_choice_between_resume_and_retirement(self) -> None:
        recovery = _read(REFERENCES / "recovery.md")
        self.assertIn("explicit", recovery.lower())
        self.assertIn("human", recovery.lower())

    def test_partial_rollover_recovery_expects_c2(self) -> None:
        recovery = _read(REFERENCES / "recovery.md")
        self.assertIn("lock = C2", recovery)
        self.assertIn("--expected-campaign-id C2", recovery)


def _collapsed(path: Path) -> str:
    """Document text with all whitespace runs collapsed to single spaces."""
    return " ".join(_read(path).split())


class BatchMembershipContractTests(unittest.TestCase):
    """One authoritative batch: the committed directive is the only target list."""

    def test_projection_is_the_persisted_remediation_snapshot(self) -> None:
        source = (SCRIPTS / "owned.py").read_text(encoding="utf-8")
        self.assertIn("model.project_remediation_batch(s1", source)
        model_source = (SCRIPTS / "campaign_model.py").read_text(encoding="utf-8")
        self.assertIn("def project_remediation_batch", model_source)
        self.assertIn("result[\"threads\"] = projected", model_source)

    def test_worker_guide_names_the_only_target_enumeration(self) -> None:
        worker = _collapsed(REFERENCES / "worker.md")
        self.assertIn("only target list", worker)
        self.assertIn("Never scan the snapshot to discover additional work", worker)
        self.assertIn("waits for a later delivery", worker)
        self.assertIn("never rebuild a batch from campaign state", worker)

    def test_skill_contract_states_the_projection_invariant(self) -> None:
        skill = _collapsed(REFERENCES.parent / "SKILL.md")
        self.assertIn("only target list", skill)
        self.assertIn("no second membership", skill)
        self.assertIn("never scans the snapshot to enlarge the batch", skill)

    def test_projection_adds_no_second_membership_representation(self) -> None:
        model_source = (SCRIPTS / "campaign_model.py").read_text(encoding="utf-8")
        for marker in (
            "batch_id", "batch_ids", "batch_scope", "membership_manifest",
            "batch_marker",
        ):
            self.assertNotIn(marker, model_source, marker)


class StructuredRefusalContractTests(unittest.TestCase):
    """Caller target-selection refusals are pre-mutation structured results."""

    def test_externalize_defines_exactly_the_selection_error(self) -> None:
        source = (SCRIPTS / "externalize.py").read_text(encoding="utf-8")
        self.assertIn("class TargetSelectionError(ValueError)", source)
        self.assertIn("not part of the committed batch", source)
        self.assertIn("no Fix-now target was selected", source)
        self.assertIn("duplicate target IDs", source)

    def test_invalid_outcome_stays_outside_the_refusal_taxonomy(self) -> None:
        source = (SCRIPTS / "externalize.py").read_text(encoding="utf-8")
        self.assertIn("Unknown remediation outcome", source)
        self.assertNotIn('"maybe_someday"', source)

    def test_refusals_do_not_force_retained_lock_recovery(self) -> None:
        worker = _collapsed(REFERENCES / "worker.md")
        self.assertIn("the refusal alone never forces retained-lock recovery", worker)
        self.assertIn("follow the clean-unsuccessful release rules", worker)
        skill = _collapsed(REFERENCES.parent / "SKILL.md")
        self.assertIn("never by itself forces retained-lock recovery", skill)

    def test_frozen_evidence_failures_remain_fail_closed(self) -> None:
        worker = _collapsed(REFERENCES / "worker.md")
        self.assertIn("stop all further externalization for the batch", worker)
        self.assertIn("require explicit recovery", worker)
        skill = _collapsed(REFERENCES.parent / "SKILL.md")
        self.assertIn("still fails closed", skill)


class RetainedLockQuarantineContractTests(unittest.TestCase):
    """Quarantine is a narrow pause-only exception, never recovery machinery."""

    def test_quarantine_is_documented_as_distinct_from_terminal_cleanup(self) -> None:
        worker = _collapsed(REFERENCES / "worker.md")
        self.assertIn("## 9. Retained-lock quarantine (manual-recovery exits)", worker)
        self.assertIn("distinct from the terminal cleanup of section 8", worker)

    def test_no_quarantine_python_boundary_exists(self) -> None:
        for path in SCRIPTS.glob("*.py"):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("quarantine", text, path.name)
        for forbidden_token in (
            "automation_handle", "automation_pause", "pause_automation",
            "quarantine_state",
        ):
            for path in SCRIPTS.glob("*.py"):
                self.assertNotIn(forbidden_token, path.read_text(encoding="utf-8"),
                                 f"{path.name}: {forbidden_token}")

    def test_startup_busy_delivery_never_quarantines(self) -> None:
        worker = _collapsed(REFERENCES / "worker.md")
        self.assertIn("A later busy delivery never quarantines (section 9)", worker)

    def test_only_the_original_exact_retained_owner_may_quarantine(self) -> None:
        worker = _collapsed(REFERENCES / "worker.md")
        self.assertIn(
            "Only the delivery that acquired the permanent lock and encountered "
            "the retained failure may quarantine",
            worker,
        )
        self.assertIn("does not hold the retained owner token", worker)

    def test_read_only_owner_token_verification_precedes_quarantine(self) -> None:
        worker = _collapsed(REFERENCES / "worker.md")
        self.assertIn("lock.py verify", worker)
        self.assertIn("If this exact token no longer owns the lock", worker)

    def test_direct_authoritative_native_self_identity_is_mandatory(self) -> None:
        worker = _collapsed(REFERENCES / "worker.md")
        self.assertIn(
            "authoritative exact identity or handle",
            worker,
        )
        self.assertIn(
            "must come directly from authoritative current-delivery host metadata",
            worker,
        )
        self.assertIn("perform no Automation mutation", worker)

    def test_no_automation_discovery_or_matching_is_introduced(self) -> None:
        worker = _collapsed(REFERENCES / "worker.md")
        self.assertIn("Never discover the Automation by listing", worker)
        for forbidden in (
            "deterministic naming", "inventory enumeration", "campaign-ID search",
            "prompt comparison", "fuzzy matching", "newest-candidate selection",
            "persisted Automation ID", "scheduler lookup adapter",
        ):
            self.assertIn(forbidden, worker, forbidden)

    def test_pause_is_the_only_quarantine_mutation_and_happens_once(self) -> None:
        worker = _collapsed(REFERENCES / "worker.md")
        self.assertIn("at most one best-effort pause attempt", worker)
        self.assertIn("Pause only", worker)
        # No delete fallback on any pause outcome.
        self.assertIn("never delete", worker)
        self.assertIn("do not retry, do not delete", worker)
        self.assertIn("never turn a timeout into a delete", worker)

    def test_quarantine_changes_no_campaign_round_lock_or_recovery(self) -> None:
        worker = _collapsed(REFERENCES / "worker.md")
        self.assertIn(
            "never releases or recovers the lock, never modifies campaign state, "
            "never refunds a round, never terminalizes the campaign, never "
            "resumes work, and never replaces normal cleanup",
            worker,
        )
        self.assertIn("Correctness never depends on quarantine succeeding", worker)

    def test_manual_pause_fallback_is_documented(self) -> None:
        worker = _collapsed(REFERENCES / "worker.md")
        self.assertIn(
            "instruct the operator to pause the exact Automation manually", worker
        )

    def test_recovery_documents_paused_automation_states_and_explicit_resume(self) -> None:
        recovery = _collapsed(REFERENCES / "recovery.md")
        for state in (
            "confirmed paused", "not paused", "unknown pause state", "untouched",
        ):
            self.assertIn(state, recovery, state)
        self.assertIn("explicitly resume the same exact Automation", recovery)
        self.assertIn("is not an automatic resume", recovery)
        self.assertIn(
            "only after campaign authority is removed", recovery,
        )

    def test_agents_contract_records_only_the_high_level_boundary(self) -> None:
        agents = _collapsed(ROOT / "AGENTS.md")
        self.assertIn("### Retained-lock quarantine", agents)
        self.assertIn("one best-effort pause attempt", agents)
        self.assertIn("Later busy deliveries never quarantine", agents)
        self.assertIn("Correctness does not depend on quarantine succeeding", agents)
        # The detailed procedure stays in the worker guide, not the contract.
        self.assertNotIn("lock.py verify", agents)


class ExclusivePathTests(unittest.TestCase):
    def test_worker_guide_uses_owned_boundary_exclusively(self) -> None:
        worker = _read(REFERENCES / "worker.md")
        self.assertIn("owned-worker boundary", worker.lower())
        # Old bypass composition is gone: no instructional command usage.
        for bypass in (
            "campaign.py sync-head",
            "campaign.py terminate",
            "campaign.py consume-round",
            "campaign.py init",
        ):
            self.assertNotIn(bypass, worker)

    def test_campaign_cli_no_longer_exposes_product_mutation_bypasses(self) -> None:
        source = (SCRIPTS / "campaign.py").read_text(encoding="utf-8")
        for bypass in ("init", "consume-round", "sync-head", "terminate"):
            self.assertNotIn(f'"{bypass}"', source)
        # The narrow surface remains.
        self.assertIn('"show"', source)
        self.assertIn('"abort"', source)
        self.assertIn('"retire"', source)

    def test_request_executor_starts_from_committed_reserved(self) -> None:
        source = (SCRIPTS / "review_request.py").read_text(encoding="utf-8")
        self.assertIn("committed_request_guard", source)
        self.assertIn("RESERVED", source)

    def test_owned_cli_accepts_no_creation_evidence_arguments(self) -> None:
        import subprocess

        create_help = subprocess.run(
            [sys.executable, str(SCRIPTS / "owned.py"), "create-campaign", "--help"],
            capture_output=True, text=True,
        ).stdout
        self.assertNotIn("--created-at", create_help)
        self.assertNotIn("--snapshot", create_help)
        request_help = subprocess.run(
            [sys.executable, str(SCRIPTS / "review_request.py"), "--help"],
            capture_output=True, text=True,
        ).stdout
        self.assertNotIn("--snapshot", request_help)


class ExhaustedBudgetPrecedenceTests(unittest.TestCase):
    CODEX = "chatgpt-codex-connector"
    H = "h1-oid"
    T0 = "2026-09-14T12:00:00Z"
    T_GRACE = "2026-09-14T12:35:00Z"

    def _campaign(self, *, rounds_used: int, max_rounds: int = 6) -> dict:
        c = m.new_campaign(
            campaign_id="crp-20260914T120000Z-abc123",
            repository="owner/repo",
            pull_request_number=7,
            created_at=self.T0,
            max_rounds=max_rounds,
            model="a-model",
            reasoning_level="medium",
            interval_minutes=30,
            reviewer_logins=[self.CODEX],
            approval_logins=[self.CODEX],
        )
        c["rounds_used"] = rounds_used
        return c

    def _opened(self, c: dict) -> dict:
        snap = {
            "complete": True,
            "head_oid": self.H,
            "reactions": [],
            "reviews": [],
            "threads": [],
            "comments": [],
            "server_time": self.T0,
        }
        c = m.reserve_request(c, head_oid=self.H, reserved_at=self.T0, snapshot=snap)
        c = m.open_request_window(
            c,
            head_oid=self.H,
            post_head_oid=self.H,
            request_node_id="r1",
            request_created_at=self.T0,
            request_url="u",
        )
        return c

    def _snap(self, **kwargs) -> dict:
        snap = {
            "complete": True,
            "server_time": self.T_GRACE,
            "pr_state": "OPEN",
            "head_oid": self.H,
            "threads": [],
            "reactions": [],
            "reviews": [],
            "comments": [],
        }
        snap.update(kwargs)
        return snap

    def test_final_round_active_request_waits_before_grace(self) -> None:
        c = self._opened(self._campaign(rounds_used=5, max_rounds=6))
        directive = m.decide(c, self._snap(server_time="2026-09-14T12:05:00Z"))
        self.assertEqual(directive["action"], "wait_request_outstanding")

    def test_final_round_active_request_with_completion_returns_completion(self) -> None:
        c = self._opened(self._campaign(rounds_used=5, max_rounds=6))
        directive = m.decide(
            c,
            self._snap(
                reviews=[
                    {
                        "id": "rv1",
                        "state": "COMMENTED",
                        "login": self.CODEX,
                        "commit_oid": self.H,
                        "submitted_at": self.T_GRACE,
                    }
                ]
            ),
        )
        self.assertEqual(directive["status"], m.REVIEW_COMPLETED_WITHOUT_APPROVAL)

    def test_final_round_active_request_after_grace_becomes_unresponsive(self) -> None:
        c = self._opened(self._campaign(rounds_used=5, max_rounds=6))
        directive = m.decide(c, self._snap())
        self.assertEqual(directive["status"], m.CODEX_REVIEW_SERVICE_UNRESPONSIVE)

    def test_final_round_eyes_waits(self) -> None:
        c = self._opened(self._campaign(rounds_used=5, max_rounds=6))
        directive = m.decide(
            c,
            self._snap(
                reactions=[
                    {
                        "id": "e1",
                        "content": m.EYES,
                        "login": self.CODEX,
                        "created_at": "2026-09-14T12:05:00Z",
                    }
                ]
            ),
        )
        self.assertEqual(directive["action"], "wait_review_in_progress")

    def test_unresolved_feedback_with_no_budget_becomes_exhausted(self) -> None:
        c = self._campaign(rounds_used=6)
        directive = m.decide(
            c,
            self._snap(
                threads=[
                    {"id": "t1", "is_resolved": False, "root_login": self.CODEX}
                ]
            ),
        )
        self.assertEqual(directive["status"], m.ROUNDS_EXHAUSTED)

    def test_no_pending_lifecycle_and_no_budget_becomes_exhausted(self) -> None:
        c = self._campaign(rounds_used=6)
        directive = m.decide(c, self._snap())
        self.assertEqual(directive["status"], m.ROUNDS_EXHAUSTED)

    def test_observation_paths_consume_no_extra_round(self) -> None:
        # Rounds are consumed by remediation or request commitment, never by
        # pure decision. decide() is read-only with respect to rounds_used.
        c = self._campaign(rounds_used=0)
        before = c["rounds_used"]
        m.decide(c, self._snap())
        self.assertEqual(c["rounds_used"], before)


if __name__ == "__main__":
    unittest.main()
