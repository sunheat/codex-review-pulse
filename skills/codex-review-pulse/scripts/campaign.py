#!/usr/bin/env python3
"""CLI for the deliberately small v2 campaign record.

This surface is deliberately narrow. Deterministic owned boundaries own every
product mutation path: campaign creation (``owned.py create-campaign``), head
synchronization, decisions, round commitment, request reservation, and
terminalization (``owned.py worker-decision``), and rollover (``owned.py
prepare-rollover``). Those flows are not exposed here, so no caller-selected
timestamps, snapshots, terminal statuses, or round consumption can bypass the
durable evidence rules.

Remaining subcommands: read-only inspection and the explicit owner- or
user-authorized local operations (abort, cancel-setup, retire). Every mutating
subcommand proves ownership and executes inside the canonical per-PR sidecar
guard. Campaign records live under the Git common directory and are shared by
all worktrees of the installation.
"""

from __future__ import annotations

import argparse
import json
import sys

import campaign_model as model
import storage


def _load(repo: str, pr: int, repository_path: str) -> dict:
    path = storage.campaign_path(repo, pr, repository_path=repository_path)
    campaign = storage.load_json(path)
    if campaign is None:
        raise RuntimeError(f"Campaign record does not exist: {path}")
    model.validate_campaign(campaign, repository=repo, pull_request_number=pr)
    return campaign


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage the v2 campaign record")
    subparsers = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--repo", required=True)
    common.add_argument("--pr", required=True, type=int)
    common.add_argument("--repository-path", default=".")

    subparsers.add_parser("show", parents=[common])

    abort = subparsers.add_parser(
        "abort",
        parents=[common],
        help="Owner-only abort of an unused active campaign (zero rounds, no guards)",
    )
    abort.add_argument("--owner-token", required=True)

    cancel_setup = subparsers.add_parser(
        "cancel-setup",
        parents=[common],
        help="Owner-only cancellation of a clean pre-campaign setup (campaign still absent)",
    )
    cancel_setup.add_argument("--owner-token", required=True)
    cancel_setup.add_argument("--expected-campaign-id", required=True)

    retire = subparsers.add_parser(
        "retire",
        parents=[common],
        help=(
            "Explicit user-authorized retirement of a valid supported campaign; "
            "a destructive human recovery boundary, never automatic"
        ),
    )
    retire.add_argument("--expected-campaign-id", required=True)
    retire.add_argument(
        "--user-authorized-retirement",
        action="store_true",
        required=True,
        help="Confirm every previous owner and product-mutating operation has stopped",
    )
    retire_mode = retire.add_mutually_exclusive_group(required=True)
    retire_mode.add_argument(
        "--retained-lock",
        action="store_true",
        help="The campaign's semantically valid matching permanent lock still exists",
    )
    retire_mode.add_argument(
        "--lock-absent",
        action="store_true",
        help="No permanent lock remains (e.g. after explicit lock recovery)",
    )

    args = parser.parse_args()

    if args.command == "show":
        print(json.dumps(_load(args.repo, args.pr, args.repository_path), indent=2))
        return

    if args.command == "abort":
        print(
            json.dumps(
                storage.abort_unused_campaign(
                    args.repo,
                    args.pr,
                    owner_token=args.owner_token,
                    repository_path=args.repository_path,
                ),
                indent=2,
            )
        )
        return

    if args.command == "cancel-setup":
        print(
            json.dumps(
                storage.cancel_setup(
                    args.repo,
                    args.pr,
                    owner_token=args.owner_token,
                    expected_campaign_id=args.expected_campaign_id,
                    repository_path=args.repository_path,
                ),
                indent=2,
            )
        )
        return

    if args.command == "retire":
        retire = (
            storage.retire_campaign_retaining_lock
            if args.retained_lock
            else storage.retire_campaign_without_lock
        )
        print(
            json.dumps(
                retire(
                    args.repo,
                    args.pr,
                    expected_campaign_id=args.expected_campaign_id,
                    user_authorized_retirement=args.user_authorized_retirement,
                    repository_path=args.repository_path,
                ),
                indent=2,
            )
        )
        return


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(storage.error_text(error), file=sys.stderr)
        raise SystemExit(1) from error
