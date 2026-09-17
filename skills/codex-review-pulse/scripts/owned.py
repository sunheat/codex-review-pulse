#!/usr/bin/env python3
"""The deterministic owned product boundaries of Phase 2.

These are the only product-facing entry points that may:

- create a campaign from post-lock owned evidence (normal setup);
- prepare and apply the fixed C1-to-C2 rollover transition;
- make the one deterministic owned worker decision per acquired delivery;
- record the environment-gate hard-failure handoff (``record_hard_failure``),
  the one guarded transition that forfeits the remaining budget and
  terminalizes ``hard_failed``.

Every helper establishes matching Phase 1 ownership itself and persists only
through the guarded Phase 1 primitives; the observation boundaries obtain
their own fresh authoritative GitHub evidence while the permanent lock remains
held and the canonical sidecar guard stays released (the hard-failure handoff
performs no observation at all). Callers cannot select snapshots, creation
evidence, terminal statuses, proof bases, round commitment, or ownership
dispositions. No remediation editing, review-request POST, Git push, issue
creation, thread resolution, or native scheduler mutation happens here.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
import sys
from typing import Any, Callable

import admission
import campaign_model as model
import github_api
import storage


FetchSnapshot = Callable[[], dict[str, Any]]


class OwnedBoundaryError(RuntimeError):
    """A handled deterministic failure inside one owned boundary."""


def _utc_now() -> str:
    """Local wall-clock stamp for gate transitions that observe nothing.

    The hard-failure handoff deliberately performs no GitHub observation, so
    no authoritative server time exists; the timestamp is diagnostic only and
    no correctness decision orders on it.
    """
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _load_campaign(
    repository: str, pr_number: int, *, repository_path: str | Path
) -> dict[str, Any]:
    path = storage.campaign_path(repository, pr_number, repository_path=repository_path)
    campaign = storage.load_json(path)
    if campaign is None:
        raise RuntimeError(f"Campaign record does not exist: {path}")
    model.validate_campaign(campaign, repository=repository, pull_request_number=pr_number)
    return campaign


def _validate_observation_identity(
    snapshot: dict[str, Any], *, repository: str, pr_number: int
) -> None:
    """Fail on any incomplete, foreign, or untimestamped owned observation."""
    if not isinstance(snapshot, dict) or snapshot.get("complete") is not True:
        raise OwnedBoundaryError("Owned observation is incomplete")
    if snapshot.get("repository") != model.canonical_repository(repository):
        raise OwnedBoundaryError("Owned observation belongs to a different repository")
    if snapshot.get("pr_number") != pr_number:
        raise OwnedBoundaryError("Owned observation belongs to a different pull request")
    if not isinstance(snapshot.get("pr_state"), str) or not snapshot.get("pr_state"):
        raise OwnedBoundaryError("Owned observation has no pull-request state")
    if not isinstance(snapshot.get("head_oid"), str) or not snapshot.get("head_oid"):
        raise OwnedBoundaryError("Owned observation has no head OID")
    try:
        model.parse_timestamp(snapshot.get("server_time"))
    except ValueError as error:
        raise OwnedBoundaryError(
            "Owned observation has no authoritative server time"
        ) from error


def _validate_creation_target(
    snapshot: dict[str, Any], *, repository: str, pr_number: int
) -> None:
    """Validate the full supported-creation target on the owned snapshot."""
    _validate_observation_identity(snapshot, repository=repository, pr_number=pr_number)
    if snapshot.get("pr_state") != "OPEN":
        raise OwnedBoundaryError(
            f"Pull request is {snapshot.get('pr_state')}; the target is not open"
        )
    if snapshot.get("head_repository") != model.canonical_repository(repository):
        raise OwnedBoundaryError(
            "Pull request head repository does not match the base repository; "
            "fork publication is not supported"
        )
    if not isinstance(snapshot.get("head_ref_name"), str) or not snapshot.get(
        "head_ref_name"
    ):
        raise OwnedBoundaryError("Owned observation has no supported head ref")


def _campaign_config(
    *,
    max_rounds: int,
    model_name: str,
    reasoning_level: str,
    interval_minutes: int,
    reviewer_logins: list[str] | None,
    approval_logins: list[str] | None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Normalized configuration plus the resolved identity collections."""
    reviewers = model.unique_logins(reviewer_logins, label="reviewer")
    approvers = model.unique_logins(approval_logins, label="approval")
    config = {
        "max_rounds": max_rounds,
        "model": model_name,
        "reasoning_level": reasoning_level,
        "interval_minutes": interval_minutes,
        "reviewer_logins": reviewers,
        "approval_logins": approvers,
    }
    return config, reviewers, approvers


def create_campaign_owned(
    *,
    repository: str,
    pr_number: int,
    owner_token: str,
    max_rounds: int,
    model_name: str,
    reasoning_level: str,
    interval_minutes: int,
    reviewer_logins: list[str] | None = None,
    approval_logins: list[str] | None = None,
    fetch_snapshot: FetchSnapshot,
    repository_path: str | Path = ".",
) -> dict[str, Any]:
    """Deterministic owned campaign creation (normal setup).

    The matching setup lock already carries the preallocated campaign identity.
    This helper itself verifies the setup ownership condition, obtains one
    fresh complete authoritative snapshot while ownership remains held,
    validates the target, derives all creation evidence from that snapshot,
    and persists through the Phase 1 guarded initialization primitive. The
    caller cannot select the snapshot, its created_at, or any target evidence.

    A handled deterministic failure before campaign persistence cancels the
    clean setup (Phase 1 owner-authorized cancellation) and returns a
    structured rejection. If cancellation itself cannot complete, the permanent
    setup lock stays in place fail closed for explicit recovery.
    """
    canonical = model.canonical_repository(repository)
    metadata = storage.verify_owner(
        repository, pr_number, owner_token, repository_path=repository_path
    )
    campaign_id = metadata["campaign_id"]
    if storage.campaign_path(
        repository, pr_number, repository_path=repository_path
    ).exists():
        raise OwnedBoundaryError(
            "Campaign record already exists; setup ownership no longer applies"
        )

    config, reviewers, approvers = _campaign_config(
        max_rounds=max_rounds,
        model_name=model_name,
        reasoning_level=reasoning_level,
        interval_minutes=interval_minutes,
        reviewer_logins=reviewer_logins,
        approval_logins=approval_logins,
    )
    try:
        snapshot = fetch_snapshot()
        _validate_creation_target(snapshot, repository=canonical, pr_number=pr_number)
        campaign = model.new_campaign(
            campaign_id=campaign_id,
            repository=canonical,
            pull_request_number=pr_number,
            created_at=snapshot["server_time"],
            max_rounds=config["max_rounds"],
            model=config["model"],
            reasoning_level=config["reasoning_level"],
            interval_minutes=config["interval_minutes"],
            reviewer_logins=reviewers,
            approval_logins=approvers,
            creation_baseline=model.creation_baseline_reaction_ids(
                snapshot, reviewer_logins=reviewers, approval_logins=approvers
            ),
        )
        model.validate_campaign(
            campaign, repository=canonical, pull_request_number=pr_number
        )
    except (OwnedBoundaryError, ValueError, RuntimeError) as error:
        reason = storage.error_text(error)
        try:
            storage.cancel_setup(
                repository,
                pr_number,
                owner_token=owner_token,
                expected_campaign_id=campaign_id,
                repository_path=repository_path,
            )
        except Exception as cancel_error:  # noqa: BLE001 - fail closed below
            return {
                "created": False,
                "cancelled": False,
                "fail_closed": True,
                "reason": reason,
                "cancellation_error": storage.error_text(cancel_error),
            }
        return {"created": False, "cancelled": True, "reason": reason}

    try:
        storage.initialize_campaign(
            repository,
            pr_number,
            owner_token=owner_token,
            campaign=campaign,
            repository_path=repository_path,
        )
    except Exception as error:  # noqa: BLE001 - guarded initialization refused
        return {
            "created": False,
            "fail_closed": True,
            "reason": storage.error_text(error),
        }
    return {
        "created": True,
        "campaign_id": campaign_id,
        "campaign": campaign,
        "head_oid": snapshot["head_oid"],
        "head_ref_name": snapshot["head_ref_name"],
        "server_time": snapshot["server_time"],
    }


def prepare_rollover_owned(
    *,
    repository: str,
    pr_number: int,
    owner_token: str,
    max_rounds: int,
    model_name: str,
    reasoning_level: str,
    interval_minutes: int,
    reviewer_logins: list[str] | None = None,
    approval_logins: list[str] | None = None,
    fetch_snapshot: FetchSnapshot,
    repository_path: str | Path = ".",
) -> dict[str, Any]:
    """Deterministic owned rollover preparation and transition.

    Establishes exact matching terminal-C1 rollover ownership, obtains one
    fresh complete authoritative snapshot itself, validates the target,
    derives C2's creation time and lifecycle baseline from that snapshot,
    allocates the exact C2 identity, and applies the Phase 1 fixed lock-first
    C1-to-C2 transition. The caller cannot select C2 creation evidence.

    A handled failure before the first identity replacement leaves C1
    unchanged, releases matching terminal-C1 ownership deterministically, and
    returns a structured rejection. If identity replacement may have begun,
    the state is preserved fail closed for explicit recovery.
    """
    canonical = model.canonical_repository(repository)
    metadata = storage.verify_owner(
        repository, pr_number, owner_token, repository_path=repository_path
    )
    c1 = _load_campaign(repository, pr_number, repository_path=repository_path)
    if c1["campaign_id"] != metadata["campaign_id"]:
        raise OwnedBoundaryError(
            "Ownership lock belongs to a different campaign; refusing rollover"
        )
    if not model.is_rollover_eligible(c1):
        raise OwnedBoundaryError("Campaign is not an eligible rollover source")

    config, reviewers, approvers = _campaign_config(
        max_rounds=max_rounds,
        model_name=model_name,
        reasoning_level=reasoning_level,
        interval_minutes=interval_minutes,
        reviewer_logins=reviewer_logins,
        approval_logins=approval_logins,
    )
    try:
        snapshot = fetch_snapshot()
        _validate_creation_target(snapshot, repository=canonical, pr_number=pr_number)
        campaign_id = model.new_campaign_id(snapshot["server_time"])
        if campaign_id == c1["campaign_id"]:
            raise OwnedBoundaryError("Generated C2 identity collides with C1")
        c2 = model.new_campaign(
            campaign_id=campaign_id,
            repository=canonical,
            pull_request_number=pr_number,
            created_at=snapshot["server_time"],
            max_rounds=config["max_rounds"],
            model=config["model"],
            reasoning_level=config["reasoning_level"],
            interval_minutes=config["interval_minutes"],
            reviewer_logins=reviewers,
            approval_logins=approvers,
            creation_baseline=model.creation_baseline_reaction_ids(
                snapshot, reviewer_logins=reviewers, approval_logins=approvers
            ),
        )
        model.validate_campaign(
            c2, repository=canonical, pull_request_number=pr_number
        )
    except (OwnedBoundaryError, ValueError, RuntimeError) as error:
        # Clean rejection before the first identity replacement: C1 unchanged,
        # no C2, matching terminal-C1 ownership released deterministically.
        try:
            release = storage.release_lock(
                repository, pr_number, owner_token, repository_path=repository_path
            )
        except Exception as release_error:  # noqa: BLE001 - fail closed below
            return {
                "rolled_over": False,
                "released": False,
                "fail_closed": True,
                "reason": storage.error_text(error),
                "release_error": storage.error_text(release_error),
            }
        return {
            "rolled_over": False,
            "released": release.get("released") is True,
            "reason": storage.error_text(error),
        }

    try:
        transition = storage.rollover_campaign_identity(
            repository,
            pr_number,
            owner_token=owner_token,
            proposed_campaign=c2,
            repository_path=repository_path,
        )
    except Exception as error:  # noqa: BLE001 - identity replacement is uncertain
        return {
            "rolled_over": False,
            "fail_closed": True,
            "reason": storage.error_text(error),
        }
    return {
        "rolled_over": True,
        "campaign_id": transition["campaign_id"],
        "previous_campaign_id": transition["previous_campaign_id"],
        "head_oid": snapshot["head_oid"],
        "head_ref_name": snapshot["head_ref_name"],
        "server_time": snapshot["server_time"],
    }


def _fail_closed(reason: str) -> dict[str, Any]:
    return {
        "outcome": "local_fail_closed",
        "reason": reason,
        "ownership": "retained",
        "scheduler_cleanup_authorized": False,
    }


_WAIT_DIRECTIVES = {
    "wait_observation_incomplete",
    "wait_review_in_progress",
    "wait_request_outstanding",
    "wait_lifecycle_attribution_unknown",
}


def _terminal_detail(directive: dict[str, Any]) -> str | None:
    """Deterministic in-memory detail derived from the decision itself."""
    for key in ("detail", "review_id", "comment_id"):
        value = directive.get(key)
        if isinstance(value, str) and value:
            return value
    proof = directive.get("proof")
    if isinstance(proof, dict) and isinstance(proof.get("id"), str):
        return f"approval:{proof['id']}"
    return None


def run_worker_decision(
    *,
    repository: str,
    pr_number: int,
    owner_token: str,
    fetch_snapshot: FetchSnapshot,
    repository_path: str | Path = ".",
) -> dict[str, Any]:
    """One deterministic owned-worker decision boundary.

    Invoked exactly once per acquired delivery after Phase 1 worker
    acquisition. This helper owns the correctness-critical sequence: matching
    active ownership, campaign-wide durable-local preflight, one fresh owned
    S1, guarded sync-head, authoritative post-sync campaign, pure decision,
    effective-action commitment or bounded S2 terminal confirmation, and the
    safe local ownership disposition. It performs no remediation editing, no
    review-request POST, no Git push, no issue creation, and no thread
    resolution; committed actions are externalized later (Phase 3).

    The returned outcome is a closed structured result with unambiguous local
    postconditions. Unknown directives or inconsistent combinations fail
    closed.
    """
    canonical = model.canonical_repository(repository)

    def release() -> bool:
        try:
            storage.release_lock(
                repository, pr_number, owner_token, repository_path=repository_path
            )
            return True
        except Exception:  # noqa: BLE001 - release must never be guessed
            return False

    # 1. Establish matching active Phase 1 ownership.
    try:
        metadata = storage.ensure_active_campaign_owner(
            repository, pr_number, owner_token, repository_path=repository_path
        )
        campaign = _load_campaign(repository, pr_number, repository_path=repository_path)
    except Exception as error:  # noqa: BLE001 - fail closed
        return _fail_closed(storage.error_text(error))
    campaign_id = metadata["campaign_id"]
    if campaign["campaign_id"] != campaign_id:
        return _fail_closed("Ownership lock belongs to a different campaign")

    # 2. Campaign-wide durable-local preflight, before any network observation.
    #    No S1 is fetched merely to prove this local result.
    reserved = model.reserved_guard_ambiguity(campaign)
    if reserved is not None:
        try:
            storage.transition_active_campaign(
                repository,
                pr_number,
                owner_token=owner_token,
                transition=lambda record: model.terminate(
                    record,
                    status=model.AMBIGUOUS_INTERRUPTION,
                    at=metadata["acquired_at"],
                    detail=(
                        "reserved request attempt for head "
                        f"{reserved.get('head_oid')} has no creation result"
                    ),
                ),
                repository_path=repository_path,
            )
        except Exception as error:  # noqa: BLE001 - fail closed
            return _fail_closed(storage.error_text(error))
        return {
            "outcome": "terminal_retained",
            "campaign_id": campaign_id,
            "status": model.AMBIGUOUS_INTERRUPTION,
            "terminal_basis": "durable_local",
            "guard_head": reserved.get("head_oid"),
            "round_committed": False,
            "ownership": "retained",
            "scheduler_cleanup_authorized": False,
        }

    # 3. Fetch exactly one fresh complete authoritative S1 while the permanent
    #    lock stays held and the sidecar guard stays released.
    try:
        s1 = fetch_snapshot()
        _validate_observation_identity(s1, repository=canonical, pr_number=pr_number)
    except Exception as error:  # noqa: BLE001 - no S1-derived mutation follows
        if release():
            return {
                "outcome": "observation_failed_released",
                "campaign_id": campaign_id,
                "reason": storage.error_text(error),
                "round_committed": False,
                "ownership": "released",
                "scheduler_cleanup_authorized": False,
            }
        return _fail_closed(
            "observation failed and release could not be confirmed: "
            + storage.error_text(error)
        )

    # 4. Guarded sync-head using S1, then the authoritative post-sync campaign.
    try:
        post_sync = storage.transition_active_campaign(
            repository,
            pr_number,
            owner_token=owner_token,
            transition=lambda record: model.supersede_active_guards(
                record, current_head_oid=s1["head_oid"], at=s1["server_time"]
            ),
            repository_path=repository_path,
        )
    except Exception as error:  # noqa: BLE001 - fail closed
        return _fail_closed(storage.error_text(error))

    # 5. Pure decision from the post-sync campaign and S1.
    directive = model.decide(post_sync, s1)
    action = directive.get("action")

    if action in _WAIT_DIRECTIVES:
        if not release():
            return _fail_closed("wait outcome could not be released")
        return {
            "outcome": "wait_released",
            "campaign_id": campaign_id,
            "directive": directive,
            "round_committed": False,
            "ownership": "released",
            "scheduler_cleanup_authorized": False,
        }

    if action == "campaign_terminal":
        return _fail_closed(
            f"campaign became terminal during the owned decision (status={directive.get('status')})"
        )

    if action == "remediation_batch":
        try:
            snapshot_path = admission.write_private_snapshot(s1)
        except Exception as error:  # noqa: BLE001 - fail closed before commitment
            return _fail_closed(
                "could not persist the owned decision snapshot: "
                + storage.error_text(error)
            )
        try:
            committed = storage.apply_campaign_transition_if_current(
                repository,
                pr_number,
                owner_token=owner_token,
                expected_source=post_sync,
                transition=lambda record: model.consume_round(record, kind="remediation"),
                repository_path=repository_path,
            )
        except Exception as error:  # noqa: BLE001 - commitment refused
            try:
                Path(snapshot_path).unlink()
            except OSError:
                pass
            return _fail_closed(
                "remediation commitment refused: " + storage.error_text(error)
            )
        return {
            "outcome": "remediation_committed",
            "campaign_id": committed["campaign_id"],
            "threads": directive["threads"],
            "snapshot_path": snapshot_path,
            "expected_head": s1["head_oid"],
            "round_committed": True,
            "rounds_used": committed["rounds_used"],
            "ownership": "retained",
            "scheduler_cleanup_authorized": False,
        }

    if action == "request_review":
        try:
            committed = storage.apply_campaign_transition_if_current(
                repository,
                pr_number,
                owner_token=owner_token,
                expected_source=post_sync,
                transition=lambda record: model.reserve_request(
                    record,
                    head_oid=s1["head_oid"],
                    reserved_at=s1["server_time"],
                    snapshot=s1,
                ),
                repository_path=repository_path,
            )
        except Exception as error:  # noqa: BLE001 - commitment refused
            return _fail_closed(
                "request commitment refused: " + storage.error_text(error)
            )
        guard = model.guard_for_head(committed, s1["head_oid"])
        return {
            "outcome": "request_committed",
            "campaign_id": committed["campaign_id"],
            "guard_head": guard["head_oid"] if guard else None,
            "guard_state": guard.get("state") if guard else None,
            "round_committed": True,
            "rounds_used": committed["rounds_used"],
            "ownership": "retained",
            "scheduler_cleanup_authorized": False,
        }

    if action == "terminal":
        status = directive.get("status")
        basis = directive.get("basis")
        if status not in model.TERMINAL_STATUSES or basis not in (
            "durable_local",
            "observation",
        ):
            return _fail_closed(
                f"unknown terminal directive (status={status!r}, basis={basis!r})"
            )

        def disposition(detail: str | None) -> dict[str, Any]:
            if status == model.AMBIGUOUS_INTERRUPTION:
                return {
                    "outcome": "terminal_retained",
                    "campaign_id": campaign_id,
                    "status": status,
                    "terminal_basis": basis,
                    "status_detail": detail,
                    "round_committed": False,
                    "ownership": "retained",
                    "scheduler_cleanup_authorized": False,
                }
            if not release():
                return _fail_closed(
                    f"terminal outcome {status} could not be released"
                )
            return {
                "outcome": "terminal_released",
                "campaign_id": campaign_id,
                "status": status,
                "terminal_basis": basis,
                "status_detail": detail,
                "round_committed": False,
                "ownership": "released",
                "scheduler_cleanup_authorized": True,
            }

        if basis == "durable_local":
            # Durable campaign state alone proves terminality; no S2 is needed.
            # Unreachable after the preflight, kept deterministic regardless.
            detail = _terminal_detail(directive)
            try:
                storage.apply_campaign_transition_if_current(
                    repository,
                    pr_number,
                    owner_token=owner_token,
                    expected_source=post_sync,
                    transition=lambda record: model.terminate(
                        record,
                        status=status,
                        at=metadata["acquired_at"],
                        detail=detail,
                    ),
                    repository_path=repository_path,
                )
            except Exception as error:  # noqa: BLE001 - fail closed
                return _fail_closed(storage.error_text(error))
            return disposition(detail)

        # Observation-derived: exactly one bounded S2 confirmation.
        try:
            s2 = fetch_snapshot()
            _validate_observation_identity(s2, repository=canonical, pr_number=pr_number)
        except Exception as error:  # noqa: BLE001 - no terminalization follows
            if release():
                return {
                    "outcome": "observation_failed_released",
                    "campaign_id": campaign_id,
                    "reason": "terminal confirmation failed: "
                    + storage.error_text(error),
                    "round_committed": False,
                    "ownership": "released",
                    "scheduler_cleanup_authorized": False,
                }
            return _fail_closed(
                "terminal confirmation failed and release could not be confirmed: "
                + storage.error_text(error)
            )

        # Head consistency is required for current-head-dependent results; a
        # target-unavailable conclusion does not depend on the head.
        if (
            status != model.TARGET_UNAVAILABLE
            and s2["head_oid"] != s1["head_oid"]
        ):
            if release():
                return {
                    "outcome": "observation_failed_released",
                    "campaign_id": campaign_id,
                    "reason": "confirmation head changed from the decision head",
                    "round_committed": False,
                    "ownership": "released",
                    "scheduler_cleanup_authorized": False,
                }
            return _fail_closed("confirmation head changed and release is unconfirmed")

        confirmation = model.decide(post_sync, s2)
        if (
            confirmation.get("action") != "terminal"
            or confirmation.get("status") != status
            or confirmation.get("basis") != "observation"
        ):
            if release():
                return {
                    "outcome": "observation_failed_released",
                    "campaign_id": campaign_id,
                    "reason": "fresh observation no longer supports the terminal decision",
                    "round_committed": False,
                    "ownership": "released",
                    "scheduler_cleanup_authorized": False,
                }
            return _fail_closed(
                "fresh observation contradicts the terminal decision and release "
                "is unconfirmed"
            )

        detail = _terminal_detail(confirmation) or _terminal_detail(directive)
        try:
            storage.apply_campaign_transition_if_current(
                repository,
                pr_number,
                owner_token=owner_token,
                expected_source=post_sync,
                transition=lambda record: model.terminate(
                    record, status=status, at=s2["server_time"], detail=detail
                ),
                repository_path=repository_path,
            )
        except Exception as error:  # noqa: BLE001 - fail closed
            return _fail_closed(storage.error_text(error))
        return disposition(detail)

    return _fail_closed(f"unknown owned-worker directive: {action!r}")


def record_hard_failure(
    *,
    repository: str,
    pr_number: int,
    campaign_id: str,
    reason: str,
    detail: str,
    repository_path: str | Path = ".",
    now: Callable[[], str] | None = None,
) -> dict[str, Any]:
    """Explicit handoff for a positively identified execution-environment failure.

    Production entry point behind the environment gate of the launcher and the
    scheduled worker. Call it only when the host authoritatively identifies an
    unsupported execution mode or missing Full access, or returns an explicit
    host-generated authorization, approval, policy, or sandbox denial for a
    required operation — and only before any external mutation has occurred,
    or after an operation is confirmed not to have occurred. It performs local
    durable work only, no GitHub observation or mutation:

    1. acquires the same worker ownership predicate as any scheduled delivery
       (busy, invalid, terminal, absent, or mismatched campaigns exit idle);
    2. applies one guarded transition that forfeits the entire remaining round
       budget and terminalizes the campaign as ``hard_failed`` with the reason
       code and concise diagnostic;
    3. releases ownership once the failure state is durably confirmed.

    Duplicate deliveries and stale campaign identities never modify a newer
    campaign; a lock owned by another worker is a non-counting idle exit.
    Unknown reason codes are refused before any shared state is touched.
    """
    canonical = model.canonical_repository(repository)
    if reason not in model.HARD_FAILURE_REASONS:
        raise ValueError(f"Unknown hard-failure reason: {reason!r}")
    if not isinstance(detail, str) or not detail.strip():
        raise ValueError("Hard-failure detail must be a non-empty string")
    at = (now or _utc_now)()

    try:
        acquisition = storage.acquire_worker_lock(
            canonical,
            pr_number,
            campaign_id=campaign_id,
            acquired_at=at,
            repository_path=repository_path,
        )
    except Exception as error:  # noqa: BLE001 - fail closed
        return {
            "recorded": False,
            "outcome": "local_fail_closed",
            "reason": storage.error_text(error),
            "ownership": "not_acquired",
            "scheduler_cleanup_authorized": False,
        }
    if not acquisition.get("acquired"):
        return {
            "recorded": False,
            "outcome": "idle_exit",
            "acquisition_status": acquisition.get("status"),
            "ownership": "not_acquired",
            "scheduler_cleanup_authorized": False,
        }
    token = acquisition["owner_token"]

    try:
        terminal = storage.transition_active_campaign(
            canonical,
            pr_number,
            owner_token=token,
            transition=lambda record: model.hard_fail(
                record, at=at, reason=reason, detail=detail
            ),
            repository_path=repository_path,
        )
    except Exception as error:  # noqa: BLE001 - persistence unconfirmed
        return {
            "recorded": False,
            "outcome": "persistence_unconfirmed",
            "reason": storage.error_text(error),
            "ownership": "retained",
            "scheduler_cleanup_authorized": False,
        }

    released = False
    release_error: str | None = None
    try:
        released = (
            storage.release_lock(
                canonical, pr_number, token, repository_path=repository_path
            ).get("released")
            is True
        )
    except Exception as error:  # noqa: BLE001 - release must never be guessed
        release_error = storage.error_text(error)
    result: dict[str, Any] = {
        "recorded": True,
        "outcome": "hard_failed",
        "campaign_id": terminal["campaign_id"],
        "status": terminal["status"],
        "reason": reason,
        "status_detail": terminal["status_detail"],
        "rounds_used": terminal["rounds_used"],
        "max_rounds": terminal["config"]["max_rounds"],
        "ownership": "released" if released else "retained",
        "scheduler_cleanup_authorized": released,
    }
    if release_error is not None:
        result["release_error"] = release_error
    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Deterministic owned campaign creation, rollover, worker decision, "
            "and environment-gate hard failure"
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--repo", required=True)
    common.add_argument("--pr", required=True, type=int)
    common.add_argument("--repository-path", default=".")
    common.add_argument("--owner-token", required=True)

    config_args = argparse.ArgumentParser(add_help=False)
    config_args.add_argument("--max-rounds", required=True, type=int)
    config_args.add_argument("--model", required=True, dest="model_name")
    config_args.add_argument("--reasoning-level", required=True)
    config_args.add_argument("--interval-minutes", required=True, type=int)
    config_args.add_argument(
        "--reviewer-login", action="append", dest="reviewer_logins"
    )
    config_args.add_argument(
        "--approval-login", action="append", dest="approval_logins"
    )

    subparsers.add_parser("create-campaign", parents=[common, config_args])
    subparsers.add_parser("prepare-rollover", parents=[common, config_args])
    subparsers.add_parser("worker-decision", parents=[common])

    hard_fail = subparsers.add_parser(
        "hard-fail",
        help=(
            "Environment-gate handoff: forfeit the entire remaining round "
            "budget and terminalize hard_failed for a positively identified "
            "unsupported execution mode, missing Full access, or explicit "
            "host authorization denial"
        ),
    )
    hard_fail.add_argument("--repo", required=True)
    hard_fail.add_argument("--pr", required=True, type=int)
    hard_fail.add_argument("--repository-path", default=".")
    hard_fail.add_argument("--campaign-id", required=True)
    hard_fail.add_argument("--reason", required=True, choices=sorted(model.HARD_FAILURE_REASONS))
    hard_fail.add_argument("--detail", required=True)

    args = parser.parse_args()

    def fetch() -> dict[str, Any]:
        return github_api.fetch_snapshot(args.repo, args.pr)

    if args.command == "create-campaign":
        result = create_campaign_owned(
            repository=args.repo,
            pr_number=args.pr,
            owner_token=args.owner_token,
            max_rounds=args.max_rounds,
            model_name=args.model_name,
            reasoning_level=args.reasoning_level,
            interval_minutes=args.interval_minutes,
            reviewer_logins=args.reviewer_logins,
            approval_logins=args.approval_logins,
            fetch_snapshot=fetch,
            repository_path=args.repository_path,
        )
    elif args.command == "prepare-rollover":
        result = prepare_rollover_owned(
            repository=args.repo,
            pr_number=args.pr,
            owner_token=args.owner_token,
            max_rounds=args.max_rounds,
            model_name=args.model_name,
            reasoning_level=args.reasoning_level,
            interval_minutes=args.interval_minutes,
            reviewer_logins=args.reviewer_logins,
            approval_logins=args.approval_logins,
            fetch_snapshot=fetch,
            repository_path=args.repository_path,
        )
    elif args.command == "hard-fail":
        result = record_hard_failure(
            repository=args.repo,
            pr_number=args.pr,
            campaign_id=args.campaign_id,
            reason=args.reason,
            detail=args.detail,
            repository_path=args.repository_path,
        )
    else:
        result = run_worker_decision(
            repository=args.repo,
            pr_number=args.pr,
            owner_token=args.owner_token,
            fetch_snapshot=fetch,
            repository_path=args.repository_path,
        )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(storage.error_text(error), file=sys.stderr)
        raise SystemExit(1) from error
