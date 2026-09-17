from __future__ import annotations

from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "codex-review-pulse" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import campaign_model as m  # noqa: E402


CODEX = "chatgpt-codex-connector"
H1 = "h1-oid"
H2 = "h2-oid"
T0 = "2026-09-14T12:00:00Z"
T_PLUS_15 = "2026-09-14T12:15:00Z"
T_PLUS_30 = "2026-09-14T12:30:00Z"
T_PLUS_45 = "2026-09-14T12:45:00Z"


def campaign(
    *,
    rounds_used: int = 0,
    max_rounds: int = 6,
    interval: int = 30,
    guards: list | None = None,
    status: str = m.ACTIVE,
) -> dict:
    campaign_data = m.new_campaign(
        campaign_id="crp-20260914T120000Z-abc123",
        repository="owner/repo",
        pull_request_number=7,
        created_at=T0,
        max_rounds=max_rounds,
        model="a-model",
        reasoning_level="medium",
        interval_minutes=interval,
        reviewer_logins=[CODEX],
        approval_logins=[CODEX],
    )
    campaign_data["rounds_used"] = rounds_used
    campaign_data["guards"] = list(guards or [])
    campaign_data["status"] = status
    return campaign_data


def snapshot(
    *,
    complete: bool = True,
    server_time: str = T_PLUS_15,
    head: str = H1,
    threads: list | None = None,
    reactions: list | None = None,
    reviews: list | None = None,
    comments: list | None = None,
    pr_state: str = "OPEN",
) -> dict:
    return {
        "complete": complete,
        "server_time": server_time,
        "pr_state": pr_state,
        "head_oid": head,
        "threads": threads or [],
        "reactions": reactions or [],
        "reviews": reviews or [],
        "comments": comments or [],
    }


def thread(thread_id: str, *, resolved: bool = False, login: str = CODEX) -> dict:
    return {
        "id": thread_id,
        "is_resolved": resolved,
        "root_login": login,
        "path": "x.py",
        "body": "change",
    }


def reaction(
    content: str, *, created_at: str = T_PLUS_15, login: str = CODEX, rid: str | None = None
) -> dict:
    return {
        "id": rid or f"r-{content}",
        "content": content,
        "login": login,
        "created_at": created_at,
    }


def review(
    *,
    state: str = "COMMENTED",
    submitted_at: str = T_PLUS_15,
    commit: str = H1,
    login: str = CODEX,
    review_id: str = "rv1",
) -> dict:
    return {
        "id": review_id,
        "state": state,
        "login": login,
        "commit_oid": commit,
        "submitted_at": submitted_at,
    }


def reserve_and_open(
    campaign_data: dict,
    *,
    head: str = H1,
    reserved_at: str = T0,
    opened_at: str = T0,
    baseline: dict | None = None,
) -> dict:
    snap = snapshot(head=head, server_time=reserved_at, comments=[])
    if baseline is not None:
        snap.update(baseline)
    campaign_data = m.reserve_request(
        campaign_data, head_oid=head, reserved_at=reserved_at, snapshot=snap
    )
    campaign_data = m.open_request_window(
        campaign_data,
        head_oid=head,
        post_head_oid=head,
        request_node_id="req1",
        request_created_at=opened_at,
        request_url="https://example.test/req1",
    )
    return campaign_data


class DecisionTests(unittest.TestCase):
    def test_incomplete_observation_waits_and_never_proves_absence(self) -> None:
        directive = m.decide(campaign(), snapshot(complete=False))
        self.assertEqual(directive["action"], "wait_observation_incomplete")
        # Even long after an interval, incomplete evidence stays a wait.
        directive = m.decide(
            campaign(),
            snapshot(complete=False, server_time=T_PLUS_45),
        )
        self.assertEqual(directive["action"], "wait_observation_incomplete")

    def test_closed_pr_is_deterministic_terminal(self) -> None:
        directive = m.decide(campaign(), snapshot(pr_state="MERGED"))
        self.assertEqual(directive["status"], m.TARGET_UNAVAILABLE)

    def test_terminal_campaign_passes_through_without_action(self) -> None:
        directive = m.decide(
            campaign(status=m.SUCCEEDED),
            snapshot(threads=[thread("T1")]),
        )
        self.assertEqual(directive["action"], "campaign_terminal")
        self.assertEqual(directive["status"], m.SUCCEEDED)

    def test_threads_request_remediation_then_approval(self) -> None:
        c = campaign()
        directive = m.decide(c, snapshot(threads=[thread("T1"), thread("T2")]))
        self.assertEqual(directive["action"], "remediation_batch")
        self.assertEqual([t["id"] for t in directive["threads"]], ["T1", "T2"])

    def test_unresolved_feedback_takes_precedence_over_thumbs_up(self) -> None:
        directive = m.decide(
            campaign(),
            snapshot(
                threads=[thread("T1")],
                reactions=[reaction(m.THUMBS_UP, rid="up1")],
            ),
        )
        self.assertEqual(directive["action"], "remediation_batch")

    def test_commit_bound_approved_review_succeeds(self) -> None:
        directive = m.decide(
            campaign(),
            snapshot(reviews=[review(state=m.APPROVED)]),
        )
        self.assertEqual(directive["status"], m.SUCCEEDED)

    def test_old_head_approval_review_is_not_applicable(self) -> None:
        directive = m.decide(
            campaign(),
            snapshot(reviews=[review(state=m.APPROVED, commit=H2)]),
        )
        self.assertEqual(directive["action"], "request_review")

    def test_thumbs_up_without_guard_cannot_prove_approval(self) -> None:
        # GitHub no longer exposes a commit-level push timestamp, so a PR-level
        # reaction cannot be attributed to the current head. It never proves
        # approval and never authorizes a request; it is a non-counting wait.
        directive = m.decide(
            campaign(),
            snapshot(reactions=[reaction(m.THUMBS_UP, created_at=T_PLUS_15)]),
        )
        self.assertEqual(directive["action"], "wait_lifecycle_attribution_unknown")
        self.assertEqual(directive["reaction_ids"], ["r-THUMBS_UP"])

    def test_old_thumbs_up_cannot_terminate_or_request_fresh_campaign(self) -> None:
        directive = m.decide(
            campaign(),
            snapshot(
                reactions=[
                    reaction(m.THUMBS_UP, created_at="2026-09-14T09:00:00Z", rid="up0")
                ]
            ),
        )
        self.assertEqual(directive["action"], "wait_lifecycle_attribution_unknown")

    def test_eyes_without_guard_is_attribution_unknown_wait(self) -> None:
        directive = m.decide(
            campaign(),
            snapshot(reactions=[reaction(m.EYES, created_at=T_PLUS_15)]),
        )
        self.assertEqual(directive["action"], "wait_lifecycle_attribution_unknown")
        self.assertEqual(directive["reaction_ids"], [f"r-{m.EYES}"])

    def test_commit_bound_approval_beats_unattributed_reactions(self) -> None:
        directive = m.decide(
            campaign(),
            snapshot(
                reactions=[
                    reaction(m.THUMBS_UP, created_at="2026-09-14T09:00:00Z", rid="up0")
                ],
                reviews=[review(state=m.APPROVED)],
            ),
        )
        self.assertEqual(directive["status"], m.SUCCEEDED)

    def test_unknown_actor_eyes_is_not_applicable(self) -> None:
        directive = m.decide(
            campaign(),
            snapshot(reactions=[reaction(m.EYES, login="some-human")]),
        )
        self.assertEqual(directive["action"], "request_review")

    def test_human_threads_are_not_actionable_and_request_still_available(self) -> None:
        directive = m.decide(
            campaign(),
            snapshot(threads=[thread("h1", login="human")]),
        )
        self.assertEqual(directive["action"], "request_review")

    def test_request_review_consumes_no_round_in_decide(self) -> None:
        c = campaign()
        directive = m.decide(c, snapshot())
        self.assertEqual(directive["action"], "request_review")
        self.assertEqual(c["rounds_used"], 0)

    def test_exhaustion_without_guard(self) -> None:
        directive = m.decide(campaign(rounds_used=6), snapshot())
        self.assertEqual(directive["status"], m.ROUNDS_EXHAUSTED)

    def test_exhausted_unattributable_thumbs_up_terminates_exhausted(self) -> None:
        # The repeated-wake incident: an active fully-consumed campaign with no
        # request guard must not loop forever on the attribution-unknown wait.
        # Exhaustion takes precedence; the unattributable reaction proves
        # neither approval nor review progress.
        directive = m.decide(
            campaign(rounds_used=6),
            snapshot(reactions=[reaction(m.THUMBS_UP, created_at=T_PLUS_15)]),
        )
        self.assertEqual(directive["action"], "terminal")
        self.assertEqual(directive["status"], m.ROUNDS_EXHAUSTED)
        self.assertEqual(directive["basis"], "observation")
        self.assertNotEqual(directive["action"], "wait_lifecycle_attribution_unknown")

    def test_exhausted_unattributable_eyes_terminates_exhausted(self) -> None:
        directive = m.decide(
            campaign(rounds_used=6),
            snapshot(reactions=[reaction(m.EYES, created_at=T_PLUS_15)]),
        )
        self.assertEqual(directive["action"], "terminal")
        self.assertEqual(directive["status"], m.ROUNDS_EXHAUSTED)
        self.assertEqual(directive["basis"], "observation")
        self.assertNotEqual(directive["action"], "wait_lifecycle_attribution_unknown")

    def test_exhausted_unattributable_reaction_never_claims_approval(self) -> None:
        # Exhaustion precedence must not reinterpret the reaction as approval.
        directive = m.decide(
            campaign(rounds_used=6),
            snapshot(reactions=[reaction(m.THUMBS_UP, created_at=T_PLUS_15)]),
        )
        self.assertNotEqual(directive["status"], m.SUCCEEDED)
        self.assertNotIn("proof", directive)

    def test_feedback_with_no_remaining_round_terminates_exhausted(self) -> None:
        c = reserve_and_open(campaign(rounds_used=5, max_rounds=6))
        directive = m.decide(
            c, snapshot(threads=[thread("T9")], server_time=T_PLUS_15)
        )
        self.assertEqual(directive["status"], m.ROUNDS_EXHAUSTED)

    def test_preexisting_completed_review_does_not_block_request(self) -> None:
        # No guard yet: a non-approval review predating the campaign request
        # neither consumes the allowance nor terminates the campaign.
        directive = m.decide(
            campaign(),
            snapshot(reviews=[review(state="COMMENTED", submitted_at="2026-09-14T11:00:00Z")]),
        )
        self.assertEqual(directive["action"], "request_review")

    def test_manual_request_comment_does_not_block_or_consume_allowance(self) -> None:
        directive = m.decide(
            campaign(),
            snapshot(
                comments=[
                    {
                        "id": "manual1",
                        "login": "human-operator",
                        "created_at": "2026-09-14T11:30:00Z",
                        "body": "@codex review",
                    }
                ]
            ),
        )
        self.assertEqual(directive["action"], "request_review")

    def test_outstanding_window_waits_before_interval(self) -> None:
        c = reserve_and_open(campaign(rounds_used=1))
        directive = m.decide(c, snapshot(server_time=T_PLUS_15))
        self.assertEqual(directive["action"], "wait_request_outstanding")

    def test_extra_immediate_delivery_waits(self) -> None:
        c = reserve_and_open(campaign(rounds_used=1))
        directive = m.decide(c, snapshot(server_time=T0))
        self.assertEqual(directive["action"], "wait_request_outstanding")

    def test_final_round_request_still_observable_while_waiting(self) -> None:
        c = reserve_and_open(campaign(rounds_used=5, max_rounds=6))
        self.assertEqual(
            m.decide(c, snapshot(server_time=T_PLUS_15))["action"],
            "wait_request_outstanding",
        )

    def test_final_round_request_still_observable_to_eyes(self) -> None:
        c = reserve_and_open(campaign(rounds_used=5, max_rounds=6))
        directive = m.decide(
            c, snapshot(reactions=[reaction(m.EYES)], server_time=T_PLUS_15)
        )
        self.assertEqual(directive["action"], "wait_review_in_progress")

    def test_final_round_request_still_observable_to_approval(self) -> None:
        c = reserve_and_open(campaign(rounds_used=5, max_rounds=6))
        directive = m.decide(
            c,
            snapshot(
                reactions=[reaction(m.THUMBS_UP, rid="up-after")],
                server_time=T_PLUS_15,
            ),
        )
        self.assertEqual(directive["status"], m.SUCCEEDED)

    def test_post_request_completion_without_approval_terminates(self) -> None:
        c = reserve_and_open(campaign(rounds_used=1))
        directive = m.decide(
            c,
            snapshot(
                reviews=[review(state="CHANGES_REQUESTED", submitted_at=T_PLUS_15)],
                server_time=T_PLUS_15,
            ),
        )
        self.assertEqual(directive["status"], m.REVIEW_COMPLETED_WITHOUT_APPROVAL)

    def test_equal_timestamp_completion_is_not_eligible(self) -> None:
        c = reserve_and_open(campaign(rounds_used=1))
        # Same-second review cannot be proven to follow the request: before the
        # interval it is a plain wait, never a negative terminal.
        directive = m.decide(
            c,
            snapshot(
                reviews=[review(state="COMMENTED", submitted_at=T0, review_id="rv-eq")],
                server_time=T_PLUS_15,
            ),
        )
        self.assertEqual(directive["action"], "wait_request_outstanding")
        # After a full interval the same ordering ambiguity requires a human.
        directive = m.decide(
            c,
            snapshot(
                reviews=[review(state="COMMENTED", submitted_at=T0, review_id="rv-eq")],
                server_time=T_PLUS_30,
            ),
        )
        self.assertEqual(directive["status"], m.MANUAL_INTERVENTION_REQUIRED)
        self.assertIn("temporal", directive["detail"])

    def test_no_response_requires_one_interval_and_complete_evidence(self) -> None:
        c = reserve_and_open(campaign(rounds_used=1))
        directive = m.decide(c, snapshot(server_time=T_PLUS_30))
        self.assertEqual(directive["status"], m.CODEX_REVIEW_SERVICE_UNRESPONSIVE)

    def test_baseline_reactions_are_not_responses(self) -> None:
        c = reserve_and_open(
            campaign(rounds_used=1),
            baseline={
                "reactions": [
                    {"id": "eyes-old", "content": m.EYES, "login": CODEX,
                     "created_at": "2026-09-14T11:59:00Z"},
                ]
            },
        )
        directive = m.decide(
            c,
            snapshot(
                server_time=T_PLUS_15,
                reactions=[
                    {"id": "eyes-old", "content": m.EYES, "login": CODEX,
                     "created_at": "2026-09-14T11:59:00Z"},
                ],
            ),
        )
        self.assertEqual(directive["action"], "wait_request_outstanding")

    def test_baseline_completed_review_does_not_terminate(self) -> None:
        c = reserve_and_open(
            campaign(rounds_used=1),
            baseline={"reviews": [review(state="COMMENTED", review_id="rv-old")]},
        )
        directive = m.decide(
            c,
            snapshot(
                server_time=T_PLUS_30,
                reviews=[review(state="COMMENTED", review_id="rv-old")],
            ),
        )
        self.assertEqual(directive["status"], m.CODEX_REVIEW_SERVICE_UNRESPONSIVE)

    def test_head_change_supersedes_window_and_allows_new_head_request(self) -> None:
        c = reserve_and_open(campaign(rounds_used=1))
        c = m.supersede_active_guards(c, current_head_oid=H2, at=T_PLUS_15)
        directive = m.decide(c, snapshot(head=H2, server_time=T_PLUS_15))
        self.assertEqual(directive["action"], "request_review")
        self.assertEqual(len(c["guards"]), 1)

    def test_returned_head_does_not_restore_allowance_or_reactivate_window(self) -> None:
        c = reserve_and_open(campaign(rounds_used=1))
        c = m.supersede_active_guards(c, current_head_oid=H2, at=T_PLUS_15)
        # Codex eventually finishes on H2, head returns to H1 much later.
        directive = m.decide(
            c,
            snapshot(
                head=H1,
                server_time=T_PLUS_45,
                reactions=[
                    reaction(m.EYES, created_at=T_PLUS_15, rid="stale-eyes"),
                ],
            ),
        )
        self.assertEqual(directive["status"], m.MANUAL_INTERVENTION_REQUIRED)

    def test_returned_head_allows_wait_only_for_genuinely_new_eyes(self) -> None:
        c = reserve_and_open(campaign(rounds_used=1))
        c = m.supersede_active_guards(c, current_head_oid=H2, at=T_PLUS_15)
        directive = m.decide(
            c,
            snapshot(
                head=H1,
                server_time=T_PLUS_45,
                reactions=[
                    reaction(m.EYES, created_at=T_PLUS_45, rid="new-eyes"),
                ],
            ),
        )
        self.assertEqual(directive["action"], "wait_review_in_progress")

    def test_returned_head_current_feedback_still_actionable(self) -> None:
        c = reserve_and_open(campaign(rounds_used=1))
        c = m.supersede_active_guards(c, current_head_oid=H2, at=T_PLUS_15)
        directive = m.decide(
            c,
            snapshot(
                head=H1,
                server_time=T_PLUS_45,
                threads=[thread("Tn")],
            ),
        )
        self.assertEqual(directive["action"], "remediation_batch")

    def test_reserved_guard_without_result_fails_closed(self) -> None:
        snap = snapshot()
        c = m.reserve_request(
            campaign(), head_oid=H1, reserved_at=T0, snapshot=snap
        )
        directive = m.decide(c, snapshot(server_time=T_PLUS_15))
        self.assertEqual(directive["status"], m.AMBIGUOUS_INTERRUPTION)
        self.assertEqual(directive["basis"], "durable_local")

    def test_reserved_guard_on_older_head_still_fails_closed(self) -> None:
        # The campaign-wide preflight inspects every guard: a RESERVED guard on
        # a head that is no longer current still terminalizes before any
        # observation could hide it.
        snap = snapshot()
        c = m.reserve_request(
            campaign(), head_oid=H2, reserved_at=T0, snapshot=snapshot(head=H2)
        )
        directive = m.decide(c, snapshot(head=H1, server_time=T_PLUS_15))
        self.assertEqual(directive["status"], m.AMBIGUOUS_INTERRUPTION)
        self.assertEqual(directive["guard_head"], H2)
        self.assertEqual(directive["basis"], "durable_local")

    def test_reserved_ambiguity_beats_incomplete_observation(self) -> None:
        snap = snapshot()
        c = m.reserve_request(
            campaign(), head_oid=H1, reserved_at=T0, snapshot=snap
        )
        directive = m.decide(c, snapshot(complete=False))
        self.assertEqual(directive["status"], m.AMBIGUOUS_INTERRUPTION)
        self.assertEqual(directive["basis"], "durable_local")

    def test_invalidated_same_head_leads_to_manual_intervention(self) -> None:
        snap = snapshot()
        c = m.reserve_request(campaign(), head_oid=H1, reserved_at=T0, snapshot=snap)
        c = m.invalidate_reserved_request(
            c, head_oid=H1, at=T0, reason="review_entered_progress"
        )
        directive = m.decide(c, snapshot(server_time=T_PLUS_30))
        self.assertEqual(directive["status"], m.MANUAL_INTERVENTION_REQUIRED)
        self.assertIn("invalidated", directive["detail"])

    def test_invalidation_eyes_then_clear_still_waits_then_manual(self) -> None:
        # Eyes appearing after reservation are honored as in-progress evidence,
        # even though the POST itself never happened.
        snap = snapshot()
        c = m.reserve_request(campaign(), head_oid=H1, reserved_at=T0, snapshot=snap)
        c = m.invalidate_reserved_request(
            c, head_oid=H1, at=T0, reason="review_entered_progress"
        )
        directive = m.decide(
            c,
            snapshot(
                server_time=T_PLUS_15,
                reactions=[reaction(m.EYES, created_at=T_PLUS_15)],
            ),
        )
        self.assertEqual(directive["action"], "wait_review_in_progress")


class CreationBaselineTests(unittest.TestCase):
    """Pre-existing creation-baseline reactions prove nothing and block nothing."""

    def baseline_campaign(self) -> dict:
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
            creation_baseline=["r-EYES", "r-THUMBS_UP"],
        )

    def test_validator_accepts_the_creation_baseline_shape(self) -> None:
        m.validate_campaign(
            self.baseline_campaign(), repository="owner/repo", pull_request_number=7
        )

    def test_baseline_eyes_does_not_prove_review_in_progress(self) -> None:
        # Without a guard the old EYES would be an attribution-unknown wait;
        # from the creation baseline it is simply pre-campaign history.
        c = self.baseline_campaign()
        directive = m.decide(
            c,
            snapshot(
                reactions=[reaction(m.EYES, rid="r-EYES", created_at="2026-09-14T09:00:00Z")]
            ),
        )
        self.assertEqual(directive["action"], "request_review")

    def test_baseline_thumbs_up_does_not_approve(self) -> None:
        c = self.baseline_campaign()
        directive = m.decide(
            c,
            snapshot(
                reactions=[
                    reaction(m.THUMBS_UP, rid="r-THUMBS_UP", created_at="2026-09-14T09:00:00Z")
                ]
            ),
        )
        self.assertEqual(directive["action"], "request_review")

    def test_baseline_does_not_block_the_request_allowance(self) -> None:
        c = self.baseline_campaign()
        directive = m.decide(c, snapshot())
        self.assertEqual(directive["action"], "request_review")
        self.assertEqual(c["rounds_used"], 0)
        self.assertEqual(c["guards"], [])

    def test_baseline_reaction_ids_are_derived_from_one_snapshot(self) -> None:
        snap = {
            "reactions": [
                {"id": "r-b", "content": m.EYES, "login": CODEX, "created_at": T0},
                {"id": "r-a", "content": m.THUMBS_UP, "login": CODEX, "created_at": T0},
                {"id": "r-human", "content": m.EYES, "login": "a-human", "created_at": T0},
                {"id": "r-heart", "content": "HEART", "login": CODEX, "created_at": T0},
            ]
        }
        self.assertEqual(
            m.creation_baseline_reaction_ids(
                snap,
                reviewer_logins=[CODEX],
                approval_logins=[CODEX],
            ),
            ["r-a", "r-b"],
        )

    def test_request_time_guard_baselines_stay_independent(self) -> None:
        c = self.baseline_campaign()
        guard_snapshot = snapshot(
            reactions=[
                reaction(m.EYES, rid="eyes-after", created_at=T_PLUS_15),
            ]
        )
        c = m.reserve_request(
            c, head_oid=H1, reserved_at=T0, snapshot=guard_snapshot
        )
        # The request-time baseline is the guard's own; the creation baseline
        # is untouched by reservation.
        self.assertEqual(
            c["creation_baseline"], {"reaction_ids": ["r-EYES", "r-THUMBS_UP"]}
        )
        self.assertIn(
            "eyes-after", c["guards"][0]["baseline"]["reaction_ids"]
        )

    def test_unresolved_threads_remain_actionable_despite_baseline(self) -> None:
        c = self.baseline_campaign()
        directive = m.decide(c, snapshot(threads=[thread("T1")]))
        self.assertEqual(directive["action"], "remediation_batch")


class GuardTransitionTests(unittest.TestCase):
    def test_reserve_consumes_round_and_rejects_duplicate_head_forever(self) -> None:
        snap = snapshot()
        c = m.reserve_request(campaign(), head_oid=H1, reserved_at=T0, snapshot=snap)
        self.assertEqual(c["rounds_used"], 1)
        for state_maker in (
            lambda: m.invalidate_reserved_request(c, head_oid=H1, at=T0, reason="x"),
            lambda: m.mark_request_creation_failed(c, head_oid=H1, at=T0, detail="x"),
        ):
            with self.assertRaises(RuntimeError):
                m.reserve_request(state_maker(), head_oid=H1, reserved_at=T0, snapshot=snap)

    def test_reserve_requires_budget_and_complete_matching_head(self) -> None:
        with self.assertRaises(RuntimeError):
            m.reserve_request(
                campaign(rounds_used=6),
                head_oid=H1,
                reserved_at=T0,
                snapshot=snapshot(),
            )
        with self.assertRaises(ValueError):
            m.reserve_request(
                campaign(),
                head_oid=H1,
                reserved_at=T0,
                snapshot=snapshot(complete=False),
            )
        with self.assertRaises(ValueError):
            m.reserve_request(
                campaign(),
                head_oid="other",
                reserved_at=T0,
                snapshot=snapshot(head=H1),
            )

    def test_consumed_request_round_is_not_restored_on_invalidation(self) -> None:
        snap = snapshot()
        c = m.reserve_request(campaign(), head_oid=H1, reserved_at=T0, snapshot=snap)
        c = m.invalidate_reserved_request(c, head_oid=H1, at=T0, reason="head_changed")
        self.assertEqual(c["rounds_used"], 1)
        self.assertEqual(c["guards"][0]["state"], m.INVALIDATED)
        self.assertEqual(c["status"], m.ACTIVE)

    def test_consumed_request_round_survives_creation_failure_and_ambiguity(self) -> None:
        snap = snapshot()
        c = m.reserve_request(campaign(), head_oid=H1, reserved_at=T0, snapshot=snap)
        failed = m.mark_request_creation_failed(c, head_oid=H1, at=T0, detail="boom")
        self.assertEqual(failed["rounds_used"], 1)
        self.assertEqual(failed["status"], m.REQUEST_CREATION_FAILED)
        c2 = m.reserve_request(campaign(), head_oid=H1, reserved_at=T0, snapshot=snap)
        ambiguous = m.mark_request_ambiguous(c2, head_oid=H1, at=T0, detail="boom")
        self.assertEqual(ambiguous["rounds_used"], 1)
        self.assertEqual(ambiguous["status"], m.AMBIGUOUS_INTERRUPTION)

    def test_bracket_mismatch_cannot_open_window(self) -> None:
        snap = snapshot()
        c = m.reserve_request(campaign(), head_oid=H1, reserved_at=T0, snapshot=snap)
        with self.assertRaises(ValueError):
            m.open_request_window(
                c,
                head_oid=H1,
                post_head_oid=H2,
                request_node_id="r",
                request_created_at=T0,
                request_url="u",
            )

    def test_terminal_cannot_be_overwritten(self) -> None:
        c = campaign()
        c["status"] = m.SUCCEEDED
        c["terminal_at"] = T0
        with self.assertRaises(RuntimeError):
            m.terminate(c, status=m.ROUNDS_EXHAUSTED, at=T0)

    def test_malformed_campaign_fails_closed(self) -> None:
        good = campaign()
        bad_schema = dict(good, schema_version=99)
        with self.assertRaises(ValueError):
            m.validate_campaign(bad_schema, repository="owner/repo", pull_request_number=7)
        bad_rounds = dict(good, rounds_used=99)
        with self.assertRaises(ValueError):
            m.validate_campaign(bad_rounds, repository="owner/repo", pull_request_number=7)
        bad_repo = dict(good)
        with self.assertRaises(ValueError):
            m.validate_campaign(bad_repo, repository="other/repo", pull_request_number=7)

    def test_new_campaign_validates_config(self) -> None:
        kwargs = dict(
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
        )
        for bad in (0, 11):
            with self.assertRaises(ValueError):
                m.new_campaign(**{**kwargs, "max_rounds": bad})
        with self.assertRaises(ValueError):
            m.new_campaign(**{**kwargs, "interval_minutes": 0})
        with self.assertRaises(ValueError):
            m.new_campaign(**{**kwargs, "model": "  "})
        self.assertEqual(
            m.new_campaign(**{**kwargs, "reviewer_logins": ["ChatGPT-Codex-Connector[bot]"]})["config"][
                "reviewer_logins"
            ],
            ["chatgpt-codex-connector"],
        )

    def test_strict_temporal_ordering(self) -> None:
        self.assertTrue(m.strictly_after(T_PLUS_15, T0))
        self.assertFalse(m.strictly_after(T0, T0))
        self.assertFalse(m.strictly_after("bad", T0))


if __name__ == "__main__":
    unittest.main()
