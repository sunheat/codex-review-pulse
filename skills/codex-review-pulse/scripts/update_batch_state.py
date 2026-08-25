#!/usr/bin/env python3
"""Update the durable frozen-batch recovery checkpoint without network I/O."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from checkpoint_store import checkpoint_path, load_checkpoint, save_checkpoint
from recurring_contract import assert_mutation_authority, load_run_contract
from state_model import (
    freeze_batch,
    record_publication_failure,
    record_publication_success,
    record_thread_outcome,
    validate_freeze_request,
    validate_checkpoint,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True, help="Canonical base repository as OWNER/REPO")
    parser.add_argument("--pr", required=True, type=int, help="Pull request number")
    parser.add_argument("--state-file", type=Path, help="Override the checkpoint path")
    parser.add_argument("--repository-path", default=".", help="Target worktree")
    parser.add_argument("--run-contract", type=Path, help="Bounded recurring run contract")
    parser.add_argument("--lease-owner-token", help="Owner token for recurring checkpoint writes")
    commands = parser.add_subparsers(dest="command", required=True)

    freeze = commands.add_parser("freeze")
    freeze.add_argument("--head-oid", required=True)
    freeze.add_argument("--thread-id", action="append", default=[])

    outcome = commands.add_parser("record-outcome")
    outcome.add_argument("--thread-id", required=True)
    outcome.add_argument(
        "--classification", required=True, choices=("fix-now", "no-fix", "defer")
    )
    outcome.add_argument("--reference")

    failed = commands.add_parser("publication-failed")
    failed.add_argument("--phase", required=True, choices=("validation", "commit", "push"))
    failed.add_argument("--pending-path", action="append", default=[])
    failed.add_argument("--pending-commit")

    succeeded = commands.add_parser("publication-succeeded")
    succeeded.add_argument("--published-commit")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    path = args.state_file or checkpoint_path(
        args.repo, args.pr, repository_path=args.repository_path
    )
    checkpoint = load_checkpoint(path)
    if checkpoint is None:
        raise RuntimeError("Checkpoint does not exist; fetch PR state before updating a batch")
    validate_checkpoint(checkpoint, args.repo, args.pr)
    if bool(args.run_contract) != bool(args.lease_owner_token):
        raise RuntimeError(
            "Recurring checkpoint writes require both run contract and lease owner token"
        )
    if args.run_contract:
        contract = load_run_contract(
            args.run_contract, repository_path=args.repository_path
        )
        if (
            contract["repository"] != args.repo.casefold()
            or contract["pull_request_number"] != args.pr
            or Path(contract["paths"]["checkpoint"]).resolve() != Path(path).resolve()
        ):
            raise RuntimeError("Run contract does not bind this checkpoint target")
        assert_mutation_authority(
            contract,
            owner_token=args.lease_owner_token,
            required_scope="recurring_execution",
            runtime_script_path=__file__,
        )

    if args.command == "freeze":
        requested_ids = validate_freeze_request(
            checkpoint, args.head_oid, args.thread_id
        )
        updated = freeze_batch(checkpoint, args.head_oid, requested_ids)
    elif args.command == "record-outcome":
        updated = record_thread_outcome(
            checkpoint,
            thread_id=args.thread_id,
            classification=args.classification,
            reference=args.reference,
        )
    elif args.command == "publication-failed":
        updated = record_publication_failure(
            checkpoint,
            phase=args.phase,
            pending_paths=args.pending_path,
            pending_commit=args.pending_commit,
        )
    else:
        updated = record_publication_success(
            checkpoint, published_commit=args.published_commit
        )
    save_checkpoint(path, updated)
    print(json.dumps(updated["active_batch"], indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from error
