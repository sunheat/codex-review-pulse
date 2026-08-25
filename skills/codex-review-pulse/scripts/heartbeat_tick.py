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
from recurring_contract import load_run_contract
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
    }


def _installation_status(contract: dict[str, Any]) -> dict[str, Any]:
    expected = contract["expected_installation"]
    return verify_installation(
        expected["skill_path"],
        expected_version=expected["version"],
        expected_source_commit=expected["source_commit"],
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
        except Exception as error:
            run_state = {"ok": False, "exists": True, "error": str(error)}
    blockers = []
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
        blockers.append("recurring_state_recovery_required")
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
    contract = load_run_contract(contract_path, repository_path=repository_path)
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
    if not owner_token:
        raise ValueError("An explicit lease owner token is required")
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
        installation = _installation_status(contract)
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
    runtime_script_path: str | Path | None = None,
) -> dict[str, Any]:
    """Record injected trigger evidence; this function never posts a comment."""
    contract = load_run_contract(contract_path, repository_path=repository_path)
    if not _execution_source_status(contract, runtime_script_path)["ok"]:
        raise RuntimeError("Heartbeat is not running from the verified installation")
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
        now=evidence["created_at"],
    )
    state_path = Path(contract["paths"]["run_state"])
    state = validate_run_state(_read_object(state_path), contract)
    state = record_trigger_result(
        state,
        attempted_head_oid=evidence["attempted_head_oid"],
        head_before=evidence["head_before"],
        head_after=evidence["head_after"],
        comment_node_id=evidence["comment_node_id"],
        created_at=evidence["created_at"],
    )
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
    contract = load_run_contract(contract_path, repository_path=repository_path)
    if not _execution_source_status(contract, runtime_script_path)["ok"]:
        raise RuntimeError("Heartbeat is not running from the verified installation")
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
    state_path = Path(contract["paths"]["run_state"])
    state = validate_run_state(_read_object(state_path), contract)
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
