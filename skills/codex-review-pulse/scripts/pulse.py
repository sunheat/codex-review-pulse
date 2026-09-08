#!/usr/bin/env python3
"""Codex-first default control surface for one PR-scoped wake.

This module deliberately depends only on the core GraphQL, state, checkpoint,
and exact-resolution primitives.  The run-contract, installation, lease, and
heartbeat-tick modules remain an opt-in hardened mode and are not imported by
the default path.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path
import secrets
import subprocess
import sys
from typing import Any, Callable, Mapping

from checkpoint_store import (
    checkpoint_path,
    git_common_directory,
    load_checkpoint,
    save_checkpoint,
)
from default_policy import (
    PolicyError,
    apply_policy_overrides,
    default_policy,
    parse_policy_json,
    policy_digest,
    normalize_policy,
)
from fetch_pr_state import fetch_stable_snapshot, resolve_target
from state_model import (
    DEFAULT_CODEX_LOGINS,
    canonical_repository,
    empty_checkpoint,
    evaluate_snapshot,
    freeze_batch,
    record_publication_failure,
    record_publication_success,
    record_resolved_thread,
    record_thread_outcome,
)


DEFAULT_CADENCE_SECONDS = 600
DEFAULT_MODE_SCHEMA_VERSION = 4
STANDALONE_TASK_PROTOCOL_VERSION = 13
# The local scheduler exposes task metadata at whole-second precision.  The
# re-anchor path must use that same representation for expected and observed
# first-run values; direct completion callbacks retain their exact/ceil path.
SCHEDULER_TIMESTAMP_PRECISION = "whole-second-truncation"
SCHEDULE_REANCHOR_TOLERANCE = timedelta(seconds=1)
HEARTBEAT_BATCH_ORDER = (
    "record-outcome",
    "focused-validation",
    "exact-resolution",
    "aggregate-validation",
    "prepare-publication",
    "commit",
    "prepare-publication",
    "push",
    "record-publication",
)

REARM_ACTIONS = {"WAIT_REVIEW", "REQUEST_REVIEW", "WAIT_RETRY"}
PAUSE_ACTIONS = {
    "PAUSE_BLOCKED",
    "PAUSE_CONCURRENT",
    "PAUSE_RECOVERY",
    "PAUSE_EXPIRED",
    "PAUSE_POLICY_CONFIRMATION",
}
TERMINAL_ACTIONS = {"STOP_TERMINAL", "STOP_CLOSED", "STOP_POLICY_LIMIT"}
SCHEDULED_TASK_DISPOSITIONS = {"PAUSED", "AUTHORIZED", "ACTIVE", "NONE", "UNKNOWN"}
RETIREMENT_PHASES = {"registered", "pending", "confirmed", "unknown"}
RETIREMENT_ROLES = {"setup", "delivered"}
HANDOFF_ONLY_PHASES = {
    "retirement_pending",
    "retirement_recovery",
    "successor_ready",
    "successor_authorized",
    "successor_finalized",
}


class DefaultWakeError(RuntimeError):
    """Raised when a default wake attempts an operation after its boundary."""


def build_standalone_task_handoff(
    repository: str,
    pr_number: int,
    *,
    policy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the immutable handoff for one clean-context scheduled task."""
    canonical = canonical_repository(repository)
    target = f"{canonical}#{pr_number}"
    effective_policy = normalize_policy(policy)
    # Keep this handoff wording stable: delivered tasks compare the complete
    # prompt and its SHA-256 before a successor can be re-armed.
    prompt = (
        "Use $codex-review-pulse from its loaded user-directory installation to "
        f"run exactly one automatic Codex review-remediation wake for {target} in "
        "this new standalone task/conversation. The structured task execution "
        "settings are authoritative in the persisted automation policy and task "
        "metadata; preserve those settings for every scheduled successor in this "
        "run. "
        "This is a scheduler-delivered "
        "standalone invocation, not a continuation of another task and not a "
        "same-task heartbeat; never reuse a Codex conversation or targetThreadId. "
        "The outer setup path registers its verified paused setup task atomically "
        "with wake one; every delivered wake registers only its exact delivered "
        "task after structured pre/post scheduler provenance validation. Never infer a predecessor "
        "from a name, prompt, age, or scheduler listing. "
        "Before every Codex automation status transition, read the task's persisted "
        "definition and submit the full cron update payload: preserve kind, name, "
        "prompt, recurrence, model, reasoning, project, environment, and destination, "
        "changing only status. Never send a status-only update. The metadata read is "
        "not a scheduler mutation; the full pause update remains the first scheduler "
        "operation of a delivered wake. "
        "Load and obey the installed skill's SKILL.md. Use the Desktop-native "
        "update_plan tool when this harness exposes it as user-visible telemetry "
        "for this standalone worker only: after startup and before the first "
        "PR/review operation, create a small outcome-oriented plan with steps "
        "for inspecting PR/review state, classifying findings, repairing, "
        "verifying, and publishing/rearming or recording the final result. Keep "
        "exactly one step in_progress while work is active; update it after the "
        "snapshot, classification, repair, focused or aggregate verification, "
        "publication, rearm, and final result, and immediately when the approach "
        "changes. Report a concrete blocker without false completion. If the "
        "native tool is unavailable, continue normally and report that telemetry "
        "was unavailable; never simulate it or persist plan state. Do not track "
        "setup or wait for delivery, reuse a plan across standalone wakes, or add "
        "CLI-specific behavior. Treat the scheduler's "
        "configured project checkout only as a "
        "read-only repository locator. After the required task-pause and checkpoint "
        "preflight, persist begin-wake using the configured checkout before any "
        "fallible setup. Then verify the remote PR head and create a new task-owned clean "
        "linked worktree at that exact head, then load and obey "
        "that worktree's AGENTS.md. Never reuse a "
        "worktree from an earlier wake, and never switch, reset, clean, or modify "
        "the configured/main checkout. Run every repository mutation, validation, "
        "Git publication command, and pulse command for this wake from the new "
         "worktree, passing it as --repository-path. Use the target repository's "
         "Git-common-dir checkpoint as the canonical durable lifecycle and control "
         "authority. Checkpoint-referenced immutable repair patches/manifests and "
         "verified scheduler task metadata may persist as auxiliary recovery or "
         "evidence artifacts, not independent workflow authorities. Generate one "
         "fresh opaque wake_id and run at "
        "most one stable frozen batch. For RUN_BATCH, verify that the wake "
        "worktree's git rev-parse HEAD equals snapshot.head_oid before freezing; "
        "the freeze guard rejects a mismatch. Then record each frozen thread "
        "outcome, apply and focused-validate any required repair, then resolve that "
        "exact thread while the PR head still equals the frozen head. Never commit "
        "or push before every frozen thread is resolved. After all exact resolutions, "
        "run aggregate validation; run prepare-publication before commit and again "
        "immediately before push; explicitly stage intended paths; commit and push "
        "at most once; verify the published head; then record the publication result. "
        "If a fix-now repair leaves uncommitted changes and a recoverable retry is "
        "needed, write an immutable patch plus manifest under the Git common dir and "
        "pass that manifest to pulse retry --pending-repair; the next clean worktree "
        "must verify and apply it before focused validation. Leave push-created "
        "review artifacts for a later wake. For a rearmable result only, complete "
        "worktree cleanup, persist immutable handoff plus exact predecessor retirement "
        "pending evidence, delete only that registered task ID, and durably confirm "
        "the exact retirement before taking the completion clock or creating anything. "
        "A timeout, malformed delete response, or unreadable exact lookup is UNKNOWN, "
        "not absence: leave the original wake handoff-only and use explicit exact "
        "reconciliation. After confirmed retirement, create one new standalone "
        "successor task in PAUSED state with the persisted handoff and a host-supported "
        "cadence-only recurring schedule; do not submit DTSTART. Extract its ID inside "
        "the durable handoff boundary, then read back the persisted task ID, status, "
        "creation timestamp, prompt and prompt digest, cron/standalone metadata, "
        "absent target thread, model, reasoning settings, and cadence before accepting "
        "it. Cleanup and confirmed exact retirement must finish before taking the host's "
        "current UTC completion anchor and before creating the successor. A known "
        "successor ID is never discarded or recreated; unknown create IDs require "
        "manual recovery, never discovery. Run authorize-successor with its "
        "verified ID and schedule while the successor remains PAUSED and the current "
        "wake remains active. Then call complete-wake to durably finalize the wake; its "
        "checkpoint must remain AUTHORIZED until delivery. Only after that finalization "
        "succeeds, activate exactly that successor as the final host mutation. Do not "
        "run pulse, cleanup, validation, or ordinary work after activation. "
        "If a host restart occurs before durable finalization, never activate from "
        "authorization alone: keep the exact task paused and preserve the active-wake "
        "guard. After finalization, reconcile only from exact task-status readback and "
        "explicit host evidence that no delivery was observed; a delivered task must "
        "go directly through begin-wake and must never be reactivated. "
        "Derive the first run from persisted created_at plus cadence, then pass both "
        "timestamps to complete-wake. The creation timestamp must be at or after this "
        "wake's final completion anchor, so the successor cannot run early. For every scheduled "
        "delivery, pass its exact task ID to begin-wake as --delivered-task-id "
        "so pulse.py validates the persisted successor provenance. Preserve non-target "
        "threads; never merge, enable auto-merge, change the base, force-push, or "
        "create issues. After complete-wake, report the result and end this "
        "invocation immediately; do not start, schedule, or consume another wake. If "
        "complete-wake raises or returns malformed data after successor activation, "
        "pause that exact successor and confirm cleanup before propagating the failure. "
        "Before END_INVOCATION, verify the fresh task-owned worktree is clean, remove "
        "that worktree, and prune its administrative entry; never remove the configured "
        "checkout. If cleanup cannot be confirmed, keep the next task paused and report "
        "the cleanup blocker. "
        "Keep the delivered task paused on every PAUSE_* or STOP_* result."
    )
    prompt_digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    return {
        "protocol_version": STANDALONE_TASK_PROTOCOL_VERSION,
        "repository": canonical,
        "pull_request_number": pr_number,
        "model": effective_policy["model"],
        "reasoning_effort": effective_policy["reasoning_effort"],
        "scheduler_kind": "cron",
        "conversation_mode": "standalone",
        "reuse_conversation": False,
        "target_thread_id": None,
        "checkpoint_scope": "git-common-dir",
        "checkout_mode": "new-linked-worktree-per-wake",
        "configured_checkout_role": "read-only-repository-locator",
        "reuse_worktree": False,
        "schedule_anchor_mode": "persisted-created-at-plus-cadence",
        "submit_dtstart": False,
        "prompt_sha256": prompt_digest,
        "batch_order": list(HEARTBEAT_BATCH_ORDER),
        "prompt": prompt,
    }


def build_heartbeat_handoff(
    repository: str,
    pr_number: int,
    *,
    policy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Backward-compatible alias for the standalone task handoff."""
    return build_standalone_task_handoff(repository, pr_number, policy=policy)


def _policy_pause(state: dict[str, Any], *, now: str, operation: str) -> dict[str, Any]:
    """Pause when a supervised policy requires a user decision."""
    return _pause(
        state,
        reason_code="policy_requires_confirmation",
        now=now,
        evidence={"operation": operation, "profile": state["automation_policy"]["profile"]},
        action="PAUSE_POLICY_CONFIRMATION",
    )


def _policy_confirmation_allows(state: dict[str, Any], operation: str) -> bool:
    confirmation = state.get("policy_confirmation")
    if not isinstance(confirmation, dict) or confirmation.get("operation") != operation:
        return False
    batch = state.get("active_batch")
    expected_head_oid = confirmation.get("head_oid")
    if expected_head_oid is not None:
        current_head_oid = (
            (state.get("latest_target_snapshot") or {}).get("head_oid")
            if operation == "review_trigger"
            else batch.get("frozen_head_oid")
            if isinstance(batch, dict)
            else None
        )
        if current_head_oid != expected_head_oid:
            return False
    if confirmation.get("targeted_thread_ids"):
        if not isinstance(batch, dict):
            return False
        if list(batch.get("targeted_thread_ids") or []) != list(
            confirmation.get("targeted_thread_ids") or []
        ):
            return False
    return True


def _consume_policy_confirmation(state: dict[str, Any], operation: str) -> None:
    if _policy_confirmation_allows(state, operation):
        state["policy_confirmation"] = None


def _consume_thread_resolution_confirmation(state: dict[str, Any]) -> None:
    batch = state.get("active_batch")
    if not isinstance(batch, dict):
        return
    targeted = set(batch.get("targeted_thread_ids") or [])
    resolved = set(batch.get("resolved_thread_ids") or [])
    if targeted and targeted.issubset(resolved):
        _consume_policy_confirmation(state, "thread_resolution")


def confirm_policy_operation(
    checkpoint: dict[str, Any], *, operation: str, now: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Record one explicit supervised continuation without generic latch clearing."""
    if operation not in {"thread_resolution", "aggregate_publication", "review_trigger"}:
        raise ValueError("Unsupported supervised confirmation operation")
    state = ensure_default_lifecycle(checkpoint)
    if state.get("active_wake_id"):
        raise DefaultWakeError("A policy confirmation cannot be recorded during an active wake")
    latch = state.get("failure_latch")
    evidence = latch.get("evidence") if isinstance(latch, dict) else None
    if not isinstance(latch, dict) or latch.get("reason_code") != "policy_requires_confirmation":
        raise DefaultWakeError("No supervised policy confirmation is pending")
    if not isinstance(evidence, dict) or evidence.get("operation") != operation:
        raise DefaultWakeError("The requested confirmation does not match the pending operation")
    policy_key = {
        "thread_resolution": "thread_resolution",
        "aggregate_publication": "publication",
        "review_trigger": "review_trigger",
    }[operation]
    if state["automation_policy"].get(policy_key) != "confirm":
        raise DefaultWakeError(
            "The persisted policy does not permit confirmation for this operation"
        )
    batch = state.get("active_batch")
    if operation in {"thread_resolution", "aggregate_publication"} and not isinstance(batch, dict):
        raise DefaultWakeError("The pending supervised operation has no active batch")
    state["failure_latch"] = None
    state["policy_confirmation"] = {
        "operation": operation,
        "confirmed_at": _iso(now),
        "head_oid": (
            (state.get("latest_target_snapshot") or {}).get("head_oid")
            if operation == "review_trigger"
            else batch.get("frozen_head_oid")
            if isinstance(batch, dict)
            else None
        ),
        "targeted_thread_ids": (
            list(batch.get("targeted_thread_ids") or [])
            if operation != "review_trigger" and isinstance(batch, dict)
            else []
        ),
    }
    state["wake_phase"] = "confirmation_ready"
    state["scheduled_task_disposition"] = "PAUSED"
    result = _decision(
        "POLICY_CONFIRMATION_RECORDED",
        "policy_confirmation_recorded",
        operation=operation,
        mutation_occurred=False,
    )
    _set_last_result(state, result)
    return state, result


def _utc(value: str | datetime) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Time inputs must include a timezone")
    return parsed.astimezone(UTC)


def _iso(value: str | datetime) -> str:
    return _utc(value).isoformat()


def _now(value: str | None) -> str:
    return _iso(value or datetime.now(UTC))


def _ceil_to_second(value: str | datetime) -> datetime:
    """Return the first representable scheduler instant at or after value."""
    parsed = _utc(value)
    if parsed.microsecond:
        parsed += timedelta(seconds=1)
    return parsed.replace(microsecond=0)


def _truncate_to_scheduler_precision(value: str | datetime) -> datetime:
    """Represent a scheduler timestamp using its authoritative whole second."""
    return _utc(value).replace(microsecond=0)


def _schedule_times_match(
    expected: str | datetime,
    observed: str | datetime,
    *,
    ordered: bool,
    scheduler_precision: bool = False,
) -> bool:
    """Compare schedule instants with a bounded, direction-aware tolerance."""
    normalize = (
        _truncate_to_scheduler_precision if scheduler_precision else _utc
    )
    delta = normalize(observed) - normalize(expected)
    if ordered:
        return timedelta(0) <= delta <= SCHEDULE_REANCHOR_TOLERANCE
    return -SCHEDULE_REANCHOR_TOLERANCE <= delta <= SCHEDULE_REANCHOR_TOLERANCE


def _default_review_epoch() -> dict[str, Any]:
    return {
        "head_oid": None,
        "codex_eyes_seen": False,
        "codex_eyes_active": False,
        "clean_epoch_proven": False,
        "idle_observation_count": 0,
    }


def _nonempty_task_id(value: object) -> str | None:
    """Return an exact scheduler ID only when it is a non-empty string."""
    return value if isinstance(value, str) and value.strip() else None


def _handoff_identity(
    state: Mapping[str, Any], *, handoff: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Build the immutable static handoff evidence for one retirement edge.

    This deliberately records the canonical rendered prompt rather than an
    in-memory ``StandaloneInvocation`` object.  A later process can therefore
    validate a known successor without rediscovering scheduler tasks.
    """
    rendered = (
        dict(handoff)
        if isinstance(handoff, Mapping)
        else build_standalone_task_handoff(
            str(state["repository"]),
            int(state["pull_request_number"]),
            policy=state["automation_policy"],
        )
    )
    required = (
        "protocol_version",
        "repository",
        "pull_request_number",
        "model",
        "reasoning_effort",
        "scheduler_kind",
        "conversation_mode",
        "target_thread_id",
        "prompt",
        "prompt_sha256",
    )
    if any(key not in rendered for key in required):
        raise ValueError("Standalone handoff evidence is incomplete")
    prompt = rendered.get("prompt")
    digest = rendered.get("prompt_sha256")
    if (
        rendered.get("protocol_version") != STANDALONE_TASK_PROTOCOL_VERSION
        or rendered.get("repository") != state.get("repository")
        or rendered.get("pull_request_number") != state.get("pull_request_number")
        or not isinstance(prompt, str)
        or not prompt
        or not isinstance(digest, str)
        or hashlib.sha256(prompt.encode("utf-8")).hexdigest() != digest
        or rendered.get("scheduler_kind") != "cron"
        or rendered.get("conversation_mode") != "standalone"
        or rendered.get("target_thread_id") is not None
    ):
        raise ValueError("Standalone handoff evidence is inconsistent")
    return {
        "protocol_version": rendered["protocol_version"],
        "repository": rendered["repository"],
        "pull_request_number": rendered["pull_request_number"],
        "model": rendered["model"],
        "reasoning_effort": rendered["reasoning_effort"],
        "cadence_seconds": state["automation_policy"]["cadence_seconds"],
        "scheduler_kind": rendered["scheduler_kind"],
        "conversation_mode": rendered["conversation_mode"],
        "target_thread_id": rendered["target_thread_id"],
        "prompt": prompt,
        "prompt_sha256": digest,
        "policy_digest": state["automation_policy_digest"],
    }


def _validate_retirement_record(state: Mapping[str, Any], record: Mapping[str, Any]) -> None:
    """Validate one bounded exact predecessor-retirement record."""
    phase = record.get("phase")
    task_id = _nonempty_task_id(record.get("task_id"))
    role = record.get("role")
    wake_id = record.get("wake_id")
    if (
        phase not in RETIREMENT_PHASES
        or task_id is None
        or role not in RETIREMENT_ROLES
        or not isinstance(wake_id, str)
        or not wake_id
        or not isinstance(record.get("provenance"), Mapping)
        or not isinstance(record.get("handoff"), Mapping)
    ):
        raise ValueError("Task retirement evidence is malformed")
    handoff = record["handoff"]
    if (
        handoff.get("repository") != state.get("repository")
        or handoff.get("pull_request_number") != state.get("pull_request_number")
        or handoff.get("protocol_version") != STANDALONE_TASK_PROTOCOL_VERSION
        or handoff.get("scheduler_kind") != "cron"
        or handoff.get("conversation_mode") != "standalone"
        or handoff.get("target_thread_id") is not None
        or isinstance(handoff.get("cadence_seconds"), bool)
        or not isinstance(handoff.get("cadence_seconds"), int)
        or handoff.get("cadence_seconds") <= 0
        or not isinstance(handoff.get("policy_digest"), str)
        or not handoff.get("policy_digest")
        or not isinstance(handoff.get("prompt"), str)
        or hashlib.sha256(handoff["prompt"].encode("utf-8")).hexdigest()
        != handoff.get("prompt_sha256")
    ):
        raise ValueError("Task retirement handoff evidence is inconsistent")
    if phase in {"pending", "confirmed", "unknown"}:
        rearm = record.get("rearm")
        if not isinstance(rearm, Mapping):
            raise ValueError("Task retirement rearm evidence is missing")
        action = rearm.get("action")
        source_action = rearm.get("source_action")
        proof = rearm.get("proof")
        if action not in REARM_ACTIONS or not isinstance(proof, Mapping):
            raise ValueError("Task retirement rearm evidence is invalid")
        if source_action == "RUN_BATCH":
            if (proof.get("publication") or {}).get("status") != "succeeded":
                raise ValueError("Task retirement publication proof is invalid")
        elif source_action == "WAIT_RETRY":
            if not isinstance(proof.get("pending_repair"), Mapping):
                raise ValueError("Task retirement retry proof is invalid")
        elif source_action == "REQUEST_REVIEW":
            if (proof.get("trigger") or {}).get("status") != "emitted":
                raise ValueError("Task retirement trigger proof is invalid")
        elif source_action == "WAIT_REVIEW":
            if not isinstance(proof.get("decision"), Mapping):
                raise ValueError("Task retirement wait proof is invalid")
        else:
            raise ValueError("Task retirement rearm source is invalid")


def _validate_retirement_shape(state: Mapping[str, Any]) -> None:
    """Enforce truthful task-pointer and disposition semantics for lifecycle v3."""
    record = state.get("task_retirement")
    task_id = _nonempty_task_id(state.get("scheduled_task_id"))
    disposition = state.get("scheduled_task_disposition")
    if record is None:
        if disposition in {"NONE", "UNKNOWN"}:
            raise ValueError("A NONE or UNKNOWN scheduler state requires retirement evidence")
        return
    if not isinstance(record, Mapping):
        raise ValueError("Task retirement evidence is malformed")
    _validate_retirement_record(state, record)
    phase = record["phase"]
    predecessor = record["task_id"]
    successor = record.get("successor")
    if phase in {"registered", "pending"}:
        if task_id != predecessor or disposition != "PAUSED":
            raise ValueError("Registered or pending predecessor state is not truthful")
        return
    if phase == "unknown" and not isinstance(successor, Mapping):
        if task_id is not None or disposition != "UNKNOWN":
            raise ValueError("Unknown predecessor retirement state is not truthful")
        return
    if phase not in {"confirmed", "unknown"}:
        return
    if not isinstance(successor, Mapping):
        if task_id is not None or disposition != "NONE":
            raise ValueError("Confirmed retirement without successor must use NONE")
        return
    successor_id = _nonempty_task_id(successor.get("task_id"))
    successor_status = successor.get("status")
    if successor_id is None:
        if successor_status != "creation_unknown" or task_id is not None or disposition != "UNKNOWN":
            raise ValueError("Unknown successor creation state is not truthful")
        return
    if task_id != successor_id:
        raise ValueError("Known successor ID must remain the current scheduler pointer")
    if successor_status == "unknown":
        if disposition != "UNKNOWN":
            raise ValueError("Unknown successor status must use UNKNOWN disposition")
    elif successor_status == "paused":
        if disposition != "PAUSED":
            raise ValueError("Known paused successor must use PAUSED disposition")
    elif successor_status == "authorized":
        if disposition != "AUTHORIZED":
            raise ValueError("Authorized successor must use AUTHORIZED disposition")
    elif successor_status == "active":
        if disposition != "ACTIVE":
            raise ValueError("Active successor must use ACTIVE disposition")
    else:
        raise ValueError("Known successor status is invalid")


def ensure_default_lifecycle(checkpoint: dict[str, Any]) -> dict[str, Any]:
    """Add and validate default lifecycle state, migrating only empty legacy state."""
    result = deepcopy(checkpoint)
    previous_version = result.get("default_mode_schema_version")
    if (
        isinstance(previous_version, bool)
        or previous_version not in (None, 1, 2, 3, DEFAULT_MODE_SCHEMA_VERSION)
    ):
        raise ValueError("Unsupported Codex-first default lifecycle schema version")
    # A v2 active initial wake did not persist its setup task identity.  Do
    # not stamp it as v3 NONE/UNKNOWN: either would invent scheduler truth.
    if (
        previous_version == 2
        and result.get("active_wake_id")
        and result.get("scheduled_task_id") is None
        and result.get("scheduled_task_disposition", "PAUSED") == "PAUSED"
    ):
        raise DefaultWakeError("legacy_active_task_identity_unknown")
    # v11 never carried the immutable retirement handoff required to compare a
    # live task safely with protocol v12.  Active chains therefore require an
    # explicit fresh setup rather than an implicit renderer compatibility mode.
    if previous_version == 2 and (
        result.get("active_wake_id") or result.get("scheduled_task_id")
    ):
        raise DefaultWakeError("legacy_v11_handoff_unverified")
    if previous_version in {1, 2, 3}:
        legacy_wake_count = result.get("wake_count", 0)
        legacy_scheduler_state = any(
            result.get(key) not in (None, "", "PAUSED")
            for key in ("active_wake_id", "scheduled_task_id")
        ) or result.get("scheduled_task_disposition") in {
            "ACTIVE",
            "AUTHORIZED",
            "UNKNOWN",
        }
        if (
            isinstance(legacy_wake_count, bool)
            or not isinstance(legacy_wake_count, int)
            or legacy_wake_count != 0
            or legacy_scheduler_state
            or result.get("task_retirement") is not None
        ):
            raise DefaultWakeError("legacy_wake_accounting_unverified")
    defaults: dict[str, Any] = {
        "default_mode_schema_version": DEFAULT_MODE_SCHEMA_VERSION,
        "active_wake_id": None,
        "wake_phase": "idle",
        "wake_started_at": None,
        "wake_completed_at": None,
        "wake_mutation_occurred": False,
        "next_not_before": None,
        "scheduled_task_disposition": "PAUSED",
        "scheduled_task_kind": "standalone",
        "scheduled_task_id": None,
        "setup_creation_authority": None,
        "creation_intent": None,
        "task_retirement": None,
        "previous_task_retirement": None,
        "wake_count": 0,
        "failure_latch": None,
        "last_wake_id": None,
        "last_wake_result": None,
        "last_decision": None,
        "policy_confirmation": None,
        "review_epoch_state": _default_review_epoch(),
        "trigger_events": {},
        "last_snapshot_wake_id": None,
        "resume_pending_batch": False,
        "automation_policy": default_policy(),
        "automation_policy_digest": None,
        "retry_state": {
            "inline_attempts": 0,
            "wake_attempts": 0,
            "last_signature": None,
            "no_progress_attempts": 0,
        },
    }
    for key, value in defaults.items():
        result.setdefault(key, deepcopy(value))
    result["default_mode_schema_version"] = DEFAULT_MODE_SCHEMA_VERSION
    try:
        result["automation_policy"] = normalize_policy(result["automation_policy"])
    except PolicyError as error:
        raise ValueError(str(error)) from error
    result["automation_policy_digest"] = policy_digest(result["automation_policy"])
    if result.get("scheduled_task_disposition") not in SCHEDULED_TASK_DISPOSITIONS:
        raise ValueError("Scheduled task disposition is invalid")
    if result.get("scheduled_task_kind") != "standalone":
        raise ValueError("Scheduled task kind must be standalone")
    scheduled_task_id = result.get("scheduled_task_id")
    if scheduled_task_id is not None and (
        not isinstance(scheduled_task_id, str) or not scheduled_task_id.strip()
    ):
        raise ValueError("Scheduled task ID is invalid")
    wake_count = result.get("wake_count")
    if not isinstance(wake_count, int) or isinstance(wake_count, bool) or wake_count < 0:
        raise ValueError("Default wake count is invalid")
    if not isinstance(result.get("trigger_events"), dict):
        raise ValueError("Default trigger events are invalid")
    setup_authority = result.get("setup_creation_authority")
    if setup_authority is not None and not isinstance(setup_authority, Mapping):
        raise ValueError("Setup creation authority is invalid")
    creation_intent = result.get("creation_intent")
    if creation_intent is not None:
        if not isinstance(creation_intent, Mapping):
            raise ValueError("Creation intent is invalid")
        if creation_intent.get("role") not in {"setup", "successor"}:
            raise ValueError("Creation intent role is invalid")
        if creation_intent.get("status") not in {
            "PENDING",
            "ID_RECORDED",
            "VERIFIED",
            "AUTHORITATIVE_NO_SUCCESSOR",
            "UNKNOWN",
        }:
            raise ValueError("Creation intent status is invalid")
        if creation_intent.get("repository") != result.get("repository"):
            raise ValueError("Creation intent repository is invalid")
        if creation_intent.get("pull_request_number") != result.get(
            "pull_request_number"
        ):
            raise ValueError("Creation intent pull request is invalid")
        if not isinstance(creation_intent.get("handoff"), Mapping):
            raise ValueError("Creation intent handoff is invalid")
        if not isinstance(creation_intent.get("creation_nonce"), str) or not creation_intent[
            "creation_nonce"
        ].strip():
            raise ValueError("Creation intent nonce is invalid")
    retry_state = result.get("retry_state")
    if not isinstance(retry_state, dict):
        raise ValueError("Default retry state is invalid")
    for key in ("inline_attempts", "wake_attempts", "no_progress_attempts"):
        value = retry_state.get(key, 0)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"Default retry state field {key} is invalid")
    _validate_retirement_shape(result)
    return result


def update_default_policy(
    checkpoint: dict[str, Any],
    *,
    overrides: Mapping[str, Any],
    now: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Persist an explicit user-supplied policy update outside an active wake."""
    state = ensure_default_lifecycle(checkpoint)
    if state.get("active_wake_id"):
        raise DefaultWakeError("Policy cannot change while a wake is active")
    batch = state.get("active_batch")
    if (
        isinstance(batch, dict)
        and (batch.get("publication") or {}).get("status") != "succeeded"
    ):
        raise DefaultWakeError("Policy cannot change while a frozen batch is unfinished")
    if state.get("wake_phase") in {"terminal", "closed"}:
        raise DefaultWakeError("The checkpoint has reached an absorbing stop")
    try:
        policy = apply_policy_overrides(state.get("automation_policy"), overrides)
    except PolicyError as error:
        raise ValueError(str(error)) from error
    state["automation_policy"] = policy
    state["automation_policy_digest"] = policy_digest(policy)
    result = _decision(
        "POLICY_UPDATED",
        "explicit_policy_update",
        policy=deepcopy(policy),
        policy_digest=state["automation_policy_digest"],
        updated_at=_iso(now),
        mutation_occurred=False,
    )
    _set_last_result(state, result)
    return state, result


def _creation_intent(
    state: Mapping[str, Any],
    *,
    role: str,
    wake_id: str | None,
    now: str,
    creation_nonce: str,
) -> dict[str, Any]:
    if role not in {"setup", "successor"}:
        raise ValueError("Creation intent role is invalid")
    if not isinstance(creation_nonce, str) or not creation_nonce.strip():
        raise ValueError("A fresh creation nonce is required")
    handoff = _handoff_identity(state)
    return {
        "role": role,
        "status": "PENDING",
        "repository": state["repository"],
        "pull_request_number": state["pull_request_number"],
        "wake_id": wake_id,
        "handoff": handoff,
        "prompt_sha256": handoff["prompt_sha256"],
        "model": handoff["model"],
        "reasoning_effort": handoff["reasoning_effort"],
        "cadence_seconds": handoff["cadence_seconds"],
        "creation_nonce": creation_nonce,
        "state_transition": (
            "setup_paused_create" if role == "setup" else "successor_paused_create"
        ),
        "created_at": _iso(now),
        "task_id": None,
    }


def record_creation_intent(
    checkpoint: dict[str, Any],
    *,
    role: str,
    now: str,
    creation_nonce: str,
    wake_id: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Persist one immutable PAUSED-task creation intent before host create."""
    state = ensure_default_lifecycle(checkpoint)
    existing = state.get("creation_intent")
    if isinstance(existing, Mapping):
        if (
            existing.get("role") == role
            and existing.get("wake_id") == wake_id
            and existing.get("status") in {"PENDING", "ID_RECORDED"}
        ):
            result = _decision(
                "CREATION_INTENT_RECORDED",
                "creation_intent_already_pending",
                role=role,
                creation_nonce=existing.get("creation_nonce"),
                mutation_occurred=False,
            )
            state["last_wake_id"] = wake_id or state.get("last_wake_id")
            state["last_wake_result"] = deepcopy(result)
            return state, result
        raise DefaultWakeError("An unresolved scheduler creation intent already exists")
    if role == "setup":
        if state.get("active_wake_id") or state.get("wake_count", 0) != 0:
            raise DefaultWakeError("Setup creation intent requires a fresh pre-wake state")
        if state.get("scheduled_task_id") is not None:
            raise DefaultWakeError("Setup creation intent cannot replace a task")
    else:
        if not isinstance(wake_id, str) or not wake_id.strip():
            raise ValueError("Successor creation intent requires the active wake ID")
        _require_active_wake(
            state,
            wake_id,
            allow_retry_completion=True,
            allow_handoff_only=True,
        )
        maximum_wakes = state["automation_policy"].get("max_wakes")
        if maximum_wakes is not None and state.get("wake_count", 0) >= maximum_wakes:
            raise DefaultWakeError("Successor creation intent exceeds the wake budget")
        rearmability = _validate_successor_rearmability(state, now=_iso(now))
        if rearmability is not None:
            raise DefaultWakeError("Successor creation intent is not rearmable")
        retirement = state.get("task_retirement")
        if (
            not isinstance(retirement, Mapping)
            or retirement.get("phase") != "confirmed"
            or retirement.get("successor") is not None
        ):
            raise DefaultWakeError("Successor creation intent requires confirmed retirement")
    intent = _creation_intent(
        state,
        role=role,
        wake_id=wake_id,
        now=now,
        creation_nonce=creation_nonce,
    )
    state["creation_intent"] = intent
    result = _decision(
        "CREATION_INTENT_RECORDED",
        "creation_intent_persisted_before_create",
        role=role,
        creation_nonce=creation_nonce,
        mutation_occurred=False,
    )
    state["last_wake_id"] = wake_id or state.get("last_wake_id")
    state["last_wake_result"] = deepcopy(result)
    return state, result


def record_setup_creation_id(
    checkpoint: dict[str, Any],
    *,
    now: str,
    task_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Persist the exact setup task ID before any fallible metadata validation."""
    state = ensure_default_lifecycle(checkpoint)
    intent = state.get("creation_intent")
    exact_id = _nonempty_task_id(task_id)
    if (
        not isinstance(intent, Mapping)
        or intent.get("role") != "setup"
        or intent.get("status") != "PENDING"
        or exact_id is None
    ):
        raise DefaultWakeError("Setup creation intent is not ready for an exact ID")
    updated = deepcopy(dict(intent))
    updated["status"] = "ID_RECORDED"
    updated["task_id"] = exact_id
    updated["id_recorded_at"] = _iso(now)
    state["creation_intent"] = updated
    result = _decision(
        "SETUP_TASK_ID_RECORDED",
        "setup_task_id_durably_recorded",
        task_id=exact_id,
        mutation_occurred=False,
    )
    state["last_wake_result"] = deepcopy(result)
    return state, result


def record_setup_creation_unknown(
    checkpoint: dict[str, Any],
    *,
    now: str,
    evidence: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Persist UNKNOWN when setup create did not yield authoritative identity."""
    state = ensure_default_lifecycle(checkpoint)
    intent = state.get("creation_intent")
    if (
        not isinstance(intent, Mapping)
        or intent.get("role") != "setup"
        or intent.get("status") not in {"PENDING", "ID_RECORDED"}
    ):
        raise DefaultWakeError("Setup creation intent is not unresolved")
    updated = deepcopy(dict(intent))
    updated["status"] = "UNKNOWN"
    updated["resolved_at"] = _iso(now)
    updated["resolution_evidence"] = deepcopy(dict(evidence or {}))
    state["creation_intent"] = updated
    result = _decision(
        "PAUSE_RECOVERY",
        "setup_creation_unknown",
        evidence=updated["resolution_evidence"],
        mutation_occurred=False,
    )
    state["last_wake_result"] = deepcopy(result)
    return state, result


def record_setup_creation_readback(
    checkpoint: dict[str, Any],
    *,
    now: str,
    task: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Verify the exact setup task and bind it as initial-wake authority."""
    state = ensure_default_lifecycle(checkpoint)
    intent = state.get("creation_intent")
    if (
        not isinstance(intent, Mapping)
        or intent.get("role") != "setup"
        or intent.get("status") != "ID_RECORDED"
    ):
        raise DefaultWakeError("Setup creation intent lacks an exact recorded ID")
    task_id = _nonempty_task_id(intent.get("task_id"))
    if task_id is None:
        raise DefaultWakeError("Setup creation intent lacks an exact task ID")
    authority = {
        "role": "setup",
        "task_id": task_id,
        "protocol_version": STANDALONE_TASK_PROTOCOL_VERSION,
        "prompt_sha256": intent["prompt_sha256"],
        "creation_nonce": intent["creation_nonce"],
        "definition": deepcopy(dict(task)),
    }
    _validated_setup_provenance(
        state,
        {
            "task_id": task_id,
            "pause_confirmed": True,
            "creation_authority": authority,
            "readback": task,
        },
    )
    updated = deepcopy(dict(intent))
    updated["status"] = "VERIFIED"
    updated["readback"] = deepcopy(dict(task))
    updated["verified_at"] = _iso(now)
    state["creation_intent"] = updated
    state["setup_creation_authority"] = {
        **authority,
        "verified_at": _iso(now),
    }
    result = _decision(
        "SETUP_TASK_VERIFIED",
        "setup_task_readback_verified",
        task_id=task_id,
        setup_task_provenance={
            "task_id": task_id,
            "pause_confirmed": True,
            "creation_authority": deepcopy(authority),
            "readback": deepcopy(dict(task)),
        },
        mutation_occurred=False,
    )
    state["last_wake_result"] = deepcopy(result)
    return state, result


def _decision(action: str, reason_code: str, **details: Any) -> dict[str, Any]:
    disposition = "pause" if action in PAUSE_ACTIONS else "complete" if action in TERMINAL_ACTIONS else "continue"
    return {
        "next_action": action,
        "reason_code": reason_code,
        "recommended_heartbeat_disposition": disposition,
        **details,
    }


def _set_last_result(state: dict[str, Any], result: dict[str, Any]) -> None:
    state["last_decision"] = deepcopy(result)
    state["last_wake_result"] = deepcopy(result)


def _note_wake_mutation(state: dict[str, Any], mutation_occurred: bool) -> None:
    """Retain a monotonic mutation audit flag for the current wake."""
    if mutation_occurred:
        state["wake_mutation_occurred"] = True


def _pause(
    state: dict[str, Any],
    *,
    reason_code: str,
    now: str,
    evidence: Any = None,
    action: str = "PAUSE_BLOCKED",
    mutation_occurred: bool = False,
    clear_active_wake: bool = True,
) -> dict[str, Any]:
    """Make pause absorbing for the current wake and persist its evidence."""
    retirement = state.get("task_retirement")
    if isinstance(retirement, Mapping) and retirement.get("phase") in {"confirmed", "unknown"}:
        return _retirement_pause(
            state,
            reason_code=reason_code,
            now=now,
            evidence=evidence,
        )
    active_wake_id = state.get("active_wake_id")
    mutation_occurred = bool(mutation_occurred) or bool(
        state.get("wake_mutation_occurred")
    )
    _note_wake_mutation(state, mutation_occurred)
    result = _decision(
        action,
        reason_code,
        evidence=evidence,
        mutation_occurred=mutation_occurred,
    )
    state["scheduled_task_disposition"] = "PAUSED"
    state["wake_phase"] = "paused"
    state["wake_completed_at"] = now
    state["next_not_before"] = None
    if clear_active_wake:
        state["active_wake_id"] = None
    state["last_wake_id"] = active_wake_id or state.get("last_wake_id")
    if not state.get("failure_latch"):
        state["failure_latch"] = {
            "reason_code": reason_code,
            "latched_at": now,
            "evidence": deepcopy(evidence),
        }
    _set_last_result(state, result)
    return result


def _terminal(
    state: dict[str, Any], *, action: str, reason_code: str, now: str, **details: Any
) -> dict[str, Any]:
    result = _decision(action, reason_code, **details)
    state["scheduled_task_disposition"] = "PAUSED"
    state["wake_phase"] = "terminal"
    state["wake_completed_at"] = now
    state["next_not_before"] = None
    state["last_wake_id"] = state.get("active_wake_id") or state.get("last_wake_id")
    state["active_wake_id"] = None
    _set_last_result(state, result)
    return result


def _require_active_wake(
    state: dict[str, Any],
    wake_id: str,
    *,
    allow_retry_completion: bool = False,
    allow_handoff_only: bool = False,
) -> None:
    if state.get("failure_latch"):
        raise DefaultWakeError("This wake is paused by a durable recovery latch")
    if state.get("active_wake_id") != wake_id:
        raise DefaultWakeError("The requested operation is not owned by the active wake")
    if (
        state.get("wake_phase") in HANDOFF_ONLY_PHASES
        and not allow_handoff_only
    ):
        raise DefaultWakeError("The active wake is limited to successor handoff recovery")
    if state.get("wake_phase") in {"paused", "terminal", "completed"} or (
        state.get("wake_phase") == "retry_waiting" and not allow_retry_completion
    ):
        raise DefaultWakeError("The current wake has already reached a terminal boundary")


def _pause_confirmation(callback: Callable[[], object] | None) -> bool:
    if callback is None:
        return False
    try:
        value = callback()
    except Exception:
        return False
    if isinstance(value, dict):
        return value.get("confirmed") is True
    return value is True


def _record_default_trigger_event(
    state: dict[str, Any], evidence: dict[str, Any]
) -> dict[str, Any]:
    """Persist injected trigger evidence without importing recurring state."""
    required = (
        "attempted_head_oid",
        "head_before",
        "head_after",
        "comment_node_id",
        "created_at",
    )
    if not all(isinstance(evidence.get(key), str) and evidence[key] for key in required):
        raise ValueError("Complete trigger evidence is required")
    _utc(evidence["created_at"])
    attempted_head = evidence["attempted_head_oid"]
    events = state.setdefault("trigger_events", {})
    if attempted_head in events:
        raise ValueError("A review trigger is already recorded for this head epoch")
    status = (
        "emitted"
        if evidence["head_before"] == attempted_head == evidence["head_after"]
        else "head_changed_during_trigger"
    )
    events[attempted_head] = {
        "status": status,
        "head_oid": attempted_head,
        "head_before": evidence["head_before"],
        "head_after": evidence["head_after"],
        "comment_node_id": evidence["comment_node_id"],
        "created_at": evidence["created_at"],
    }
    return events[attempted_head]


def _validated_setup_provenance(
    state: Mapping[str, Any], provenance: Mapping[str, Any]
) -> dict[str, Any]:
    """Require the outer setup path's exact paused-task readback proof.

    ``pause_confirmed`` is deliberately only one member of this proof.  The
    setup creation authority and the complete task readback are what bind the
    first wake to the exact task that the host created.
    """
    task_id = _nonempty_task_id(provenance.get("task_id"))
    readback = provenance.get("readback")
    authority = provenance.get("creation_authority")
    if (
        task_id is None
        or provenance.get("pause_confirmed") is not True
        or not isinstance(readback, Mapping)
    ):
        raise DefaultWakeError("verified setup task provenance is required")
    handoff = _handoff_identity(state)
    # Direct injected lifecycle tests from protocol v12 predate the durable
    # setup-intent field.  They remain a low-level compatibility input; the
    # CLI and standalone adapter require the authority below before calling
    # this function on a production path.
    legacy_authority = authority is None
    if legacy_authority:
        authority = {}
    if not isinstance(authority, Mapping):
        raise DefaultWakeError("setup creation authority is invalid")
    if (
        not legacy_authority
        and (
            authority.get("role") != "setup"
        or authority.get("task_id") != task_id
        or authority.get("protocol_version") != STANDALONE_TASK_PROTOCOL_VERSION
        or authority.get("prompt_sha256") != handoff["prompt_sha256"]
        or not isinstance(authority.get("creation_nonce"), str)
        or not authority["creation_nonce"].strip()
        )
    ):
        raise DefaultWakeError("setup creation authority is invalid")
    persisted_authority = state.get("setup_creation_authority")
    if not legacy_authority and persisted_authority is not None:
        if (
            not isinstance(persisted_authority, Mapping)
            or persisted_authority.get("role") != "setup"
            or persisted_authority.get("task_id") != task_id
            or persisted_authority.get("creation_nonce")
            != authority.get("creation_nonce")
            or persisted_authority.get("prompt_sha256")
            != authority.get("prompt_sha256")
        ):
            raise DefaultWakeError("setup task authority does not match checkpoint")
    expected = {
        "id": task_id,
        "status": "PAUSED",
        "prompt": handoff["prompt"],
        "prompt_sha256": handoff["prompt_sha256"],
        "scheduler_kind": "cron",
        "conversation_mode": "standalone",
        "target_thread_id": None,
        "model": handoff["model"],
        "reasoning_effort": handoff["reasoning_effort"],
        "cadence_seconds": handoff["cadence_seconds"],
    }
    for key, expected_value in expected.items():
        actual = readback.get(key)
        if key == "id" and actual is None:
            actual = readback.get("task_id")
        if actual != expected_value:
            raise DefaultWakeError("verified setup task provenance does not match handoff")
    if not legacy_authority:
        for key in ("created_at", "first_run"):
            if not isinstance(readback.get(key), str) or not readback[key].strip():
                raise DefaultWakeError(
                    "verified setup task provenance lacks persisted schedule metadata"
                )
    definition = authority.get("definition")
    if definition is not None:
        if not isinstance(definition, Mapping) or not _task_metadata_equal(
            definition, readback, ignore_status=True
        ):
            raise DefaultWakeError("setup creation authority does not match readback")
    return {
        "task_id": task_id,
        "pause_confirmed": True,
        "creation_authority": (
            deepcopy(dict(authority)) if not legacy_authority else None
        ),
        "readback": deepcopy(dict(readback)),
    }


def _task_metadata_equal(
    left: Mapping[str, Any], right: Mapping[str, Any], *, ignore_status: bool
) -> bool:
    """Compare scheduler records without allowing status-only drift to hide."""
    def canonical(value: Mapping[str, Any]) -> dict[str, Any]:
        result = deepcopy(dict(value))
        if "id" not in result and "task_id" in result:
            result["id"] = result["task_id"]
        result.pop("task_id", None)
        if ignore_status:
            result.pop("status", None)
        return result

    return canonical(left) == canonical(right)


def _validated_delivered_provenance(
    state: Mapping[str, Any],
    *,
    task_id: str,
    provenance: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate exact pre/post pause evidence for one delivered successor."""
    pre_pause = provenance.get("pre_pause_readback")
    post_pause = provenance.get("post_pause_readback")
    if (
        provenance.get("task_id") != task_id
        or provenance.get("pause_confirmed") is not True
        or not isinstance(pre_pause, Mapping)
        or not isinstance(post_pause, Mapping)
    ):
        raise DefaultWakeError("verified delivered task provenance is required")
    predecessor = state.get("task_retirement")
    successor = predecessor.get("successor") if isinstance(predecessor, Mapping) else None
    durable_readback = successor.get("readback") if isinstance(successor, Mapping) else None
    if (
        not isinstance(predecessor, Mapping)
        or predecessor.get("phase") != "confirmed"
        or not isinstance(successor, Mapping)
        or successor.get("task_id") != task_id
        or not isinstance(durable_readback, Mapping)
    ):
        raise DefaultWakeError("delivered task has no durable successor readback")
    for label, record in (("pre-pause", pre_pause), ("post-pause", post_pause)):
        observed_id = record.get("id", record.get("task_id"))
        if observed_id != task_id:
            raise DefaultWakeError(f"delivered {label} readback does not match task ID")
        if label == "post-pause" and record.get("status") != "PAUSED":
            raise DefaultWakeError("delivered post-pause task is not PAUSED")
        if label == "pre-pause" and record.get("status") not in {"ACTIVE", "AUTHORIZED"}:
            raise DefaultWakeError("delivered pre-pause task status is invalid")
        if not _task_metadata_equal(record, durable_readback, ignore_status=True):
            raise DefaultWakeError(
                f"delivered {label} readback does not match durable successor definition"
            )
    if not _task_metadata_equal(pre_pause, post_pause, ignore_status=True):
        raise DefaultWakeError(
            "delivered pre/post task metadata changed outside status"
        )
    return {
        "task_id": task_id,
        "pause_confirmed": True,
        "pre_pause_readback": deepcopy(dict(pre_pause)),
        "post_pause_readback": deepcopy(dict(post_pause)),
    }


def _register_retirement_target(
    state: dict[str, Any],
    *,
    wake_id: str,
    task_id: str,
    role: str,
    provenance: Mapping[str, Any],
) -> None:
    """Install the one exact predecessor with the same mutation as begin-wake."""
    if role not in RETIREMENT_ROLES or _nonempty_task_id(task_id) is None:
        raise ValueError("Task retirement registration is invalid")
    existing = state.get("task_retirement")
    if isinstance(existing, Mapping) and existing.get("phase") == "confirmed":
        state["previous_task_retirement"] = deepcopy(dict(existing))
    state["task_retirement"] = {
        "phase": "registered",
        "wake_id": wake_id,
        "task_id": task_id,
        "role": role,
        "provenance": deepcopy(dict(provenance)),
        "handoff": _handoff_identity(state),
        "rearm": None,
        "successor": None,
    }
    state["scheduled_task_id"] = task_id
    state["scheduled_task_disposition"] = "PAUSED"


def begin_wake(
    checkpoint: dict[str, Any],
    *,
    wake_id: str,
    now: str,
    cadence_seconds: int | None = None,
    policy_overrides: Mapping[str, Any] | None = None,
    pause_heartbeat: Callable[[], object] | None = None,
    delivered_task_id: str | None = None,
    setup_task_provenance: Mapping[str, Any] | None = None,
    delivered_task_provenance: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Begin one wake after authenticating any scheduled delivery."""
    if not isinstance(wake_id, str) or not wake_id.strip():
        raise ValueError("A non-empty wake_id is required")
    now = _iso(now)
    state = ensure_default_lifecycle(checkpoint)

    if state.get("last_wake_id") == wake_id and state.get("last_wake_result"):
        return state, deepcopy(state["last_wake_result"])

    if policy_overrides is not None:
        if state.get("wake_count", 0) > 0 or state.get("active_wake_id"):
            raise DefaultWakeError(
                "Policy overrides are only accepted on the initial wake; use configure-policy for an explicit update"
            )
        try:
            state["automation_policy"] = apply_policy_overrides(
                state.get("automation_policy"), policy_overrides
            )
        except PolicyError as error:
            raise ValueError(str(error)) from error
        state["automation_policy_digest"] = policy_digest(state["automation_policy"])

    effective_cadence = (
        state["automation_policy"]["cadence_seconds"]
        if cadence_seconds is None
        else cadence_seconds
    )
    if isinstance(effective_cadence, bool) or not isinstance(effective_cadence, int) or effective_cadence <= 0:
        raise ValueError("Cadence must be positive")
    if delivered_task_id is not None and (
        not isinstance(delivered_task_id, str) or not delivered_task_id.strip()
    ):
        raise ValueError("Delivered task ID must be a non-empty string")
    if setup_task_provenance is not None and delivered_task_id is not None:
        raise ValueError("Setup provenance cannot be supplied for a delivered task")

    if state.get("wake_phase") in {"terminal", "closed"}:
        raise DefaultWakeError(
            "The checkpoint has reached an absorbing stop; an explicit user command is required to reopen it"
        )
    if state.get("active_wake_id"):
        if state["active_wake_id"] == wake_id and state.get("last_wake_result"):
            return state, deepcopy(state["last_wake_result"])
        result = _pause(
            state,
            reason_code="incomplete_wake",
            now=now,
            evidence={"active_wake_id": state["active_wake_id"]},
            action="PAUSE_RECOVERY",
            clear_active_wake=False,
        )
        return state, result
    if state.get("failure_latch"):
        result = _pause(
            state,
            reason_code="failure_latched",
            now=now,
            evidence=state["failure_latch"],
            action="PAUSE_RECOVERY",
        )
        return state, result

    scheduled_disposition = state.get("scheduled_task_disposition")
    active_schedule = scheduled_disposition in {"ACTIVE", "AUTHORIZED"}
    if (active_schedule and delivered_task_id is None) or (
        delivered_task_id is not None
        and (
            not active_schedule
            or state.get("scheduled_task_id") != delivered_task_id
        )
    ):
        result = _pause(
            state,
            reason_code="scheduled_task_identity_mismatch",
            now=now,
            evidence={
                "delivered_task_id": delivered_task_id,
                "scheduled_task_id": state.get("scheduled_task_id"),
                "scheduled_task_disposition": state.get(
                    "scheduled_task_disposition"
                ),
            },
            action="PAUSE_RECOVERY",
        )
        state["last_wake_id"] = wake_id
        return state, result

    policy = state["automation_policy"]
    maximum_wakes = policy.get("max_wakes")
    if maximum_wakes is not None and state.get("wake_count", 0) >= maximum_wakes:
        result = _terminal(
            state,
            action="STOP_POLICY_LIMIT",
            reason_code="maximum_wakes_reached",
            now=now,
            limit=maximum_wakes,
            wake_count=state.get("wake_count", 0),
        )
        state["last_wake_id"] = wake_id
        return state, result
    deadline_at = policy.get("deadline_at")
    if deadline_at is not None and _utc(now) >= _utc(deadline_at):
        result = _terminal(
            state,
            action="STOP_POLICY_LIMIT",
            reason_code="deadline_reached",
            now=now,
            deadline_at=deadline_at,
        )
        state["last_wake_id"] = wake_id
        return state, result

    next_not_before = state.get("next_not_before")
    if next_not_before is not None:
        try:
            not_before = _utc(next_not_before)
        except (TypeError, ValueError) as error:
            result = _pause(
                state,
                reason_code="checkpoint_invalid",
                now=now,
                evidence={
                    "next_not_before": next_not_before,
                    "error": str(error),
                },
                action="PAUSE_RECOVERY",
            )
            state["last_wake_id"] = wake_id
            return state, result
        if _utc(now) < not_before:
            result = _pause(
                state,
                reason_code="cadence_not_elapsed",
                now=now,
                evidence={"next_not_before": next_not_before, "wake_id": wake_id},
            )
            state["last_wake_id"] = wake_id
            return state, result

    pause_confirmed = _pause_confirmation(pause_heartbeat)
    validated_setup: dict[str, Any] | None = None
    validated_delivery: dict[str, Any] | None = None
    if pause_confirmed:
        try:
            if delivered_task_id is not None:
                if delivered_task_provenance is not None:
                    validated_delivery = _validated_delivered_provenance(
                        state,
                        task_id=delivered_task_id,
                        provenance=delivered_task_provenance,
                    )
            elif setup_task_provenance is not None:
                validated_setup = _validated_setup_provenance(
                    state, setup_task_provenance
                )
        except (DefaultWakeError, TypeError, ValueError) as error:
            result = _pause(
                state,
                reason_code="scheduler_provenance_invalid",
                now=now,
                evidence={"error": str(error)},
                action="PAUSE_RECOVERY",
            )
            state["last_wake_id"] = wake_id
            return state, result
    else:
        result = _pause(
            state,
            reason_code="heartbeat_pause_unconfirmed",
            now=now,
            evidence={"wake_id": wake_id},
        )
        state["last_wake_id"] = wake_id
        return state, result

    retry_successor_delivery = (
        state.get("wake_phase") in {"successor_authorized", "successor_finalized"}
        and (state.get("last_decision") or {}).get("next_action") == "WAIT_RETRY"
    )
    pending_batch = (
        isinstance(state.get("active_batch"), dict)
        and (state.get("active_batch") or {}).get("publication", {}).get("status") != "succeeded"
        and (
            state.get("wake_phase") in {"retry_waiting", "confirmation_ready"}
            or retry_successor_delivery
        )
    )
    authorized_delivery = scheduled_disposition == "AUTHORIZED"
    state["active_wake_id"] = wake_id
    state["wake_phase"] = "started"
    state["wake_started_at"] = now
    state["wake_completed_at"] = None
    state["wake_mutation_occurred"] = False
    state["next_not_before"] = None
    state["scheduled_task_disposition"] = "PAUSED"
    if authorized_delivery:
        state["successor_authorization"] = None
    state["last_decision"] = None
    state["last_wake_result"] = None
    state["resume_pending_batch"] = pending_batch
    state["pending_repair_restored"] = None
    if pending_batch:
        pending_repair = (state.get("active_batch") or {}).get("pending_repair")
        fix_now_threads = [
            thread_id
            for thread_id, outcome in (
                (state.get("active_batch") or {}).get("thread_outcomes") or {}
            ).items()
            if isinstance(outcome, Mapping) and outcome.get("classification") == "fix-now"
        ]
        if fix_now_threads and not isinstance(pending_repair, Mapping):
            result = _pause(
                state,
                reason_code="pending_repair_missing",
                now=now,
                evidence={"thread_ids": sorted(fix_now_threads)},
                action="PAUSE_RECOVERY",
            )
            state["last_wake_id"] = wake_id
            return state, result
        state["wake_phase"] = "processing"
        state["last_decision"] = _decision(
            "RUN_BATCH",
            "resume_confirmed_batch"
            if state.get("policy_confirmation")
            else "resume_pending_batch",
            targeted_thread_ids=list(
                (state.get("active_batch") or {}).get("targeted_thread_ids") or []
            ),
            **({"pending_repair": deepcopy(pending_repair)} if pending_repair else {}),
        )
    # Registration is the last part of a successful begin-wake replacement,
    # after preflight and pending-repair validation.  A rejected delivery must
    # leave the prior completed retirement evidence untouched.
    if delivered_task_id is not None:
        _register_retirement_target(
            state,
            wake_id=wake_id,
            task_id=delivered_task_id,
            role="delivered",
            provenance=(
                {
                    **deepcopy(validated_delivery),
                    "validation": "exact_pre_post_pause_scheduler_provenance",
                }
                if validated_delivery is not None
                else {
                    "task_id": delivered_task_id,
                    "validation": "legacy_injected_direct_callback",
                }
            ),
        )
    elif validated_setup is not None:
        state["setup_creation_authority"] = deepcopy(
            validated_setup["creation_authority"]
        )
        _register_retirement_target(
            state,
            wake_id=wake_id,
            task_id=validated_setup["task_id"],
            role="setup",
            provenance={
                **deepcopy(validated_setup),
                "validation": "exact_setup_creation_authority_and_readback",
            },
        )
    # The scheduler adapter is deliberately injected.  Count only after the
    # complete structured proof and all checkpoint admission checks succeed.
    state["wake_count"] += 1
    state["last_wake_id"] = wake_id
    result = _decision(
        "WAKE_STARTED",
        "heartbeat_paused_before_wake",
        wake_id=wake_id,
        wake_count=state["wake_count"],
        resume_pending_batch=pending_batch,
        mutation_occurred=False,
    )
    state["last_wake_result"] = deepcopy(result)
    return state, result


def _update_review_epoch(
    state: dict[str, Any], snapshot: dict[str, Any]
) -> dict[str, Any]:
    epoch = deepcopy(state.get("review_epoch_state") or _default_review_epoch())
    head_oid = snapshot.get("head_oid")
    if not isinstance(head_oid, str) or not head_oid:
        raise ValueError("Normalized snapshot requires head_oid")
    if epoch.get("head_oid") != head_oid:
        epoch = _default_review_epoch()
        epoch["head_oid"] = head_oid
    review_activity = snapshot.get("review_in_progress")
    eyes_active = (
        review_activity.get("active") is True
        if isinstance(review_activity, dict)
        else bool(review_activity)
    )
    if eyes_active:
        epoch["codex_eyes_seen"] = True
        epoch["codex_eyes_active"] = True
        epoch["clean_epoch_proven"] = False
        epoch["idle_observation_count"] = 0
    else:
        epoch["codex_eyes_active"] = False
        if snapshot.get("targeted_thread_ids") or snapshot.get("approval_evidence", {}).get("status") == "approved_current_head":
            epoch["idle_observation_count"] = 0
        else:
            epoch["idle_observation_count"] = int(epoch.get("idle_observation_count", 0)) + 1
        if epoch.get("codex_eyes_seen") and not snapshot.get("targeted_thread_ids"):
            epoch["clean_epoch_proven"] = True
    state["review_epoch_state"] = epoch
    return epoch


def decide_snapshot(
    state: dict[str, Any], snapshot: dict[str, Any], *, now: str
) -> dict[str, Any]:
    """Evaluate one normalized snapshot without performing I/O."""
    epoch = _update_review_epoch(state, snapshot)
    if snapshot.get("snapshot_stable") is not True:
        return _pause(
            state,
            reason_code="mixed_head_snapshot",
            now=now,
            evidence=snapshot.get("server_evidence"),
        )
    if not snapshot.get("review_activity_ok", True):
        return _pause(
            state,
            reason_code="review_activity_evidence_invalid",
            now=now,
            evidence=snapshot.get("review_in_progress"),
        )
    if snapshot.get("pull_request_state") in {"CLOSED", "MERGED"}:
        return _terminal(
            state,
            action="STOP_CLOSED",
            reason_code="pull_request_closed_or_merged",
            now=now,
        )
    review_activity = snapshot.get("review_in_progress")
    eyes_active = (
        review_activity.get("active") is True
        if isinstance(review_activity, dict)
        else bool(review_activity)
    )
    if eyes_active:
        result = _decision("WAIT_REVIEW", "codex_review_in_progress")
    elif snapshot.get("approval_evidence", {}).get("status") == "approved_current_head" and not snapshot.get("targeted_thread_ids"):
        return _terminal(
            state,
            action="STOP_TERMINAL",
            reason_code="current_head_approval_proven",
            now=now,
        )
    elif snapshot.get("targeted_thread_ids"):
        if state["automation_policy"]["profile"] == "observe-only":
            return _policy_pause(state, now=now, operation="process_review_threads")
        result = _decision(
            "RUN_BATCH",
            "targeted_work_available",
            targeted_thread_ids=list(snapshot["targeted_thread_ids"]),
        )
    elif state.get("trigger_events", {}).get(snapshot.get("head_oid"), {}).get("status") == "emitted":
        return _pause(
            state,
            reason_code="review_trigger_did_not_start",
            now=now,
            evidence=state["trigger_events"][snapshot["head_oid"]],
        )
    elif epoch.get("idle_observation_count", 0) >= 2:
        trigger_policy = state["automation_policy"]["review_trigger"]
        if (
            trigger_policy != "auto"
            and not _policy_confirmation_allows(state, "review_trigger")
        ):
            return _policy_pause(state, now=now, operation="review_trigger")
        result = _decision("REQUEST_REVIEW", "idle_boundary_reached", head_oid=snapshot.get("head_oid"))
    else:
        result = _decision("WAIT_REVIEW", "awaiting_review_epoch")
    state["wake_phase"] = "snapshotted"
    _set_last_result(state, result)
    return result


def record_snapshot(
    checkpoint: dict[str, Any],
    snapshot: dict[str, Any],
    *,
    wake_id: str,
    now: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Persist one normalized snapshot and its one-wake decision."""
    state = ensure_default_lifecycle(checkpoint)
    if state.get("wake_phase") == "paused":
        raise DefaultWakeError("The current wake is paused and cannot continue")
    if state.get("last_wake_id") == wake_id and state.get("active_wake_id") is None and state.get("last_wake_result"):
        return state, deepcopy(state["last_wake_result"])
    _require_active_wake(state, wake_id)
    if state.get("wake_phase") == "snapshotted" and state.get("last_decision"):
        return state, deepcopy(state["last_decision"])
    active_batch = state.get("active_batch")
    resume_pending_batch = (
        state.get("resume_pending_batch") is True
        and isinstance(active_batch, dict)
        and (active_batch.get("publication") or {}).get("status") != "succeeded"
    )
    if resume_pending_batch:
        frozen_head_oid = active_batch.get("frozen_head_oid")
        frozen_thread_ids = list(active_batch.get("targeted_thread_ids") or [])
        if snapshot.get("head_oid") != frozen_head_oid:
            result = _pause(
                state,
                reason_code="retry_batch_head_changed",
                now=_iso(now),
                evidence={
                    "frozen_head_oid": frozen_head_oid,
                    "observed_head_oid": snapshot.get("head_oid"),
                },
                action="PAUSE_RECOVERY",
            )
            state["last_snapshot_wake_id"] = wake_id
            return state, result
        if (
            snapshot.get("snapshot_stable") is not True
            or not snapshot.get("review_activity_ok", True)
            or snapshot.get("pull_request_state") != "OPEN"
        ):
            result = _pause(
                state,
                reason_code="retry_batch_snapshot_invalid",
                now=_iso(now),
                evidence={
                    "head_oid": snapshot.get("head_oid"),
                    "pull_request_state": snapshot.get("pull_request_state"),
                    "review_activity_ok": snapshot.get("review_activity_ok"),
                    "snapshot_stable": snapshot.get("snapshot_stable"),
                },
                action="PAUSE_RECOVERY",
            )
            state["last_snapshot_wake_id"] = wake_id
            return state, result
        latest_target_snapshot = state.setdefault("latest_target_snapshot", {})
        latest_target_snapshot["head_oid"] = frozen_head_oid
        latest_target_snapshot["targeted_unresolved_thread_ids"] = frozen_thread_ids
        latest_target_snapshot["reviewer_logins"] = list(
            active_batch.get("reviewer_logins") or DEFAULT_CODEX_LOGINS
        )
        state["last_snapshot_wake_id"] = wake_id
        state["resume_pending_batch"] = False
        state["wake_phase"] = "snapshotted"
        result = _decision(
            "RUN_BATCH",
            "resume_confirmed_batch"
            if state.get("policy_confirmation")
            else "resume_pending_batch",
            targeted_thread_ids=frozen_thread_ids,
        )
        _set_last_result(state, result)
        return state, result
    state["latest_target_snapshot"] = {
        "head_oid": snapshot.get("head_oid"),
        "targeted_unresolved_thread_ids": list(snapshot.get("targeted_thread_ids") or []),
        "reviewer_logins": list(snapshot.get("reviewer_logins") or DEFAULT_CODEX_LOGINS),
    }
    state["reviewer_logins"] = list(snapshot.get("reviewer_logins") or DEFAULT_CODEX_LOGINS)
    state["approval_logins"] = list(snapshot.get("approval_logins") or DEFAULT_CODEX_LOGINS)
    state["last_snapshot_wake_id"] = wake_id
    result = decide_snapshot(state, snapshot, now=_iso(now))
    if result["next_action"] in PAUSE_ACTIONS:
        state["last_wake_id"] = wake_id
    elif result["next_action"] in TERMINAL_ACTIONS:
        state["last_wake_id"] = wake_id
    else:
        state["last_snapshot"] = deepcopy(snapshot)
    return state, result


def freeze_default_batch(
    checkpoint: dict[str, Any],
    *,
    wake_id: str,
    worktree_head_oid: str | None = None,
    now: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    state = ensure_default_lifecycle(checkpoint)
    _require_active_wake(state, wake_id)
    if state.get("wake_phase") == "frozen" and isinstance(state.get("active_batch"), dict):
        return state, deepcopy(state["active_batch"])
    decision = state.get("last_decision") or {}
    if decision.get("next_action") != "RUN_BATCH":
        raise DefaultWakeError("Only a RUN_BATCH decision can freeze a batch")
    snapshot = state.get("latest_target_snapshot") or {}
    head_oid = snapshot.get("head_oid")
    thread_ids = snapshot.get("targeted_unresolved_thread_ids") or []
    if not head_oid:
        raise DefaultWakeError("The normalized snapshot has no frozen head")
    if worktree_head_oid is not None and worktree_head_oid != head_oid:
        result = _pause(
            state,
            reason_code="worktree_head_mismatch",
            now=_iso(now or datetime.now(UTC).isoformat()),
            evidence={
                "snapshot_head_oid": head_oid,
                "worktree_head_oid": worktree_head_oid,
            },
            action="PAUSE_RECOVERY",
        )
        state["last_wake_id"] = wake_id
        return state, result
    state = freeze_batch(state, head_oid, thread_ids)
    state["wake_phase"] = "frozen"
    state["last_decision"] = deepcopy(decision)
    state["last_wake_result"] = deepcopy(decision)
    return state, deepcopy(state["active_batch"])


def record_default_outcome(
    checkpoint: dict[str, Any],
    *,
    wake_id: str,
    thread_id: str,
    classification: str,
    reference: str | None = None,
    now: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    state = ensure_default_lifecycle(checkpoint)
    _require_active_wake(state, wake_id)
    if (
        state["automation_policy"]["thread_resolution"] != "auto"
        and not _policy_confirmation_allows(state, "thread_resolution")
    ):
        result = _policy_pause(state, now=_iso(now), operation="thread_resolution")
        state["last_wake_id"] = wake_id
        return state, result
    if classification == "ambiguous":
        state.setdefault("ambiguous_outcomes", {})[thread_id] = {
            "classification": classification,
            "reference": reference,
        }
        result = _pause(
            state,
            reason_code="ambiguous_thread_outcome",
            now=_iso(now),
            evidence={"thread_id": thread_id, "reference": reference},
        )
        state["last_wake_id"] = wake_id
        return state, result
    state = record_thread_outcome(
        state,
        thread_id=thread_id,
        classification=classification,
        reference=reference,
    )
    state["wake_phase"] = "processing"
    result = {
        "next_action": "PROCESS_BATCH",
        "reason_code": "thread_outcome_recorded",
        "thread_id": thread_id,
        "classification": classification,
        "mutation_occurred": False,
    }
    _set_last_result(state, result)
    return state, result


def record_retry(
    checkpoint: dict[str, Any],
    *,
    wake_id: str,
    reason_code: str,
    now: str,
    evidence: Any = None,
    signature: str | None = None,
    count_no_progress: bool = False,
    pending_repair: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Persist a recoverable failure and make the next wake retryable."""
    if not isinstance(reason_code, str) or not reason_code.strip():
        raise ValueError("A non-empty retry reason_code is required")
    if signature is not None and (
        not isinstance(signature, str) or not signature.strip()
    ):
        raise ValueError("A retry signature must be a non-empty string")
    state = ensure_default_lifecycle(checkpoint)
    _require_active_wake(state, wake_id)
    policy = state["automation_policy"]
    if policy["validation_failure"] == "pause":
        result = _policy_pause(state, now=_iso(now), operation="validation_failure")
        state["last_wake_id"] = wake_id
        return state, result
    batch = state.get("active_batch")
    fix_now_threads = (
        [
            thread_id
            for thread_id, outcome in (batch.get("thread_outcomes") or {}).items()
            if isinstance(outcome, Mapping) and outcome.get("classification") == "fix-now"
        ]
        if isinstance(batch, Mapping)
        else []
    )
    if (
        fix_now_threads
        and pending_repair is None
    ):
        result = _pause(
            state,
            reason_code="pending_repair_unpersisted",
            now=_iso(now),
            evidence={"thread_ids": sorted(fix_now_threads)},
            action="PAUSE_RECOVERY",
        )
        state["last_wake_id"] = wake_id
        return state, result
    if pending_repair is not None:
        if not isinstance(pending_repair, Mapping):
            raise ValueError("Pending repair must be a JSON object")
        if not isinstance(batch, Mapping):
            raise ValueError("A pending repair requires an active frozen batch")
        frozen_head_oid = batch.get("frozen_head_oid")
        patch_path = pending_repair.get("patch_path")
        patch_sha256 = pending_repair.get("patch_sha256")
        if not isinstance(patch_path, str) or not patch_path.strip():
            raise ValueError("Pending repair patch_path is required")
        if not isinstance(patch_sha256, str) or len(patch_sha256) != 64 or any(
            character not in "0123456789abcdefABCDEF" for character in patch_sha256
        ):
            raise ValueError("Pending repair patch_sha256 must be a SHA-256 hex digest")
        if pending_repair.get("frozen_head_oid") != frozen_head_oid:
            raise ValueError("Pending repair frozen head does not match the active batch")
        batch["pending_repair"] = deepcopy(dict(pending_repair))
    if count_no_progress and not signature:
        raise ValueError("A failure signature is required to count no progress")
    retry_state = state.setdefault("retry_state", {})
    if count_no_progress:
        if signature and signature == retry_state.get("last_signature"):
            retry_state["no_progress_attempts"] = int(
                retry_state.get("no_progress_attempts", 0)
            ) + 1
        else:
            retry_state["no_progress_attempts"] = 1
    else:
        retry_state["no_progress_attempts"] = 0
    retry_state["last_signature"] = signature
    no_progress_attempts = int(retry_state.get("no_progress_attempts", 0))
    if count_no_progress and no_progress_attempts >= policy["no_progress_limit"]:
        result = _pause(
            state,
            reason_code="no_progress_limit_reached",
            now=_iso(now),
            evidence={
                "signature": signature,
                "attempts": no_progress_attempts,
                "limit": policy["no_progress_limit"],
                "detail": evidence,
            },
            action="PAUSE_BLOCKED",
        )
        state["last_wake_id"] = wake_id
        return state, result
    wake_attempts = int(retry_state.get("wake_attempts", 0)) + 1
    retry_limit = policy.get("retry_wake_limit")
    if retry_limit is not None and wake_attempts > retry_limit:
        result = _terminal(
            state,
            action="STOP_POLICY_LIMIT",
            reason_code="retry_wake_limit_reached",
            now=_iso(now),
            limit=retry_limit,
            retry_wake_attempts=wake_attempts,
            evidence=evidence,
        )
        state["last_wake_id"] = wake_id
        return state, result
    retry_state["wake_attempts"] = wake_attempts
    retry_state["inline_attempts"] = 0
    state["wake_phase"] = "retry_waiting"
    result = _decision(
        "WAIT_RETRY",
        reason_code,
        retry_wake_attempts=wake_attempts,
        retry_limit=retry_limit,
        evidence=evidence,
        mutation_occurred=False,
    )
    _set_last_result(state, result)
    return state, result


def _require_restored_repair(
    state: Mapping[str, Any],
    wake_id: str,
    *,
    repository_path: str | Path | None = None,
) -> None:
    if not (state.get("active_batch") or {}).get("pending_repair"):
        return
    restored = state.get("pending_repair_restored") or {}
    if restored.get("wake_id") != wake_id:
        raise DefaultWakeError(
            "Restore and verify the pending repair before resolution or publication"
        )
    if repository_path is None:
        raise DefaultWakeError(
            "The current worktree is required to verify the restored pending repair"
        )
    checkout = str(Path(repository_path).resolve())
    if restored.get("checkout") != checkout:
        raise DefaultWakeError("Pending repair was not restored in this worktree")


def resolve_default_thread(
    checkpoint: dict[str, Any],
    *,
    wake_id: str,
    thread_id: str,
    graphql_call: Callable[[str, dict[str, object]], dict[str, Any]],
    repository_path: str | Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Resolve through the existing exact resolver after a local outcome."""
    from resolve_thread import resolve_exact_thread

    state = ensure_default_lifecycle(checkpoint)
    _require_active_wake(state, wake_id)
    if (
        state["automation_policy"]["thread_resolution"] != "auto"
        and not _policy_confirmation_allows(state, "thread_resolution")
    ):
        raise DefaultWakeError("The current policy requires confirmation before thread resolution")
    batch = state.get("active_batch")
    if not isinstance(batch, dict) or thread_id not in batch.get("targeted_thread_ids", []):
        raise DefaultWakeError("Thread is not in the active frozen batch")
    if thread_id not in batch.get("thread_outcomes", {}):
        raise DefaultWakeError("Record the thread outcome before exact resolution")
    _require_restored_repair(state, wake_id, repository_path=repository_path)
    if thread_id in batch.get("resolved_thread_ids", []):
        _consume_thread_resolution_confirmation(state)
        return state, {"id": thread_id, "isResolved": True, "alreadyResolved": True}

    def recheck_boundary() -> None:
        if state.get("active_wake_id") != wake_id or state.get("wake_phase") in {"paused", "terminal"}:
            raise DefaultWakeError("Wake boundary no longer permits PR mutation")
        if state.get("failure_latch"):
            raise DefaultWakeError("Wake is paused by a durable recovery latch")

    thread = resolve_exact_thread(
        repository=state["repository"],
        pr_number=state["pull_request_number"],
        thread_id=thread_id,
        expected_thread_ids=list(batch["targeted_thread_ids"]),
        reviewer_logins=list(batch.get("reviewer_logins") or DEFAULT_CODEX_LOGINS),
        expected_head_oid=batch.get("frozen_head_oid"),
        before_mutation=recheck_boundary,
        graphql_call=graphql_call,
    )
    state = record_resolved_thread(state, thread_id)
    _consume_thread_resolution_confirmation(state)
    result = {
        "next_action": "THREAD_RESOLVED",
        "reason_code": "exact_thread_resolution_confirmed",
        "thread_id": thread_id,
        "resolved": True,
        "mutation_occurred": not bool(thread.get("alreadyResolved")),
    }
    _note_wake_mutation(state, result["mutation_occurred"])
    _set_last_result(state, result)
    return state, result


def prepare_default_publication(
    checkpoint: dict[str, Any],
    *,
    wake_id: str,
    now: str,
    actual_head_oid: str,
    repository_path: str | Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Authorize commit/push only after exact resolution on the frozen head."""
    state = ensure_default_lifecycle(checkpoint)
    _require_active_wake(state, wake_id)
    _require_restored_repair(state, wake_id, repository_path=repository_path)
    if (
        state["automation_policy"]["publication"] != "auto"
        and not _policy_confirmation_allows(state, "aggregate_publication")
    ):
        result = _policy_pause(state, now=_iso(now), operation="aggregate_publication")
        state["last_wake_id"] = wake_id
        return state, result
    batch = state.get("active_batch")
    if not isinstance(batch, dict):
        raise DefaultWakeError("No active frozen batch exists")
    targeted = set(batch.get("targeted_thread_ids") or [])
    outcomes = set((batch.get("thread_outcomes") or {}).keys())
    resolved = set(batch.get("resolved_thread_ids") or [])
    missing_outcomes = sorted(targeted - outcomes)
    unresolved = sorted(targeted - resolved)
    if missing_outcomes or unresolved:
        evidence = {
            "missing_outcome_thread_ids": missing_outcomes,
            "unresolved_thread_ids": unresolved,
        }
        result = _pause(
            state,
            reason_code="publication_not_ready",
            now=_iso(now),
            evidence=evidence,
            action="PAUSE_RECOVERY",
        )
        state["last_wake_id"] = wake_id
        return state, result
    frozen_head_oid = batch.get("frozen_head_oid")
    if not isinstance(actual_head_oid, str) or not actual_head_oid:
        raise ValueError("Authoritative publication head is required")
    if actual_head_oid != frozen_head_oid:
        evidence = {
            "frozen_head_oid": frozen_head_oid,
            "actual_head_oid": actual_head_oid,
        }
        result = _pause(
            state,
            reason_code="publication_head_changed",
            now=_iso(now),
            evidence=evidence,
            action="PAUSE_RECOVERY",
        )
        state["last_wake_id"] = wake_id
        return state, result
    prepared_at = _iso(now)
    prior_publication = batch.get("publication")
    if not isinstance(prior_publication, dict):
        prior_publication = {}
    preparation_count = int(prior_publication.get("preparation_count") or 0) + 1
    batch["publication"] = {
        "status": "ready",
        "authorized_head_oid": frozen_head_oid,
        "first_prepared_at": prior_publication.get("first_prepared_at") or prepared_at,
        "prepared_at": prepared_at,
        "preparation_count": preparation_count,
    }
    state["wake_phase"] = "publication_ready"
    result = {
        "next_action": "PUBLISH_BATCH",
        "reason_code": "publication_prepared",
        "authorized_head_oid": frozen_head_oid,
        "prepared_at": prepared_at,
        "preparation_count": preparation_count,
        "mutation_occurred": False,
    }
    _set_last_result(state, result)
    return state, result


def record_default_trigger(
    checkpoint: dict[str, Any],
    *,
    wake_id: str,
    evidence: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    state = ensure_default_lifecycle(checkpoint)
    _require_active_wake(state, wake_id)
    if (
        state["automation_policy"]["review_trigger"] != "auto"
        and not _policy_confirmation_allows(state, "review_trigger")
    ):
        try:
            policy_now = _iso(evidence.get("created_at", datetime.now(UTC).isoformat()))
        except (TypeError, ValueError):
            policy_now = _iso(datetime.now(UTC))
        result = _policy_pause(state, now=policy_now, operation="review_trigger")
        return state, result
    if (state.get("last_decision") or {}).get("next_action") != "REQUEST_REVIEW":
        raise DefaultWakeError("This wake did not authorize a review trigger")
    head_oid = state.get("last_snapshot", {}).get("head_oid")
    if head_oid and evidence.get("attempted_head_oid") != head_oid:
        raise DefaultWakeError("Review trigger evidence is for a different head")
    event = _record_default_trigger_event(state, evidence)
    if event.get("status") != "emitted":
        result = _pause(
            state,
            reason_code="trigger_head_changed",
            now=evidence["created_at"],
            evidence=event,
            action="PAUSE_RECOVERY",
        )
    else:
        state["wake_phase"] = "trigger_recorded"
        _consume_policy_confirmation(state, "review_trigger")
        result = {
            "next_action": "REQUEST_REVIEW",
            "reason_code": "review_trigger_recorded",
            "trigger": event,
            "mutation_occurred": True,
        }
        _note_wake_mutation(state, True)
        _set_last_result(state, result)
    return state, result


def record_publication_result(
    checkpoint: dict[str, Any],
    *,
    wake_id: str,
    status: str,
    now: str,
    phase: str | None = None,
    pending_paths: list[str] | None = None,
    pending_commit: str | None = None,
    published_commit: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    state = ensure_default_lifecycle(checkpoint)
    _require_active_wake(state, wake_id)
    if status == "failed":
        if phase not in {"validation", "commit", "push"}:
            raise ValueError("Publication failure phase must be validation, commit, or push")
        state = record_publication_failure(
            state,
            phase=phase,
            pending_paths=pending_paths or [],
            pending_commit=pending_commit,
        )
        result = _pause(
            state,
            reason_code="publication_failed",
            now=_iso(now),
            evidence={"phase": phase, "pending_paths": pending_paths or [], "pending_commit": pending_commit},
            action="PAUSE_RECOVERY",
        )
        state["last_wake_id"] = wake_id
        return state, result
    if status != "succeeded":
        raise ValueError("Publication status must be succeeded or failed")
    if (
        state["automation_policy"]["publication"] != "auto"
        and not _policy_confirmation_allows(state, "aggregate_publication")
    ):
        result = _policy_pause(state, now=_iso(now), operation="aggregate_publication")
        state["last_wake_id"] = wake_id
        return state, result
    publication = (state.get("active_batch") or {}).get("publication") or {}
    if publication.get("status") != "ready" or publication.get("preparation_count", 0) < 2:
        result = _pause(
            state,
            reason_code="publication_not_prepared",
            now=_iso(now),
            evidence={
                "publication": deepcopy(publication),
                "published_commit": published_commit,
            },
            action="PAUSE_RECOVERY",
        )
        state["last_wake_id"] = wake_id
        return state, result
    state = record_publication_success(state, published_commit=published_commit)
    _consume_policy_confirmation(state, "aggregate_publication")
    state["retry_state"] = {
        "inline_attempts": 0,
        "wake_attempts": 0,
        "last_signature": None,
        "no_progress_attempts": 0,
    }
    state["wake_phase"] = "publication_succeeded"
    result = {
        "next_action": "WAIT_REVIEW",
        "reason_code": "aggregate_publication_succeeded",
        "published_commit": published_commit,
        "mutation_occurred": bool(published_commit),
    }
    _note_wake_mutation(state, result["mutation_occurred"])
    result["mutation_occurred"] = bool(state.get("wake_mutation_occurred"))
    _set_last_result(state, result)
    return state, result


def _validate_successor_rearmability(
    state: dict[str, Any], *, now: str
) -> dict[str, Any] | None:
    """Require the same durable completion evidence before authorizing a successor."""
    decision = state.get("last_decision") or {}
    action = decision.get("next_action")
    if action == "RUN_BATCH":
        publication = (state.get("active_batch") or {}).get("publication") or {}
        if publication.get("status") != "succeeded":
            return _pause(
                state,
                reason_code="batch_publication_incomplete",
                now=now,
                evidence=publication,
                action="PAUSE_RECOVERY",
            )
    elif action == "REQUEST_REVIEW":
        head_oid = state.get("last_snapshot", {}).get("head_oid")
        event = state.get("trigger_events", {}).get(head_oid, {})
        if event.get("status") != "emitted":
            return _pause(
                state,
                reason_code="review_trigger_not_confirmed",
                now=now,
                evidence=event,
                action="PAUSE_RECOVERY",
            )
    elif action not in {"WAIT_REVIEW", "WAIT_RETRY"}:
        return _pause(
            state,
            reason_code="successor_not_rearmable",
            now=now,
            evidence={"last_decision": deepcopy(decision)},
            action="PAUSE_RECOVERY",
        )
    return None


def _retirement_rearm_proof(state: Mapping[str, Any]) -> dict[str, Any]:
    """Freeze the original rearmable result instead of trusting later recovery."""
    decision = state.get("last_decision")
    if not isinstance(decision, Mapping):
        raise DefaultWakeError("The wake has no durable rearmable decision")
    action = decision.get("next_action")
    if action == "WAIT_REVIEW":
        publication = (state.get("active_batch") or {}).get("publication") or {}
        if publication.get("status") == "succeeded":
            return {
                "action": "WAIT_REVIEW",
                "source_action": "RUN_BATCH",
                "proof": {"publication": deepcopy(publication)},
            }
        return {
            "action": "WAIT_REVIEW",
            "source_action": "WAIT_REVIEW",
            "proof": {"decision": deepcopy(dict(decision))},
        }
    if action == "WAIT_RETRY":
        batch = state.get("active_batch")
        pending_repair = batch.get("pending_repair") if isinstance(batch, Mapping) else None
        if not isinstance(pending_repair, Mapping):
            raise DefaultWakeError("Retry retirement requires a durable pending repair")
        return {
            "action": "WAIT_RETRY",
            "source_action": "WAIT_RETRY",
            "proof": {"pending_repair": deepcopy(dict(pending_repair))},
        }
    if action == "REQUEST_REVIEW":
        snapshot = state.get("last_snapshot")
        head_oid = snapshot.get("head_oid") if isinstance(snapshot, Mapping) else None
        event = (
            (state.get("trigger_events") or {}).get(head_oid)
            if isinstance(head_oid, str)
            else None
        )
        if not isinstance(event, Mapping) or event.get("status") != "emitted":
            raise DefaultWakeError("Review-trigger retirement requires emitted evidence")
        return {
            "action": "REQUEST_REVIEW",
            "source_action": "REQUEST_REVIEW",
            "proof": {"head_oid": head_oid, "trigger": deepcopy(dict(event))},
        }
    if action == "RUN_BATCH":
        publication = (state.get("active_batch") or {}).get("publication") or {}
        if publication.get("status") != "succeeded":
            raise DefaultWakeError("Batch retirement requires completed publication")
        return {
            "action": "WAIT_REVIEW",
            "source_action": "RUN_BATCH",
            "proof": {"publication": deepcopy(publication)},
        }
    raise DefaultWakeError("The wake has no rearmable completion proof")


def _retirement_pause(
    state: dict[str, Any],
    *,
    reason_code: str,
    now: str,
    evidence: Any = None,
) -> dict[str, Any]:
    """Fail closed after predecessor retirement without fabricating PAUSED state."""
    record = state.get("task_retirement")
    if not isinstance(record, Mapping) or record.get("phase") not in {"confirmed", "unknown"}:
        raise DefaultWakeError("Retirement-aware pause requires retirement evidence")
    successor = record.get("successor")
    if record.get("phase") == "unknown":
        state["scheduled_task_id"] = None
        state["scheduled_task_disposition"] = "UNKNOWN"
    elif not isinstance(successor, Mapping):
        state["scheduled_task_id"] = None
        state["scheduled_task_disposition"] = "NONE"
    elif _nonempty_task_id(successor.get("task_id")) is None:
        state["scheduled_task_id"] = None
        state["scheduled_task_disposition"] = "UNKNOWN"
    elif successor.get("status") == "unknown":
        state["scheduled_task_id"] = successor["task_id"]
        state["scheduled_task_disposition"] = "UNKNOWN"
    else:
        state["scheduled_task_id"] = successor["task_id"]
        state["scheduled_task_disposition"] = "PAUSED"
        if isinstance(successor, dict):
            successor["status"] = "paused"
    state["wake_phase"] = "retirement_recovery"
    state["failure_latch"] = {
        "reason_code": reason_code,
        "latched_at": now,
        "evidence": deepcopy(evidence),
    }
    result = _decision(
        "PAUSE_RECOVERY",
        reason_code,
        evidence=deepcopy(evidence),
        mutation_occurred=bool(state.get("wake_mutation_occurred")),
    )
    _set_last_result(state, result)
    return result


def prepare_task_retirement(
    checkpoint: dict[str, Any],
    *,
    wake_id: str,
    now: str,
    worktree_cleanup_confirmed: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Persist the exact irreversible-retirement boundary before host deletion."""
    state = ensure_default_lifecycle(checkpoint)
    _require_active_wake(state, wake_id, allow_retry_completion=True)
    record = state.get("task_retirement")
    if not isinstance(record, Mapping) or record.get("phase") != "registered":
        result = _pause(
            state,
            reason_code="retirement_provenance_missing",
            now=_iso(now),
            evidence={"task_retirement": deepcopy(record)},
            action="PAUSE_RECOVERY",
        )
        return state, result
    if record.get("wake_id") != wake_id:
        raise DefaultWakeError("Retirement target does not belong to the active wake")
    if worktree_cleanup_confirmed is not True:
        result = _pause(
            state,
            reason_code="worktree_cleanup_unconfirmed",
            now=_iso(now),
            evidence={"worktree_cleanup_confirmed": False},
            action="PAUSE_RECOVERY",
        )
        return state, result
    try:
        proof = _retirement_rearm_proof(state)
    except DefaultWakeError as error:
        result = _pause(
            state,
            reason_code="retirement_not_rearmable",
            now=_iso(now),
            evidence={"error": str(error)},
            action="PAUSE_RECOVERY",
        )
        return state, result
    pending = deepcopy(dict(record))
    pending["phase"] = "pending"
    pending["rearm"] = proof
    pending["worktree_cleanup_confirmed"] = True
    pending["prepared_at"] = _iso(now)
    state["task_retirement"] = pending
    state["wake_phase"] = "retirement_pending"
    result = _decision(
        "RETIREMENT_PENDING",
        "task_retirement_prepared",
        wake_id=wake_id,
        scheduled_task_id=pending["task_id"],
        role=pending["role"],
        mutation_occurred=False,
    )
    state["last_wake_result"] = deepcopy(result)
    return state, result


def confirm_task_retirement(
    checkpoint: dict[str, Any],
    *,
    wake_id: str,
    now: str,
    task_id: str,
    role: str,
    outcome: str,
    evidence: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Record an exact deletion result; only confirmed deletion may rearm."""
    if outcome not in {"confirmed", "non_deletion", "unknown"}:
        raise ValueError("Task retirement outcome is invalid")
    state = ensure_default_lifecycle(checkpoint)
    record = state.get("task_retirement")
    if not isinstance(record, Mapping):
        raise DefaultWakeError("Task retirement evidence is missing")
    if (
        record.get("wake_id") != wake_id
        or record.get("task_id") != task_id
        or record.get("role") != role
        or record.get("phase") not in {"pending", "unknown", "confirmed"}
    ):
        raise DefaultWakeError("Task retirement result does not match the pending predecessor")
    if record.get("phase") == "confirmed" and outcome == "confirmed":
        return state, _decision(
            "RETIREMENT_CONFIRMED",
            "task_retirement_already_confirmed",
            scheduled_task_id=task_id,
            mutation_occurred=False,
        )
    if outcome == "non_deletion":
        state["scheduled_task_id"] = task_id
        state["scheduled_task_disposition"] = "PAUSED"
        state["task_retirement"] = {**deepcopy(dict(record)), "phase": "pending"}
        return state, _pause(
            state,
            reason_code="task_retirement_not_confirmed",
            now=_iso(now),
            evidence=deepcopy(dict(evidence or {})),
            action="PAUSE_RECOVERY",
        )
    updated = deepcopy(dict(record))
    updated["retirement_checked_at"] = _iso(now)
    updated["retirement_evidence"] = deepcopy(dict(evidence or {}))
    if outcome == "unknown":
        updated["phase"] = "unknown"
        state["task_retirement"] = updated
        return state, _retirement_pause(
            state,
            reason_code="task_retirement_unknown",
            now=_iso(now),
            evidence=updated["retirement_evidence"],
        )
    updated["phase"] = "confirmed"
    updated["confirmed_at"] = _iso(now)
    state["task_retirement"] = updated
    state["scheduled_task_id"] = None
    state["scheduled_task_disposition"] = "NONE"
    state["wake_phase"] = "retirement_recovery"
    result = _decision(
        "RETIREMENT_CONFIRMED",
        "task_retirement_confirmed",
        scheduled_task_id=task_id,
        role=role,
        mutation_occurred=False,
    )
    state["last_wake_result"] = deepcopy(result)
    return state, result


def reconcile_task_retirement(
    checkpoint: dict[str, Any],
    *,
    wake_id: str,
    now: str,
    task_id: str,
    role: str,
    lookup: str,
    evidence: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Retry only a named pending/unknown retirement through exact readback."""
    if lookup not in {"PRESENT", "AUTHORITATIVE_NOT_FOUND", "READBACK_UNKNOWN"}:
        raise ValueError("Retirement lookup result is invalid")
    state = ensure_default_lifecycle(checkpoint)
    record = state.get("task_retirement")
    if not isinstance(record, Mapping) or record.get("phase") not in {"pending", "unknown"}:
        raise DefaultWakeError("No pending exact retirement can be reconciled")
    if record.get("wake_id") != wake_id or record.get("task_id") != task_id or record.get("role") != role:
        raise DefaultWakeError("Retirement reconciliation does not match the exact predecessor")
    if lookup == "AUTHORITATIVE_NOT_FOUND":
        return confirm_task_retirement(
            state,
            wake_id=wake_id,
            now=now,
            task_id=task_id,
            role=role,
            outcome="confirmed",
            evidence={"lookup": lookup, **deepcopy(dict(evidence or {}))},
        )
    if lookup == "PRESENT":
        return confirm_task_retirement(
            state,
            wake_id=wake_id,
            now=now,
            task_id=task_id,
            role=role,
            outcome="non_deletion",
            evidence={"lookup": lookup, **deepcopy(dict(evidence or {}))},
        )
    return confirm_task_retirement(
        state,
        wake_id=wake_id,
        now=now,
        task_id=task_id,
        role=role,
        outcome="unknown",
        evidence={"lookup": lookup, **deepcopy(dict(evidence or {}))},
    )


def record_retirement_successor_creation(
    checkpoint: dict[str, Any],
    *,
    wake_id: str,
    now: str,
    outcome: str,
    task_id: str | None = None,
    completion_anchor: str | None = None,
    evidence: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Durably retain a known successor ID before its fallible readback."""
    if outcome not in {
        "CREATED_EXACT_ID",
        "AUTHORITATIVE_NO_SUCCESSOR",
        "SUCCESSOR_CREATION_UNKNOWN",
    }:
        raise ValueError("Successor creation outcome is invalid")
    state = ensure_default_lifecycle(checkpoint)
    record = state.get("task_retirement")
    if (
        not isinstance(record, Mapping)
        or record.get("phase") != "confirmed"
        or record.get("wake_id") != wake_id
    ):
        raise DefaultWakeError("Successor creation requires confirmed predecessor retirement")
    if record.get("successor") is not None:
        raise DefaultWakeError("A known successor must be reused rather than recreated")
    intent = state.get("creation_intent")
    if isinstance(intent, Mapping) and (
        intent.get("role") != "successor"
        or intent.get("wake_id") != wake_id
        or intent.get("status") not in {"PENDING", "ID_RECORDED"}
    ):
        raise DefaultWakeError("Successor creation result does not match its intent")
    updated = deepcopy(dict(record))
    if outcome == "AUTHORITATIVE_NO_SUCCESSOR":
        if isinstance(intent, Mapping):
            completed_intent = deepcopy(dict(intent))
            completed_intent["status"] = "AUTHORITATIVE_NO_SUCCESSOR"
            completed_intent["resolved_at"] = _iso(now)
            state["creation_intent"] = completed_intent
        updated["successor_creation"] = {
            "outcome": outcome,
            "recorded_at": _iso(now),
            "evidence": deepcopy(dict(evidence or {})),
        }
        state["task_retirement"] = updated
        return state, _retirement_pause(
            state,
            reason_code="successor_creation_rejected",
            now=_iso(now),
            evidence=updated["successor_creation"],
        )
    if outcome == "SUCCESSOR_CREATION_UNKNOWN":
        if isinstance(intent, Mapping):
            unresolved_intent = deepcopy(dict(intent))
            unresolved_intent["status"] = "UNKNOWN"
            unresolved_intent["resolved_at"] = _iso(now)
            state["creation_intent"] = unresolved_intent
        updated["successor"] = {
            "task_id": None,
            "status": "creation_unknown",
            "recorded_at": _iso(now),
            "evidence": deepcopy(dict(evidence or {})),
        }
        state["task_retirement"] = updated
        return state, _retirement_pause(
            state,
            reason_code="successor_creation_unknown",
            now=_iso(now),
            evidence=updated["successor"],
        )
    exact_id = _nonempty_task_id(task_id)
    if exact_id is None or not isinstance(completion_anchor, str):
        raise ValueError("An exact successor ID and completion anchor are required")
    _utc(completion_anchor)
    updated["successor"] = {
        "task_id": exact_id,
        "status": "unknown",
        "completion_anchor": _iso(completion_anchor),
        "recorded_at": _iso(now),
        "evidence": deepcopy(dict(evidence or {})),
    }
    if isinstance(intent, Mapping):
        recorded_intent = deepcopy(dict(intent))
        recorded_intent["status"] = "ID_RECORDED"
        recorded_intent["task_id"] = exact_id
        recorded_intent["id_recorded_at"] = _iso(now)
        updated["successor"]["creation_intent"] = recorded_intent
        state["creation_intent"] = recorded_intent
    state["task_retirement"] = updated
    state["scheduled_task_id"] = exact_id
    state["scheduled_task_disposition"] = "UNKNOWN"
    state["wake_phase"] = "retirement_recovery"
    result = _decision(
        "SUCCESSOR_CREATED",
        "successor_identity_recorded",
        scheduled_task_id=exact_id,
        mutation_occurred=False,
    )
    state["last_wake_result"] = deepcopy(result)
    return state, result


def record_retirement_successor_readback(
    checkpoint: dict[str, Any],
    *,
    wake_id: str,
    now: str,
    task: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Validate a known paused successor against durable handoff evidence."""
    state = ensure_default_lifecycle(checkpoint)
    record = state.get("task_retirement")
    if (
        not isinstance(record, Mapping)
        or record.get("phase") != "confirmed"
        or record.get("wake_id") != wake_id
        or not isinstance(record.get("successor"), Mapping)
    ):
        raise DefaultWakeError("Successor readback requires a confirmed exact retirement")
    successor = record["successor"]
    task_id = _nonempty_task_id(successor.get("task_id"))
    if task_id is None:
        raise DefaultWakeError("A successor without an exact ID cannot be read back")
    handoff = record["handoff"]
    observed_id = task.get("id", task.get("task_id"))
    expected = {
        "id": task_id,
        "status": "PAUSED",
        "prompt": handoff["prompt"],
        "prompt_sha256": handoff["prompt_sha256"],
        "scheduler_kind": handoff["scheduler_kind"],
        "conversation_mode": handoff["conversation_mode"],
        "target_thread_id": handoff["target_thread_id"],
        "model": handoff["model"],
        "reasoning_effort": handoff["reasoning_effort"],
        "cadence_seconds": handoff["cadence_seconds"],
    }
    for key, expected_value in expected.items():
        actual = observed_id if key == "id" else task.get(key)
        if actual != expected_value:
            raise DefaultWakeError("Successor readback does not match immutable handoff")
    created_at = task.get("created_at")
    first_run = task.get("first_run")
    if not isinstance(created_at, str) or not isinstance(first_run, str):
        raise DefaultWakeError("Successor readback lacks persisted schedule metadata")
    created = _utc(created_at)
    anchor = _utc(str(successor.get("completion_anchor")))
    if _truncate_to_scheduler_precision(created) < _truncate_to_scheduler_precision(anchor):
        raise DefaultWakeError("Successor creation predates the handoff anchor")
    expected_first_run = _truncate_to_scheduler_precision(
        created + timedelta(seconds=handoff["cadence_seconds"])
    ).isoformat()
    if not _schedule_times_match(
        expected_first_run,
        first_run,
        ordered=True,
        scheduler_precision=True,
    ):
        raise DefaultWakeError("Successor first run does not match persisted creation")
    updated = deepcopy(dict(record))
    updated_successor = {
        **deepcopy(dict(successor)),
        "status": "paused",
        "created_at": _iso(created_at),
        "first_run": _iso(first_run),
        "readback_at": _iso(now),
        "readback": deepcopy(dict(task)),
    }
    intent = state.get("creation_intent")
    if isinstance(intent, Mapping) and intent.get("task_id") == task_id:
        verified_intent = deepcopy(dict(intent))
        verified_intent["status"] = "VERIFIED"
        verified_intent["verified_at"] = _iso(now)
        updated_successor["creation_intent"] = verified_intent
        state["creation_intent"] = None
    updated["successor"] = updated_successor
    state["task_retirement"] = updated
    state["scheduled_task_id"] = task_id
    state["scheduled_task_disposition"] = "PAUSED"
    state["wake_phase"] = "successor_ready"
    result = _decision(
        "SUCCESSOR_READY",
        "successor_readback_verified",
        scheduled_task_id=task_id,
        mutation_occurred=False,
    )
    state["last_wake_result"] = deepcopy(result)
    return state, result


def record_retirement_successor_pause(
    checkpoint: dict[str, Any],
    *,
    wake_id: str,
    now: str,
    task_id: str,
    confirmed: bool,
    evidence: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Record the only safe fallback after known-successor readback failure."""
    state = ensure_default_lifecycle(checkpoint)
    record = state.get("task_retirement")
    successor = record.get("successor") if isinstance(record, Mapping) else None
    if (
        not isinstance(record, Mapping)
        or record.get("phase") != "confirmed"
        or record.get("wake_id") != wake_id
        or not isinstance(successor, Mapping)
        or successor.get("task_id") != task_id
    ):
        raise DefaultWakeError("Successor pause evidence does not match retirement handoff")
    updated = deepcopy(dict(record))
    updated_successor = deepcopy(dict(successor))
    updated_successor["status"] = "paused" if confirmed else "unknown"
    updated_successor["pause_evidence"] = deepcopy(dict(evidence or {}))
    updated["successor"] = updated_successor
    state["task_retirement"] = updated
    state["scheduled_task_id"] = task_id
    state["scheduled_task_disposition"] = "PAUSED" if confirmed else "UNKNOWN"
    state["wake_phase"] = "retirement_recovery"
    result = _decision(
        "SUCCESSOR_PAUSED" if confirmed else "PAUSE_RECOVERY",
        "successor_pause_confirmed" if confirmed else "successor_status_unknown",
        scheduled_task_id=task_id,
        mutation_occurred=False,
    )
    state["last_wake_result"] = deepcopy(result)
    return state, result


def recover_retirement_successor(
    checkpoint: dict[str, Any],
    *,
    wake_id: str,
    now: str,
    action: str,
    task: Mapping[str, Any],
    delivery_observed: bool,
    activation_confirmed: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Resume only a verified PAUSED successor handoff, never PR work.

    The caller supplies fresh exact task readback after the user-authorized host
    operation.  The latch is consumed only in this one returned checkpoint
    replacement, so no durable unlatched active wake can run ordinary work.
    """
    if action not in {"authorize", "finalize"}:
        raise ValueError("Retirement successor recovery action is invalid")
    state = ensure_default_lifecycle(checkpoint)
    record = state.get("task_retirement")
    if (
        not isinstance(record, Mapping)
        or record.get("phase") != "confirmed"
        or record.get("wake_id") != wake_id
        or not isinstance(record.get("rearm"), Mapping)
        or not isinstance(record.get("successor"), Mapping)
    ):
        raise DefaultWakeError("Retirement successor recovery evidence is incomplete")
    successor = record["successor"]
    task_id = _nonempty_task_id(successor.get("task_id"))
    if task_id is None or successor.get("status") not in {"paused", "authorized"}:
        raise DefaultWakeError("Retirement successor is not a known paused task")
    if delivery_observed or activation_confirmed or task.get("status") != "PAUSED":
        raise DefaultWakeError("Recovered successor must be paused and undelivered")
    # Validate all immutable static and persisted timestamp fields against a
    # copy first.  The live state stays unchanged if that check rejects.
    probe = deepcopy(state)
    record_retirement_successor_readback(
        probe,
        wake_id=wake_id,
        now=now,
        task=task,
    )
    latch = state.get("failure_latch")
    if latch is not None:
        if not isinstance(latch, Mapping) or latch.get("reason_code") not in {
            "task_retirement_unknown",
            "successor_creation_rejected",
            "successor_creation_unknown",
            "completion_anchor_unavailable",
            "successor_authorization_unconfirmed",
            "successor_finalization_interrupted",
            "successor_activation_unconfirmed",
            "successor_cleanup_unconfirmed",
        }:
            raise DefaultWakeError("Retirement recovery cannot consume an unrelated latch")
        state["failure_latch"] = None
    rearm = record["rearm"]
    recovered_action = rearm.get("action")
    if recovered_action not in REARM_ACTIONS:
        raise DefaultWakeError("Retirement rearm proof is invalid")
    state["last_decision"] = {
        "next_action": recovered_action,
        "reason_code": "recovered_retirement_handoff",
        "proof": deepcopy(rearm.get("proof")),
    }
    if action == "authorize":
        return authorize_successor(
            state,
            wake_id=wake_id,
            now=successor["completion_anchor"],
            scheduled_created_at=successor["created_at"],
            scheduled_first_run=successor["first_run"],
            scheduled_task_id=task_id,
        )
    authorization = state.get("successor_authorization")
    if (
        not isinstance(authorization, Mapping)
        or authorization.get("wake_id") != wake_id
        or authorization.get("scheduled_task_id") != task_id
        or state.get("wake_phase") not in {"successor_authorized", "retirement_recovery"}
    ):
        raise DefaultWakeError("Retirement finalization requires exact durable authorization")
    state["wake_phase"] = "successor_authorized"
    return complete_wake(
        state,
        wake_id=wake_id,
        now=successor["completion_anchor"],
        schedule_next_wake=lambda _: successor["first_run"],
        schedule_anchor_created_at=successor["created_at"],
        scheduled_task_id=task_id,
        require_schedule_anchor=True,
    )


def complete_wake(
    checkpoint: dict[str, Any],
    *,
    wake_id: str,
    now: str,
    cadence_seconds: int | None = None,
    schedule_next_wake: Callable[[str], object] | None = None,
    schedule_anchor_created_at: str | None = None,
    scheduled_task_id: str | None = None,
    completion_failure: Mapping[str, Any] | None = None,
    require_schedule_anchor: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Complete one wake after proving schedule anchoring and successor identity.

    The public CLI passes ``require_schedule_anchor=True`` for its standalone
    reanchor path. The default remains compatible with injected direct-first-run
    callbacks used by older integrations.
    """
    now = _iso(now)
    state = ensure_default_lifecycle(checkpoint)
    persisted_cadence = state["automation_policy"]["cadence_seconds"]
    if cadence_seconds is not None and cadence_seconds != persisted_cadence:
        raise ValueError(
            "Completion cadence must match the persisted automation policy"
        )
    effective_cadence = persisted_cadence
    if isinstance(effective_cadence, bool) or not isinstance(effective_cadence, int) or effective_cadence <= 0:
        raise ValueError("Cadence must be positive")
    if scheduled_task_id is not None and (
        not isinstance(scheduled_task_id, str) or not scheduled_task_id.strip()
    ):
        raise ValueError("Scheduled task ID must be a non-empty string")
    if schedule_anchor_created_at is not None and schedule_next_wake is None:
        raise ValueError("A schedule creation anchor requires re-anchor confirmation")
    authorization = state.get("successor_authorization")
    authorization_for_wake = (
        isinstance(authorization, Mapping)
        and authorization.get("wake_id") == wake_id
    )
    authorized_handoff = (
        authorization_for_wake
        and state.get("wake_phase") in {"successor_authorized", "successor_finalized"}
        and (
            state.get("active_wake_id") == wake_id
            or (
                state.get("active_wake_id") is None
                and state.get("wake_phase") == "successor_finalized"
            )
        )
    )
    if (
        state.get("last_wake_id") == wake_id
        and state.get("active_wake_id") is None
        and state.get("last_wake_result")
        and (not authorized_handoff or completion_failure is None)
    ):
        return state, deepcopy(state["last_wake_result"])
    if authorized_handoff:
        if state.get("wake_phase") not in {
            "successor_authorized",
            "successor_finalized",
        }:
            raise DefaultWakeError("Successor authorization is not ready for completion")
    else:
        final_retirement_terminal = (
            state["automation_policy"].get("max_wakes") is not None
            and state.get("wake_count", 0) >= state["automation_policy"].get("max_wakes")
            and isinstance(state.get("task_retirement"), Mapping)
            and state["task_retirement"].get("phase") == "confirmed"
            and state["task_retirement"].get("successor") is None
            and scheduled_task_id is None
        )
        _require_active_wake(
            state,
            wake_id,
            allow_retry_completion=True,
            allow_handoff_only=completion_failure is not None or final_retirement_terminal,
        )
    if (
        require_schedule_anchor
        and schedule_anchor_created_at is not None
        and scheduled_task_id is not None
        and not authorized_handoff
    ):
        result = _pause(
            state,
            reason_code="successor_authorization_required",
            now=now,
            evidence={
                "wake_id": wake_id,
                "scheduled_task_id": scheduled_task_id,
                "scheduled_task_created_at": schedule_anchor_created_at,
                "successor_authorization": authorization,
            },
            action="PAUSE_RECOVERY",
        )
        state["last_wake_id"] = wake_id
        return state, result
    decision = state.get("last_decision") or {}
    action = decision.get("next_action")
    mutation_occurred = bool(state.get("wake_mutation_occurred")) or bool(
        decision.get("mutation_occurred")
    )
    if completion_failure is not None:
        reason_code = completion_failure.get("reason_code")
        if not isinstance(reason_code, str) or not reason_code.strip():
            raise ValueError("Completion failure reason code is required")
        result = _pause(
            state,
            reason_code=reason_code,
            now=now,
            evidence=completion_failure.get("evidence"),
            action="PAUSE_RECOVERY",
            mutation_occurred=mutation_occurred,
        )
        if authorized_handoff and not (
            isinstance(state.get("task_retirement"), Mapping)
            and state["task_retirement"].get("phase") == "confirmed"
        ):
            state["successor_authorization"] = None
        state["last_wake_id"] = wake_id
        return state, result
    if action == "RUN_BATCH":
        publication = (state.get("active_batch") or {}).get("publication") or {}
        if publication.get("status") != "succeeded":
            result = _pause(
                state,
                reason_code="batch_publication_incomplete",
                now=now,
                evidence=publication,
                action="PAUSE_RECOVERY",
            )
            state["last_wake_id"] = wake_id
            return state, result
        mutation_occurred = mutation_occurred or bool(publication.get("published_commit"))
        action = "WAIT_REVIEW"
    elif action == "REQUEST_REVIEW":
        head_oid = state.get("last_snapshot", {}).get("head_oid")
        event = state.get("trigger_events", {}).get(head_oid, {})
        if event.get("status") != "emitted":
            result = _pause(
                state,
                reason_code="review_trigger_not_confirmed",
                now=now,
                evidence=event,
            )
            state["last_wake_id"] = wake_id
            return state, result
    elif action in {"WAIT_REVIEW", "WAIT_RETRY"}:
        pass
    else:
        raise DefaultWakeError("The wake has no rearmable WAIT_REVIEW or REQUEST_REVIEW result")

    # A final admitted wake ends the chain after its exact predecessor has
    # already been retired.  Never manufacture a sixth delivery whose first
    # operation would merely discover the exhausted budget.
    maximum_wakes = state["automation_policy"].get("max_wakes")
    retirement = state.get("task_retirement")
    if (
        maximum_wakes is not None
        and state.get("wake_count", 0) >= maximum_wakes
        and isinstance(retirement, Mapping)
        and retirement.get("phase") == "confirmed"
        and retirement.get("successor") is None
        and scheduled_task_id is None
    ):
        state["wake_completed_at"] = now
        state["active_wake_id"] = None
        state["next_not_before"] = None
        state["scheduled_task_disposition"] = "PAUSED"
        state["wake_phase"] = "terminal"
        state["last_wake_id"] = wake_id
        result = _decision(
            "STOP_POLICY_LIMIT",
            "maximum_wakes_reached_after_admission",
            limit=maximum_wakes,
            wake_count=state.get("wake_count", 0),
            mutation_occurred=mutation_occurred,
        )
        _set_last_result(state, result)
        return state, result

    completed_at = _utc(now)
    next_not_before = _ceil_to_second(
        completed_at + timedelta(seconds=effective_cadence)
    ).isoformat()
    expected_first_run = next_not_before
    if (
        require_schedule_anchor
        and schedule_next_wake is not None
        and schedule_anchor_created_at is None
    ):
        return_state_result = _pause(
            state,
            reason_code="scheduled_task_anchor_missing",
            now=now,
            evidence={
                "wake_completed_at": now,
                "scheduled_task_created_at": None,
                "scheduled_task_id": scheduled_task_id,
            },
            action="PAUSE_RECOVERY",
            mutation_occurred=mutation_occurred,
        )
        state["next_not_before"] = next_not_before
        state["last_wake_id"] = wake_id
        return state, return_state_result
    state["wake_completed_at"] = now
    state["next_not_before"] = next_not_before
    state["active_wake_id"] = None
    result = _decision(
        action,
        "wake_completed",
        wake_id=wake_id,
        wake_completed_at=now,
        next_not_before=next_not_before,
        mutation_occurred=mutation_occurred,
    )
    if scheduled_task_id is not None:
        result["scheduled_task_id"] = scheduled_task_id
    observed_first_run: object = None
    if schedule_next_wake is not None:
        try:
            observed_first_run = schedule_next_wake(next_not_before)
        except Exception:
            observed_first_run = None
    if observed_first_run is None or isinstance(observed_first_run, bool):
        return_state_result = _pause(
            state,
            reason_code="scheduled_task_reanchor_unavailable",
            now=now,
            evidence={
                "expected_first_run": next_not_before,
                "observed_first_run": observed_first_run,
            },
            mutation_occurred=mutation_occurred,
        )
        state["next_not_before"] = next_not_before
        state["last_wake_id"] = wake_id
        return state, return_state_result

    raw_observed_first_run = observed_first_run
    parsed_anchor: datetime | None = None
    if schedule_anchor_created_at is not None:
        try:
            parsed_anchor = _utc(schedule_anchor_created_at)
        except (TypeError, ValueError):
            parsed_anchor = None
        # Scheduler task metadata is represented at whole-second precision.
        # Compare at that precision so a task created later in the same
        # represented second is not rejected because ``now`` retained
        # microseconds.
        if parsed_anchor is None or _truncate_to_scheduler_precision(
            parsed_anchor
        ) < _truncate_to_scheduler_precision(completed_at):
            return_state_result = _pause(
                state,
                reason_code="scheduled_task_anchor_mismatch",
                now=now,
                evidence={
                    "wake_completed_at": now,
                    "scheduled_task_created_at": schedule_anchor_created_at,
                },
                mutation_occurred=mutation_occurred,
            )
            state["next_not_before"] = next_not_before
            state["last_wake_id"] = wake_id
            return state, return_state_result
        # The scheduler's persisted first run is the lifecycle deadline for an
        # anchored successor.  Using ceil(completed_at + cadence) can be one
        # represented second later than truncate(created_at + cadence).
        expected_first_run = _truncate_to_scheduler_precision(
            parsed_anchor + timedelta(seconds=effective_cadence)
        ).isoformat()
        next_not_before = expected_first_run
        state["next_not_before"] = next_not_before
        result["next_not_before"] = next_not_before
        result["scheduled_task_created_at"] = parsed_anchor.isoformat()
    try:
        observed_first_run = _utc(str(raw_observed_first_run))
    except (TypeError, ValueError):
        observed_first_run = None
    if observed_first_run is None or not _schedule_times_match(
        expected_first_run,
        observed_first_run,
        ordered=True,
        scheduler_precision=schedule_anchor_created_at is not None,
    ):
        return_state_result = _pause(
            state,
            reason_code="scheduled_task_reanchor_mismatch",
            now=now,
            evidence={
                "expected_first_run": expected_first_run,
                "observed_first_run": raw_observed_first_run,
            },
            mutation_occurred=mutation_occurred,
        )
        state["next_not_before"] = next_not_before
        state["last_wake_id"] = wake_id
        return state, return_state_result

    if isinstance(authorization, Mapping):
        authorization_matches = authorization.get("wake_id") == wake_id
        expected_task_id = authorization.get("scheduled_task_id")
        expected_created_at = authorization.get("scheduled_created_at")
        expected_authorized_first_run = authorization.get("scheduled_first_run")
        try:
            authorization_matches = authorization_matches and (
                expected_task_id == scheduled_task_id
                and isinstance(expected_created_at, str)
                and schedule_anchor_created_at is not None
                and _truncate_to_scheduler_precision(_utc(expected_created_at))
                == _truncate_to_scheduler_precision(_utc(schedule_anchor_created_at))
                and isinstance(expected_authorized_first_run, str)
                and isinstance(raw_observed_first_run, str)
                and _truncate_to_scheduler_precision(_utc(expected_authorized_first_run))
                == _truncate_to_scheduler_precision(_utc(raw_observed_first_run))
            )
        except (TypeError, ValueError):
            authorization_matches = False
        if not authorization_matches:
            return_state_result = _pause(
                state,
                reason_code="successor_authorization_mismatch",
                now=now,
                evidence={
                    "authorized": {
                        "wake_id": authorization.get("wake_id"),
                        "scheduled_task_id": expected_task_id,
                        "scheduled_created_at": expected_created_at,
                        "scheduled_first_run": expected_authorized_first_run,
                    },
                    "provided": {
                        "wake_id": wake_id,
                        "scheduled_task_id": scheduled_task_id,
                        "scheduled_created_at": schedule_anchor_created_at,
                        "scheduled_first_run": raw_observed_first_run,
                    },
                },
                action="PAUSE_RECOVERY",
                mutation_occurred=mutation_occurred,
            )
            state["next_not_before"] = next_not_before
            state["last_wake_id"] = wake_id
            return state, return_state_result

    if scheduled_task_id is None:
        return_state_result = _pause(
            state,
            reason_code="scheduled_task_identity_missing",
            now=now,
            evidence={
                "expected_first_run": next_not_before,
                "observed_first_run": raw_observed_first_run,
                "scheduled_task_id": scheduled_task_id,
            },
            action="PAUSE_RECOVERY",
            mutation_occurred=mutation_occurred,
        )
        state["next_not_before"] = next_not_before
        state["last_wake_id"] = wake_id
        return state, return_state_result

    state["scheduled_task_disposition"] = (
        "AUTHORIZED" if authorized_handoff else "ACTIVE"
    )
    state["scheduled_task_kind"] = "standalone"
    if scheduled_task_id is not None:
        state["scheduled_task_id"] = scheduled_task_id
    retirement = state.get("task_retirement")
    if isinstance(retirement, Mapping) and retirement.get("phase") == "confirmed":
        successor = retirement.get("successor")
        if isinstance(successor, Mapping) and successor.get("task_id") == scheduled_task_id:
            updated_retirement = deepcopy(dict(retirement))
            updated_successor = deepcopy(dict(successor))
            updated_successor["status"] = "authorized"
            updated_retirement["successor"] = updated_successor
            state["task_retirement"] = updated_retirement
    elif isinstance(retirement, Mapping) and retirement.get("phase") == "registered":
        # Older injected direct-first-run callbacks predate the host-side
        # exact-delete contract.  They remain test-only compatibility input;
        # never treat their generic task pointer as setup provenance.
        state["task_retirement"] = None
    state["wake_phase"] = (
        "successor_finalized"
        if authorized_handoff
        else ("retry_waiting" if action == "WAIT_RETRY" else "completed")
    )
    state["last_wake_id"] = wake_id
    _set_last_result(state, result)
    return state, result


def authorize_successor(
    checkpoint: dict[str, Any],
    *,
    wake_id: str,
    now: str,
    scheduled_created_at: str,
    scheduled_first_run: str,
    scheduled_task_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Persist verified successor authority while the host task remains paused."""
    state = ensure_default_lifecycle(checkpoint)
    _require_active_wake(
        state,
        wake_id,
        allow_retry_completion=True,
        allow_handoff_only=True,
    )
    if not isinstance(scheduled_task_id, str) or not scheduled_task_id.strip():
        raise ValueError("Scheduled task ID must be a non-empty string")
    rearmability_result = _validate_successor_rearmability(
        state, now=_iso(now)
    )
    if rearmability_result is not None:
        return state, rearmability_result
    completed_at = _utc(now)
    created_at = _utc(scheduled_created_at)
    if _truncate_to_scheduler_precision(created_at) < _truncate_to_scheduler_precision(
        completed_at
    ):
        raise DefaultWakeError("Successor creation predates wake completion")
    expected_first_run = _truncate_to_scheduler_precision(
        created_at
        + timedelta(seconds=state["automation_policy"]["cadence_seconds"])
    ).isoformat()
    if not _schedule_times_match(
        expected_first_run,
        scheduled_first_run,
        ordered=True,
        scheduler_precision=True,
    ):
        raise DefaultWakeError("Successor first run does not match its creation anchor")
    retirement = state.get("task_retirement")
    if isinstance(retirement, Mapping) and retirement.get("phase") == "confirmed":
        successor = retirement.get("successor")
        if (
            not isinstance(successor, Mapping)
            or successor.get("task_id") != scheduled_task_id
            or successor.get("status") != "paused"
            or state.get("wake_phase") not in {"successor_ready", "retirement_recovery"}
        ):
            raise DefaultWakeError("Recovered successor authorization lacks exact paused readback")
        if _truncate_to_scheduler_precision(_utc(successor.get("created_at"))) != _truncate_to_scheduler_precision(created_at):
            raise DefaultWakeError("Recovered successor authorization creation time does not match")
        if not _schedule_times_match(
            successor.get("first_run"),
            scheduled_first_run,
            ordered=True,
            scheduler_precision=True,
        ):
            raise DefaultWakeError("Recovered successor authorization first run does not match")
    elif isinstance(retirement, Mapping) and retirement.get("phase") == "registered":
        # Direct callback integrations from before retirement support never
        # supplied outer provenance or a delete controller.  They are kept as
        # a compatibility input only and cannot claim exact-task retirement.
        state["task_retirement"] = None
    next_not_before = expected_first_run
    state["successor_authorization"] = {
        "wake_id": wake_id,
        "scheduled_task_id": scheduled_task_id,
        "scheduled_created_at": created_at.isoformat(),
        "scheduled_first_run": _iso(scheduled_first_run),
    }
    state["scheduled_task_id"] = scheduled_task_id
    state["scheduled_task_kind"] = "standalone"
    # Authorization is only a verified setup handoff.  Keep the wake active and
    # the task paused until complete_wake durably closes the wake; activation is
    # then the final host mutation.  This prevents a partial authorization from
    # looking like a completed recurring delivery.
    state["scheduled_task_disposition"] = "PAUSED"
    state["wake_phase"] = "successor_authorized"
    state["last_wake_id"] = wake_id
    result = {
        "next_action": "SUCCESSOR_AUTHORIZED",
        "reason_code": "successor_authorized",
        "scheduled_task_id": scheduled_task_id,
        "scheduled_created_at": created_at.isoformat(),
        "scheduled_first_run": _iso(scheduled_first_run),
        "mutation_occurred": False,
    }
    state["last_wake_result"] = deepcopy(result)
    return state, result


def reconcile_authorized_successor(
    checkpoint: dict[str, Any],
    *,
    now: str,
    scheduled_task_id: str,
    action: str,
    confirmed: bool,
    evidence: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Recover an authorized successor after a host restart before activation."""
    if action not in {"activate", "pause"}:
        raise ValueError("Successor reconciliation action must be activate or pause")
    if not confirmed:
        raise DefaultWakeError("Successor reconciliation requires host confirmation")
    if not isinstance(scheduled_task_id, str) or not scheduled_task_id.strip():
        raise ValueError("Scheduled task ID must be a non-empty string")
    state = ensure_default_lifecycle(checkpoint)
    authorization = state.get("successor_authorization")
    if state.get("wake_phase") not in {
        "successor_authorized",
        "successor_finalized",
    }:
        raise DefaultWakeError(
            "Successor reconciliation requires an authorized successor checkpoint"
        )
    if not isinstance(authorization, Mapping):
        raise DefaultWakeError("Successor authorization evidence is missing")
    if (
        authorization.get("scheduled_task_id") != scheduled_task_id
        or state.get("scheduled_task_id") != scheduled_task_id
    ):
        raise DefaultWakeError("Successor reconciliation task ID does not match")

    # A restart cannot safely activate a successor while the original wake is
    # still active: authorization alone is only setup evidence.  Keep the
    # exact task paused and preserve the active-wake guard for explicit
    # recovery instead of clearing it and allowing ordinary work to continue.
    if state.get("active_wake_id"):
        if action == "activate":
            raise DefaultWakeError(
                "Cannot activate a successor before the wake is durably finalized"
            )
        state["scheduled_task_disposition"] = "PAUSED"
        state["failure_latch"] = {
            "reason_code": "successor_finalization_interrupted",
            "latched_at": _iso(now),
            "evidence": deepcopy(dict(evidence or {})),
        }
        result = {
            "next_action": "PAUSE_RECOVERY",
            "reason_code": "successor_finalization_interrupted",
            "scheduled_task_id": scheduled_task_id,
            "evidence": deepcopy(dict(evidence or {})),
            "mutation_occurred": False,
        }
        _set_last_result(state, result)
        return state, result

    if state.get("scheduled_task_disposition") != "AUTHORIZED":
        raise DefaultWakeError(
            "Successor reconciliation requires AUTHORIZED task disposition"
        )

    observed = evidence if isinstance(evidence, Mapping) else {}
    if observed.get("delivery_observed") is not False:
        raise DefaultWakeError(
            "Successor reconciliation requires host evidence that no delivery was observed"
        )
    if observed.get("task_status") not in {"PAUSED", "ACTIVE"}:
        raise DefaultWakeError(
            "Successor reconciliation requires the exact task status readback"
        )
    if observed.get("delivered_task_id") == scheduled_task_id:
        raise DefaultWakeError(
            "A delivered successor must be consumed by begin-wake, not reactivated"
        )

    if action == "activate":
        prior_action = (state.get("last_decision") or {}).get("next_action")
        state["scheduled_task_disposition"] = "ACTIVE"
        state["wake_phase"] = "retry_waiting" if prior_action == "WAIT_RETRY" else "completed"
        state["successor_authorization"] = None
        retirement = state.get("task_retirement")
        if isinstance(retirement, Mapping) and isinstance(retirement.get("successor"), Mapping):
            updated_retirement = deepcopy(dict(retirement))
            updated_successor = deepcopy(dict(updated_retirement["successor"]))
            updated_successor["status"] = "active"
            updated_retirement["successor"] = updated_successor
            state["task_retirement"] = updated_retirement
        result = {
            "next_action": "SUCCESSOR_RECONCILED",
            "reason_code": "successor_activation_reconciled",
            "scheduled_task_id": scheduled_task_id,
            "next_not_before": state.get("next_not_before"),
            "evidence": deepcopy(dict(evidence or {})),
            "mutation_occurred": False,
        }
        _set_last_result(state, result)
        return state, result

    state["scheduled_task_disposition"] = "PAUSED"
    state["wake_phase"] = "paused"
    state["wake_completed_at"] = _iso(now)
    state["next_not_before"] = None
    state["failure_latch"] = {
        "reason_code": "successor_activation_recovery_required",
        "latched_at": _iso(now),
        "evidence": deepcopy(dict(evidence or {})),
    }
    retirement = state.get("task_retirement")
    if isinstance(retirement, Mapping) and isinstance(retirement.get("successor"), Mapping):
        updated_retirement = deepcopy(dict(retirement))
        updated_successor = deepcopy(dict(updated_retirement["successor"]))
        updated_successor["status"] = "paused"
        updated_retirement["successor"] = updated_successor
        state["task_retirement"] = updated_retirement
    result = {
        "next_action": "PAUSE_RECOVERY",
        "reason_code": "successor_activation_recovery_required",
        "scheduled_task_id": scheduled_task_id,
        "evidence": deepcopy(dict(evidence or {})),
        "mutation_occurred": False,
    }
    _set_last_result(state, result)
    return state, result


def normalize_snapshot(
    raw: dict[str, Any], evaluation: dict[str, Any], *, observed_at: str
) -> dict[str, Any]:
    """Produce the agent-facing snapshot without an intermediate observation JSON."""
    pull_request = raw["pull_request"]
    targeted_ids = set(evaluation["targeted_unresolved_thread_ids"])
    targeted_threads = [
        thread for thread in raw.get("review_threads", []) if thread.get("id") in targeted_ids
    ]
    return {
        "mode": "codex-first-default",
        "repository": raw["repository"].casefold(),
        "pull_request_number": pull_request["number"],
        "pull_request_state": pull_request["state"],
        "head_oid": pull_request["headRefOid"],
        "targeted_thread_ids": evaluation["targeted_unresolved_thread_ids"],
        "targeted_threads": targeted_threads,
        "non_target_threads": evaluation["non_target_unresolved_threads"],
        "review_in_progress": {
            "active": evaluation["codex_review_in_progress"],
            "reaction_ids": [
                item["id"] for item in evaluation["codex_review_in_progress_reactions"]
            ],
            "reactions": evaluation["codex_review_in_progress_reactions"],
        },
        "review_activity_ok": evaluation["review_activity_ok"],
        "approval_evidence": {
            "status": evaluation["approval_status"],
            "proof": evaluation["approval_proof"],
            "reaction_ids": evaluation["proven_current_head_reaction_ids"],
            "reactions": evaluation["qualifying_approval_reactions"],
            "review_ids": [
                item["id"] for item in evaluation["qualifying_current_head_approval_reviews"]
            ],
            "reviews": evaluation["qualifying_current_head_approval_reviews"],
            "diagnostic": evaluation["approval_diagnostic"],
        },
        "review_epoch_state": {
            "head_oid": pull_request["headRefOid"],
            "transition": evaluation["approval_epoch_transition"],
            "cold_start": evaluation["cold_start"],
            "proven_reaction_ids": evaluation["proven_current_head_reaction_ids"],
        },
        "server_evidence": {
            "head_before": pull_request["headRefOid"],
            "head_after": pull_request["headRefOid"],
            "head_bracketed": True,
            "observed_at": observed_at,
        },
        "snapshot_stable": True,
    }


def _checkpoint_target(checkpoint: dict[str, Any] | None) -> tuple[str, int] | None:
    if not isinstance(checkpoint, dict):
        return None
    repository = checkpoint.get("repository")
    pr_number = checkpoint.get("pull_request_number")
    if not isinstance(repository, str) or not isinstance(pr_number, int):
        return None
    if isinstance(pr_number, bool) or pr_number < 1:
        raise RuntimeError("Checkpoint pull request binding is invalid")
    try:
        return canonical_repository(repository), pr_number
    except ValueError as error:
        raise RuntimeError("Checkpoint repository binding is invalid") from error


def _resolve_command_target(
    args: argparse.Namespace,
    *,
    checkpoint: dict[str, Any] | None = None,
) -> tuple[str, int]:
    """Resolve once, or reuse an already-bound checkpoint target."""
    bound = _checkpoint_target(checkpoint)
    if bound is not None:
        bound_repository, bound_pr = bound
        if args.repo is not None and canonical_repository(args.repo) != bound_repository:
            raise RuntimeError("CLI repository does not match the checkpoint target")
        if args.pr is not None and args.pr != bound_pr:
            raise RuntimeError("CLI pull request does not match the checkpoint target")
        return bound

    try:
        owner, repo, pr_number = resolve_target(
            args.repo,
            args.pr,
            repository_path=args.repository_path,
        )
    except (RuntimeError, ValueError) as error:
        if args.repo is None or args.pr is None:
            raise RuntimeError(
                "Cannot identify a unique current pull request; run from a PR checkout or pass --repo OWNER/REPO and --pr NUMBER"
            ) from error
        raise
    return canonical_repository(f"{owner}/{repo}"), pr_number


def _assert_checkpoint_target(
    checkpoint: dict[str, Any], repository: str, pr_number: int
) -> None:
    bound = _checkpoint_target(checkpoint)
    if bound != (canonical_repository(repository), pr_number):
        raise RuntimeError("Checkpoint target does not match the requested pull request")


def _checkout_head(repository_path: str | Path) -> str:
    process = subprocess.run(
        ["git", "-C", str(repository_path), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
    )
    if process.returncode != 0:
        raise RuntimeError(process.stderr.strip() or "Unable to read worktree HEAD")
    head_oid = process.stdout.strip()
    if not head_oid:
        raise RuntimeError("Worktree HEAD is empty")
    return head_oid


def _load_pending_repair(
    manifest_path: Path, *, repository_path: str | Path
) -> dict[str, Any]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("Pending repair manifest must be a JSON object")
    patch_path = manifest.get("patch_path")
    if not isinstance(patch_path, str) or not patch_path.strip():
        raise ValueError("Pending repair patch_path is required")
    patch = Path(patch_path).resolve()
    common = git_common_directory(repository_path)
    try:
        patch.relative_to(common)
    except ValueError as error:
        raise ValueError("Pending repair patch must be under the Git common directory") from error
    if not patch.is_file():
        raise ValueError("Pending repair patch does not exist")
    expected_sha256 = manifest.get("patch_sha256")
    actual_sha256 = hashlib.sha256(patch.read_bytes()).hexdigest()
    if expected_sha256 != actual_sha256:
        raise ValueError("Pending repair patch SHA-256 does not match its manifest")
    manifest["patch_path"] = str(patch)
    return manifest


def restore_pending_repair(
    state: dict[str, Any], *, wake_id: str, repository_path: str | Path
) -> dict[str, Any]:
    """Verify stored bytes and restore them only into this wake's clean checkout."""
    _require_active_wake(state, wake_id)
    batch = state.get("active_batch") or {}
    manifest = batch.get("pending_repair")
    if not state.get("resume_pending_batch") or not isinstance(manifest, dict):
        raise DefaultWakeError("No pending repair to restore")
    patch = Path(manifest["patch_path"]).resolve()
    patch.relative_to(git_common_directory(repository_path))
    content = patch.read_bytes()
    if hashlib.sha256(content).hexdigest() != manifest.get("patch_sha256"):
        raise DefaultWakeError("Pending repair patch SHA-256 mismatch")
    if _checkout_head(repository_path) != batch.get("frozen_head_oid") or (
        manifest.get("frozen_head_oid") != batch.get("frozen_head_oid")
    ):
        raise DefaultWakeError("Pending repair frozen head mismatch")
    checkout = str(Path(repository_path).resolve())
    restored = state.get("pending_repair_restored") or {}
    if restored.get("wake_id") == wake_id and restored.get("checkout") == checkout:
        return {"next_action": "PENDING_REPAIR_RESTORED", "already_restored": True}
    dirty = subprocess.run(
        ["git", "-C", checkout, "status", "--porcelain"],
        capture_output=True, text=True, check=True,
    ).stdout
    if dirty.strip():
        raise DefaultWakeError("Pending repair requires a clean wake worktree")
    subprocess.run(
        ["git", "-C", checkout, "apply", "--check", "-"], input=content,
        capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "-C", checkout, "apply", "-"], input=content,
        capture_output=True, check=True,
    )
    state["pending_repair_restored"] = {
        "wake_id": wake_id, "checkout": checkout, "patch_sha256": manifest["patch_sha256"],
    }
    return {"next_action": "PENDING_REPAIR_RESTORED", "already_restored": False}


def _state_path(
    args: argparse.Namespace,
    *,
    checkpoint: dict[str, Any] | None = None,
) -> Path:
    if args.state_file:
        return args.state_file
    repository, pr_number = _resolve_command_target(args, checkpoint=checkpoint)
    return checkpoint_path(repository, pr_number, repository_path=args.repository_path)


def _load_state(args: argparse.Namespace) -> tuple[Path, dict[str, Any]]:
    supplied_checkpoint = load_checkpoint(args.state_file) if args.state_file else None
    path = _state_path(args, checkpoint=supplied_checkpoint)
    checkpoint = load_checkpoint(path)
    if checkpoint is None:
        raise RuntimeError(
            "Checkpoint does not exist; run begin-wake from the target PR checkout first"
        )
    repository, pr_number = _resolve_command_target(args, checkpoint=checkpoint)
    _assert_checkpoint_target(checkpoint, repository, pr_number)
    return path, ensure_default_lifecycle(checkpoint)


def _write(path: Path, state: dict[str, Any], result: dict[str, Any]) -> None:
    save_checkpoint(path, state)
    output = deepcopy(result)
    output["checkpoint_path"] = str(path)
    print(json.dumps(output, indent=2, sort_keys=True))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run one Codex-first default wake without hardened contract ceremony",
        epilog=(
            "Host handoff:\n"
            "  pause the delivered standalone task before begin-wake; pass --pause-confirmed "
            "after success.\n"
            "  after confirmed cleanup, persist pending exact predecessor retirement, "
            "delete and confirm that exact ID, then create/read back one cadence-only "
            "standalone successor before complete-wake --schedule-reanchored "
            "--scheduled-created-at PERSISTED_CREATED_AT "
            "--scheduled-first-run DERIVED_FIRST_RUN "
            "--scheduled-task-id SUCCESSOR_ID."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--repo",
        help="OWNER/REPO; omitted fields are inferred from the current PR checkout",
    )
    parser.add_argument(
        "--pr",
        type=int,
        help="Pull request number; omitted fields are inferred from the current PR checkout",
    )
    parser.add_argument("--repository-path", default=".", help="Target checkout")
    parser.add_argument("--state-file", type=Path, help="Checkpoint override for tests")
    parser.add_argument("--now", help="UTC timestamp for deterministic tests")
    parser.add_argument("--wake-id", help="Host invocation ID")
    parser.add_argument("--reviewer-login", action="append", default=[])
    parser.add_argument("--approval-login", action="append", default=[])
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser(
        "heartbeat-prompt",
        help="Render the standalone-task prompt (legacy command name)",
    )
    commands.add_parser(
        "standalone-task-prompt",
        help="Render the canonical clean-context standalone-task prompt",
    )

    begin = commands.add_parser(
        "begin-wake",
        help="Start one wake after the host has paused the scheduled task",
    )
    begin.add_argument(
        "--pause-confirmed",
        action="store_true",
        help="Acknowledge that the host pause call succeeded; pulse.py does not perform it",
    )
    begin.add_argument(
        "--delivered-task-id",
        help="Scheduled task ID delivered by the host; must match the persisted successor",
    )
    begin.add_argument(
        "--setup-task-provenance",
        type=Path,
        help="Verified initial paused-task readback JSON; atomically registered with wake one",
    )
    begin.add_argument(
        "--delivered-task-provenance",
        type=Path,
        help="Optional exact delivered-task readback JSON retained with its wake registration",
    )
    begin.add_argument(
        "--policy-json",
        dest="command_policy_json",
        help=(
            "JSON object of prompt-derived policy overrides for the initial wake; "
            "supports model and reasoning_effort"
        ),
    )

    creation_intent = commands.add_parser(
        "record-creation-intent",
        help="Persist one immutable PAUSED-task creation intent before host create",
    )
    creation_intent.add_argument("--role", choices=["setup", "successor"], required=True)
    creation_intent.add_argument("--creation-nonce", required=True)
    creation_intent.add_argument(
        "--intent-wake-id",
        help="Active wake ID for successor creation; omitted for setup creation",
    )
    setup_id = commands.add_parser(
        "record-setup-id",
        help="Persist the exact returned setup task ID before readback validation",
    )
    setup_id.add_argument("--task-id", required=True)
    setup_readback = commands.add_parser(
        "record-setup-readback",
        help="Validate and persist the exact paused setup task readback",
    )
    setup_readback.add_argument("--readback", type=Path, required=True)
    setup_unknown = commands.add_parser(
        "record-setup-unknown",
        help="Persist UNKNOWN when setup creation identity cannot be established",
    )
    setup_unknown.add_argument("--evidence", type=Path)

    commands.add_parser("snapshot", help="Fetch and normalize one stable PR snapshot")
    commands.add_parser("freeze", help="Freeze the targeted threads from the snapshot")
    commands.add_parser("restore-repair", help="Verify and apply a resumed pending patch")

    record = commands.add_parser("record", help="Persist one thread outcome")
    record.add_argument("--thread-id", required=True)
    record.add_argument("--classification", required=True, choices=["fix-now", "no-fix", "defer", "ambiguous"])
    record.add_argument("--reference")

    resolve = commands.add_parser("resolve", help="Resolve one exact frozen GraphQL thread")
    resolve.add_argument("--thread-id", required=True)

    commands.add_parser(
        "prepare-publication",
        help="Authorize commit and push after every frozen thread is resolved",
    )

    trigger = commands.add_parser("trigger-result", help="Persist injected bracketed trigger evidence")
    trigger.add_argument("--evidence", required=True, type=Path)

    retry = commands.add_parser(
        "retry",
        help="Persist a recoverable failure and prepare a completion-relative retry",
    )
    retry.add_argument("--reason-code", required=True)
    retry.add_argument("--signature")
    retry.add_argument("--evidence", type=Path)
    retry.add_argument(
        "--no-progress",
        action="store_true",
        help="Count this retry only when the same validation failure made no progress",
    )
    retry.add_argument(
        "--pending-repair",
        type=Path,
        help="JSON manifest for an uncommitted repair patch retained across retry wakes",
    )

    configure = commands.add_parser(
        "configure-policy",
        help="Persist explicit prompt-derived default automation policy overrides",
    )
    configure.add_argument(
        "--policy-json",
        dest="command_policy_json",
        help=(
            "JSON object of prompt-derived policy overrides; supports model and "
            "reasoning_effort"
        ),
    )

    confirm = commands.add_parser(
        "confirm-policy",
        help="Record one explicit supervised continuation for a pending operation",
    )
    confirm.add_argument(
        "--operation",
        required=True,
        choices=["thread_resolution", "aggregate_publication", "review_trigger"],
    )

    publication = commands.add_parser("publication-result", help="Persist aggregate publication outcome")
    publication.add_argument("--status", required=True, choices=["succeeded", "failed"])
    publication.add_argument("--phase", choices=["validation", "commit", "push"])
    publication.add_argument("--pending-path", action="append", default=[])
    publication.add_argument("--pending-commit")
    publication.add_argument("--published-commit")

    complete = commands.add_parser(
        "complete-wake",
        aliases=["authorize-successor"],
        help="Complete the wake after the host re-anchors its next run",
    )
    complete.add_argument(
        "--schedule-reanchored",
        action="store_true",
        help="Confirm that the host successor create and schedule readback succeeded",
    )
    complete.add_argument(
        "--scheduled-first-run",
        help=(
            "Verified first run, either persisted directly or derived from the "
            "persisted task creation anchor plus cadence"
        ),
    )
    complete.add_argument(
        "--scheduled-created-at",
        help=(
            "Persisted successor creation timestamp when the host anchors a "
            "recurring schedule at task creation"
        ),
    )
    complete.add_argument(
        "--scheduled-task-id",
        help="ID of the newly created standalone successor task",
    )
    complete.add_argument(
        "--completion-failure",
        type=Path,
        help=(
            "JSON file describing a successor handoff failure to persist as "
            "PAUSE_RECOVERY; do not combine with --schedule-reanchored"
        ),
    )
    complete.add_argument("--cadence-seconds", type=int)

    reconcile_successor = commands.add_parser(
        "reconcile-successor",
        help="Recover an AUTHORIZED successor after a host restart",
    )
    reconcile_successor.add_argument("--scheduled-task-id", required=True)
    reconcile_successor.add_argument(
        "--action", required=True, choices=["activate", "pause"]
    )
    reconcile_successor.add_argument(
        "--confirmed",
        action="store_true",
        help="Confirm that the host performed the requested exact task operation",
    )
    reconcile_successor.add_argument("--evidence", type=Path)

    prepare_retirement = commands.add_parser(
        "prepare-retirement",
        help="Persist the exact predecessor retirement boundary after confirmed cleanup",
    )
    prepare_retirement.add_argument("--worktree-cleanup-confirmed", action="store_true")

    confirm_retirement = commands.add_parser(
        "confirm-retirement",
        help="Persist the normalized exact predecessor deletion result",
    )
    confirm_retirement.add_argument("--task-id", required=True)
    confirm_retirement.add_argument("--role", required=True, choices=sorted(RETIREMENT_ROLES))
    confirm_retirement.add_argument(
        "--outcome", required=True, choices=["confirmed", "non_deletion", "unknown"]
    )
    confirm_retirement.add_argument("--evidence", type=Path)

    reconcile_retirement = commands.add_parser(
        "reconcile-retirement",
        help="Reconcile only an exact pending or unknown predecessor retirement",
    )
    reconcile_retirement.add_argument("--task-id", required=True)
    reconcile_retirement.add_argument("--role", required=True, choices=sorted(RETIREMENT_ROLES))
    reconcile_retirement.add_argument(
        "--lookup",
        required=True,
        choices=["PRESENT", "AUTHORITATIVE_NOT_FOUND", "READBACK_UNKNOWN"],
    )
    reconcile_retirement.add_argument("--evidence", type=Path)

    successor_creation = commands.add_parser(
        "record-successor-creation",
        help="Persist normalized successor creation evidence after confirmed retirement",
    )
    successor_creation.add_argument(
        "--outcome",
        required=True,
        choices=[
            "CREATED_EXACT_ID",
            "AUTHORITATIVE_NO_SUCCESSOR",
            "SUCCESSOR_CREATION_UNKNOWN",
        ],
    )
    successor_creation.add_argument("--scheduled-task-id")
    successor_creation.add_argument("--completion-anchor")
    successor_creation.add_argument("--evidence", type=Path)

    successor_readback = commands.add_parser(
        "record-successor-readback",
        help="Persist exact paused-successor readback against the retirement handoff",
    )
    successor_readback.add_argument("--task-readback", required=True, type=Path)

    successor_pause = commands.add_parser(
        "record-successor-pause",
        help="Persist the exact known-successor pause fallback after readback failure",
    )
    successor_pause.add_argument("--scheduled-task-id", required=True)
    successor_pause.add_argument("--confirmed", action="store_true")
    successor_pause.add_argument("--evidence", type=Path)

    recover_retirement = commands.add_parser(
        "recover-retirement-successor",
        help="Resume only an exact paused, undelivered retired-successor handoff",
    )
    recover_retirement.add_argument("--action", required=True, choices=["authorize", "finalize"])
    recover_retirement.add_argument("--task-readback", required=True, type=Path)
    recover_retirement.add_argument("--delivery-observed", action="store_true")
    recover_retirement.add_argument("--activation-confirmed", action="store_true")
    parser.add_argument(
        "--policy-json",
        dest="root_policy_json",
        help=(
            "JSON object of prompt-derived policy overrides (initial wake or "
            "configure-policy), including model and reasoning_effort"
        ),
    )
    args = parser.parse_args()
    command_policy_json = getattr(args, "command_policy_json", None)
    if args.root_policy_json is not None and command_policy_json is not None:
        parser.error("--policy-json may be supplied only once")
    args.policy_json = (
        command_policy_json
        if command_policy_json is not None
        else args.root_policy_json
    )
    return args


def _read_json_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Cannot read {label} JSON: {path}") from error
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} JSON must be an object")
    return value


def main() -> None:
    args = parse_args()
    now = _now(args.now)
    policy_overrides = (
        parse_policy_json(args.policy_json) if args.policy_json is not None else None
    )
    if args.command in {"heartbeat-prompt", "standalone-task-prompt"}:
        supplied_checkpoint = load_checkpoint(args.state_file) if args.state_file else None
        repository, pr_number = _resolve_command_target(
            args,
            checkpoint=supplied_checkpoint,
        )
        handoff_policy = policy_overrides
        if handoff_policy is None:
            persisted_path = _state_path(args, checkpoint=supplied_checkpoint)
            persisted_checkpoint = load_checkpoint(persisted_path)
            if persisted_checkpoint is not None:
                _assert_checkpoint_target(persisted_checkpoint, repository, pr_number)
                handoff_policy = ensure_default_lifecycle(
                    persisted_checkpoint
                )["automation_policy"]
        print(
            json.dumps(
                build_standalone_task_handoff(
                    repository,
                    pr_number,
                    policy=handoff_policy,
                ),
                indent=2,
                sort_keys=True,
            )
        )
        return
    if args.command == "begin-wake":
        if not args.wake_id:
            raise RuntimeError("--wake-id is required")
        supplied_checkpoint = load_checkpoint(args.state_file) if args.state_file else None
        repository, pr_number = _resolve_command_target(
            args,
            checkpoint=supplied_checkpoint,
        )
        path = _state_path(args, checkpoint=supplied_checkpoint)
        state = ensure_default_lifecycle(load_checkpoint(path) or {})
        if not state.get("repository"):
            state = ensure_default_lifecycle(empty_checkpoint(repository, pr_number))
        else:
            _assert_checkpoint_target(state, repository, pr_number)
        setup_provenance = (
            _read_json_object(args.setup_task_provenance, label="setup task provenance")
            if args.setup_task_provenance is not None
            else (
                {}
                if args.pause_confirmed and args.delivered_task_id is None
                else None
            )
        )
        delivered_provenance = (
            _read_json_object(
                args.delivered_task_provenance,
                label="delivered task provenance",
            )
            if args.delivered_task_provenance is not None
            else (
                {}
                if args.pause_confirmed and args.delivered_task_id is not None
                else None
            )
        )
        state, result = begin_wake(
            state,
            wake_id=args.wake_id,
            now=now,
            policy_overrides=policy_overrides,
            pause_heartbeat=lambda: args.pause_confirmed,
            delivered_task_id=args.delivered_task_id,
            setup_task_provenance=setup_provenance,
            delivered_task_provenance=delivered_provenance,
        )
        _write(path, state, result)
        return

    if args.command == "record-creation-intent" and args.role == "setup":
        supplied_checkpoint = load_checkpoint(args.state_file) if args.state_file else None
        repository, pr_number = _resolve_command_target(
            args,
            checkpoint=supplied_checkpoint,
        )
        path = _state_path(args, checkpoint=supplied_checkpoint)
        state = load_checkpoint(path)
        if state is None:
            state = empty_checkpoint(repository, pr_number)
        else:
            _assert_checkpoint_target(state, repository, pr_number)
        state, result = record_creation_intent(
            state,
            role="setup",
            now=now,
            creation_nonce=args.creation_nonce,
            wake_id=None,
        )
        _write(path, state, result)
        return

    path, state = _load_state(args)

    if args.command == "record-creation-intent":
        state, result = record_creation_intent(
            state,
            role=args.role,
            now=now,
            creation_nonce=args.creation_nonce,
            wake_id=args.intent_wake_id,
        )
        _write(path, state, result)
        return

    if args.command == "record-setup-id":
        state, result = record_setup_creation_id(
            state,
            now=now,
            task_id=args.task_id,
        )
        _write(path, state, result)
        return

    if args.command == "record-setup-readback":
        state, result = record_setup_creation_readback(
            state,
            now=now,
            task=_read_json_object(args.readback, label="setup task readback"),
        )
        _write(path, state, result)
        return

    if args.command == "record-setup-unknown":
        evidence = (
            _read_json_object(args.evidence, label="setup creation evidence")
            if args.evidence is not None
            else {}
        )
        state, result = record_setup_creation_unknown(
            state,
            now=now,
            evidence=evidence,
        )
        _write(path, state, result)
        return

    if args.command == "configure-policy":
        if policy_overrides is None:
            raise RuntimeError("--policy-json is required for configure-policy")
        state, result = update_default_policy(
            state,
            overrides=policy_overrides,
            now=now,
        )
        _write(path, state, result)
        return

    if args.command == "confirm-policy":
        state, result = confirm_policy_operation(
            state,
            operation=args.operation,
            now=now,
        )
        _write(path, state, result)
        return

    if not args.wake_id:
        raise RuntimeError("--wake-id is required")

    if args.command == "prepare-retirement":
        state, result = prepare_task_retirement(
            state,
            wake_id=args.wake_id,
            now=now,
            worktree_cleanup_confirmed=args.worktree_cleanup_confirmed,
        )
        _write(path, state, result)
        return
    if args.command == "confirm-retirement":
        evidence = (
            _read_json_object(args.evidence, label="retirement evidence")
            if args.evidence is not None
            else None
        )
        state, result = confirm_task_retirement(
            state,
            wake_id=args.wake_id,
            now=now,
            task_id=args.task_id,
            role=args.role,
            outcome=args.outcome,
            evidence=evidence,
        )
        _write(path, state, result)
        return
    if args.command == "reconcile-retirement":
        evidence = (
            _read_json_object(args.evidence, label="retirement reconciliation evidence")
            if args.evidence is not None
            else None
        )
        state, result = reconcile_task_retirement(
            state,
            wake_id=args.wake_id,
            now=now,
            task_id=args.task_id,
            role=args.role,
            lookup=args.lookup,
            evidence=evidence,
        )
        _write(path, state, result)
        return
    if args.command == "record-successor-creation":
        evidence = (
            _read_json_object(args.evidence, label="successor creation evidence")
            if args.evidence is not None
            else None
        )
        state, result = record_retirement_successor_creation(
            state,
            wake_id=args.wake_id,
            now=now,
            outcome=args.outcome,
            task_id=args.scheduled_task_id,
            completion_anchor=args.completion_anchor,
            evidence=evidence,
        )
        _write(path, state, result)
        return
    if args.command == "record-successor-readback":
        state, result = record_retirement_successor_readback(
            state,
            wake_id=args.wake_id,
            now=now,
            task=_read_json_object(args.task_readback, label="successor task readback"),
        )
        _write(path, state, result)
        return
    if args.command == "record-successor-pause":
        evidence = (
            _read_json_object(args.evidence, label="successor pause evidence")
            if args.evidence is not None
            else None
        )
        state, result = record_retirement_successor_pause(
            state,
            wake_id=args.wake_id,
            now=now,
            task_id=args.scheduled_task_id,
            confirmed=args.confirmed,
            evidence=evidence,
        )
        _write(path, state, result)
        return
    if args.command == "recover-retirement-successor":
        state, result = recover_retirement_successor(
            state,
            wake_id=args.wake_id,
            now=now,
            action=args.action,
            task=_read_json_object(args.task_readback, label="retirement successor readback"),
            delivery_observed=args.delivery_observed,
            activation_confirmed=args.activation_confirmed,
        )
        _write(path, state, result)
        return

    if args.command == "snapshot":
        _require_active_wake(state, args.wake_id)
        if (
            state.get("last_snapshot_wake_id") == args.wake_id
            and isinstance(state.get("last_snapshot"), dict)
        ):
            _write(path, state, state["last_snapshot"])
            return
        repository = state["repository"]
        pr_number = state["pull_request_number"]
        owner, repo_name = repository.split("/", 1)
        raw = fetch_stable_snapshot(owner, repo_name, pr_number)
        pull_request = raw["pull_request"]
        previous = state
        evaluation, state = evaluate_snapshot(
            repository=raw["repository"],
            pr_number=pr_number,
            head_oid=pull_request["headRefOid"],
            pull_request_state=pull_request["state"],
            review_threads=raw["review_threads"],
            reactions=raw["thumbs_up_reactions"],
            review_activity_reactions=raw["eyes_reactions"],
            reviews=raw["reviews"],
            reviewer_logins=args.reviewer_login or state.get("reviewer_logins") or list(DEFAULT_CODEX_LOGINS),
            approval_logins=args.approval_login or state.get("approval_logins") or list(DEFAULT_CODEX_LOGINS),
            checkpoint=previous,
            observed_at=now,
        )
        normalized = normalize_snapshot(raw, evaluation, observed_at=now)
        normalized["reviewer_logins"] = evaluation["reviewer_logins"]
        normalized["approval_logins"] = evaluation["approval_logins"]
        state["reviewer_logins"] = evaluation["reviewer_logins"]
        state["approval_logins"] = evaluation["approval_logins"]
        state, result = record_snapshot(state, normalized, wake_id=args.wake_id, now=now)
        normalized["decision"] = result
        normalized["review_epoch_state"] = deepcopy(state["review_epoch_state"])
        state["last_snapshot"] = normalized
        _write(path, state, {**normalized, "decision": result})
        return

    if args.command == "freeze":
        try:
            worktree_head_oid = _checkout_head(args.repository_path)
        except RuntimeError as error:
            result = _pause(
                state,
                reason_code="worktree_head_unavailable",
                now=now,
                evidence={"error": str(error)},
                action="PAUSE_RECOVERY",
            )
            state["last_wake_id"] = args.wake_id
            _write(path, state, result)
            return
        state, outcome = freeze_default_batch(
            state,
            wake_id=args.wake_id,
            worktree_head_oid=worktree_head_oid,
            now=now,
        )
        if state.get("wake_phase") != "frozen":
            _write(path, state, outcome)
            return
        _write(path, state, {"next_action": "RUN_BATCH", "batch": outcome})
        return
    if args.command == "record":
        state, result = record_default_outcome(
            state,
            wake_id=args.wake_id,
            thread_id=args.thread_id,
            classification=args.classification,
            reference=args.reference,
            now=now,
        )
        _write(path, state, result)
        return
    if args.command == "restore-repair":
        result = restore_pending_repair(
            state, wake_id=args.wake_id, repository_path=args.repository_path
        )
        _write(path, state, result)
        return
    if args.command == "resolve":
        from resolve_thread import graphql

        state, result = resolve_default_thread(
            state,
            wake_id=args.wake_id,
            thread_id=args.thread_id,
            graphql_call=graphql,
            repository_path=args.repository_path,
        )
        _write(path, state, result)
        return
    if args.command == "prepare-publication":
        repository = state["repository"]
        pr_number = state["pull_request_number"]
        owner, repo_name = repository.split("/", 1)
        raw = fetch_stable_snapshot(
            owner,
            repo_name,
            pr_number,
            include_conversation=False,
            require_server_time=False,
        )
        state, result = prepare_default_publication(
            state,
            wake_id=args.wake_id,
            now=now,
            actual_head_oid=raw["pull_request"]["headRefOid"],
            repository_path=args.repository_path,
        )
        _write(path, state, result)
        return
    if args.command == "trigger-result":
        evidence = json.loads(args.evidence.read_text(encoding="utf-8"))
        state, result = record_default_trigger(state, wake_id=args.wake_id, evidence=evidence)
        _write(path, state, result)
        return
    if args.command == "retry":
        evidence = (
            json.loads(args.evidence.read_text(encoding="utf-8"))
            if args.evidence is not None
            else None
        )
        pending_repair = (
            _load_pending_repair(
                args.pending_repair,
                repository_path=args.repository_path,
            )
            if args.pending_repair is not None
            else None
        )
        state, result = record_retry(
            state,
            wake_id=args.wake_id,
            reason_code=args.reason_code,
            now=now,
            evidence=evidence,
            signature=args.signature,
            count_no_progress=args.no_progress,
            pending_repair=pending_repair,
        )
        _write(path, state, result)
        return
    if args.command == "publication-result":
        state, result = record_publication_result(
            state,
            wake_id=args.wake_id,
            status=args.status,
            now=now,
            phase=args.phase,
            pending_paths=args.pending_path,
            pending_commit=args.pending_commit,
            published_commit=args.published_commit,
        )
        _write(path, state, result)
        return
    if args.command == "authorize-successor":
        if (
            not args.schedule_reanchored
            or not args.scheduled_task_id
            or not args.scheduled_created_at
            or not args.scheduled_first_run
        ):
            raise DefaultWakeError(
                "Successor authorization requires verified task and schedule"
            )
        state, result = authorize_successor(
            state,
            wake_id=args.wake_id,
            now=now,
            scheduled_created_at=args.scheduled_created_at,
            scheduled_first_run=args.scheduled_first_run,
            scheduled_task_id=args.scheduled_task_id,
        )
        _write(path, state, result)
        return
    if args.command == "reconcile-successor":
        evidence = None
        if args.evidence is not None:
            try:
                evidence = json.loads(args.evidence.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise RuntimeError(
                    f"Cannot read successor reconciliation evidence JSON: {args.evidence}"
                ) from error
            if not isinstance(evidence, dict):
                raise RuntimeError("Successor reconciliation evidence must be an object")
        state, result = reconcile_authorized_successor(
            state,
            now=now,
            scheduled_task_id=args.scheduled_task_id,
            action=args.action,
            confirmed=args.confirmed,
            evidence=evidence,
        )
        _write(path, state, result)
        return
    if args.command == "complete-wake":
        completion_failure = None
        if args.completion_failure is not None:
            try:
                completion_failure = json.loads(
                    args.completion_failure.read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError) as error:
                raise RuntimeError(
                    f"Cannot read completion failure JSON: {args.completion_failure}"
                ) from error
            if not isinstance(completion_failure, dict):
                raise RuntimeError("Completion failure JSON must be an object")
            if args.schedule_reanchored:
                raise RuntimeError(
                    "--completion-failure cannot be combined with --schedule-reanchored"
                )
        if completion_failure and (
            (state.get("successor_authorization") or {}).get("wake_id")
            == args.wake_id
        ):
            result = _pause(
                state, reason_code=completion_failure["reason_code"], now=now,
                evidence=completion_failure.get("evidence"), action="PAUSE_RECOVERY",
            )
            _write(path, state, result)
            return
        state, result = complete_wake(
            state,
            wake_id=args.wake_id,
            now=now,
            cadence_seconds=args.cadence_seconds,
            schedule_next_wake=(
                (lambda _: args.scheduled_first_run)
                if args.schedule_reanchored
                else None
            ),
            schedule_anchor_created_at=args.scheduled_created_at,
            scheduled_task_id=args.scheduled_task_id,
            completion_failure=completion_failure,
            require_schedule_anchor=args.schedule_reanchored,
        )
        _write(path, state, result)
        return
    raise RuntimeError(f"Unsupported command: {args.command}")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from error
