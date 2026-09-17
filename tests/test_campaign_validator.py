"""Phase 1 campaign validator, transition compatibility, and terminal invariant.

Every supported durable state is built with the real pure transition helpers,
proven to pass the validator, then corrupted state-specific data is proven to
fail validation. Network-free.
"""

from __future__ import annotations

from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "codex-review-pulse" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import campaign_model as m  # noqa: E402


H1 = "head-oid-1"
H2 = "head-oid-2"
T0 = "2026-09-14T12:00:00Z"
T1 = "2026-09-14T12:05:00Z"
T2 = "2026-09-14T12:10:00Z"
CODEX = "chatgpt-codex-connector"


def base_campaign() -> dict:
    return m.new_campaign(
        campaign_id="crp-20260914T120000Z-abc123",
        repository="owner/repo",
        pull_request_number=7,
        created_at=T0,
        max_rounds=6,
        model="a-model",
        reasoning_level="medium",
        interval_minutes=30,
        reviewer_logins=[CODEX],
        approval_logins=[CODEX],
    )


def snapshot(head: str = H1) -> dict:
    return {
        "complete": True,
        "head_oid": head,
        "reactions": [],
        "reviews": [],
        "threads": [],
        "comments": [],
    }


def validate(campaign: dict) -> None:
    m.validate_campaign(campaign, repository="owner/repo", pull_request_number=7)


class ValidatorAcceptsRealStates(unittest.TestCase):
    def test_new_active_state(self) -> None:
        validate(base_campaign())

    def test_active_below_and_exactly_at_budget(self) -> None:
        campaign = m.consume_round(base_campaign(), kind="remediation")
        validate(campaign)
        at_budget = campaign
        while at_budget["rounds_used"] < at_budget["config"]["max_rounds"]:
            at_budget = m.consume_round(at_budget, kind="remediation")
        self.assertEqual(
            at_budget["rounds_used"], at_budget["config"]["max_rounds"]
        )
        self.assertEqual(at_budget["status"], m.ACTIVE)
        validate(at_budget)

    def test_reserved_guard_state(self) -> None:
        campaign = m.reserve_request(
            base_campaign(), head_oid=H1, reserved_at=T1, snapshot=snapshot()
        )
        validate(campaign)

    def test_invalidated_guard_state(self) -> None:
        campaign = m.reserve_request(
            base_campaign(), head_oid=H1, reserved_at=T1, snapshot=snapshot()
        )
        campaign = m.invalidate_reserved_request(
            campaign, head_oid=H1, at=T2, reason="head_changed"
        )
        validate(campaign)

    def test_active_window_guard_state(self) -> None:
        campaign = m.reserve_request(
            base_campaign(), head_oid=H1, reserved_at=T1, snapshot=snapshot()
        )
        campaign = m.open_request_window(
            campaign,
            head_oid=H1,
            post_head_oid=H1,
            request_node_id="r1",
            request_created_at=T2,
            request_url="https://example.test/c1",
        )
        validate(campaign)

    def test_superseded_guard_state(self) -> None:
        campaign = m.reserve_request(
            base_campaign(), head_oid=H1, reserved_at=T1, snapshot=snapshot()
        )
        campaign = m.open_request_window(
            campaign,
            head_oid=H1,
            post_head_oid=H1,
            request_node_id="r1",
            request_created_at=T2,
            request_url="",
        )
        campaign = m.supersede_active_guards(
            campaign, current_head_oid=H2, at=T2
        )
        validate(campaign)

    def test_request_creation_failure_state(self) -> None:
        campaign = m.reserve_request(
            base_campaign(), head_oid=H1, reserved_at=T1, snapshot=snapshot()
        )
        campaign = m.mark_request_creation_failed(
            campaign, head_oid=H1, at=T2, detail="definitive failure"
        )
        validate(campaign)

    def test_unbracketed_guard_state(self) -> None:
        campaign = m.reserve_request(
            base_campaign(), head_oid=H1, reserved_at=T1, snapshot=snapshot()
        )
        campaign = m.mark_unbracketed_request(
            campaign,
            head_oid=H1,
            post_head_oid=None,
            request_node_id="r1",
            request_created_at=T2,
            request_url="",
            at=T2,
        )
        validate(campaign)

    def test_ambiguous_request_state(self) -> None:
        campaign = m.reserve_request(
            base_campaign(), head_oid=H1, reserved_at=T1, snapshot=snapshot()
        )
        campaign = m.mark_request_ambiguous(
            campaign, head_oid=H1, at=T2, detail="unknown"
        )
        validate(campaign)

    def test_every_supported_terminal_state(self) -> None:
        for status in sorted(m.TERMINAL_STATUSES):
            campaign = m.terminate(base_campaign(), status=status, at=T1)
            self.assertEqual(campaign["status"], status)
            validate(campaign)

    def test_every_supported_terminal_state_with_detail(self) -> None:
        for status in sorted(m.TERMINAL_STATUSES):
            campaign = m.terminate(
                base_campaign(), status=status, at=T1, detail="why"
            )
            validate(campaign)

    def test_closed_guard_state_via_generic_terminalization(self) -> None:
        campaign = m.reserve_request(
            base_campaign(), head_oid=H1, reserved_at=T1, snapshot=snapshot()
        )
        campaign = m.open_request_window(
            campaign,
            head_oid=H1,
            post_head_oid=H1,
            request_node_id="r1",
            request_created_at=T2,
            request_url="u",
        )
        campaign = m.terminate(
            campaign, status=m.ROUNDS_EXHAUSTED, at=T2
        )
        validate(campaign)

    def test_campaign_id_timestamp_is_independent_of_created_at(self) -> None:
        # Identity material: the embedded stamp need not equal created_at.
        campaign = base_campaign()
        campaign["campaign_id"] = "crp-20200101T000000Z-abc123"
        validate(campaign)

    def test_rollover_eligibility_matrix(self) -> None:
        active = base_campaign()
        self.assertFalse(m.is_rollover_eligible(active))
        consumed_active = active
        while consumed_active["rounds_used"] < consumed_active["config"]["max_rounds"]:
            consumed_active = m.consume_round(consumed_active, kind="remediation")
        self.assertFalse(m.is_rollover_eligible(consumed_active))
        for status in sorted(m.ROLLOVER_TERMINAL_STATUSES):
            eligible = m.terminate(
                {**base_campaign(), "rounds_used": 6}, status=status, at=T1
            )
            self.assertTrue(m.is_rollover_eligible(eligible), status)
        for status in (
            m.REQUEST_CREATION_FAILED,
            m.MANUAL_INTERVENTION_REQUIRED,
            m.AMBIGUOUS_INTERRUPTION,
            m.TARGET_UNAVAILABLE,
            m.HARD_FAILED,
        ):
            ineligible = m.terminate(
                {**base_campaign(), "rounds_used": 6}, status=status, at=T1
            )
            self.assertFalse(m.is_rollover_eligible(ineligible), status)
        unused_terminal = m.terminate(base_campaign(), status=m.SUCCEEDED, at=T1)
        self.assertFalse(m.is_rollover_eligible(unused_terminal))


class ValidatorRejectsCorruption(unittest.TestCase):
    def corrupt(self, mutate) -> None:
        campaign = base_campaign()
        mutate(campaign)
        with self.assertRaises(ValueError):
            validate(campaign)

    def test_naive_timestamps_are_rejected(self) -> None:
        self.corrupt(lambda c: c.update(created_at="2026-09-14T12:00:00"))
        terminal = m.terminate(base_campaign(), status=m.SUCCEEDED, at=T1)
        terminal["terminal_at"] = "2026-09-14T13:00:00"
        with self.assertRaises(ValueError):
            validate(terminal)
        campaign = m.reserve_request(
            base_campaign(), head_oid=H1, reserved_at=T1, snapshot=snapshot()
        )
        campaign["guards"][0]["reserved_at"] = "2026-09-14T12:05:00"
        with self.assertRaises(ValueError):
            validate(campaign)
        opened = m.open_request_window(
            m.reserve_request(
                base_campaign(), head_oid=H1, reserved_at=T1, snapshot=snapshot()
            ),
            head_oid=H1,
            post_head_oid=H1,
            request_node_id="r1",
            request_created_at=T2,
            request_url="u",
        )
        opened["guards"][0]["window_opened_at"] = "2026-09-14T12:10:00"
        with self.assertRaises(ValueError):
            validate(opened)

    def test_boolean_numeric_fields_are_rejected(self) -> None:
        self.corrupt(lambda c: c.update(rounds_used=True))
        self.corrupt(lambda c: c["config"].update(max_rounds=True))
        self.corrupt(lambda c: c["config"].update(interval_minutes=True))
        self.corrupt(lambda c: c.update(pull_request_number=True))
        self.corrupt(lambda c: c.update(schema_version=True))

    def test_malformed_creation_baseline_is_rejected(self) -> None:
        self.corrupt(lambda c: c.pop("creation_baseline"))
        self.corrupt(lambda c: c.update(creation_baseline=[]))
        self.corrupt(lambda c: c.update(creation_baseline={"reaction_ids": "x"}))
        self.corrupt(lambda c: c.update(creation_baseline={"reaction_ids": [1]}))
        self.corrupt(lambda c: c.update(creation_baseline={"reaction_ids": ["b", "a"]}))
        self.corrupt(lambda c: c.update(creation_baseline={"reaction_ids": ["a", "a"]}))
        self.corrupt(
            lambda c: c.update(creation_baseline={"reaction_ids": ["a"], "extra": []})
        )
        # The canonical sorted unique shape from the real constructor passes.
        validate(
            m.new_campaign(
                campaign_id="crp-20260914T120000Z-abc123",
                repository="owner/repo",
                pull_request_number=7,
                created_at=T0,
                max_rounds=6,
                model="m",
                reasoning_level="medium",
                interval_minutes=30,
                reviewer_logins=[CODEX],
                approval_logins=[CODEX],
                creation_baseline=["b", "a", "b"],
            )
        )

    def test_malformed_campaign_id_is_rejected(self) -> None:
        self.corrupt(lambda c: c.update(campaign_id="crp-bad"))
        self.corrupt(lambda c: c.update(campaign_id=""))

    def test_malformed_model_and_reasoning_are_rejected(self) -> None:
        self.corrupt(lambda c: c["config"].update(model=""))
        self.corrupt(lambda c: c["config"].update(reasoning_level="   "))

    def test_non_normalized_logins_are_rejected(self) -> None:
        self.corrupt(
            lambda c: c["config"].update(reviewer_logins=["ChatGPT-Codex-Connector"])
        )
        self.corrupt(
            lambda c: c["config"].update(approval_logins=["chatgpt-codex-connector[bot]"])
        )

    def test_active_campaign_must_not_carry_terminal_metadata(self) -> None:
        self.corrupt(lambda c: c.update(terminal_at=T1))
        self.corrupt(lambda c: c.update(status_detail="stranded"))

    def test_terminal_campaign_requires_terminal_at(self) -> None:
        terminal = m.terminate(base_campaign(), status=m.SUCCEEDED, at=T1)
        terminal["terminal_at"] = None
        with self.assertRaises(ValueError):
            validate(terminal)

    def test_malformed_guard_state_data_is_rejected(self) -> None:
        reserved = m.reserve_request(
            base_campaign(), head_oid=H1, reserved_at=T1, snapshot=snapshot()
        )
        unknown = m.deepcopy(reserved)
        unknown["guards"][0]["state"] = "mystery"
        with self.assertRaises(ValueError):
            validate(unknown)
        # A reserved guard must not claim a response window or request.
        with_request = m.deepcopy(reserved)
        with_request["guards"][0].update(
            request={"node_id": "r", "created_at": T1, "url": "u"}
        )
        with self.assertRaises(ValueError):
            validate(with_request)
        with_window = m.deepcopy(reserved)
        with_window["guards"][0].update(window_opened_at=T2)
        with self.assertRaises(ValueError):
            validate(with_window)
        opened = m.open_request_window(
            m.reserve_request(
                base_campaign(), head_oid=H1, reserved_at=T1, snapshot=snapshot()
            ),
            head_oid=H1,
            post_head_oid=H1,
            request_node_id="r1",
            request_created_at=T2,
            request_url="u",
        )
        no_request = m.deepcopy(opened)
        no_request["guards"][0]["request"] = None
        with self.assertRaises(ValueError):
            validate(no_request)
        no_window = m.deepcopy(opened)
        no_window["guards"][0]["window_opened_at"] = None
        with self.assertRaises(ValueError):
            validate(no_window)
        # SUPERSEDED requires its supersession timestamp.
        superseded = m.supersede_active_guards(opened, current_head_oid=H2, at=T2)
        missing = m.deepcopy(superseded)
        missing["guards"][0]["superseded_at"] = None
        with self.assertRaises(ValueError):
            validate(missing)
        # INVALIDATED requires its reason.
        invalidated = m.invalidate_reserved_request(
            m.reserve_request(
                base_campaign(), head_oid=H1, reserved_at=T1, snapshot=snapshot()
            ),
            head_oid=H1,
            at=T2,
            reason="head_changed",
        )
        no_reason = m.deepcopy(invalidated)
        no_reason["guards"][0]["invalidation_reason"] = None
        with self.assertRaises(ValueError):
            validate(no_reason)
        # UNBRACKETED requires the post-head request record.
        unbracketed = m.mark_unbracketed_request(
            m.reserve_request(
                base_campaign(), head_oid=H1, reserved_at=T1, snapshot=snapshot()
            ),
            head_oid=H1,
            post_head_oid=H2,
            request_node_id="r1",
            request_created_at=T2,
            request_url="u",
            at=T2,
        )
        missing_post = m.deepcopy(unbracketed)
        del missing_post["guards"][0]["request"]["post_head_oid"]
        with self.assertRaises(ValueError):
            validate(missing_post)
        # Two guards for one head are rejected.
        two_heads = m.reserve_request(
            base_campaign(), head_oid=H1, reserved_at=T1, snapshot=snapshot()
        )
        clone = m.deepcopy(two_heads["guards"][0])
        two_heads["guards"].append(clone)
        with self.assertRaises(ValueError):
            validate(two_heads)
        # Baseline shape is required.
        no_baseline = m.deepcopy(reserved)
        no_baseline["guards"][0]["baseline"] = None
        with self.assertRaises(ValueError):
            validate(no_baseline)

    def test_optional_empty_and_none_transition_outputs_remain_accepted(self) -> None:
        # Helper contract permits an empty request URL and a None post head.
        campaign = m.mark_unbracketed_request(
            m.reserve_request(
                base_campaign(), head_oid=H1, reserved_at=T1, snapshot=snapshot()
            ),
            head_oid=H1,
            post_head_oid=None,
            request_node_id="r1",
            request_created_at=T2,
            request_url="",
            at=T2,
        )
        validate(campaign)
        no_detail = m.terminate(base_campaign(), status=m.SUCCEEDED, at=T1)
        self.assertIsNone(no_detail["status_detail"])
        validate(no_detail)


class UnifiedTerminalInvariant(unittest.TestCase):
    def reserved_two_heads(self) -> dict:
        campaign = m.reserve_request(
            base_campaign(), head_oid=H1, reserved_at=T1, snapshot=snapshot()
        )
        return m.reserve_request(
            campaign, head_oid=H2, reserved_at=T1, snapshot=snapshot(H2)
        )

    def test_generic_terminalization_closes_every_active_window(self) -> None:
        campaign = self.reserved_two_heads()
        campaign = m.open_request_window(
            campaign,
            head_oid=H1,
            post_head_oid=H1,
            request_node_id="r1",
            request_created_at=T2,
            request_url="u",
        )
        campaign = m.open_request_window(
            campaign,
            head_oid=H2,
            post_head_oid=H2,
            request_node_id="r2",
            request_created_at=T2,
            request_url="u",
        )
        campaign = m.terminate(campaign, status=m.SUCCEEDED, at=T2)
        states = {g["head_oid"]: g["state"] for g in campaign["guards"]}
        self.assertEqual(states, {H1: m.CLOSED, H2: m.CLOSED})
        validate(campaign)

    def test_creation_failure_preserves_diagnostic_state_and_closes_windows(self) -> None:
        campaign = self.reserved_two_heads()
        campaign = m.open_request_window(
            campaign,
            head_oid=H1,
            post_head_oid=H1,
            request_node_id="r1",
            request_created_at=T2,
            request_url="u",
        )
        campaign = m.mark_request_creation_failed(
            campaign, head_oid=H2, at=T2, detail="failed"
        )
        states = {g["head_oid"]: g["state"] for g in campaign["guards"]}
        self.assertEqual(states, {H1: m.CLOSED, H2: m.CREATION_FAILED})
        self.assertEqual(campaign["status"], m.REQUEST_CREATION_FAILED)
        validate(campaign)

    def test_unbracketed_and_ambiguous_preserve_their_diagnostic_state(self) -> None:
        campaign = m.mark_unbracketed_request(
            self.reserved_two_heads(),
            head_oid=H2,
            post_head_oid="other",
            request_node_id="r2",
            request_created_at=T2,
            request_url="u",
            at=T2,
        )
        states = {g["head_oid"]: g["state"] for g in campaign["guards"]}
        # A sibling guard still in RESERVED is retained as interruption
        # evidence; only active response windows close.
        self.assertEqual(states, {H1: m.RESERVED, H2: m.UNBRACKETED})
        self.assertEqual(campaign["status"], m.MANUAL_INTERVENTION_REQUIRED)
        validate(campaign)

        campaign = m.mark_request_ambiguous(
            self.reserved_two_heads(), head_oid=H2, at=T2, detail="unknown"
        )
        states = {g["head_oid"]: g["state"] for g in campaign["guards"]}
        self.assertEqual(states, {H1: m.RESERVED, H2: m.GUARD_AMBIGUOUS})
        self.assertEqual(campaign["status"], m.AMBIGUOUS_INTERRUPTION)
        validate(campaign)

    def test_non_active_diagnostic_states_are_not_converted_to_closed(self) -> None:
        campaign = self.reserved_two_heads()
        campaign = m.invalidate_reserved_request(
            campaign, head_oid=H1, at=T2, reason="head_changed"
        )
        campaign = m.terminate(campaign, status=m.TARGET_UNAVAILABLE, at=T2)
        states = {g["head_oid"]: g["state"] for g in campaign["guards"]}
        self.assertEqual(states, {H1: m.INVALIDATED, H2: m.RESERVED})
        validate(campaign)

    def test_terminalization_requires_an_active_campaign(self) -> None:
        terminal = m.terminate(base_campaign(), status=m.SUCCEEDED, at=T1)
        with self.assertRaises(RuntimeError):
            m.terminate(terminal, status=m.ROUNDS_EXHAUSTED, at=T2)


if __name__ == "__main__":
    unittest.main()
