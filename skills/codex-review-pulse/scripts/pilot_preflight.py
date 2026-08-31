#!/usr/bin/env python3
"""Read-only readiness checks for a supervised Codex Review Pulse pilot."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any, Callable

from checkpoint_store import checkpoint_path, load_checkpoint, runtime_artifact_path
from fetch_pr_state import fetch_stable_snapshot, graphql
from manage_pilot_install import installation_path, verify_installation
from runner_lease import inspect_lease
from state_model import (
    DEFAULT_CODEX_LOGINS,
    SCHEMA_VERSION,
    canonical_repository,
    evaluate_snapshot,
    validate_checkpoint,
)


CommandRunner = Callable[[list[str]], subprocess.CompletedProcess[str]]
GraphqlCall = Callable[[str, str, str, int, str | None], dict[str, Any]]


def run_command(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True)


def _tool_check(
    name: str,
    version_command: list[str],
    *,
    command_runner: CommandRunner,
    which: Callable[[str], str | None],
) -> dict[str, Any]:
    executable = which(name)
    if executable is None:
        return {"ok": False, "path": None, "version": None, "error": "not_found"}
    process = command_runner(version_command)
    output = (process.stdout or process.stderr).strip().splitlines()
    return {
        "ok": process.returncode == 0,
        "path": executable,
        "version": output[0] if output else None,
        "error": None if process.returncode == 0 else "version_check_failed",
    }


def _checkpoint_diagnostic(
    path: Path, repository: str, pr_number: int
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    if not path.exists():
        return None, {
            "path": str(path),
            "exists": False,
            "schema_version": None,
            "expected_schema_version": SCHEMA_VERSION,
            "schema_valid": True,
            "active_batch": None,
            "recovery_required": False,
            "error": None,
        }
    state_path: Path | None = None
    try:
        checkpoint = load_checkpoint(path)
        if checkpoint is None:
            raise ValueError("checkpoint disappeared during inspection")
        validate_checkpoint(checkpoint, repository, pr_number)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        return None, {
            "path": str(path),
            "exists": True,
            "schema_version": None,
            "expected_schema_version": SCHEMA_VERSION,
            "schema_valid": False,
            "active_batch": None,
            "recovery_required": True,
            "error": str(error),
        }
    batch = checkpoint.get("active_batch")
    publication_status = (
        (batch.get("publication") or {}).get("status")
        if isinstance(batch, dict)
        else None
    )
    recovery_required = isinstance(batch, dict) and publication_status != "succeeded"
    batch_summary = None
    if isinstance(batch, dict):
        batch_summary = {
            "frozen_head_oid": batch.get("frozen_head_oid"),
            "targeted_thread_ids": batch.get("targeted_thread_ids") or [],
            "resolved_thread_ids": batch.get("resolved_thread_ids") or [],
            "publication": batch.get("publication"),
        }
    return checkpoint, {
        "path": str(path),
        "exists": True,
        "schema_version": checkpoint.get("schema_version"),
        "expected_schema_version": SCHEMA_VERSION,
        "schema_valid": True,
        "active_batch": batch_summary,
        "recovery_required": recovery_required,
        "error": None,
    }


def _github_repository_from_remote(remote_url: str) -> str | None:
    value = remote_url.strip().rstrip("/")
    patterns = (
        r"^https?://github\.com/([^/]+)/([^/]+?)(?:\.git)?$",
        r"^git@github\.com:([^/]+)/([^/]+?)(?:\.git)?$",
        r"^ssh://git@github\.com/([^/]+)/([^/]+?)(?:\.git)?$",
    )
    for pattern in patterns:
        match = re.match(pattern, value, flags=re.IGNORECASE)
        if match:
            return f"{match.group(1)}/{match.group(2)}"
    return None


def inspect_local_checkout(
    repository_path: str | Path,
    *,
    expected_head_repository: str | None,
    expected_head_oid: str,
    command_runner: CommandRunner,
) -> dict[str, Any]:
    path = Path(repository_path).resolve()

    def git(*arguments: str) -> subprocess.CompletedProcess[str]:
        return command_runner(["git", "-C", str(path), *arguments])

    inside = git("rev-parse", "--is-inside-work-tree")
    if inside.returncode != 0 or inside.stdout.strip().casefold() != "true":
        return {
            "ok": False,
            "path": str(path),
            "error": inside.stderr.strip() or "repository_path_is_not_a_git_worktree",
        }
    head = git("rev-parse", "HEAD")
    status = git("status", "--porcelain=v1", "--untracked-files=all")
    remotes = git("remote", "get-url", "--all", "origin")
    push_remotes = git("remote", "get-url", "--push", "--all", "origin")
    if any(
        process.returncode != 0 for process in (head, status, remotes, push_remotes)
    ):
        error = next(
            (
                process.stderr.strip()
                for process in (head, status, remotes, push_remotes)
                if process.returncode != 0 and process.stderr.strip()
            ),
            "local_checkout_inspection_failed",
        )
        return {"ok": False, "path": str(path), "error": error}
    remote_urls = [line.strip() for line in remotes.stdout.splitlines() if line.strip()]
    push_urls = [line.strip() for line in push_remotes.stdout.splitlines() if line.strip()]
    remote_repositories = [
        repository
        for repository in (
            _github_repository_from_remote(remote_url) for remote_url in remote_urls
        )
        if repository is not None
    ]
    push_repositories = [
        repository
        for repository in (
            _github_repository_from_remote(remote_url) for remote_url in push_urls
        )
        if repository is not None
    ]
    head_matches = head.stdout.strip() == expected_head_oid
    clean = not status.stdout.strip()
    fetch_remote_matches = (
        isinstance(expected_head_repository, str)
        and any(
            canonical_repository(repository)
            == canonical_repository(expected_head_repository)
            for repository in remote_repositories
        )
    )
    push_remote_matches = (
        isinstance(expected_head_repository, str)
        and any(
            canonical_repository(repository)
            == canonical_repository(expected_head_repository)
            for repository in push_repositories
        )
    )
    errors: list[str] = []
    if not clean:
        errors.append("target_checkout_dirty")
    if not head_matches:
        errors.append("target_checkout_head_mismatch")
    if not fetch_remote_matches:
        errors.append("target_checkout_origin_fetch_mismatch")
    if not push_remote_matches:
        errors.append("target_checkout_origin_push_mismatch")
    return {
        "ok": not errors,
        "path": str(path),
        "head_oid": head.stdout.strip(),
        "expected_head_oid": expected_head_oid,
        "clean": clean,
        "origin_urls": remote_urls,
        "origin_repositories": remote_repositories,
        "origin_push_urls": push_urls,
        "origin_push_repositories": push_repositories,
        "expected_head_repository": expected_head_repository,
        "errors": errors,
        "error": None,
    }


def build_preflight(
    *,
    repository: str,
    pr_number: int,
    repository_path: str | Path,
    reviewer_logins: list[str] | None,
    approval_logins: list[str] | None,
    state_file: str | Path | None,
    install_root: str | Path | None,
    expected_skill_version: str,
    expected_source_commit: str,
    single_runner_confirmed: bool,
    runtime_skill_path: str | Path | None = None,
    command_runner: CommandRunner = run_command,
    which: Callable[[str], str | None] = shutil.which,
    graphql_call: GraphqlCall = graphql,
) -> dict[str, Any]:
    """Return structured readiness evidence without persisting state."""
    result: dict[str, Any] = {
        "schema_version": 1,
        "mode": "read_only_preflight",
        "generated_at": datetime.now(UTC).isoformat(),
        "mutations_performed": False,
        "checkpoint_write_performed": False,
        "ready_for_supervised_pilot": False,
    }
    toolchain = {
        "python": {
            "ok": sys.version_info >= (3, 10),
            "path": sys.executable,
            "version": sys.version.split()[0],
            "error": None if sys.version_info >= (3, 10) else "python_3_10_required",
        },
        "git": _tool_check(
            "git", ["git", "--version"], command_runner=command_runner, which=which
        ),
        "gh": _tool_check(
            "gh", ["gh", "--version"], command_runner=command_runner, which=which
        ),
    }
    result["toolchain"] = toolchain
    if not all(item["ok"] for item in toolchain.values()):
        result["failure_phase"] = "toolchain"
        return result

    auth = command_runner(["gh", "auth", "status"])
    result["github_authentication"] = {
        "ok": auth.returncode == 0,
        "error": None if auth.returncode == 0 else (auth.stderr.strip() or "gh auth status failed"),
    }
    if auth.returncode != 0:
        result["failure_phase"] = "github_authentication"
        return result

    try:
        requested_repository = canonical_repository(repository)
        owner, repo = repository.split("/", 1)
        snapshot = fetch_stable_snapshot(
            owner,
            repo,
            pr_number,
            include_conversation=False,
            require_server_time=False,
            graphql_call=graphql_call,
        )
        canonical_name = snapshot["repository"]
        if canonical_repository(canonical_name) != requested_repository:
            raise RuntimeError("GitHub returned a different canonical repository")
    except Exception as error:
        result["github_snapshot"] = {"ok": False, "error": str(error)}
        result["failure_phase"] = "github_snapshot"
        return result

    pull_request = snapshot["pull_request"]
    result["github_snapshot"] = {
        "ok": True,
        "canonical_repository": canonical_name,
        "pull_request_number": pull_request.get("number"),
        "head_oid": pull_request.get("headRefOid"),
        "state": pull_request.get("state"),
        "is_draft": pull_request.get("isDraft"),
        "url": pull_request.get("url"),
        "error": None,
    }
    head_repository = (pull_request.get("headRepository") or {}).get("nameWithOwner")
    local_checkout = inspect_local_checkout(
        repository_path,
        expected_head_repository=head_repository,
        expected_head_oid=pull_request["headRefOid"],
        command_runner=command_runner,
    )
    result["local_checkout"] = local_checkout

    try:
        state_path = (
            Path(state_file).resolve()
            if state_file is not None
            else checkpoint_path(
                canonical_name, pr_number, repository_path=repository_path
            )
        )
        checkpoint, checkpoint_info = _checkpoint_diagnostic(
            state_path, canonical_name, pr_number
        )
    except Exception as error:
        checkpoint = None
        checkpoint_info = {
            "path": None,
            "exists": None,
            "schema_version": None,
            "expected_schema_version": SCHEMA_VERSION,
            "schema_valid": False,
            "active_batch": None,
            "recovery_required": True,
            "error": f"Unable to resolve checkpoint path: {error}",
        }
    result["checkpoint"] = checkpoint_info
    try:
        if state_file is not None and state_path is not None:
            lease_path = state_path.with_name(state_path.stem + ".lease.json")
        else:
            lease_path = runtime_artifact_path(
                canonical_name,
                pr_number,
                "lease.json",
                repository_path=repository_path,
            )
        lease_info = inspect_lease(
            lease_path,
            repository=canonical_name,
            pr_number=pr_number,
            now=result["generated_at"],
        )
    except Exception as error:
        lease_path = None
        lease_info = {"status": "invalid", "exists": None, "error": str(error)}
    result["runner_lease"] = {
        **lease_info,
        "path": str(lease_path) if lease_path is not None else None,
        "inspection_only": True,
    }

    try:
        evaluation, next_checkpoint = evaluate_snapshot(
            repository=canonical_name,
            pr_number=pr_number,
            head_oid=pull_request["headRefOid"],
            pull_request_state=pull_request["state"],
            review_threads=snapshot["review_threads"],
            reactions=snapshot["thumbs_up_reactions"],
            review_activity_reactions=snapshot["eyes_reactions"],
            reviews=snapshot["reviews"],
            reviewer_logins=reviewer_logins or list(DEFAULT_CODEX_LOGINS),
            approval_logins=approval_logins or list(DEFAULT_CODEX_LOGINS),
            checkpoint=checkpoint,
            observed_at=result["generated_at"],
            head_repository=head_repository,
        )
    except Exception as error:
        result["approval"] = {"ok": False, "error": str(error)}
        result["failure_phase"] = "state_evaluation"
        return result

    result["identities"] = {
        "reviewer_logins": evaluation["reviewer_logins"],
        "approval_logins": evaluation["approval_logins"],
    }
    result["threads"] = {
        "targeted_unresolved_thread_ids": evaluation[
            "targeted_unresolved_thread_ids"
        ],
        "non_target_unresolved_threads": evaluation[
            "non_target_unresolved_threads"
        ],
    }
    result["review_activity"] = {
        "ok": evaluation["review_activity_ok"],
        "codex_review_in_progress": evaluation["codex_review_in_progress"],
        "reactions": evaluation["codex_review_in_progress_reactions"],
        "invalid_reaction_ids": evaluation[
            "invalid_review_activity_reaction_ids"
        ],
        "meaning": (
            "A current PR-level EYES reaction from a configured Codex identity "
            "is wait-only in-progress evidence, never approval evidence."
        ),
    }
    result["approval"] = {
        "ok": True,
        "status": evaluation["approval_status"],
        "proof": evaluation["approval_proof"],
        "diagnostic": evaluation["approval_diagnostic"],
        "cold_start": evaluation["cold_start"],
        "epoch_transition": evaluation["approval_epoch_transition"],
        "existing_reaction_ambiguous": evaluation["approval_status"]
        == "ambiguous_existing_reaction",
        "reaction_binding_limit": (
            "Reaction id and createdAt do not bind a PR-level reaction to a head OID."
        ),
        "qualifying_approval_reactions": evaluation[
            "qualifying_approval_reactions"
        ],
        "proven_current_head_reaction_ids": evaluation[
            "proven_current_head_reaction_ids"
        ],
        "current_head_approved_reviews": evaluation[
            "qualifying_current_head_approval_reviews"
        ],
        "excluded_approval_reviews": evaluation["excluded_approval_reviews"],
        "codex_terminal": evaluation["codex_terminal"],
        "checkpoint_would_change": checkpoint != next_checkpoint,
        "formal_run_must_persist_epoch": checkpoint != next_checkpoint,
        "error": None,
    }

    running_skill_path = Path(
        runtime_skill_path
        if runtime_skill_path is not None
        else Path(__file__).resolve().parents[1]
    ).resolve()
    if install_root is None:
        expected_installation_path = running_skill_path
        effective_install_root = running_skill_path.parent
    else:
        effective_install_root = Path(install_root).resolve()
        expected_installation_path = installation_path(effective_install_root).resolve()
    installed = verify_installation(
        expected_installation_path,
        expected_version=expected_skill_version,
        expected_source_commit=expected_source_commit,
    )
    installed["install_root"] = str(effective_install_root)
    installed["running_skill_path"] = str(running_skill_path)
    installed["running_from_verified_installation"] = (
        running_skill_path == expected_installation_path
    )
    result["installed_skill"] = installed
    result["runner"] = {
        "single_runner_confirmed": single_runner_confirmed,
        "evidence": "operator_confirmation" if single_runner_confirmed else None,
        "ok": single_runner_confirmed,
    }

    blockers: list[str] = []
    if pull_request.get("state") != "OPEN":
        blockers.append("pull_request_not_open")
    if pull_request.get("isDraft") is True:
        blockers.append("pull_request_is_draft")
    if not local_checkout["ok"]:
        blockers.append("local_checkout_verification_failed")
    if not checkpoint_info["schema_valid"]:
        blockers.append("checkpoint_invalid")
    if checkpoint_info["recovery_required"]:
        blockers.append("active_batch_recovery_required")
    if not evaluation["review_activity_ok"]:
        blockers.append("review_activity_evidence_invalid")
    if not installed["ok"]:
        blockers.append("installed_skill_verification_failed")
    if not installed["running_from_verified_installation"]:
        blockers.append("preflight_not_running_from_verified_installation")
    if not single_runner_confirmed:
        blockers.append("single_runner_not_confirmed")
    if lease_info["status"] in {"active", "invalid"}:
        blockers.append("runner_lease_unavailable")
    result["blockers"] = blockers
    result["ready_for_supervised_pilot"] = not blockers
    return result


def select_install_root(*roots: Path | None) -> Path | None:
    """Return one explicit parent root while rejecting ambiguous CLI input."""
    supplied = [root for root in roots if root is not None]
    if not supplied:
        return None
    selected = supplied[0]
    if any(root.resolve() != selected.resolve() for root in supplied[1:]):
        raise ValueError(
            "--install-root and --skills-root values must name the same directory"
        )
    return selected


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a read-only supervised-pilot readiness check"
    )
    parser.add_argument("--repo", required=True, help="Canonical base repository as OWNER/REPO")
    parser.add_argument("--pr", required=True, type=int, help="Pull request number")
    parser.add_argument("--repository-path", default=".", help="Target worktree")
    parser.add_argument("--state-file", type=Path, help="Override checkpoint path")
    parser.add_argument(
        "--install-root",
        type=Path,
        action="append",
        dest="install_roots",
        help="Override skill parent directory",
    )
    parser.add_argument(
        "--skills-root",
        type=Path,
        action="append",
        dest="skills_roots",
        help="Alias for --install-root, matching the installer CLI",
    )
    parser.add_argument("--expected-skill-version", required=True)
    parser.add_argument("--expected-source-commit", required=True)
    parser.add_argument("--reviewer-login", action="append", dest="reviewer_logins")
    parser.add_argument("--approval-login", action="append", dest="approval_logins")
    parser.add_argument(
        "--single-runner-confirmed",
        action="store_true",
        help="Record the operator's confirmation that no other runner targets this PR",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.pr < 1:
        raise RuntimeError("--pr must be positive")
    result = build_preflight(
        repository=args.repo,
        pr_number=args.pr,
        repository_path=args.repository_path,
        reviewer_logins=args.reviewer_logins,
        approval_logins=args.approval_logins,
        state_file=args.state_file,
        install_root=select_install_root(
            *(args.install_roots or []), *(args.skills_roots or [])
        ),
        expected_skill_version=args.expected_skill_version,
        expected_source_commit=args.expected_source_commit,
        single_runner_confirmed=args.single_runner_confirmed,
    )
    print(json.dumps(result, indent=2))
    if not result["ready_for_supervised_pilot"]:
        raise SystemExit(1)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from error
