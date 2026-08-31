#!/usr/bin/env python3
"""One-wake planning and read-only doctor interface for recurring pilots."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
from pathlib import Path
import sys
from typing import Any

from checkpoint_store import load_checkpoint, save_checkpoint
from manage_pilot_install import verify_installation
from pilot_preflight import inspect_local_checkout, run_command
from recurring_contract import (
    RunContractDriftError,
    inspect_contract_authority_anchor,
    load_mutation_run_contract,
    load_run_contract,
    persist_contract_authority_anchor,
    release_anchored_lease,
)
from recurring_model import (
    NextAction,
    advance_observation_state,
    empty_run_state,
    evaluate_recurring_action,
    latch_failure,
    record_trigger_result,
    validate_run_state,
)
from runner_lease import (
    acquire_lease,
    assert_lease_owner,
    inspect_lease,
    release_lease,
)
from state_model import validate_checkpoint


def _now(value: str | None) -> str:
    if value is None:
        return datetime.now(UTC).isoformat()
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("--now must include a timezone")
    return parsed.astimezone(UTC).isoformat()


def _read_object(path: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return payload


def _checkpoint_status(contract: dict[str, Any]) -> dict[str, Any]:
    path = Path(contract["paths"]["checkpoint"])
    if not path.exists():
        return {"ok": True, "exists": False, "recovery_status": "none"}
    try:
        checkpoint = load_checkpoint(path)
        if checkpoint is None:
            return {"ok": True, "exists": False, "recovery_status": "none"}
        validate_checkpoint(
            checkpoint, contract["repository"], contract["pull_request_number"]
        )
    except Exception as error:
        return {
            "ok": False,
            "exists": True,
            "error": str(error),
            "recovery_status": "invalid_checkpoint",
        }
    batch = checkpoint.get("active_batch")
    if not isinstance(batch, dict):
        recovery = "none"
    else:
        publication = batch.get("publication") or {}
        status = publication.get("status")
        if status == "succeeded":
            recovery = "none"
        elif status == "failed":
            recovery = f"publication_failed:{publication.get('phase') or 'unknown'}"
        else:
            unresolved = set(batch.get("targeted_thread_ids") or []) - set(
                batch.get("resolved_thread_ids") or []
            )
            recovery = "unfinished_frozen_batch" if unresolved else "resolved_before_publication"
    return {
        "ok": True,
        "exists": True,
        "schema_version": checkpoint.get("schema_version"),
        "recovery_status": recovery,
        "active_batch": batch,
        "latest_target_snapshot": checkpoint.get("latest_target_snapshot"),
    }


def _snapshot_ids(value: Any, *, label: str) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise ValueError(f"Persisted snapshot {label} is invalid")
    return sorted(set(value))


def _require_persisted_snapshot(observation: dict[str, Any], checkpoint: dict[str, Any]) -> None:
    """Bind planning inputs to the stable snapshot persisted by fetch_pr_state."""
    snapshot = checkpoint.get("latest_target_snapshot")
    if not isinstance(snapshot, dict):
        raise ValueError("No persisted head-bracketed GraphQL snapshot is available")

    if not isinstance(observation.get("head_oid"), str) or not observation["head_oid"]:
        raise ValueError("Observation head OID is required")
    if observation["head_oid"].casefold() != str(snapshot.get("head_oid", "")).casefold():
        raise ValueError("Observation head OID does not match the persisted snapshot")

    observed_ids = observation.get("targeted_thread_ids")
    if observed_ids is None:
        observed_ids = observation.get("targeted_unresolved_thread_ids")
    persisted_ids = snapshot.get("targeted_unresolved_thread_ids")
    if persisted_ids is None:
        persisted_ids = snapshot.get("targeted_thread_ids")
    if _snapshot_ids(observed_ids, label="targeted thread IDs") != _snapshot_ids(
        persisted_ids, label="targeted thread IDs"
    ):
        raise ValueError("Observation targeted thread IDs do not match the persisted snapshot")

    observed_non_target = _snapshot_ids(
        observation.get("non_target_thread_ids") or [], label="non-target thread IDs"
    )
    persisted_non_target = _snapshot_ids(
        snapshot.get("non_target_thread_ids") or [], label="non-target thread IDs"
    )
    if observed_non_target != persisted_non_target:
        raise ValueError(
            "Observation non-target thread IDs do not match the persisted snapshot"
        )

    fields = (
        "head_repository",
        "pull_request_state",
        "approval_status",
        "codex_review_in_progress",
        "review_activity_ok",
        "batch_publication_event",
        "relevant_codex_events",
        "snapshot_stable",
        "mixed_head",
        "auth_ok",
        "api_ok",
        "server_time",
    )
    for field in fields:
        if field not in observation or field not in snapshot:
            raise ValueError(f"Snapshot evidence field is missing: {field}")
        if observation[field] != snapshot[field]:
            raise ValueError(f"Observation field does not match the persisted snapshot: {field}")

    if _snapshot_ids(
        observation.get("review_in_progress_reaction_ids") or [],
        label="review activity reaction IDs",
    ) != _snapshot_ids(
        snapshot.get("review_in_progress_reaction_ids") or [],
        label="review activity reaction IDs",
    ):
        raise ValueError(
            "Observation review activity IDs do not match the persisted snapshot"
        )

    if observation.get("reviewer_logins") is not None:
        if _snapshot_ids(observation["reviewer_logins"], label="reviewer logins") != _snapshot_ids(
            snapshot.get("reviewer_logins") or [], label="reviewer logins"
        ):
            raise ValueError("Observation reviewer identities do not match the persisted snapshot")


def _installation_status(contract: dict[str, Any]) -> dict[str, Any]:
    expected = contract["expected_installation"]
    return verify_installation(
        expected["skill_path"],
        expected_version=expected["version"],
        expected_source_commit=expected["source_commit"],
        expected_source_repository=expected["source_repository"],
    )


def _execution_source_status(
    contract: dict[str, Any], runtime_script_path: str | Path | None
) -> dict[str, Any]:
    expected = (
        Path(contract["expected_installation"]["skill_path"])
        / "scripts"
        / "heartbeat_tick.py"
    ).resolve()
    actual = Path(runtime_script_path or __file__).resolve()
    return {
        "ok": actual == expected and actual.is_file(),
        "actual": str(actual),
        "expected": str(expected),
    }


def _completion_head_expectation(
    state: dict[str, Any], checkpoint: dict[str, Any]
) -> dict[str, Any]:
    inflight = state.get("inflight_action")
    if not isinstance(inflight, dict):
        raise ValueError("No retained action is available for completion")
    action_head = inflight.get("head_oid")
    if not isinstance(action_head, str) or not action_head:
        raise ValueError("Retained action has no expected head OID")

    expected_heads = [action_head]
    if inflight.get("next_action") == NextAction.RUN_BATCH.value:
        batch = checkpoint.get("active_batch")
        publication = batch.get("publication") if isinstance(batch, dict) else None
        published_commit = (
            publication.get("published_commit")
            if isinstance(publication, dict)
            and publication.get("status") == "succeeded"
            else None
        )
        if isinstance(published_commit, str) and published_commit:
            expected_heads.append(published_commit)
    return {
        "action": inflight.get("next_action"),
        "expected_head_oids": sorted(set(expected_heads)),
    }


def _redact_lease(lease: dict[str, Any]) -> dict[str, Any]:
    """Return status evidence without exposing the mutation capability token."""
    return {key: value for key, value in lease.items() if key != "owner_token"}


def doctor(
    *,
    contract_path: str | Path,
    repository_path: str | Path,
    now: str,
    runtime_script_path: str | Path | None = None,
) -> dict[str, Any]:
    """Inspect readiness without acquiring a lease or writing runtime state."""
    contract = load_run_contract(contract_path, repository_path=repository_path)
    try:
        authority_anchor = inspect_contract_authority_anchor(contract_path, contract)
    except RunContractDriftError as error:
        authority_anchor = {
            "ok": False,
            "exists": True,
            "path": str(Path(contract_path).resolve()),
            "reason_code": "run_contract_drift",
            "error": str(error),
        }
    lease = inspect_lease(
        contract["paths"]["lease"],
        repository=contract["repository"],
        pr_number=contract["pull_request_number"],
        now=now,
    )
    checkpoint = _checkpoint_status(contract)
    installation = _installation_status(contract)
    execution_source = _execution_source_status(contract, runtime_script_path)
    run_state_path = Path(contract["paths"]["run_state"])
    run_state: dict[str, Any]
    if not run_state_path.exists():
        run_state = {"ok": True, "exists": False, "wake_count": 0, "failure_latch": None}
    else:
        try:
            state = _read_object(run_state_path)
            validate_run_state(state, contract)
            run_state = {
                "ok": True,
                "exists": True,
                "wake_count": state["wake_count"],
                "failure_latch": state.get("failure_latch"),
                "inflight_action": (
                    {
                        key: value
                        for key, value in state["inflight_action"].items()
                        if key != "owner_token"
                    }
                    if isinstance(state.get("inflight_action"), dict)
                    else state.get("inflight_action")
                ),
                "last_result": state.get("last_result"),
            }
        except RunContractDriftError as error:
            run_state = {
                "ok": False,
                "exists": True,
                "reason_code": "run_contract_drift",
                "error": str(error),
            }
        except Exception as error:
            run_state = {"ok": False, "exists": True, "error": str(error)}
    blockers = []
    if not authority_anchor["ok"]:
        blockers.append("run_contract_drift")
    if lease["status"] in {"active", "invalid"}:
        blockers.append("runner_lease_unavailable")
    if not checkpoint["ok"] or checkpoint["recovery_status"] != "none":
        blockers.append("checkpoint_recovery_required")
    if not installation["ok"]:
        blockers.append("installed_skill_verification_failed")
    if not execution_source["ok"]:
        blockers.append("heartbeat_not_running_from_verified_installation")
    if (
        not run_state["ok"]
        or run_state.get("failure_latch")
        or run_state.get("inflight_action")
    ):
        blockers.append(
            "run_contract_drift"
            if run_state.get("reason_code") == "run_contract_drift"
            else "recurring_state_recovery_required"
        )
    current_time = datetime.fromisoformat(now.replace("Z", "+00:00")).astimezone(UTC)
    if contract.get("expires_at") and current_time >= datetime.fromisoformat(
        contract["expires_at"].replace("Z", "+00:00")
    ).astimezone(UTC):
        blockers.append("run_contract_expired")
    if run_state.get("wake_count", 0) >= contract["maximum_wakes"]:
        blockers.append("wake_budget_exhausted")
    return {
        "schema_version": 1,
        "mode": "read_only_doctor",
        "generated_at": now,
        "mutations_performed": False,
        "lease_mutation_performed": False,
        "contract": {
            "repository": contract["repository"],
            "pull_request_number": contract["pull_request_number"],
            "maximum_wakes": contract["maximum_wakes"],
            "expires_at": contract["expires_at"],
            "connector_capability": contract["connector_capability"],
        },
        "lease": _redact_lease(lease),
        "authority_anchor": authority_anchor,
        "checkpoint": checkpoint,
        "installed_skill": installation,
        "execution_source": execution_source,
        "run_state": run_state,
        "blockers": blockers,
        "ready_for_bounded_recurring_pilot": not blockers,
    }


def plan_tick(
    *,
    contract_path: str | Path,
    repository_path: str | Path,
    observation: dict[str, Any],
    now: str,
    owner_token: str,
    lease_duration_seconds: int = 300,
    checkout_inspector=inspect_local_checkout,
    runtime_script_path: str | Path | None = None,
) -> dict[str, Any]:
    """Acquire authority, persist one wake, and return at most one next action."""
    if not owner_token:
        raise ValueError("An explicit lease owner token is required")
    try:
        contract = load_mutation_run_contract(
            contract_path,
            repository_path=repository_path,
            owner_token=owner_token,
        )
    except RunContractDriftError as error:
        return {
            "schema_version": 1,
            "run_status": "paused",
            "next_action": NextAction.PAUSE_BLOCKED.value,
            "reason_code": "run_contract_drift",
            "details": str(error),
            "mutation_occurred": False,
            "recommended_heartbeat_disposition": "pause",
            "lease": {"status": "released_or_absent"},
        }
    execution_source = _execution_source_status(contract, runtime_script_path)
    if not execution_source["ok"]:
        return {
            "schema_version": 1,
            "run_status": "paused",
            "next_action": NextAction.PAUSE_BLOCKED.value,
            "reason_code": "heartbeat_not_running_from_verified_installation",
            "execution_source": execution_source,
            "mutation_occurred": False,
            "recommended_heartbeat_disposition": "pause",
            "lease": {"status": "not_acquired"},
        }
    installation = _installation_status(contract)
    if not installation["ok"]:
        return {
            "schema_version": 1,
            "run_status": "paused",
            "next_action": NextAction.PAUSE_BLOCKED.value,
            "reason_code": "install_provenance_drift",
            "installed_skill": installation,
            "mutation_occurred": False,
            "recommended_heartbeat_disposition": "pause",
            "lease": {"status": "not_acquired"},
        }
    try:
        anchor = inspect_contract_authority_anchor(contract_path, contract)
        if not anchor["exists"]:
            anchor = persist_contract_authority_anchor(contract_path, contract)
    except RunContractDriftError as error:
        released = release_anchored_lease(contract_path, owner_token=owner_token)
        return {
            "schema_version": 1,
            "run_status": "paused",
            "next_action": NextAction.PAUSE_BLOCKED.value,
            "reason_code": "run_contract_drift",
            "details": str(error),
            "authority_anchor": {"status": "drift"},
            "mutation_occurred": False,
            "recommended_heartbeat_disposition": "pause",
            "lease": {"status": "released" if released else "not_owned"},
        }
    token = owner_token
    acquisition = acquire_lease(
        contract["paths"]["lease"],
        repository=contract["repository"],
        pr_number=contract["pull_request_number"],
        owner_token=token,
        now=now,
        duration_seconds=lease_duration_seconds,
    )
    if not acquisition["acquired"]:
        lease = acquisition["lease"]
        return {
            "schema_version": 1,
            "run_status": "paused",
            "next_action": NextAction.PAUSE_CONCURRENT.value,
            "reason_code": "concurrent_runner_lease_active",
            "lease": {
                "status": "active",
                "expires_at": lease.get("expires_at"),
            },
            "mutation_occurred": False,
            "recommended_heartbeat_disposition": "pause",
        }

    retained = False
    try:
        state_path = Path(contract["paths"]["run_state"])
        if state_path.exists():
            try:
                state = validate_run_state(_read_object(state_path), contract)
            except RunContractDriftError as error:
                return {
                    "schema_version": 1,
                    "run_status": "paused",
                    "next_action": NextAction.PAUSE_BLOCKED.value,
                    "reason_code": "run_contract_drift",
                    "details": str(error),
                    "lease": {"status": "releasing"},
                    "mutation_occurred": False,
                    "recommended_heartbeat_disposition": "pause",
                }
            except Exception as error:
                return {
                    "schema_version": 1,
                    "run_status": "paused",
                    "next_action": NextAction.PAUSE_BLOCKED.value,
                    "reason_code": "invalid_run_state_schema",
                    "details": str(error),
                    "lease": {"status": "owned"},
                    "mutation_occurred": False,
                    "recommended_heartbeat_disposition": "pause",
                }
        else:
            state = empty_run_state(contract)

        inflight = state.get("inflight_action")
        if isinstance(inflight, dict) and inflight.get("owner_token") != token:
            state = latch_failure(
                state,
                reason_code="abandoned_inflight_action",
                observed_at=now,
                details=inflight,
            )
            state["last_result"] = {
                "next_action": NextAction.PAUSE_RECOVERY.value,
                "reason_code": "abandoned_inflight_action",
                "recommended_heartbeat_disposition": "pause",
                "recorded_at": now,
            }
            save_checkpoint(state_path, state)
            return {
                "schema_version": 1,
                "run_status": "paused",
                "next_action": NextAction.PAUSE_RECOVERY.value,
                "reason_code": "abandoned_inflight_action",
                "recommended_heartbeat_disposition": "pause",
                "mutation_occurred": False,
                "lease": {"status": "releasing"},
            }

        current_time = datetime.fromisoformat(now.replace("Z", "+00:00")).astimezone(UTC)
        deadline = contract.get("expires_at")
        if state["wake_count"] >= contract["maximum_wakes"] or (
            deadline
            and current_time
            >= datetime.fromisoformat(deadline.replace("Z", "+00:00")).astimezone(UTC)
        ):
            return {
                "schema_version": 1,
                "run_status": "paused",
                "next_action": NextAction.PAUSE_EXPIRED.value,
                "reason_code": (
                    "wake_budget_exhausted"
                    if state["wake_count"] >= contract["maximum_wakes"]
                    else "run_deadline_expired"
                ),
                "wake_count": state["wake_count"],
                "maximum_wakes": contract["maximum_wakes"],
                "mutation_occurred": False,
                "recommended_heartbeat_disposition": "pause",
                "lease": {"status": "releasing"},
            }

        checkpoint = _checkpoint_status(contract)
        try:
            _require_persisted_snapshot(observation, checkpoint)
        except ValueError as error:
            return {
                "schema_version": 1,
                "run_status": "paused",
                "next_action": NextAction.PAUSE_BLOCKED.value,
                "reason_code": "snapshot_evidence_unavailable",
                "details": str(error),
                "mutation_occurred": False,
                "recommended_heartbeat_disposition": "pause",
                "lease": {"status": "releasing"},
            }
        local_checkout = checkout_inspector(
            repository_path,
            expected_head_repository=observation.get("head_repository"),
            expected_head_oid=observation.get("head_oid"),
            command_runner=run_command,
        )
        prepared = dict(observation)
        prepared["run_contract_ok"] = True
        prepared["install_ok"] = installation["ok"]
        prepared["recovery_status"] = checkpoint["recovery_status"]
        prepared["local_checkout_ok"] = local_checkout["ok"]
        prepared["lease_status"] = "owned"
        previous_head = state.get("last_head_oid")
        prepared["external_head_advance"] = bool(
            previous_head
            and prepared.get("head_oid") != previous_head
            and prepared.get("expected_head_transition_from") != previous_head
        )

        state = advance_observation_state(state, prepared)
        state["wake_count"] += 1
        decision = evaluate_recurring_action(
            contract=contract, observation=prepared, state=state, now=now
        )
        if decision.get("latch"):
            state = latch_failure(
                state,
                reason_code=decision["reason_code"],
                observed_at=now,
                details={"head_oid": prepared.get("head_oid")},
            )
        state["last_head_oid"] = prepared.get("head_oid")
        state["last_result"] = {
            **decision,
            "wake_count": state["wake_count"],
            "head_oid": prepared.get("head_oid"),
            "recorded_at": now,
        }
        retained = decision["next_action"] in {
            NextAction.RUN_BATCH.value,
            NextAction.REQUEST_REVIEW.value,
        }
        state["inflight_action"] = (
            {
                "next_action": decision["next_action"],
                "owner_token": token,
                "head_oid": prepared.get("head_oid"),
                "started_at": now,
            }
            if retained
            else None
        )
        save_checkpoint(state_path, state)
        targeted = prepared.get("targeted_thread_ids") or []
        non_target = prepared.get("non_target_thread_ids") or []
        active_batch = checkpoint.get("active_batch") or {}
        frozen_head_oid = active_batch.get("frozen_head_oid")
        if frozen_head_oid is None and decision["next_action"] == NextAction.RUN_BATCH.value:
            frozen_head_oid = prepared.get("head_oid")
        result = {
            "schema_version": 1,
            "run_status": (
                "completed"
                if decision["recommended_heartbeat_disposition"] == "complete"
                else "paused"
                if decision["recommended_heartbeat_disposition"] == "pause"
                else "active"
            ),
            **decision,
            "current_head_oid": prepared.get("head_oid"),
            "frozen_head_oid": frozen_head_oid,
            "targeted_count": len(targeted),
            "non_target_count": len(non_target),
            "active_batch_recovery": checkpoint["recovery_status"],
            "local_checkout": local_checkout,
            "approval_evidence": {
                "status": prepared.get("approval_status"),
                "ids": prepared.get("approval_evidence_ids") or [],
            },
            "trigger_state": state.get("trigger_events", {}).get(prepared.get("head_oid")),
            "wake_count": state["wake_count"],
            "maximum_wakes": contract["maximum_wakes"],
            "mutation_occurred": False,
            "lease": {
                "status": "retained" if retained else "releasing",
                "expires_at": acquisition["lease"]["expires_at"],
                "stale_recovered": acquisition["stale_recovered"],
            },
        }
        return result
    finally:
        if not retained:
            try:
                release_lease(
                    contract["paths"]["lease"],
                    repository=contract["repository"],
                    pr_number=contract["pull_request_number"],
                    owner_token=token,
                )
            except Exception:
                pass


def record_trigger(
    *,
    contract_path: str | Path,
    repository_path: str | Path,
    owner_token: str,
    evidence: dict[str, Any],
    now: str | None = None,
    runtime_script_path: str | Path | None = None,
) -> dict[str, Any]:
    """Record injected trigger evidence; this function never posts a comment."""
    try:
        contract = load_mutation_run_contract(
            contract_path,
            repository_path=repository_path,
            owner_token=owner_token,
        )
    except RunContractDriftError as error:
        return {
            "status": "paused",
            "reason_code": "run_contract_drift",
            "details": str(error),
            "mutation_occurred": False,
            "lease": {"status": "released_or_absent"},
        }
    try:
        anchor = inspect_contract_authority_anchor(contract_path, contract)
        if not anchor["exists"]:
            raise RunContractDriftError("run_contract_drift: authority anchor is missing")
    except RunContractDriftError as error:
        released = release_anchored_lease(contract_path, owner_token=owner_token)
        return {
            "status": "paused",
            "reason_code": "run_contract_drift",
            "details": str(error),
            "mutation_occurred": False,
            "lease": {"status": "released" if released else "not_owned"},
        }
    if not _execution_source_status(contract, runtime_script_path)["ok"]:
        raise RuntimeError("Heartbeat is not running from the verified installation")
    state_path = Path(contract["paths"]["run_state"])
    try:
        state = validate_run_state(_read_object(state_path), contract)
    except RunContractDriftError as error:
        release_lease(
            contract["paths"]["lease"],
            repository=contract["repository"],
            pr_number=contract["pull_request_number"],
            owner_token=owner_token,
        )
        return {
            "status": "paused",
            "reason_code": "run_contract_drift",
            "details": str(error),
            "mutation_occurred": False,
            "lease": {"status": "released"},
        }
    if not _installation_status(contract)["ok"]:
        raise RuntimeError("Heartbeat installation verification failed")
    if not contract["mutation_scope"]["review_trigger"]:
        raise RuntimeError("Run contract does not authorize a review trigger")
    if contract.get("review_trigger_head_oid") != evidence.get("attempted_head_oid"):
        raise RuntimeError("Run contract does not authorize a trigger for this head")
    assert_lease_owner(
        contract["paths"]["lease"],
        repository=contract["repository"],
        pr_number=contract["pull_request_number"],
        owner_token=owner_token,
        now=now or datetime.now(UTC).isoformat(),
    )
    state = record_trigger_result(
        state,
        attempted_head_oid=evidence["attempted_head_oid"],
        head_before=evidence["head_before"],
        head_after=evidence["head_after"],
        comment_node_id=evidence["comment_node_id"],
        created_at=evidence["created_at"],
    )
    try:
        assert_lease_owner(
            contract["paths"]["lease"],
            repository=contract["repository"],
            pr_number=contract["pull_request_number"],
            owner_token=owner_token,
            now=now or datetime.now(UTC).isoformat(),
        )
    except Exception:
        release_lease(
            contract["paths"]["lease"],
            repository=contract["repository"],
            pr_number=contract["pull_request_number"],
            owner_token=owner_token,
        )
        raise
    save_checkpoint(state_path, state)
    return state["trigger_events"][evidence["attempted_head_oid"]]


def complete_tick(
    *,
    contract_path: str | Path,
    repository_path: str | Path,
    owner_token: str,
    final_observation: dict[str, Any],
    now: str,
    mutation_occurred: bool,
    failure_reason: str | None = None,
    runtime_script_path: str | Path | None = None,
) -> dict[str, Any]:
    """Persist final evidence for the retained lease and release it safely."""
    try:
        contract = load_mutation_run_contract(
            contract_path,
            repository_path=repository_path,
            owner_token=owner_token,
        )
    except RunContractDriftError as error:
        return {
            "schema_version": 1,
            "run_status": "paused",
            "next_action": NextAction.PAUSE_BLOCKED.value,
            "reason_code": "run_contract_drift",
            "details": str(error),
            "mutation_occurred": mutation_occurred,
            "recommended_heartbeat_disposition": "pause",
            "lease": {"status": "released_or_absent"},
        }
    try:
        anchor = inspect_contract_authority_anchor(contract_path, contract)
        if not anchor["exists"]:
            raise RunContractDriftError("run_contract_drift: authority anchor is missing")
    except RunContractDriftError as error:
        released = release_anchored_lease(contract_path, owner_token=owner_token)
        return {
            "schema_version": 1,
            "run_status": "paused",
            "next_action": NextAction.PAUSE_BLOCKED.value,
            "reason_code": "run_contract_drift",
            "details": str(error),
            "mutation_occurred": mutation_occurred,
            "recommended_heartbeat_disposition": "pause",
            "lease": {"status": "released" if released else "not_owned"},
        }
    if not _execution_source_status(contract, runtime_script_path)["ok"]:
        raise RuntimeError("Heartbeat is not running from the verified installation")
    state_path = Path(contract["paths"]["run_state"])
    try:
        state = validate_run_state(_read_object(state_path), contract)
    except RunContractDriftError as error:
        release_lease(
            contract["paths"]["lease"],
            repository=contract["repository"],
            pr_number=contract["pull_request_number"],
            owner_token=owner_token,
        )
        return {
            "schema_version": 1,
            "run_status": "paused",
            "next_action": NextAction.PAUSE_BLOCKED.value,
            "reason_code": "run_contract_drift",
            "details": str(error),
            "mutation_occurred": mutation_occurred,
            "recommended_heartbeat_disposition": "pause",
            "lease": {"status": "released"},
        }
    if not _installation_status(contract)["ok"]:
        raise RuntimeError("Heartbeat installation verification failed")
    try:
        assert_lease_owner(
            contract["paths"]["lease"],
            repository=contract["repository"],
            pr_number=contract["pull_request_number"],
            owner_token=owner_token,
            now=now,
        )
    except Exception as error:
        return {
            "schema_version": 1,
            "run_status": "paused",
            "next_action": NextAction.PAUSE_CONCURRENT.value,
            "reason_code": "lease_lost",
            "details": str(error),
            "mutation_occurred": mutation_occurred,
            "recommended_heartbeat_disposition": "pause",
            "lease": {"status": "lost"},
        }
    checkpoint = _checkpoint_status(contract)
    try:
        if not checkpoint["ok"]:
            raise ValueError(
                "Persisted checkpoint is invalid: "
                f"{checkpoint.get('error', 'unknown checkpoint error')}"
            )
        _require_persisted_snapshot(final_observation, checkpoint)
    except ValueError as error:
        release_lease(
            contract["paths"]["lease"],
            repository=contract["repository"],
            pr_number=contract["pull_request_number"],
            owner_token=owner_token,
        )
        return {
            "schema_version": 1,
            "run_status": "paused",
            "next_action": NextAction.PAUSE_BLOCKED.value,
            "reason_code": "snapshot_evidence_unavailable",
            "details": str(error),
            "mutation_occurred": mutation_occurred,
            "recommended_heartbeat_disposition": "pause",
            "lease": {"status": "released"},
        }
    try:
        head_expectation = _completion_head_expectation(state, checkpoint)
        observed_head = final_observation.get("head_oid")
        expected_heads = head_expectation["expected_head_oids"]
        if not isinstance(observed_head, str) or not any(
            observed_head.casefold() == expected.casefold()
            for expected in expected_heads
        ):
            raise ValueError(
                "Final observation head does not match the retained action or published commit"
            )
    except ValueError as error:
        evidence = {
            "observed_head_oid": final_observation.get("head_oid"),
            "expected_head_oids": (
                head_expectation["expected_head_oids"]
                if "head_expectation" in locals()
                else []
            ),
            "action": (
                head_expectation.get("action")
                if "head_expectation" in locals()
                else None
            ),
        }
        state = latch_failure(
            state,
            reason_code="unexpected_remote_head_advance",
            observed_at=now,
            details={**evidence, "error": str(error)},
        )
        state["inflight_action"] = None
        state["last_result"] = {
            "next_action": NextAction.PAUSE_RECOVERY.value,
            "reason_code": "unexpected_remote_head_advance",
            "recorded_at": now,
            "mutation_occurred": mutation_occurred,
            "evidence": evidence,
        }
        save_checkpoint(state_path, state)
        release_lease(
            contract["paths"]["lease"],
            repository=contract["repository"],
            pr_number=contract["pull_request_number"],
            owner_token=owner_token,
        )
        return {
            "schema_version": 1,
            "run_status": "paused",
            "next_action": NextAction.PAUSE_RECOVERY.value,
            "reason_code": "unexpected_remote_head_advance",
            "details": evidence,
            "mutation_occurred": mutation_occurred,
            "recommended_heartbeat_disposition": "pause",
            "lease": {"status": "released"},
        }
    if failure_reason:
        state = latch_failure(
            state, reason_code=failure_reason, observed_at=now, details=final_observation
        )
        decision = {
            "next_action": NextAction.PAUSE_RECOVERY.value,
            "reason_code": "failure_latched",
            "recommended_heartbeat_disposition": "pause",
        }
    else:
        prepared = dict(final_observation)
        prepared.setdefault("run_contract_ok", True)
        prepared.setdefault("install_ok", True)
        prepared.setdefault("recovery_status", "none")
        prepared["lease_status"] = "owned"
        state = advance_observation_state(state, prepared)
        decision = evaluate_recurring_action(
            contract=contract, observation=prepared, state=state, now=now
        )
        if decision.get("latch"):
            state = latch_failure(
                state, reason_code=decision["reason_code"], observed_at=now
            )
        state["last_head_oid"] = prepared.get("head_oid")
    state["last_result"] = {
        **decision,
        "recorded_at": now,
        "mutation_occurred": mutation_occurred,
    }
    state["inflight_action"] = None
    save_checkpoint(state_path, state)
    release_lease(
        contract["paths"]["lease"],
        repository=contract["repository"],
        pr_number=contract["pull_request_number"],
        owner_token=owner_token,
    )
    return {
        "schema_version": 1,
        "run_status": (
            "paused"
            if decision["recommended_heartbeat_disposition"] == "pause"
            else "completed"
            if decision["recommended_heartbeat_disposition"] == "complete"
            else "active"
        ),
        **decision,
        "wake_count": state["wake_count"],
        "mutation_occurred": mutation_occurred,
        "lease": {"status": "released"},
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Plan or inspect one bounded heartbeat wake")
    parser.add_argument("--contract", required=True, type=Path)
    parser.add_argument("--repository-path", required=True, type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    doctor_command = commands.add_parser("doctor")
    doctor_command.add_argument("--now")
    plan = commands.add_parser("plan")
    plan.add_argument("--observation", required=True, type=Path)
    plan.add_argument("--now")
    plan.add_argument("--owner-token", required=True)
    plan.add_argument("--lease-duration-seconds", type=int, default=300)
    trigger = commands.add_parser("record-trigger")
    trigger.add_argument("--owner-token", required=True)
    trigger.add_argument("--evidence", required=True, type=Path)
    complete = commands.add_parser("complete")
    complete.add_argument("--owner-token", required=True)
    complete.add_argument("--observation", required=True, type=Path)
    complete.add_argument("--now")
    complete.add_argument("--mutation-occurred", action="store_true")
    complete.add_argument("--failure-reason")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    now = _now(getattr(args, "now", None))
    common = {
        "contract_path": args.contract,
        "repository_path": args.repository_path,
    }
    if args.command == "doctor":
        result = doctor(now=now, **common)
    elif args.command == "plan":
        result = plan_tick(
            observation=_read_object(args.observation),
            now=now,
            owner_token=args.owner_token,
            lease_duration_seconds=args.lease_duration_seconds,
            **common,
        )
    elif args.command == "record-trigger":
        result = record_trigger(
            owner_token=args.owner_token,
            evidence=_read_object(args.evidence),
            **common,
        )
    else:
        result = complete_tick(
            owner_token=args.owner_token,
            final_observation=_read_object(args.observation),
            now=now,
            mutation_occurred=args.mutation_occurred,
            failure_reason=args.failure_reason,
            **common,
        )
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from error
