#!/usr/bin/env python3
"""Pure next-action evaluation for one bounded recurring wake."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from enum import Enum
import hashlib
import json
from typing import Any

from recurring_contract import (
    RUN_STATE_AUTHORITY_SCHEMA_VERSION,
    contract_authority_digest,
    validate_contract_authority_binding,
)

RUN_STATE_SCHEMA_VERSION = RUN_STATE_AUTHORITY_SCHEMA_VERSION


class NextAction(str, Enum):
    RUN_BATCH = "RUN_BATCH"
    WAIT_REVIEW = "WAIT_REVIEW"
    REQUEST_REVIEW = "REQUEST_REVIEW"
    STOP_TERMINAL = "STOP_TERMINAL"
    STOP_CLOSED = "STOP_CLOSED"
    PAUSE_RECOVERY = "PAUSE_RECOVERY"
    PAUSE_CONCURRENT = "PAUSE_CONCURRENT"
    PAUSE_BLOCKED = "PAUSE_BLOCKED"
    PAUSE_EXPIRED = "PAUSE_EXPIRED"


DISPOSITION = {
    NextAction.RUN_BATCH: "continue",
    NextAction.WAIT_REVIEW: "continue",
    NextAction.REQUEST_REVIEW: "continue",
    NextAction.STOP_TERMINAL: "complete",
    NextAction.STOP_CLOSED: "complete",
    NextAction.PAUSE_RECOVERY: "pause",
    NextAction.PAUSE_CONCURRENT: "pause",
    NextAction.PAUSE_BLOCKED: "pause",
    NextAction.PAUSE_EXPIRED: "pause",
}


def _utc(value: str | datetime) -> datetime:
    parsed = (
        value
        if isinstance(value, datetime)
        else datetime.fromisoformat(value.replace("Z", "+00:00"))
    )
    if parsed.tzinfo is None:
        raise ValueError("Time inputs must include a timezone")
    return parsed.astimezone(UTC)


def empty_run_state(contract: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": RUN_STATE_SCHEMA_VERSION,
        "repository": contract["repository"],
        "pull_request_number": contract["pull_request_number"],
        "authorization_id": contract["authorization_id"],
        "contract_authority_digest": contract_authority_digest(contract),
        "wake_count": 0,
        "last_head_oid": None,
        "stable_observation": None,
        "trigger_events": {},
        "failure_latch": None,
        "inflight_action": None,
        "last_result": None,
        # These fields are used only when the optional hardened controller is
        # invoked with an explicit host wake identity.  The Codex-first path
        # keeps the same lifecycle in pulse.py without importing this module.
        "active_wake_id": None,
        "wake_phase": "idle",
        "wake_started_at": None,
        "wake_completed_at": None,
        "next_not_before": None,
        "scheduled_task_disposition": "PAUSED",
        "last_wake_id": None,
    }


def validate_run_state(
    state: dict[str, Any], contract: dict[str, Any]
) -> dict[str, Any]:
    if state.get("schema_version") != RUN_STATE_SCHEMA_VERSION:
        raise ValueError("Unsupported recurring run-state schema version")
    validate_contract_authority_binding(state, contract)
    if state.get("repository") != contract["repository"]:
        raise ValueError("Run-state repository does not match run contract")
    if state.get("pull_request_number") != contract["pull_request_number"]:
        raise ValueError("Run-state pull request does not match run contract")
    if state.get("authorization_id") != contract["authorization_id"]:
        raise ValueError("Run-state authorization does not match run contract")
    wake_count = state.get("wake_count")
    if not isinstance(wake_count, int) or isinstance(wake_count, bool) or wake_count < 0:
        raise ValueError("Run-state wake count is invalid")
    if not isinstance(state.get("trigger_events", {}), dict):
        raise ValueError("Run-state trigger events are invalid")
    if state.get("scheduled_task_disposition", "PAUSED") not in {"PAUSED", "ACTIVE"}:
        raise ValueError("Run-state scheduled task disposition is invalid")
    return state


def observation_fingerprint(observation: dict[str, Any]) -> str:
    """Hash only server evidence that should reset the stable-wait counter."""
    relevant = {
        "head_oid": observation.get("head_oid"),
        "pull_request_state": observation.get("pull_request_state"),
        "targeted_thread_ids": sorted(observation.get("targeted_thread_ids") or []),
        "non_target_thread_ids": sorted(observation.get("non_target_thread_ids") or []),
        "approval_status": observation.get("approval_status"),
        "approval_evidence_ids": sorted(observation.get("approval_evidence_ids") or []),
        "codex_review_in_progress": observation.get("codex_review_in_progress", False),
        "review_in_progress_reaction_ids": sorted(
            observation.get("review_in_progress_reaction_ids") or []
        ),
        "relevant_event_ids": sorted(
            event.get("id")
            for event in observation.get("relevant_codex_events") or []
            if isinstance(event, dict) and isinstance(event.get("id"), str)
        ),
    }
    encoded = json.dumps(relevant, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def advance_observation_state(
    state: dict[str, Any], observation: dict[str, Any]
) -> dict[str, Any]:
    result = deepcopy(state)
    fingerprint = observation_fingerprint(observation)
    previous = result.get("stable_observation") or {}
    same = (
        previous.get("head_oid") == observation.get("head_oid")
        and previous.get("fingerprint") == fingerprint
    )
    result["stable_observation"] = {
        "head_oid": observation.get("head_oid"),
        "fingerprint": fingerprint,
        "count": int(previous.get("count", 0)) + 1 if same else 1,
        "first_server_observed_at": (
            previous.get("first_server_observed_at")
            if same
            else observation.get("server_time")
        ),
        "server_observed_at": observation.get("server_time"),
    }
    return result


def record_trigger_result(
    state: dict[str, Any],
    *,
    attempted_head_oid: str,
    head_before: str,
    head_after: str,
    comment_node_id: str,
    created_at: str,
) -> dict[str, Any]:
    """Persist one trigger attempt so restarts cannot repeat it for that head."""
    if not all(isinstance(value, str) and value for value in (
        attempted_head_oid, head_before, head_after, comment_node_id, created_at
    )):
        raise ValueError("Complete trigger evidence is required")
    _utc(created_at)
    result = deepcopy(state)
    events = result.setdefault("trigger_events", {})
    if attempted_head_oid in events:
        raise ValueError("A review trigger is already recorded for this head epoch")
    status = (
        "emitted"
        if head_before == attempted_head_oid == head_after
        else "head_changed_during_trigger"
    )
    events[attempted_head_oid] = {
        "status": status,
        "head_oid": attempted_head_oid,
        "head_before": head_before,
        "head_after": head_after,
        "comment_node_id": comment_node_id,
        "created_at": created_at,
    }
    if status != "emitted":
        result["failure_latch"] = {
            "reason_code": "trigger_head_changed",
            "head_oid": attempted_head_oid,
            "evidence_id": comment_node_id,
            "latched_at": created_at,
        }
    return result


def latch_failure(
    state: dict[str, Any], *, reason_code: str, observed_at: str, details: Any = None
) -> dict[str, Any]:
    result = deepcopy(state)
    result["failure_latch"] = {
        "reason_code": reason_code,
        "latched_at": observed_at,
        "details": details,
    }
    return result


def clear_failure_latch(
    state: dict[str, Any],
    *,
    recovery_authorization_id: str,
    authorization_source: str | None = None,
    authorization_verified: bool = False,
) -> dict[str, Any]:
    """Clear a latch only with verifiable, external recovery authority.

    A scheduled agent-provided string is evidence, not authorization.  The
    optional hardened controller may clear a latch only when a new user turn
    or an isolated external authority has verified the authorization.
    """
    if (
        not recovery_authorization_id.strip()
        or authorization_source not in {"user_interaction", "external_authority"}
        or authorization_verified is not True
    ):
        raise ValueError("Verified user or external recovery authorization is required")
    result = deepcopy(state)
    result["failure_latch"] = None
    result["last_recovery_authorization_id"] = recovery_authorization_id
    return result


def classify_stalled_review(
    *, contract: dict[str, Any], observation: dict[str, Any], state: dict[str, Any]
) -> dict[str, Any]:
    if not observation.get("api_ok", False) or not observation.get("auth_ok", False):
        return {"stalled": False, "reason_code": "github_health_unavailable"}
    server_time = observation.get("server_time")
    if not isinstance(server_time, str):
        return {"stalled": False, "reason_code": "github_server_time_missing"}
    now = _utc(server_time)
    head_oid = observation.get("head_oid")
    stable = state.get("stable_observation") or {}
    minimum_observations = contract["wait_policy"]["minimum_stable_observations"]
    if stable.get("head_oid") != head_oid or stable.get("count", 0) < minimum_observations:
        return {"stalled": False, "reason_code": "stable_observation_budget_pending"}

    boundaries: list[tuple[datetime, str]] = []
    publication = observation.get("batch_publication_event")
    if isinstance(publication, dict) and publication.get("head_oid") == head_oid:
        boundaries.append((_utc(publication["created_at"]), "batch_publication"))
    trigger = state.get("trigger_events", {}).get(head_oid)
    if isinstance(trigger, dict) and trigger.get("status") == "emitted":
        boundaries.append((_utc(trigger["created_at"]), "authorized_trigger"))
    # Publication and trigger timestamps are authoritative server boundaries.
    # The first stable idle observation is only a cold-start fallback; letting
    # a later observation replace a known mutation boundary would restart the
    # wait budget every time observation resumes.
    if not boundaries:
        first_idle_observation = stable.get("first_server_observed_at")
        if isinstance(first_idle_observation, str):
            boundaries.append((_utc(first_idle_observation), "idle_observation"))
    if not boundaries:
        return {"stalled": False, "reason_code": "wait_boundary_missing"}
    boundary_time, boundary_kind = max(boundaries)

    relevant = []
    for event in observation.get("relevant_codex_events") or []:
        if not isinstance(event, dict) or event.get("head_oid") != head_oid:
            continue
        created_at = event.get("created_at")
        if isinstance(created_at, str):
            relevant.append(_utc(created_at))
    if any(event_time > boundary_time for event_time in relevant):
        return {"stalled": False, "reason_code": "relevant_codex_event_observed"}
    elapsed = (now - boundary_time).total_seconds()
    if elapsed < contract["wait_policy"]["minimum_server_wait_seconds"]:
        return {"stalled": False, "reason_code": "server_wait_budget_pending"}
    return {
        "stalled": True,
        "reason_code": "deterministic_wait_policy_satisfied",
        "boundary_kind": boundary_kind,
        "boundary_at": boundary_time.isoformat(),
        "server_time": now.isoformat(),
    }


def _decision(action: NextAction, reason_code: str, **details: Any) -> dict[str, Any]:
    return {
        "next_action": action.value,
        "reason_code": reason_code,
        "recommended_heartbeat_disposition": DISPOSITION[action],
        **details,
    }


def evaluate_recurring_action(
    *,
    contract: dict[str, Any],
    observation: dict[str, Any],
    state: dict[str, Any],
    now: str | datetime,
) -> dict[str, Any]:
    """Return one stable action without performing I/O or mutation."""
    current_time = _utc(now)
    if state.get("failure_latch"):
        return _decision(
            NextAction.PAUSE_RECOVERY,
            "failure_latched",
            failure_latch=state["failure_latch"],
        )
    if observation.get("lease_status") == "lost":
        return _decision(NextAction.PAUSE_CONCURRENT, "lease_lost")
    if not observation.get("auth_ok", False):
        return _decision(NextAction.PAUSE_BLOCKED, "github_authentication_failed", latch=True)
    if not observation.get("api_ok", False):
        return _decision(NextAction.PAUSE_BLOCKED, "github_api_failed", latch=True)
    if observation.get("mixed_head") or not observation.get("snapshot_stable", False):
        return _decision(NextAction.PAUSE_BLOCKED, "mixed_head_snapshot", latch=True)
    if not observation.get("run_contract_ok", True):
        return _decision(NextAction.PAUSE_BLOCKED, "run_contract_mismatch", latch=True)
    if not observation.get("install_ok", True):
        return _decision(NextAction.PAUSE_BLOCKED, "install_provenance_drift", latch=True)
    if not observation.get("local_checkout_ok", True):
        return _decision(NextAction.PAUSE_BLOCKED, "local_checkout_drift", latch=True)
    if not observation.get("review_activity_ok", True):
        return _decision(
            NextAction.PAUSE_BLOCKED,
            "review_activity_evidence_invalid",
            latch=True,
        )
    recovery = observation.get("recovery_status", "none")
    if recovery != "none":
        return _decision(
            NextAction.PAUSE_RECOVERY,
            "active_batch_recovery_required",
            recovery_status=recovery,
            latch=True,
        )
    if observation.get("external_head_advance", False):
        return _decision(NextAction.PAUSE_RECOVERY, "unexpected_remote_head_advance", latch=True)

    pr_state = observation.get("pull_request_state")
    if pr_state in {"CLOSED", "MERGED"}:
        return _decision(NextAction.STOP_CLOSED, "pull_request_closed_or_merged")
    targeted = observation.get("targeted_thread_ids") or []
    if (
        observation.get("approval_status") == "approved_current_head"
        and not targeted
        and not observation.get("codex_review_in_progress", False)
    ):
        return _decision(NextAction.STOP_TERMINAL, "current_head_approval_proven")

    expires_at = contract.get("expires_at")
    if expires_at is not None and current_time >= _utc(expires_at):
        return _decision(NextAction.PAUSE_EXPIRED, "run_deadline_expired")
    if state.get("wake_count", 0) > contract["maximum_wakes"]:
        return _decision(NextAction.PAUSE_EXPIRED, "wake_budget_exhausted")

    if observation.get("codex_review_in_progress", False):
        return _decision(
            NextAction.WAIT_REVIEW,
            "codex_review_in_progress",
            review_in_progress_reaction_ids=sorted(
                observation.get("review_in_progress_reaction_ids") or []
            ),
        )

    if targeted:
        required = ("code_edits", "resolve_threads", "commit", "push")
        missing = [key for key in required if not contract["mutation_scope"][key]]
        if missing:
            return _decision(
                NextAction.PAUSE_BLOCKED,
                "batch_mutation_not_authorized",
                missing_authorizations=missing,
            )
        return _decision(NextAction.RUN_BATCH, "targeted_work_available")

    head_oid = observation.get("head_oid")
    stalled = classify_stalled_review(
        contract=contract, observation=observation, state=state
    )
    if stalled["stalled"]:
        trigger = state.get("trigger_events", {}).get(head_oid)
        if isinstance(trigger, dict) and trigger.get("status") == "emitted":
            return _decision(
                NextAction.PAUSE_BLOCKED,
                "review_trigger_did_not_start",
                trigger_state=trigger,
                stalled_review=stalled,
            )
        restricted_head = contract.get("review_trigger_head_oid")
        if (
            contract["mutation_scope"]["review_trigger"]
            and (restricted_head is None or restricted_head == head_oid)
        ):
            return _decision(
                NextAction.REQUEST_REVIEW,
                "authorized_single_trigger_available",
                stalled_review=stalled,
            )
        return _decision(
            NextAction.PAUSE_BLOCKED,
            (
                "review_trigger_head_not_authorized"
                if contract["mutation_scope"]["review_trigger"]
                and restricted_head != head_oid
                else "review_trigger_not_authorized"
            ),
            stalled_review=stalled,
        )

    non_target = observation.get("non_target_thread_ids") or []
    if state.get("wake_count", 0) >= contract["maximum_wakes"]:
        return _decision(NextAction.PAUSE_EXPIRED, "wake_budget_exhausted")
    reason = "non_target_only" if non_target else stalled["reason_code"]
    return _decision(
        NextAction.WAIT_REVIEW,
        reason,
        stalled_review=stalled,
    )
