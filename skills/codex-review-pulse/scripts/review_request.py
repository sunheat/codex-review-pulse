#!/usr/bin/env python3
"""Execute one committed automatic @codex review request attempt.

The durable RESERVED guard is the authoritative handoff from the owned-worker
boundary (which consumed the round and reserved the per-head allowance) to
this executor. Ordering is safety-critical:

  1. establish current authority from durable state: matching campaign and
     lock identity, active campaign, exactly one committed RESERVED guard;
  2. authoritatively revalidate the request conditions;
  3. post the comment;
  4. immediately re-observe and bind a response window only when the
     pre/post head OIDs agree.

This executor never reserves or consumes again: it creates no second guard,
consumes no second round, and treats the committed action as durable. The
request snapshot is not re-played as reservation proof; the guard's own
baseline is authoritative. Definitive failure with proven absence of any
created comment terminates cleanly and releases ownership. Anything
unknowable fails closed and retains the permanent lock for manual recovery.

Final mutation-boundary revalidation, POST outcome classification, and
post-mutation guarded persistence hardening remain Phase 3 work.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Callable

import campaign_model as model
import github_api
import storage

REQUEST_BODY = "@codex review"


def committed_request_guard(campaign: dict[str, Any]) -> dict[str, Any]:
    """Locate the exact committed RESERVED guard from durable state.

    Refuses unless the campaign carries exactly one guard in RESERVED: zero
    means nothing was committed, and more than one is malformed state that
    must not be driven blindly.
    """
    reserved = [
        guard
        for guard in campaign.get("guards", [])
        if isinstance(guard, dict) and guard.get("state") == model.RESERVED
    ]
    if len(reserved) != 1:
        raise RuntimeError(
            "Request execution requires exactly one committed RESERVED guard; "
            f"found {len(reserved)}"
        )
    return reserved[0]


def _find_unexpected_request_comment(
    snapshot: dict[str, Any], baseline_comment_ids: set[str]
) -> dict[str, Any] | None:
    """A viewer-authored @codex comment not present in the complete baseline."""
    viewer = snapshot.get("viewer")
    for comment in snapshot.get("comments", []):
        if not isinstance(comment, dict):
            continue
        if comment.get("id") in baseline_comment_ids:
            continue
        if comment.get("login") != viewer:
            continue
        if github_api.normalize_comment_body(comment.get("body")) != REQUEST_BODY:
            continue
        return comment
    return None


def execute_request_attempt(
    *,
    campaign: dict[str, Any],
    head_oid: str,
    reserved_at: str,
    baseline_comment_ids: set[str],
    observer: Callable[[], dict[str, Any]],
    commenter: Callable[[str, str], dict[str, Any]],
    persist: Callable[[dict[str, Any]], None],
) -> dict[str, Any]:
    """Return an outcome dict. ``campaign`` already carries the committed guard.

    Every model transition is handed to ``persist`` before any further external
    call, so the guard state on disk never trails the mutation order.
    """
    result: dict[str, Any] = {
        "outcome": None,
        "campaign": campaign,
        "head_oid": head_oid,
        "retain_lock": False,
        "terminal": False,
    }

    # 2. Authoritative revalidation after the durable commitment.
    reobserved = observer()
    invalidation = _revalidation_reason(campaign, reobserved, head_oid)
    if invalidation is not None:
        campaign = model.invalidate_reserved_request(
            campaign,
            head_oid=head_oid,
            at=reobserved["server_time"],
            reason=invalidation,
        )
        persist(campaign)
        result.update(
            outcome="invalidated",
            campaign=campaign,
            retain_lock=False,
            detail=invalidation,
        )
        return result

    # 3. External mutation.
    try:
        created = commenter(reobserved["node_id"], REQUEST_BODY)
    except Exception as mutation_error:  # noqa: BLE001 - classification follows
        return _classify_creation_failure(
            result=result,
            campaign=campaign,
            head_oid=head_oid,
            reserved_at=reserved_at,
            baseline_comment_ids=baseline_comment_ids,
            observer=observer,
            mutation_error=mutation_error,
            persist=persist,
        )

    # 4. Immediate post-mutation head bracket.
    post_snapshot = observer()
    if (
        post_snapshot.get("complete") is not True
        or post_snapshot.get("head_oid") != head_oid
    ):
        campaign = model.mark_unbracketed_request(
            campaign,
            head_oid=head_oid,
            post_head_oid=post_snapshot.get("head_oid"),
            request_node_id=created["node_id"],
            request_created_at=created["created_at"],
            request_url=created.get("url") or "",
            at=post_snapshot.get("server_time") or created["created_at"],
        )
        persist(campaign)
        result.update(
            outcome="unbracketed",
            campaign=campaign,
            retain_lock=False,
            terminal=True,
        )
        return result

    campaign = model.open_request_window(
        campaign,
        head_oid=head_oid,
        post_head_oid=post_snapshot["head_oid"],
        request_node_id=created["node_id"],
        request_created_at=created["created_at"],
        request_url=created.get("url") or "",
    )
    persist(campaign)
    result.update(outcome="window_open", campaign=campaign, retain_lock=False)
    return result


def _revalidation_reason(
    campaign: dict[str, Any], snapshot: dict[str, Any], head_oid: str
) -> str | None:
    if snapshot.get("complete") is not True:
        return "incomplete_revalidation_evidence"
    if snapshot.get("pr_state") != "OPEN":
        return "pull_request_not_open"
    if snapshot.get("head_oid") != head_oid:
        return "head_changed"
    evidence = model.evaluate(campaign, snapshot)
    if evidence["threads"]:
        return "applicable_feedback_appeared"
    if evidence["eyes"]:
        return "review_entered_progress"
    if evidence["approval"] is not None:
        return "approval_proven"
    return None


def _classify_creation_failure(
    *,
    result: dict[str, Any],
    campaign: dict[str, Any],
    head_oid: str,
    reserved_at: str,
    baseline_comment_ids: set[str],
    observer: Callable[[], dict[str, Any]],
    mutation_error: Exception,
    persist: Callable[[dict[str, Any]], None],
) -> dict[str, Any]:
    try:
        post_snapshot = observer()
    except Exception:  # noqa: BLE001 - cannot establish anything -> fail closed
        campaign = model.mark_request_ambiguous(
            campaign,
            head_oid=head_oid,
            at=reserved_at,
            detail=f"creation error and re-observation failed: {mutation_error}",
        )
        persist(campaign)
        result.update(
            outcome="ambiguous", campaign=campaign, retain_lock=True, terminal=True
        )
        return result

    if post_snapshot.get("complete") is not True:
        campaign = model.mark_request_ambiguous(
            campaign,
            head_oid=head_oid,
            at=reserved_at,
            detail="creation error and post evidence is incomplete",
        )
        persist(campaign)
        result.update(
            outcome="ambiguous", campaign=campaign, retain_lock=True, terminal=True
        )
        return result

    unexpected = _find_unexpected_request_comment(
        post_snapshot, baseline_comment_ids
    )
    if unexpected is None:
        campaign = model.mark_request_creation_failed(
            campaign,
            head_oid=head_oid,
            at=post_snapshot["server_time"],
            detail=f"request creation definitively failed; no request created: {mutation_error}",
        )
        persist(campaign)
        result.update(
            outcome="creation_failed",
            campaign=campaign,
            retain_lock=False,
            terminal=True,
        )
        return result

    # A new viewer comment exists but the mutation reported failure: creation is
    # not provably ours, and equality/earlier timestamps cannot settle it either.
    campaign = model.mark_request_ambiguous(
        campaign,
        head_oid=head_oid,
        at=post_snapshot["server_time"],
        detail=(
            "creation error but an unaccounted viewer request comment exists: "
            + str(unexpected.get("id"))
        ),
    )
    persist(campaign)
    result.update(
        outcome="ambiguous", campaign=campaign, retain_lock=True, terminal=True
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Execute one committed @codex review request from its RESERVED guard"
    )
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pr", required=True, type=int)
    parser.add_argument("--repository-path", default=".")
    parser.add_argument("--owner-token", required=True)
    args = parser.parse_args()

    lock_metadata = storage.verify_owner(
        args.repo, args.pr, args.owner_token,
        repository_path=args.repository_path,
    )
    campaign_path = storage.campaign_path(
        args.repo, args.pr, repository_path=args.repository_path
    )
    campaign = storage.load_json(campaign_path)
    if campaign is None:
        raise RuntimeError(f"Campaign record does not exist: {campaign_path}")
    model.validate_campaign(
        campaign, repository=args.repo, pull_request_number=args.pr
    )
    if lock_metadata["campaign_id"] != campaign["campaign_id"]:
        raise RuntimeError(
            "Ownership lock belongs to a different campaign; refusing mutation"
        )
    if model.is_terminal(campaign):
        raise RuntimeError("Campaign is terminal; a request attempt cannot begin")

    # Durable handoff identity: matching campaign + owner + one exact
    # RESERVED guard. No snapshot replay, no second reservation.
    guard = committed_request_guard(campaign)
    head_oid = guard["head_oid"]
    reserved_at = guard["reserved_at"]
    baseline_comment_ids = set(guard.get("baseline", {}).get("comment_ids", []))

    def observer() -> dict[str, Any]:
        return github_api.fetch_snapshot(args.repo, args.pr)

    def commenter(subject_id: str, body: str) -> dict[str, Any]:
        return github_api.add_comment(subject_id, body)

    outcome = execute_request_attempt(
        campaign=campaign,
        head_oid=head_oid,
        reserved_at=reserved_at,
        baseline_comment_ids=baseline_comment_ids,
        observer=observer,
        commenter=commenter,
        persist=lambda record: storage.save_json(campaign_path, record),
    )
    storage.save_json(campaign_path, outcome["campaign"])
    print(json.dumps({k: v for k, v in outcome.items() if k != "campaign"} | {
        "campaign_status": outcome["campaign"]["status"],
        "guard_state": (
            model.guard_for_head(outcome["campaign"], head_oid) or {}
        ).get("state"),
        "rounds_used": outcome["campaign"]["rounds_used"],
    }, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from error
