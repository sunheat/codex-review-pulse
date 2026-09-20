#!/usr/bin/env python3
"""Pure v2 campaign state transitions, evidence evaluation, and decisions.

No filesystem, Git, network, or wall-clock access lives here. Every function is
deterministic given its inputs so the safety invariants are unit-testable.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import re
import secrets
from typing import Any, Iterable


SCHEMA_VERSION = 3
DEFAULT_CODEX_LOGINS = ("chatgpt-codex-connector",)

MIN_ROUNDS = 1
MAX_ROUNDS_CAP = 10
MIN_INTERVAL_MINUTES = 1

# Authoritative floor for the review-response grace, independent of the
# scheduler cadence. A request may become codex_review_service_unresponsive
# only at or after max(interval_minutes, MIN_REVIEW_RESPONSE_GRACE_MINUTES).
MIN_REVIEW_RESPONSE_GRACE_MINUTES = 20

ACTIVE = "active"
SUCCEEDED = "succeeded"
REVIEW_COMPLETED_WITHOUT_APPROVAL = "review_completed_without_approval"
CODEX_REVIEW_SERVICE_UNRESPONSIVE = "codex_review_service_unresponsive"
ROUNDS_EXHAUSTED = "rounds_exhausted"
REQUEST_CREATION_FAILED = "request_creation_failed"
MANUAL_INTERVENTION_REQUIRED = "manual_intervention_required"
TARGET_UNAVAILABLE = "target_unavailable"
AMBIGUOUS_INTERRUPTION = "ambiguous_interruption"
HARD_FAILED = "hard_failed"

TERMINAL_STATUSES = {
    SUCCEEDED,
    REVIEW_COMPLETED_WITHOUT_APPROVAL,
    CODEX_REVIEW_SERVICE_UNRESPONSIVE,
    ROUNDS_EXHAUSTED,
    REQUEST_CREATION_FAILED,
    MANUAL_INTERVENTION_REQUIRED,
    TARGET_UNAVAILABLE,
    AMBIGUOUS_INTERRUPTION,
    HARD_FAILED,
}

# Positively identified execution-environment failures (environment gate).
# A closed set: generic permission, network, authentication, and timeout
# errors are never classified as these reasons.
HARD_FAILURE_REASONS = frozenset(
    {
        "unsupported_execution_mode",
        "insufficient_effective_access",
        "host_authorization_denied",
    }
)
HARD_FAILURE_DETAIL_LIMIT = 500

# Per-head request guard states.
RESERVED = "reserved"
GUARD_ACTIVE = "active"
INVALIDATED = "invalidated"
SUPERSEDED = "superseded"
CREATION_FAILED = "creation_failed"
UNBRACKETED = "unbracketed"
GUARD_AMBIGUOUS = "ambiguous"
CLOSED = "closed"

APPROVED = "APPROVED"
NON_APPROVAL_REVIEW_STATES = {"COMMENTED", "CHANGES_REQUESTED", "DISMISSED"}
THUMBS_UP = "THUMBS_UP"
EYES = "EYES"

CAMPAIN_ID_RE = re.compile(r"^crp-[0-9]{8}T[0-9]{6}Z-[a-f0-9]{6}$")

GUARD_STATES = {
    RESERVED,
    GUARD_ACTIVE,
    INVALIDATED,
    SUPERSEDED,
    CREATION_FAILED,
    UNBRACKETED,
    GUARD_AMBIGUOUS,
    CLOSED,
}

# Terminal statuses from which a later campaign may automatically roll over.
ROLLOVER_TERMINAL_STATUSES = {
    ROUNDS_EXHAUSTED,
    SUCCEEDED,
    REVIEW_COMPLETED_WITHOUT_APPROVAL,
    CODEX_REVIEW_SERVICE_UNRESPONSIVE,
}


def canonical_repository(repository: str) -> str:
    value = repository.strip()
    if value.count("/") != 1 or any(not part for part in value.split("/")):
        raise ValueError("Repository must be OWNER/REPO")
    return value.casefold()


def normalize_login(login: object) -> str | None:
    """Normalize GitHub bot/non-bot spellings to one stable identity key."""
    if not isinstance(login, str):
        return None
    value = login.strip().casefold()
    if value.endswith("[bot]"):
        value = value[:-5]
    return value or None


def unique_logins(
    configured: Iterable[str] | None,
    *,
    defaults: Iterable[str] = DEFAULT_CODEX_LOGINS,
    label: str,
) -> list[str]:
    values = list(configured) if configured is not None else list(defaults)
    result: list[str] = []
    seen: set[str] = set()
    for login in values:
        key = normalize_login(login)
        if key is None:
            raise ValueError(f"{label} logins must not be empty")
        if key not in seen:
            result.append(key)
            seen.add(key)
    if not result:
        raise ValueError(f"At least one {label} login is required")
    return result


def campaign_source_digest(campaign: dict[str, Any]) -> str:
    """Canonical digest of one durable campaign record (the source witness).

    This is the campaign-source compare-and-swap representation: the digest
    changes whenever the record changes, so every authoritative transition
    that would make a proposal prepared from the previous source stale also
    invalidates the witness observed by later finalization. It is derived
    from the record itself, never synchronized as a second campaign state.
    """
    canonical = json.dumps(
        campaign,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def parse_timestamp(value: object) -> datetime:
    """Parse one persisted timestamp; naive times are always rejected.

    Creation, transitions, and loaded-state validation share this one rule so
    every timestamp used for ordering or elapsed arithmetic is offset-aware.
    """
    if not isinstance(value, str) or not value:
        raise ValueError("Timestamp must be a non-empty ISO 8601 string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Timestamp must be offset-aware")
    return parsed


def strictly_after(event_time: object, floor_time: object) -> bool:
    """True only when the event is strictly later than the floor.

    Equality at the platform's timestamp precision never proves ordering.
    """
    try:
        return parse_timestamp(event_time) > parse_timestamp(floor_time)
    except ValueError:
        return False


def new_campaign_id(created_at: str) -> str:
    stamp = parse_timestamp(created_at).strftime("%Y%m%dT%H%M%SZ")
    return f"crp-{stamp}-{secrets.token_hex(3)}"


def canonical_creation_baseline(reaction_ids: Iterable[str] | None) -> dict[str, Any]:
    """Canonical sorted unique reaction-ID collection for a lifecycle baseline."""
    ids: list[str] = []
    seen: set[str] = set()
    for item in reaction_ids or []:
        if not isinstance(item, str) or not item:
            raise ValueError("Creation baseline reaction ids must be non-empty strings")
        if item not in seen:
            seen.add(item)
            ids.append(item)
    return {"reaction_ids": sorted(ids)}


def creation_baseline_reaction_ids(
    snapshot: dict[str, Any],
    *,
    reviewer_logins: Iterable[str],
    approval_logins: Iterable[str],
) -> list[str]:
    """Applicable pre-existing lifecycle reaction IDs for the creation baseline.

    Collected from one complete owned snapshot captured before the campaign
    record exists. Baseline reactions neither prove review-in-progress for the
    new campaign, nor approve it, nor create lifecycle-attribution waiting, nor
    consume or block its per-head request allowance.
    """
    reviewer_keys = set(reviewer_logins)
    approval_keys = set(approval_logins)
    ids: set[str] = set()
    for reaction in snapshot.get("reactions", []):
        if not isinstance(reaction, dict) or not isinstance(reaction.get("id"), str):
            continue
        content = reaction.get("content")
        login = reaction.get("login")
        if (content == EYES and login in reviewer_keys) or (
            content == THUMBS_UP and login in approval_keys
        ):
            ids.add(reaction["id"])
    return sorted(ids)


def new_campaign(
    *,
    campaign_id: str,
    repository: str,
    pull_request_number: int,
    created_at: str,
    max_rounds: int,
    model: str,
    reasoning_level: str,
    interval_minutes: int,
    reviewer_logins: Iterable[str],
    approval_logins: Iterable[str],
    creation_baseline: Iterable[str] | None = None,
) -> dict[str, Any]:
    if not CAMPAIN_ID_RE.fullmatch(campaign_id):
        raise ValueError("Campaign id must have the form crp-YYYYMMDDTHHMMSSZ-hex")
    parse_timestamp(created_at)
    if (
        not isinstance(max_rounds, int)
        or isinstance(max_rounds, bool)
        or not MIN_ROUNDS <= max_rounds <= MAX_ROUNDS_CAP
    ):
        raise ValueError(f"max_rounds must be an integer between {MIN_ROUNDS} and {MAX_ROUNDS_CAP}")
    if (
        not isinstance(interval_minutes, int)
        or isinstance(interval_minutes, bool)
        or interval_minutes < MIN_INTERVAL_MINUTES
    ):
        raise ValueError(
            f"interval_minutes must be a positive integer >= {MIN_INTERVAL_MINUTES}"
        )
    if not isinstance(model, str) or not model.strip():
        raise ValueError("model must be a non-empty string")
    if not isinstance(reasoning_level, str) or not reasoning_level.strip():
        raise ValueError("reasoning_level must be a non-empty string")
    if not _is_int(pull_request_number) or pull_request_number < 1:
        raise ValueError("pull_request_number must be a positive integer")
    return {
        "schema_version": SCHEMA_VERSION,
        "campaign_id": campaign_id,
        "repository": canonical_repository(repository),
        "pull_request_number": pull_request_number,
        "created_at": created_at,
        "status": ACTIVE,
        "status_detail": None,
        "terminal_at": None,
        "rounds_used": 0,
        "creation_baseline": canonical_creation_baseline(creation_baseline),
        "config": {
            "max_rounds": max_rounds,
            "model": model.strip(),
            "reasoning_level": reasoning_level.strip(),
            "interval_minutes": interval_minutes,
            "reviewer_logins": unique_logins(reviewer_logins, label="reviewer"),
            "approval_logins": unique_logins(approval_logins, label="approval"),
        },
        "guards": [],
    }


def _is_int(value: object) -> bool:
    """True for a real integer; Python booleans are never accepted."""
    return isinstance(value, int) and not isinstance(value, bool)


def _is_nonempty_str(value: object) -> bool:
    return isinstance(value, str) and value.strip() != ""


def _validate_guard(guard: object, seen_heads: set[str]) -> None:
    """Validate one request guard against its actual state.

    This accepts exactly the states and field shapes produced by the real
    transition helpers (reserve, open window, invalidate, supersede, fail,
    unbracket, ambiguous, close) and rejects everything else.
    """
    if not isinstance(guard, dict):
        raise ValueError("Request guard is invalid")
    head = guard.get("head_oid")
    if not _is_nonempty_str(head):
        raise ValueError("Request guard head is invalid")
    if head in seen_heads:
        raise ValueError("More than one request guard exists for one head")
    seen_heads.add(head)
    state = guard.get("state")
    if state not in GUARD_STATES:
        raise ValueError(f"Request guard state is invalid: {state}")
    parse_timestamp(guard.get("reserved_at"))
    superseded_at = guard.get("superseded_at")
    if superseded_at is not None:
        parse_timestamp(superseded_at)
    if state == SUPERSEDED and not isinstance(superseded_at, str):
        raise ValueError("Superseded guard requires a superseded_at timestamp")
    invalidation_reason = guard.get("invalidation_reason")
    if invalidation_reason is not None and not _is_nonempty_str(invalidation_reason):
        raise ValueError("Request guard invalidation reason is invalid")
    if state == INVALIDATED and not _is_nonempty_str(invalidation_reason):
        raise ValueError("Invalidated guard requires an invalidation reason")
    request = guard.get("request")
    if request is not None:
        if not isinstance(request, dict):
            raise ValueError("Request guard request shape is invalid")
        if not _is_nonempty_str(request.get("node_id")):
            raise ValueError("Request node id is invalid")
        parse_timestamp(request.get("created_at"))
        # The helper contract permits an empty URL when GitHub returns none.
        if not isinstance(request.get("url"), str):
            raise ValueError("Request url is invalid")
        post_head_oid = request.get("post_head_oid")
        if post_head_oid is not None and not _is_nonempty_str(post_head_oid):
            raise ValueError("Request post_head_oid is invalid")
    window_opened_at = guard.get("window_opened_at")
    if window_opened_at is not None:
        parse_timestamp(window_opened_at)
    baseline = guard.get("baseline")
    if not isinstance(baseline, dict):
        raise ValueError("Request guard baseline is missing")
    for key in ("reaction_ids", "review_ids", "thread_ids", "comment_ids"):
        ids = baseline.get(key)
        if not isinstance(ids, list) or not all(isinstance(item, str) for item in ids):
            raise ValueError(f"Request guard baseline {key} is invalid")
    # State-specific requirements match what the transition helpers produce.
    if state in (GUARD_ACTIVE, SUPERSEDED, CLOSED):
        if request is None or not isinstance(window_opened_at, str):
            raise ValueError(
                f"Request guard state {state} requires a bound request and window"
            )
    if state == UNBRACKETED:
        if request is None or "post_head_oid" not in request:
            raise ValueError("Unbracketed guard requires its post-head request record")
        if isinstance(window_opened_at, str):
            raise ValueError("Request guard state unbracketed must not claim a window")
    if state in (RESERVED, INVALIDATED, CREATION_FAILED, GUARD_AMBIGUOUS):
        if request is not None or isinstance(window_opened_at, str):
            raise ValueError(
                f"Request guard state {state} must not claim a request or window"
            )
        if superseded_at is not None or (
            invalidation_reason is not None and state != INVALIDATED
        ):
            raise ValueError(
                f"Request guard state {state} must not carry supersession or invalidation data"
            )


def validate_campaign(
    campaign: dict[str, Any], *, repository: str, pull_request_number: int
) -> None:
    """Fail closed on any malformed, foreign, or unsupported campaign record."""
    version = campaign.get("schema_version")
    if not _is_int(version) or version != SCHEMA_VERSION:
        raise ValueError("Unsupported campaign schema version")
    campaign_id = campaign.get("campaign_id")
    if not isinstance(campaign_id, str) or not CAMPAIN_ID_RE.fullmatch(campaign_id):
        raise ValueError("Campaign id is invalid")
    if campaign.get("repository") != canonical_repository(repository):
        raise ValueError("Campaign repository does not match the requested repository")
    number = campaign.get("pull_request_number")
    if not _is_int(number) or number < 1 or number != pull_request_number:
        raise ValueError("Campaign pull request does not match the requested pull request")
    parse_timestamp(campaign.get("created_at"))
    baseline = campaign.get("creation_baseline")
    if not isinstance(baseline, dict) or set(baseline.keys()) != {"reaction_ids"}:
        raise ValueError("Campaign creation_baseline is invalid")
    baseline_ids = baseline.get("reaction_ids")
    if (
        not isinstance(baseline_ids, list)
        or not all(isinstance(item, str) and item for item in baseline_ids)
        or any(baseline_ids[i] >= baseline_ids[i + 1] for i in range(len(baseline_ids) - 1))
    ):
        raise ValueError(
            "Campaign creation_baseline reaction_ids must be sorted unique strings"
        )
    config = campaign.get("config")
    if not isinstance(config, dict):
        raise ValueError("Campaign config is missing")
    max_rounds = config.get("max_rounds")
    if not _is_int(max_rounds) or not MIN_ROUNDS <= max_rounds <= MAX_ROUNDS_CAP:
        raise ValueError("Campaign max_rounds is invalid")
    rounds_used = campaign.get("rounds_used")
    if not _is_int(rounds_used) or rounds_used < 0 or rounds_used > max_rounds:
        raise ValueError("Campaign rounds_used is invalid")
    interval_minutes = config.get("interval_minutes")
    if not _is_int(interval_minutes) or interval_minutes < MIN_INTERVAL_MINUTES:
        raise ValueError("Campaign interval_minutes is invalid")
    for key in ("model", "reasoning_level"):
        if not _is_nonempty_str(config.get(key)):
            raise ValueError(f"Campaign {key} is invalid")
    for key in ("reviewer_logins", "approval_logins"):
        logins = config.get(key)
        if not isinstance(logins, list) or not logins or not all(
            isinstance(item, str) and normalize_login(item) == item for item in logins
        ):
            raise ValueError(f"Campaign {key} is invalid")
    status = campaign.get("status")
    if status not in TERMINAL_STATUSES | {ACTIVE}:
        raise ValueError("Campaign status is invalid")
    detail = campaign.get("status_detail")
    if detail is not None and not isinstance(detail, str):
        raise ValueError("Campaign status_detail is invalid")
    terminal_at = campaign.get("terminal_at")
    if status == ACTIVE:
        if terminal_at is not None or detail is not None:
            raise ValueError("Active campaign must not carry terminal metadata")
    else:
        parse_timestamp(terminal_at)
    guards = campaign.get("guards")
    if not isinstance(guards, list):
        raise ValueError("Campaign guards must be a list")
    seen_heads: set[str] = set()
    for guard in guards:
        _validate_guard(guard, seen_heads)


def is_terminal(campaign: dict[str, Any]) -> bool:
    return campaign.get("status") in TERMINAL_STATUSES


def is_rollover_eligible(campaign: dict[str, Any]) -> bool:
    """True when a durably terminal campaign may be rolled over automatically."""
    return (
        is_terminal(campaign)
        and campaign.get("rounds_used") == campaign.get("config", {}).get("max_rounds")
        and campaign.get("status") in ROLLOVER_TERMINAL_STATUSES
    )


def guard_for_head(campaign: dict[str, Any], head_oid: str) -> dict[str, Any] | None:
    for guard in campaign.get("guards", []):
        if guard.get("head_oid") == head_oid:
            return guard
    return None


def reserved_guard_ambiguity(campaign: dict[str, Any]) -> dict[str, Any] | None:
    """Campaign-wide durable-local ambiguity probe.

    Any request guard still RESERVED proves one request round was consumed,
    the per-head allowance was reserved, and the previous external request
    result was never durably classified; blind continuation is unsafe. Every
    guard is inspected, not only the guard for an observed current head, so
    snapshot incompleteness or head movement cannot hide the ambiguity.
    """
    for guard in campaign.get("guards", []):
        if isinstance(guard, dict) and guard.get("state") == RESERVED:
            return guard
    return None


def _require_active(campaign: dict[str, Any]) -> None:
    if campaign.get("status") != ACTIVE:
        raise RuntimeError(
            f"Campaign is not active (status={campaign.get('status')})"
        )


def consume_round(campaign: dict[str, Any], *, kind: str) -> dict[str, Any]:
    if kind != "remediation":
        raise ValueError("Only remediation rounds are consumed outside request reservation")
    _require_active(campaign)
    config = campaign["config"]
    if campaign["rounds_used"] >= config["max_rounds"]:
        raise RuntimeError("Effective-round budget is exhausted")
    result = deepcopy(campaign)
    result["rounds_used"] += 1
    return result


def reserve_request(
    campaign: dict[str, Any],
    *,
    head_oid: str,
    reserved_at: str,
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    """Durably consume one round AND reserve the per-head allowance.

    This single transition is the campaign's commitment to the request attempt.
    It happens before the external mutation and is never rolled back.
    """
    _require_active(campaign)
    parse_timestamp(reserved_at)
    if not isinstance(head_oid, str) or not head_oid:
        raise ValueError("head_oid is required")
    if campaign["rounds_used"] >= campaign["config"]["max_rounds"]:
        raise RuntimeError("Effective-round budget is exhausted")
    if guard_for_head(campaign, head_oid) is not None:
        raise RuntimeError("This campaign already reserved its request for this head")
    if not snapshot.get("complete"):
        raise ValueError("Request reservation requires complete authoritative evidence")
    if snapshot.get("head_oid") != head_oid:
        raise ValueError("Reservation head does not match the snapshot head")
    result = deepcopy(campaign)
    result["rounds_used"] += 1
    result["guards"].append(
        {
            "head_oid": head_oid,
            "state": RESERVED,
            "reserved_at": reserved_at,
            "request": None,
            "baseline": {
                "reaction_ids": sorted(
                    item["id"]
                    for item in snapshot.get("reactions", [])
                    if isinstance(item, dict) and isinstance(item.get("id"), str)
                ),
                "review_ids": sorted(
                    item["id"]
                    for item in snapshot.get("reviews", [])
                    if isinstance(item, dict) and isinstance(item.get("id"), str)
                ),
                "thread_ids": sorted(
                    item["id"]
                    for item in snapshot.get("threads", [])
                    if isinstance(item, dict) and isinstance(item.get("id"), str)
                ),
                "comment_ids": sorted(
                    item["id"]
                    for item in snapshot.get("comments", [])
                    if isinstance(item, dict) and isinstance(item.get("id"), str)
                ),
            },
            "window_opened_at": None,
            "superseded_at": None,
            "invalidation_reason": None,
        }
    )
    return result


def _mutable_guard(result: dict[str, Any], head_oid: str, expected: str) -> dict[str, Any]:
    guard = guard_for_head(result, head_oid)
    if guard is None:
        raise RuntimeError("No request guard exists for this head")
    if guard.get("state") != expected:
        raise RuntimeError(
            f"Guard for this head is {guard.get('state')}, expected {expected}"
        )
    return guard


def invalidate_reserved_request(
    campaign: dict[str, Any], *, head_oid: str, at: str, reason: str
) -> dict[str, Any]:
    """Final revalidation prevented the POST. Round and guard stay consumed."""
    parse_timestamp(at)
    result = deepcopy(campaign)
    guard = _mutable_guard(result, head_oid, RESERVED)
    guard["state"] = INVALIDATED
    guard["invalidation_reason"] = reason
    return result


def open_request_window(
    campaign: dict[str, Any],
    *,
    head_oid: str,
    post_head_oid: str,
    request_node_id: str,
    request_created_at: str,
    request_url: str,
) -> dict[str, Any]:
    """Bind the created request to the reserved head only when heads bracket."""
    parse_timestamp(request_created_at)
    if post_head_oid != head_oid:
        raise ValueError("Caller must route a bracket mismatch to mark_unbracketed_request")
    result = deepcopy(campaign)
    guard = _mutable_guard(result, head_oid, RESERVED)
    guard["state"] = GUARD_ACTIVE
    guard["request"] = {
        "node_id": request_node_id,
        "created_at": request_created_at,
        "url": request_url,
    }
    guard["window_opened_at"] = request_created_at
    return result


def _apply_terminal_invariant(
    result: dict[str, Any],
    *,
    status: str,
    at: str,
    detail: str | None,
) -> None:
    """The one common terminal-state behavior every terminal transition shares.

    Requires a supported terminal status, stamps the terminal fields, and
    closes every request guard still claiming an active response window.
    Diagnostic guard states (RESERVED retained as evidence, CREATION_FAILED,
    UNBRACKETED, GUARD_AMBIGUOUS, INVALIDATED, SUPERSEDED) are preserved.
    """
    if status not in TERMINAL_STATUSES:
        raise ValueError(f"Unknown terminal status: {status}")
    if detail is not None and not isinstance(detail, str):
        raise ValueError("Terminal status detail must be a string or None")
    parse_timestamp(at)
    result["status"] = status
    result["status_detail"] = detail
    result["terminal_at"] = at
    for guard in result["guards"]:
        if guard.get("state") == GUARD_ACTIVE:
            guard["state"] = CLOSED


def mark_request_creation_failed(
    campaign: dict[str, Any], *, head_oid: str, at: str, detail: str
) -> dict[str, Any]:
    parse_timestamp(at)
    result = deepcopy(campaign)
    guard = _mutable_guard(result, head_oid, RESERVED)
    guard["state"] = CREATION_FAILED
    _apply_terminal_invariant(result, status=REQUEST_CREATION_FAILED, at=at, detail=detail)
    return result


def mark_unbracketed_request(
    campaign: dict[str, Any],
    *,
    head_oid: str,
    post_head_oid: str | None,
    request_node_id: str,
    request_created_at: str,
    request_url: str,
    at: str,
) -> dict[str, Any]:
    parse_timestamp(at)
    parse_timestamp(request_created_at)
    result = deepcopy(campaign)
    guard = _mutable_guard(result, head_oid, RESERVED)
    guard["state"] = UNBRACKETED
    guard["request"] = {
        "node_id": request_node_id,
        "created_at": request_created_at,
        "url": request_url,
        "post_head_oid": post_head_oid,
    }
    _apply_terminal_invariant(
        result,
        status=MANUAL_INTERVENTION_REQUIRED,
        at=at,
        detail="review_request_head_bracket_failed",
    )
    return result


def mark_request_ambiguous(
    campaign: dict[str, Any], *, head_oid: str, at: str, detail: str
) -> dict[str, Any]:
    parse_timestamp(at)
    result = deepcopy(campaign)
    guard = _mutable_guard(result, head_oid, RESERVED)
    guard["state"] = GUARD_AMBIGUOUS
    _apply_terminal_invariant(result, status=AMBIGUOUS_INTERRUPTION, at=at, detail=detail)
    return result


def supersede_active_guards(
    campaign: dict[str, Any], *, current_head_oid: str, at: str
) -> dict[str, Any]:
    """A response window belongs to one head. A head change supersedes it."""
    parse_timestamp(at)
    result = deepcopy(campaign)
    changed = False
    for guard in result.get("guards", []):
        if guard.get("state") == GUARD_ACTIVE and guard.get("head_oid") != current_head_oid:
            guard["state"] = SUPERSEDED
            guard["superseded_at"] = at
            changed = True
    return result if changed else campaign


def terminate(
    campaign: dict[str, Any],
    *,
    status: str,
    at: str,
    detail: str | None = None,
) -> dict[str, Any]:
    """Generic terminalization through the common terminal-state invariant."""
    if campaign.get("status") != ACTIVE:
        raise RuntimeError(
            f"Campaign already terminal as {campaign.get('status')}; refusing {status}"
        )
    result = deepcopy(campaign)
    _apply_terminal_invariant(result, status=status, at=at, detail=detail)
    return result


def hard_fail(
    campaign: dict[str, Any],
    *,
    at: str,
    reason: str,
    detail: str,
) -> dict[str, Any]:
    """Terminal failure for a positively identified execution-environment rejection.

    One guarded transition forfeits the entire remaining round budget
    (``rounds_used = max_rounds``) and terminalizes the campaign as
    ``hard_failed`` with the reason code plus concise host diagnostic. A
    campaign carrying an unclassified RESERVED request attempt is refused:
    that ambiguity keeps its existing fail-closed handling and is never
    masked by this transition.
    """
    _require_active(campaign)
    if reason not in HARD_FAILURE_REASONS:
        raise ValueError(f"Unknown hard-failure reason: {reason!r}")
    if not isinstance(detail, str) or not detail.strip():
        raise ValueError("Hard-failure detail must be a non-empty string")
    reserved = reserved_guard_ambiguity(campaign)
    if reserved is not None:
        raise RuntimeError(
            "Campaign carries an unclassified RESERVED request attempt; "
            "hard-failure terminalization is refused"
        )
    parse_timestamp(at)
    result = deepcopy(campaign)
    result["rounds_used"] = result["config"]["max_rounds"]
    concise = " ".join(detail.split())[:HARD_FAILURE_DETAIL_LIMIT]
    _apply_terminal_invariant(
        result, status=HARD_FAILED, at=at, detail=f"{reason}: {concise}"
    )
    return result


# ---------------------------------------------------------------------------
# Evidence evaluation


def applicable_unresolved_threads(
    snapshot: dict[str, Any], reviewer_logins: Iterable[str]
) -> list[dict[str, Any]]:
    """Applicable unresolved threads with their frozen identity fields.

    Emitted fields are the Python-owned frozen evidence the Phase 3 mutation
    boundaries revalidate before every externalization: exact thread path,
    root review-comment ID, normalized root-author identity, exact root body,
    and root ``updatedAt``. Boundaries derive these fields from the snapshot
    themselves; the model never supplies or retypes them.
    """
    keys = set(reviewer_logins)
    threads: list[dict[str, Any]] = []
    for thread in snapshot.get("threads", []):
        if not isinstance(thread, dict) or thread.get("is_resolved") is True:
            continue
        if thread.get("root_login") in keys and isinstance(thread.get("id"), str):
            threads.append(
                {
                    "id": thread["id"],
                    "path": thread.get("path"),
                    "url": thread.get("url"),
                    "body": thread.get("body"),
                    "root_comment_id": thread.get("root_comment_id"),
                    "root_author": thread.get("root_login"),
                    "root_updated_at": thread.get("root_updated_at"),
                }
            )
    return threads


def project_remediation_batch(
    snapshot: dict[str, Any], directive_threads: list[dict[str, Any]]
) -> dict[str, Any]:
    """Project the frozen S1 observation to exactly the committed batch.

    The returned snapshot preserves every top-level evidence field but its
    ``threads`` array contains exactly the raw frozen records of the selected
    directive targets, in directive order. This projection is the single
    batch-membership representation: the committed directive enumeration and
    the persisted snapshot must correspond one-for-one, so a worker can never
    enlarge its batch by scanning the snapshot. A missing or duplicate raw
    mapping for any selected ID is an internal invariant failure and fails
    closed before the remediation round is consumed.
    """
    if not isinstance(directive_threads, list) or not directive_threads:
        raise ValueError("remediation directive selects no target")
    raw = snapshot.get("threads")
    if not isinstance(raw, list):
        raise ValueError("observation has no thread evidence array")
    selected_ids: list[str] = []
    for record in directive_threads:
        if not isinstance(record, dict) or not isinstance(record.get("id"), str):
            raise ValueError("remediation directive contains a malformed target")
        selected_ids.append(record["id"])
    if len(set(selected_ids)) != len(selected_ids):
        raise ValueError("remediation directive selects a duplicate target ID")
    by_id: dict[str, list[dict[str, Any]]] = {}
    for record in raw:
        if isinstance(record, dict) and isinstance(record.get("id"), str):
            by_id.setdefault(record["id"], []).append(record)
    projected: list[dict[str, Any]] = []
    for thread_id in selected_ids:
        matches = by_id.get(thread_id)
        if not matches:
            raise ValueError(
                f"selected target has no raw frozen record: {thread_id}"
            )
        if len(matches) > 1:
            raise ValueError(
                f"selected target matches multiple raw frozen records: {thread_id}"
            )
        projected.append(matches[0])
    result = dict(snapshot)
    result["threads"] = projected
    return result


def _temporal_floor(guard: dict[str, Any] | None) -> str | None:
    """Authoritative timestamp an event must strictly follow to be eligible.

    Without a per-head request guard there is no authoritative temporal floor:
    GitHub removed the last commit-level push timestamp, so a PR-level reaction
    alone can never prove current-head currency.
    """
    if guard is None:
        return None
    state = guard.get("state")
    if state == GUARD_ACTIVE:
        return guard.get("window_opened_at") or guard.get("reserved_at")
    if state == RESERVED:
        return guard.get("reserved_at")
    if state == INVALIDATED:
        return guard.get("reserved_at")
    if state == SUPERSEDED:
        return guard.get("superseded_at") or guard.get("window_opened_at")
    return None


def _in_baseline(
    campaign: dict[str, Any], guard: dict[str, Any] | None, artifact_id: object
) -> bool:
    """True when the artifact predates the campaign or the guard's own baseline.

    The campaign-level creation baseline excludes pre-existing lifecycle
    reactions; the per-guard baseline excludes request-time evidence.
    """
    if not isinstance(artifact_id, str):
        return False
    baseline = campaign.get("creation_baseline") or {}
    if artifact_id in (baseline.get("reaction_ids") or []):
        return True
    if guard is None:
        return False
    for ids in (guard.get("baseline") or {}).values():
        if artifact_id in (ids or []):
            return True
    return False


def _eligible(
    artifact: dict[str, Any],
    *,
    created_field: str,
    campaign: dict[str, Any],
    guard: dict[str, Any] | None,
    floor: str | None,
) -> bool:
    """Strict temporal eligibility with baseline exclusion."""
    artifact_id = artifact.get("id")
    if not isinstance(artifact_id, str):
        return False
    if _in_baseline(campaign, guard, artifact_id):
        return False
    created_at = artifact.get(created_field)
    if floor is None:
        return False
    return strictly_after(created_at, floor)


def evaluate(
    campaign: dict[str, Any], snapshot: dict[str, Any]
) -> dict[str, Any]:
    """Normalize current-state evidence for the current head."""
    config = campaign["config"]
    reviewer_keys = set(config["reviewer_logins"])
    approval_keys = set(config["approval_logins"])
    actor_keys = reviewer_keys | approval_keys
    head_oid = snapshot.get("head_oid")
    guard = guard_for_head(campaign, head_oid) if isinstance(head_oid, str) else None
    floor = _temporal_floor(guard)

    threads = applicable_unresolved_threads(snapshot, config["reviewer_logins"])

    # Independently authoritative approval: a commit-bound APPROVED review.
    approval: dict[str, Any] | None = None
    for review in snapshot.get("reviews", []):
        if (
            isinstance(review, dict)
            and review.get("state") == APPROVED
            and review.get("login") in approval_keys
            and review.get("commit_oid") == head_oid
        ):
            approval = {"kind": "review", "id": review.get("id")}
            break

    eligible_eyes: list[dict[str, Any]] = []
    if approval is None:
        for reaction in snapshot.get("reactions", []):
            if (
                isinstance(reaction, dict)
                and reaction.get("content") == EYES
                and reaction.get("login") in reviewer_keys
                and _eligible(
                    reaction,
                    created_field="created_at",
                    campaign=campaign,
                    guard=guard,
                    floor=floor,
                )
            ):
                eligible_eyes.append(
                    {"id": reaction["id"], "created_at": reaction["created_at"]}
                )

    # Reaction approval needs the same bounded temporal eligibility.
    if approval is None:
        for reaction in snapshot.get("reactions", []):
            if (
                isinstance(reaction, dict)
                and reaction.get("content") == THUMBS_UP
                and reaction.get("login") in approval_keys
                and _eligible(
                    reaction,
                    created_field="created_at",
                    campaign=campaign,
                    guard=guard,
                    floor=floor,
                )
            ):
                approval = {
                    "kind": "reaction",
                    "id": reaction["id"],
                    "created_at": reaction["created_at"],
                }
                break

    completed_reviews: list[dict[str, Any]] = []
    ambiguous_artifacts: list[dict[str, Any]] = []
    for review in snapshot.get("reviews", []):
        if not isinstance(review, dict) or review.get("login") not in actor_keys:
            continue
        if review.get("commit_oid") != head_oid:
            continue
        if review.get("state") not in NON_APPROVAL_REVIEW_STATES:
            continue
        if _in_baseline(campaign, guard, review.get("id")):
            continue
        submitted_at = review.get("submitted_at")
        if floor is None:
            continue
        if strictly_after(submitted_at, floor):
            completed_reviews.append(
                {"id": review.get("id"), "submitted_at": submitted_at}
            )
        elif not strictly_after(submitted_at, floor) and guard is not None:
            # A current-head review missing from the complete baseline yet not
            # strictly after the floor cannot be temporally placed.
            ambiguous_artifacts.append(
                {"kind": "review", "id": review.get("id"), "submitted_at": submitted_at}
            )

    # Codex conversation activity (never the viewer's own request comment) can
    # be a completion signal and always blocks a "no response" conclusion.
    codex_comments: list[dict[str, Any]] = []
    for comment in snapshot.get("comments", []):
        if not isinstance(comment, dict) or comment.get("login") not in actor_keys:
            continue
        if _in_baseline(campaign, guard, comment.get("id")):
            continue
        if floor is not None and strictly_after(comment.get("created_at"), floor):
            codex_comments.append(
                {"id": comment.get("id"), "created_at": comment.get("created_at")}
            )

    for reaction in snapshot.get("reactions", []):
        if (
            isinstance(reaction, dict)
            and reaction.get("login") in actor_keys
            and reaction.get("content") in (THUMBS_UP, EYES)
            and not _in_baseline(campaign, guard, reaction.get("id"))
            and floor is not None
            and not strictly_after(reaction.get("created_at"), floor)
            and not (
                reaction.get("content") == EYES
                and any(item["id"] == reaction.get("id") for item in eligible_eyes)
            )
            and approval is None
        ):
            ambiguous_artifacts.append(
                {
                    "kind": "reaction",
                    "id": reaction.get("id"),
                    "content": reaction.get("content"),
                    "created_at": reaction.get("created_at"),
                }
            )

    # Without a guard there is no authoritative temporal floor, so an applicable
    # Codex reaction exists but current-head attribution cannot be proven. It
    # may only cause a non-counting wait; it never proves progress or approval.
    unattributable_reactions: list[dict[str, Any]] = []
    if guard is None:
        for reaction in snapshot.get("reactions", []):
            if not isinstance(reaction, dict) or not isinstance(reaction.get("id"), str):
                continue
            if _in_baseline(campaign, None, reaction.get("id")):
                continue
            content = reaction.get("content")
            if (content == EYES and reaction.get("login") in reviewer_keys) or (
                content == THUMBS_UP and reaction.get("login") in approval_keys
            ):
                unattributable_reactions.append(
                    {
                        "id": reaction["id"],
                        "content": content,
                        "created_at": reaction.get("created_at"),
                    }
                )

    return {
        "guard_state": guard.get("state") if guard else None,
        "temporal_floor": floor,
        "threads": threads,
        "approval": approval,
        "eyes": eligible_eyes,
        "completed_reviews": completed_reviews,
        "codex_comments": codex_comments,
        "ambiguous_artifacts": ambiguous_artifacts,
        "unattributable_reactions": unattributable_reactions,
    }


def effective_response_grace_minutes(campaign: dict[str, Any]) -> int:
    """Authoritative response grace, independent of the scheduler cadence."""
    return max(
        campaign["config"]["interval_minutes"],
        MIN_REVIEW_RESPONSE_GRACE_MINUTES,
    )


def _response_grace_elapsed(
    campaign: dict[str, Any], guard: dict[str, Any], snapshot: dict[str, Any]
) -> bool:
    opened_at = guard.get("window_opened_at")
    if not isinstance(opened_at, str):
        return False
    elapsed_seconds = (
        parse_timestamp(snapshot["server_time"]) - parse_timestamp(opened_at)
    ).total_seconds()
    return elapsed_seconds >= effective_response_grace_minutes(campaign) * 60


def decide(campaign: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    """Map campaign state plus one complete observation to one directive.

    Directives that mutate GitHub/Git are effective actions; everything else is
    a non-counting observation or a terminal outcome. Terminal directives carry
    a deterministic ``basis``: ``durable_local`` when current durable campaign
    state alone proves terminality, ``observation`` when the conclusion also
    depends on current GitHub state. This metadata is in-memory decision
    output, never persisted workflow state.
    """
    status = campaign.get("status")
    if status in TERMINAL_STATUSES:
        return {"action": "campaign_terminal", "status": status}

    # Durable-local ambiguity: campaign state alone proves terminality, before
    # any observation. A RESERVED guard anywhere - on any head - blocks blind
    # continuation, so snapshot incompleteness cannot hide it either.
    reserved = reserved_guard_ambiguity(campaign)
    if reserved is not None:
        return {
            "action": "terminal",
            "status": AMBIGUOUS_INTERRUPTION,
            "detail": "reserved request attempt has no creation result",
            "guard_head": reserved.get("head_oid"),
            "basis": "durable_local",
        }

    if not snapshot.get("complete"):
        return {
            "action": "wait_observation_incomplete",
            "reason": "authoritative evidence is incomplete; unknown is not absence",
        }

    pr_state = snapshot.get("pr_state")
    if pr_state != "OPEN":
        return {
            "action": "terminal",
            "status": TARGET_UNAVAILABLE,
            "detail": f"pull request is {pr_state}",
            "basis": "observation",
        }

    head_oid = snapshot.get("head_oid")
    guard = guard_for_head(campaign, head_oid) if isinstance(head_oid, str) else None
    evidence = evaluate(campaign, snapshot)
    budget_remaining = campaign["rounds_used"] < campaign["config"]["max_rounds"]

    # Unresolved applicable feedback always wins over reaction/approval signals.
    if evidence["threads"]:
        if budget_remaining:
            return {
                "action": "remediation_batch",
                "threads": evidence["threads"],
            }
        return {
            "action": "terminal",
            "status": ROUNDS_EXHAUSTED,
            "detail": "applicable unresolved feedback remains with no round budget",
            "basis": "observation",
        }

    if evidence["approval"] is not None:
        return {
            "action": "terminal",
            "status": SUCCEEDED,
            "proof": evidence["approval"],
            "basis": "observation",
        }

    if evidence["eyes"]:
        return {
            "action": "wait_review_in_progress",
            "reaction_ids": [item["id"] for item in evidence["eyes"]],
        }

    # An applicable Codex reaction without a guard cannot be proven current-head.
    # With round budget remaining this is a non-counting fail-closed wait: no
    # round, no guard, no request. Once the budget is exhausted with no request
    # guard, waiting would repeat forever, so exhaustion takes precedence; the
    # unattributable reaction still proves neither approval nor review progress.
    if evidence["unattributable_reactions"] and budget_remaining:
        return {
            "action": "wait_lifecycle_attribution_unknown",
            "reaction_ids": [item["id"] for item in evidence["unattributable_reactions"]],
            "reason": (
                "applicable Codex reaction exists but current-head attribution "
                "cannot be proven; no review progress or approval is claimed"
            ),
        }

    if guard is None:
        if budget_remaining:
            return {"action": "request_review"}
        return {
            "action": "terminal",
            "status": ROUNDS_EXHAUSTED,
            "detail": "no effective action remains",
            "basis": "observation",
        }

    guard_state = guard.get("state")

    if guard_state == GUARD_ACTIVE:
        if evidence["completed_reviews"]:
            return {
                "action": "terminal",
                "status": REVIEW_COMPLETED_WITHOUT_APPROVAL,
                "review_id": evidence["completed_reviews"][0]["id"],
                "basis": "observation",
            }
        if not _response_grace_elapsed(campaign, guard, snapshot):
            return {
                "action": "wait_request_outstanding",
                "guard_head": head_oid,
            }
        # The response grace has elapsed with complete evidence.
        if evidence["codex_comments"]:
            return {
                "action": "terminal",
                "status": REVIEW_COMPLETED_WITHOUT_APPROVAL,
                "comment_id": evidence["codex_comments"][0]["id"],
                "basis": "observation",
            }
        if evidence["ambiguous_artifacts"]:
            return {
                "action": "terminal",
                "status": MANUAL_INTERVENTION_REQUIRED,
                "detail": "temporal eligibility cannot be established for possible Codex activity",
                "artifacts": evidence["ambiguous_artifacts"],
                "basis": "observation",
            }
        return {
            "action": "terminal",
            "status": CODEX_REVIEW_SERVICE_UNRESPONSIVE,
            "detail": (
                "response grace elapsed with complete evidence and no "
                "attributable Codex response"
            ),
            "basis": "observation",
        }

    if guard_state in (CREATION_FAILED, UNBRACKETED, GUARD_AMBIGUOUS, CLOSED):
        return {
            "action": "terminal",
            "status": MANUAL_INTERVENTION_REQUIRED,
            "detail": f"request guard already reached state {guard_state}",
            "basis": "observation",
        }

    # INVALIDATED on the same head, or SUPERSEDED on a head that returned: the
    # per-head allowance is permanently gone and the window must not reactivate.
    # Terminality still requires the observed current head, so the proof stays
    # observation-derived rather than classified from the guard state name.
    if guard_state in (INVALIDATED, SUPERSEDED):
        return {
            "action": "terminal",
            "status": MANUAL_INTERVENTION_REQUIRED,
            "detail": f"request guard state {guard_state} leaves no automatic action for this head",
            "basis": "observation",
        }

    return {
        "action": "terminal",
        "status": MANUAL_INTERVENTION_REQUIRED,
        "detail": f"unhandled guard state: {guard_state}",
        "basis": "observation",
    }


def library_main() -> None:
    parser = argparse.ArgumentParser(
        description="Pure v2 campaign model, evidence evaluation, and decisions"
    )
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args()
    if args.selftest:
        print("campaign_model ok")


if __name__ == "__main__":
    library_main()
