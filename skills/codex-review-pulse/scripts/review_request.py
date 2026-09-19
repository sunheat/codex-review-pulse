#!/usr/bin/env python3
"""Execute one committed automatic @codex review request attempt.

The durable RESERVED guard is the authoritative handoff from the owned-worker
boundary (which consumed the round and reserved the per-head allowance) to
this Phase 3 boundary. Ordering is safety-critical:

  1. establish current authority from durable state: matching campaign and
     lock identity, active campaign, exactly one committed RESERVED guard;
  2. authoritatively revalidate the request conditions;
  3. revalidate matching local ownership as the final local authority
     operation immediately before the mutation;
  4. post the comment, at most once;
  5. immediately re-observe and bind a response window only when the
     pre/post head OIDs agree;
  6. classify the result and persist every campaign transition through the
     guarded Phase 1 stale-write protection;
  7. complete the local ownership disposition: release only after the durable
     result is confirmed, retain on ambiguity.

This boundary never reserves or consumes again: it creates no second guard,
consumes no second round, and treats the committed action as durable. The
request snapshot is not re-played as reservation proof; the guard's own
baseline is authoritative. A transport-level POST failure with no visible
comment is never definitive on its own: the mutation may still complete.
Definitive failure requires a server-authoritative rejection or proven
absence of any created comment. Anything unknowable fails closed and retains
the permanent lock for manual recovery.
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


def _fail_closed(outcome: str, reason: str) -> dict[str, Any]:
    return {
        "outcome": outcome,
        "reason": reason,
        "terminal": False,
        "ownership": "retained",
        "scheduler_cleanup_authorized": False,
    }


def run_committed_request(
    *,
    repository: str,
    pr_number: int,
    owner_token: str,
    repository_path: str | Path = ".",
    observer: Callable[[], dict[str, Any]] | None = None,
    commenter: Callable[[str, str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """The Phase 3 committed-request execution boundary.

    Derives the durable handoff (exact campaign identity, owner lock/token,
    one exact RESERVED guard) itself, performs final pre-POST ownership
    revalidation, persists every transition through guarded stale-write
    protection, and completes the local ownership disposition before
    returning. The returned ``ownership`` field is the actual completed
    disposition, never a recommendation.
    """
    canonical = model.canonical_repository(repository)
    if observer is None:
        observer = lambda: github_api.fetch_snapshot(canonical, pr_number)  # noqa: E731
    if commenter is None:
        commenter = github_api.add_comment

    # 1. Establish the durable handoff from current local state.
    try:
        metadata = storage.ensure_active_campaign_owner(
            repository, pr_number, owner_token, repository_path=repository_path
        )
        campaign_path = storage.campaign_path(
            repository, pr_number, repository_path=repository_path
        )
        campaign = storage.load_json(campaign_path)
        if campaign is None:
            raise RuntimeError(f"Campaign record does not exist: {campaign_path}")
        model.validate_campaign(
            campaign, repository=repository, pull_request_number=pr_number
        )
        if campaign["campaign_id"] != metadata["campaign_id"]:
            raise RuntimeError(
                "Ownership lock belongs to a different campaign; refusing mutation"
            )
        guard = committed_request_guard(campaign)
    except Exception as error:  # noqa: BLE001 - fail closed without mutation
        return _fail_closed("local_fail_closed", storage.error_text(error))

    head_oid = guard["head_oid"]
    reserved_at = guard["reserved_at"]
    baseline_comment_ids = set(guard.get("baseline", {}).get("comment_ids", []))
    state = {"campaign": campaign}

    def persist_transition(transition: Callable[[dict[str, Any]], dict[str, Any]]) -> dict[str, Any]:
        """Guarded stale-write persistence of one operation-specific transition."""
        persisted = storage.apply_campaign_transition_if_current(
            repository,
            pr_number,
            owner_token=owner_token,
            expected_source=state["campaign"],
            transition=transition,
            repository_path=repository_path,
        )
        state["campaign"] = persisted
        return persisted

    def final_authority_check() -> None:
        """The final local authority operation before the POST.

        Re-proves the exact matching ownership and that the committed RESERVED
        guard for this head is still the campaign's single committed request.
        """
        current_metadata = storage.ensure_active_campaign_owner(
            repository, pr_number, owner_token, repository_path=repository_path
        )
        current = storage.load_json(campaign_path)
        if current is None:
            raise RuntimeError("Campaign record disappeared before the request POST")
        model.validate_campaign(
            current, repository=repository, pull_request_number=pr_number
        )
        if current["campaign_id"] != current_metadata["campaign_id"]:
            raise RuntimeError(
                "Ownership lock belongs to a different campaign before the POST"
            )
        current_guard = committed_request_guard(current)
        if current_guard["head_oid"] != head_oid:
            raise RuntimeError("The committed request guard changed before the POST")

    try:
        result = execute_request_attempt(
            campaign=state["campaign"],
            head_oid=head_oid,
            reserved_at=reserved_at,
            baseline_comment_ids=baseline_comment_ids,
            observer=observer,
            commenter=commenter,
            persist_transition=persist_transition,
            final_authority_check=final_authority_check,
        )
    except Exception as error:  # noqa: BLE001 - post-mutation state is uncertain
        return _fail_closed(
            "local_fail_closed",
            "request execution failed and the campaign state is uncertain: "
            + storage.error_text(error),
        )
    return _dispose(
        repository,
        pr_number,
        owner_token,
        repository_path=repository_path,
        result=result,
    )


def _dispose(
    repository: str,
    pr_number: int,
    owner_token: str,
    *,
    repository_path: str | Path,
    result: dict[str, Any],
) -> dict[str, Any]:
    """Complete the boundary-owned local ownership disposition."""
    campaign = result["campaign"]
    printable = {
        "outcome": result["outcome"],
        "detail": result.get("detail"),
        "terminal": result["terminal"],
        "guard_state": (model.guard_for_head(campaign, result["head_oid"]) or {}).get("state"),
        "campaign_status": campaign["status"],
        "rounds_used": campaign["rounds_used"],
    }
    if result["retain_lock"]:
        return {**printable, "ownership": "retained", "scheduler_cleanup_authorized": False}
    try:
        storage.release_lock(
            repository, pr_number, owner_token, repository_path=repository_path
        )
    except Exception as error:  # noqa: BLE001 - release must never be guessed
        # Release failure changes only the ownership disposition. The
        # campaign terminality was already determined by the request attempt
        # and must not be rewritten here.
        return {
            **printable,
            "outcome": "local_fail_closed",
            "reason": (
                f"request outcome {result['outcome']} completed but release "
                "could not be confirmed: " + storage.error_text(error)
            ),
            "terminal": result["terminal"],
            "ownership": "release_unconfirmed",
            "scheduler_cleanup_authorized": False,
        }
    return {
        **printable,
        "ownership": "released",
        "scheduler_cleanup_authorized": result["terminal"],
    }


def execute_request_attempt(
    *,
    campaign: dict[str, Any],
    head_oid: str,
    reserved_at: str,
    baseline_comment_ids: set[str],
    observer: Callable[[], dict[str, Any]],
    commenter: Callable[[str, str], dict[str, Any]],
    persist_transition: Callable[[Callable[[dict[str, Any]], dict[str, Any]]], dict[str, Any]],
    final_authority_check: Callable[[], None],
) -> dict[str, Any]:
    """Return an outcome dict; ``campaign`` is the latest persisted record.

    Every model transition is handed to ``persist_transition`` so the guard
    state on disk never trails the mutation order and stale writes are
    refused by the Phase 1 guarded primitive.
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
        campaign = persist_transition(
            lambda record: model.invalidate_reserved_request(
                record, head_oid=head_oid, at=reobserved["server_time"], reason=invalidation
            )
        )
        result.update(
            outcome="invalidated",
            campaign=campaign,
            retain_lock=False,
            detail=invalidation,
        )
        return result

    # 3. Final local authority operation immediately before the mutation.
    final_authority_check()

    # 4. The one external mutation attempt.
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
            persist_transition=persist_transition,
        )

    # 5. Immediate post-mutation head bracket.
    post_snapshot = observer()
    if (
        post_snapshot.get("complete") is not True
        or post_snapshot.get("head_oid") != head_oid
    ):
        campaign = persist_transition(
            lambda record: model.mark_unbracketed_request(
                record,
                head_oid=head_oid,
                post_head_oid=post_snapshot.get("head_oid"),
                request_node_id=created["node_id"],
                request_created_at=created["created_at"],
                request_url=created.get("url") or "",
                at=post_snapshot.get("server_time") or created["created_at"],
            )
        )
        result.update(
            outcome="unbracketed",
            campaign=campaign,
            retain_lock=False,
            terminal=True,
        )
        return result

    campaign = persist_transition(
        lambda record: model.open_request_window(
            record,
            head_oid=head_oid,
            post_head_oid=post_snapshot["head_oid"],
            request_node_id=created["node_id"],
            request_created_at=created["created_at"],
            request_url=created.get("url") or "",
        )
    )
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
    persist_transition: Callable[[Callable[[dict[str, Any]], dict[str, Any]]], dict[str, Any]],
) -> dict[str, Any]:
    try:
        post_snapshot = observer()
    except Exception:  # noqa: BLE001 - cannot establish anything -> fail closed
        campaign = persist_transition(
            lambda record: model.mark_request_ambiguous(
                record,
                head_oid=head_oid,
                at=reserved_at,
                detail=f"creation error and re-observation failed: {mutation_error}",
            )
        )
        result.update(
            outcome="ambiguous", campaign=campaign, retain_lock=True, terminal=True
        )
        return result

    if post_snapshot.get("complete") is not True:
        campaign = persist_transition(
            lambda record: model.mark_request_ambiguous(
                record,
                head_oid=head_oid,
                at=reserved_at,
                detail="creation error and post evidence is incomplete",
            )
        )
        result.update(
            outcome="ambiguous", campaign=campaign, retain_lock=True, terminal=True
        )
        return result

    unexpected = _find_unexpected_request_comment(
        post_snapshot, baseline_comment_ids
    )
    if unexpected is not None:
        # A new viewer comment exists but the mutation reported failure: creation is
        # not provably ours, and equality/earlier timestamps cannot settle it either.
        campaign = persist_transition(
            lambda record: model.mark_request_ambiguous(
                record,
                head_oid=head_oid,
                at=post_snapshot["server_time"],
                detail=(
                    "creation error but an unaccounted viewer request comment exists: "
                    + str(unexpected.get("id"))
                ),
            )
        )
        result.update(
            outcome="ambiguous", campaign=campaign, retain_lock=True, terminal=True
        )
        return result

    # No comment is visible. Only a server-authoritative rejection proves the
    # request was not created and cannot later complete from this attempt; a
    # transport-level failure (timeout, nonzero exit) may still complete.
    if isinstance(mutation_error, github_api.GithubRejectionError):
        campaign = persist_transition(
            lambda record: model.mark_request_creation_failed(
                record,
                head_oid=head_oid,
                at=post_snapshot["server_time"],
                detail=(
                    "request creation definitively failed; GitHub rejected the "
                    f"mutation and no request exists: {mutation_error}"
                ),
            )
        )
        result.update(
            outcome="creation_failed",
            campaign=campaign,
            retain_lock=False,
            terminal=True,
        )
        return result

    campaign = persist_transition(
        lambda record: model.mark_request_ambiguous(
            record,
            head_oid=head_oid,
            at=post_snapshot["server_time"],
            detail=(
                "creation error with no visible request comment; the mutation "
                f"may still complete: {mutation_error}"
            ),
        )
    )
    result.update(
        outcome="ambiguous", campaign=campaign, retain_lock=True, terminal=True
    )
    return result


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


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Execute one committed @codex review request from its RESERVED guard"
    )
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pr", required=True, type=int)
    parser.add_argument("--repository-path", default=".")
    parser.add_argument("--owner-token", required=True)
    args = parser.parse_args()

    result = run_committed_request(
        repository=args.repo,
        pr_number=args.pr,
        owner_token=args.owner_token,
        repository_path=args.repository_path,
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(storage.error_text(error), file=sys.stderr)
        raise SystemExit(1) from error
