#!/usr/bin/env python3
"""Bounded recurring-run contracts and mutation-authority checks."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import re
import sys
from typing import Any

from checkpoint_store import checkpoint_path, runtime_artifact_path
from manage_pilot_install import verify_installation
from runner_lease import assert_lease_owner
from state_model import canonical_repository, unique_logins


RUN_CONTRACT_SCHEMA_VERSION = 1
MUTATION_KEYS = (
    "recurring_execution",
    "code_edits",
    "resolve_threads",
    "commit",
    "push",
    "review_trigger",
    "issue_creation",
    "merge",
    "auto_merge",
    "base_change",
    "force_push",
    "generic_reviewer_handling",
    "non_target_thread_resolution",
)
ALWAYS_DENIED = {
    "issue_creation",
    "merge",
    "auto_merge",
    "base_change",
    "force_push",
    "generic_reviewer_handling",
    "non_target_thread_resolution",
}
CONNECTOR_CAPABILITIES = {"unknown", "manual_trigger", "automatic_review"}


def _parse_timestamp(value: object, *, label: str, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{label} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None:
        raise ValueError(f"{label} must include a timezone")
    return parsed.isoformat()


def expected_runtime_paths(
    repository: str, pr_number: int, *, repository_path: str | Path
) -> dict[str, str]:
    return {
        "checkpoint": str(
            checkpoint_path(repository, pr_number, repository_path=repository_path).resolve()
        ),
        "lease": str(
            runtime_artifact_path(
                repository, pr_number, "lease.json", repository_path=repository_path
            ).resolve()
        ),
        "run_state": str(
            runtime_artifact_path(
                repository, pr_number, "run.json", repository_path=repository_path
            ).resolve()
        ),
    }


def validate_run_contract(
    contract: dict[str, Any], *, repository_path: str | Path | None = None
) -> dict[str, Any]:
    """Validate and normalize an explicit, non-expandable authorization contract."""
    if contract.get("schema_version") != RUN_CONTRACT_SCHEMA_VERSION:
        raise ValueError("Unsupported run-contract schema version")
    repository = canonical_repository(contract.get("repository", ""))
    pr_number = contract.get("pull_request_number")
    if not isinstance(pr_number, int) or isinstance(pr_number, bool) or pr_number < 1:
        raise ValueError("Run-contract pull request number must be positive")
    reviewers = unique_logins(contract.get("reviewer_logins"), label="reviewer")
    approvers = unique_logins(contract.get("approval_logins"), label="approval")

    installation = contract.get("expected_installation")
    if not isinstance(installation, dict):
        raise ValueError("Run contract requires expected installation provenance")
    version = installation.get("version")
    source_commit = installation.get("source_commit")
    skill_path = installation.get("skill_path")
    if not isinstance(version, str) or not version:
        raise ValueError("Expected skill version is required")
    if not isinstance(source_commit, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", source_commit):
        raise ValueError("Expected source commit must be a full Git OID")
    if not isinstance(skill_path, str) or not Path(skill_path).is_absolute():
        raise ValueError("Expected skill path must be absolute")

    mutation_scope = contract.get("mutation_scope")
    if not isinstance(mutation_scope, dict) or set(mutation_scope) != set(MUTATION_KEYS):
        raise ValueError("Run contract must explicitly set every mutation scope")
    if any(not isinstance(mutation_scope[key], bool) for key in MUTATION_KEYS):
        raise ValueError("Mutation-scope values must be booleans")
    forbidden = sorted(key for key in ALWAYS_DENIED if mutation_scope[key])
    if forbidden:
        raise ValueError("This release cannot authorize: " + ", ".join(forbidden))
    if not mutation_scope["recurring_execution"]:
        raise ValueError("Bounded recurring execution must be explicitly authorized")
    trigger_head_oid = contract.get("review_trigger_head_oid")
    if trigger_head_oid is not None and (
        not isinstance(trigger_head_oid, str)
        or not re.fullmatch(r"[0-9a-fA-F]{40}", trigger_head_oid)
    ):
        raise ValueError("Review-trigger head must be a full Git OID")
    if mutation_scope["review_trigger"] and trigger_head_oid is None:
        raise ValueError("Review-trigger authorization must name its exact head OID")

    maximum_wakes = contract.get("maximum_wakes")
    if not isinstance(maximum_wakes, int) or isinstance(maximum_wakes, bool) or maximum_wakes < 1:
        raise ValueError("maximum_wakes must be a positive integer")
    expires_at = _parse_timestamp(contract.get("expires_at"), label="expires_at", optional=True)
    runner_identity = contract.get("runner_identity")
    automation_identity = contract.get("automation_identity")
    authorization_id = contract.get("authorization_id")
    if any(not isinstance(value, str) or not value.strip() for value in (
        runner_identity, automation_identity, authorization_id
    )):
        raise ValueError("Runner, automation, and authorization identities are required")

    capability = contract.get("connector_capability", "unknown")
    if capability not in CONNECTOR_CAPABILITIES:
        raise ValueError("Unsupported connector capability")
    wait_policy = contract.get("wait_policy")
    if not isinstance(wait_policy, dict):
        raise ValueError("Run contract requires an explicit wait policy")
    minimum_seconds = wait_policy.get("minimum_server_wait_seconds")
    minimum_observations = wait_policy.get("minimum_stable_observations")
    if (
        not isinstance(minimum_seconds, int)
        or isinstance(minimum_seconds, bool)
        or minimum_seconds < 0
        or not isinstance(minimum_observations, int)
        or isinstance(minimum_observations, bool)
        or minimum_observations < 2
    ):
        raise ValueError("Wait policy requires nonnegative seconds and at least two observations")

    paths = contract.get("paths")
    if not isinstance(paths, dict) or set(paths) != {"checkpoint", "lease", "run_state"}:
        raise ValueError("Run contract requires checkpoint, lease, and run-state paths")
    if any(not isinstance(value, str) or not Path(value).is_absolute() for value in paths.values()):
        raise ValueError("Run-contract runtime paths must be absolute")
    if repository_path is not None:
        expected = expected_runtime_paths(repository, pr_number, repository_path=repository_path)
        actual = {key: str(Path(value).resolve()) for key, value in paths.items()}
        if actual != expected:
            raise ValueError(
                "Run-contract runtime paths do not match the target Git common directory"
            )

    normalized = dict(contract)
    normalized.update(
        {
            "repository": repository,
            "reviewer_logins": reviewers,
            "approval_logins": approvers,
            "expires_at": expires_at,
            "connector_capability": capability,
            "review_trigger_head_oid": (
                trigger_head_oid.casefold() if trigger_head_oid is not None else None
            ),
        }
    )
    normalized["expected_installation"] = {
        "version": version,
        "source_commit": source_commit.casefold(),
        "skill_path": str(Path(skill_path).resolve()),
    }
    normalized["paths"] = {
        key: str(Path(value).resolve()) for key, value in paths.items()
    }
    return normalized


def load_run_contract(
    path: str | Path, *, repository_path: str | Path | None = None
) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Run-contract root must be an object")
    return validate_run_contract(payload, repository_path=repository_path)


def assert_mutation_authority(
    contract: dict[str, Any],
    *,
    owner_token: str,
    required_scope: str,
    now: str | datetime | None = None,
    runtime_script_path: str | Path | None = None,
) -> dict[str, Any]:
    normalized = validate_run_contract(contract)
    if required_scope not in MUTATION_KEYS or not normalized["mutation_scope"][required_scope]:
        raise RuntimeError(f"Run contract does not authorize {required_scope}")
    if runtime_script_path is not None:
        actual = Path(runtime_script_path).resolve()
        expected = (
            Path(normalized["expected_installation"]["skill_path"])
            / "scripts"
            / actual.name
        ).resolve()
        if actual != expected or not actual.is_file():
            raise RuntimeError(
                "Recurring mutation entrypoint is not running from the verified installation"
            )
        installation = normalized["expected_installation"]
        verified = verify_installation(
            installation["skill_path"],
            expected_version=installation["version"],
            expected_source_commit=installation["source_commit"],
        )
        if not verified["ok"]:
            raise RuntimeError("Recurring mutation installation verification failed")
    return assert_lease_owner(
        normalized["paths"]["lease"],
        repository=normalized["repository"],
        pr_number=normalized["pull_request_number"],
        owner_token=owner_token,
        now=now,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate a bounded recurring run contract")
    parser.add_argument("contract", type=Path)
    parser.add_argument("--repository-path", type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    contract = load_run_contract(args.contract, repository_path=args.repository_path)
    print(json.dumps(contract, indent=2, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from error
