#!/usr/bin/env python3
"""CLI for the deliberately small v2 campaign record.

Every mutating subcommand proves ownership and executes entirely inside the
canonical per-PR sidecar guard. Campaign records live under the Git common
directory and are shared by all worktrees of the installation.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
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

    init = subparsers.add_parser("init", parents=[common])
    init.add_argument("--campaign-id")
    init.add_argument("--created-at", help="Authoritative timestamp (snapshot server time)")
    init.add_argument("--snapshot", type=Path, help="Snapshot file supplying time/target")
    init.add_argument("--max-rounds", required=True, type=int)
    init.add_argument("--model", required=True)
    init.add_argument("--reasoning-level", required=True)
    init.add_argument("--interval-minutes", required=True, type=int)
    init.add_argument("--reviewer-login", action="append", dest="reviewer_logins")
    init.add_argument("--approval-login", action="append", dest="approval_logins")
    init.add_argument("--owner-token", required=True)

    subparsers.add_parser("show", parents=[common])

    consume = subparsers.add_parser("consume-round", parents=[common])
    consume.add_argument("--kind", choices=["remediation"], default="remediation")
    consume.add_argument("--owner-token", required=True)

    sync = subparsers.add_parser("sync-head", parents=[common])
    sync.add_argument("--head-oid", required=True)
    sync.add_argument("--server-time", required=True)
    sync.add_argument("--owner-token", required=True)

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

    terminate = subparsers.add_parser("terminate", parents=[common])
    terminate.add_argument("--status", required=True, choices=sorted(model.TERMINAL_STATUSES))
    terminate.add_argument("--at", required=True)
    terminate.add_argument("--detail")
    terminate.add_argument("--owner-token", required=True)

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

    if args.command == "init":
        created_at = args.created_at
        if args.snapshot:
            snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
            if snapshot.get("complete") is not True:
                raise RuntimeError("Cannot init from an incomplete snapshot")
            created_at = created_at or snapshot["server_time"]
        if not created_at:
            raise RuntimeError("--created-at or --snapshot is required")
        # The proposed campaign may be fully constructed and validated outside
        # the sidecar guard; persistence and identity checks re-run inside.
        lock_metadata = storage.verify_owner(
            args.repo, args.pr, args.owner_token,
            repository_path=args.repository_path,
        )
        if args.campaign_id and args.campaign_id != lock_metadata["campaign_id"]:
            raise RuntimeError(
                "Init campaign id does not match the acquired lock identity"
            )
        campaign = model.new_campaign(
            campaign_id=lock_metadata["campaign_id"],
            repository=args.repo,
            pull_request_number=args.pr,
            created_at=created_at,
            max_rounds=args.max_rounds,
            model=args.model,
            reasoning_level=args.reasoning_level,
            interval_minutes=args.interval_minutes,
            reviewer_logins=args.reviewer_logins or model.DEFAULT_CODEX_LOGINS,
            approval_logins=args.approval_logins or model.DEFAULT_CODEX_LOGINS,
        )
        result = storage.initialize_campaign(
            args.repo,
            args.pr,
            owner_token=args.owner_token,
            campaign=campaign,
            repository_path=args.repository_path,
        )
        print(json.dumps(result, indent=2))
        return

    if args.command == "show":
        print(json.dumps(_load(args.repo, args.pr, args.repository_path), indent=2))
        return

    if args.command == "consume-round":
        campaign = storage.transition_active_campaign(
            args.repo,
            args.pr,
            owner_token=args.owner_token,
            transition=lambda record: model.consume_round(record, kind=args.kind),
            repository_path=args.repository_path,
        )
    elif args.command == "sync-head":
        campaign = storage.transition_active_campaign(
            args.repo,
            args.pr,
            owner_token=args.owner_token,
            transition=lambda record: model.supersede_active_guards(
                record,
                current_head_oid=args.head_oid,
                at=args.server_time,
            ),
            repository_path=args.repository_path,
        )
    elif args.command == "terminate":
        campaign = storage.transition_active_campaign(
            args.repo,
            args.pr,
            owner_token=args.owner_token,
            transition=lambda record: model.terminate(
                record,
                status=args.status,
                at=args.at,
                detail=args.detail,
            ),
            repository_path=args.repository_path,
        )
    elif args.command == "abort":
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
    elif args.command == "cancel-setup":
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
    elif args.command == "retire":
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

    print(json.dumps({"ok": True, "campaign": campaign}, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(storage.error_text(error), file=sys.stderr)
        raise SystemExit(1) from error
