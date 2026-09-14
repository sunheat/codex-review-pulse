#!/usr/bin/env python3
"""CLI for the deliberately small v2 campaign record.

Every mutating subcommand proves ownership first. Campaign records live under
the Git common directory and are shared by all worktrees of the installation.
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


def _save(campaign: dict, repo: str, pr: int, repository_path: str) -> None:
    model.validate_campaign(campaign, repository=repo, pull_request_number=pr)
    storage.save_json(
        storage.campaign_path(repo, pr, repository_path=repository_path), campaign
    )


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
        help="Launcher-only abort before any effective round consumed",
    )
    abort.add_argument("--owner-token", required=True)

    terminate = subparsers.add_parser("terminate", parents=[common])
    terminate.add_argument("--status", required=True, choices=sorted(model.TERMINAL_STATUSES))
    terminate.add_argument("--at", required=True)
    terminate.add_argument("--detail")
    terminate.add_argument("--guard-head")
    terminate.add_argument("--owner-token", required=True)

    args = parser.parse_args()

    if args.command == "init":
        lock_metadata = storage.verify_owner(
            args.repo, args.pr, args.owner_token,
            repository_path=args.repository_path,
        )
        path = storage.campaign_path(
            args.repo, args.pr, repository_path=args.repository_path
        )
        if path.exists():
            raise RuntimeError(f"Campaign record already exists: {path}")
        created_at = args.created_at
        if args.snapshot:
            snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
            if snapshot.get("complete") is not True:
                raise RuntimeError("Cannot init from an incomplete snapshot")
            created_at = created_at or snapshot["server_time"]
        if not created_at:
            raise RuntimeError("--created-at or --snapshot is required")
        if args.campaign_id and args.campaign_id != lock_metadata["campaign_id"]:
            raise RuntimeError(
                "Init campaign id does not match the acquired lock identity"
            )
        campaign_id = lock_metadata["campaign_id"]
        campaign = model.new_campaign(
            campaign_id=campaign_id,
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
        _save(campaign, args.repo, args.pr, args.repository_path)
        print(json.dumps({"initialized": True, "campaign": campaign}, indent=2))
        return

    campaign = _load(args.repo, args.pr, args.repository_path)

    if args.command == "show":
        print(json.dumps(campaign, indent=2))
        return

    lock_metadata = storage.verify_owner(
        args.repo, args.pr, args.owner_token,
        repository_path=args.repository_path,
    )
    if lock_metadata["campaign_id"] != campaign["campaign_id"]:
        raise RuntimeError(
            "Ownership lock belongs to a different campaign; refusing mutation"
        )

    if args.command == "abort":
        if campaign["rounds_used"] != 0 or campaign["guards"] or model.is_terminal(campaign):
            raise RuntimeError(
                "Cannot abort a campaign that consumed rounds or reached a terminal state"
            )
        path = storage.campaign_path(
            args.repo, args.pr, repository_path=args.repository_path
        )
        path.unlink()
        storage.release_lock(
            args.repo, args.pr, args.owner_token,
            repository_path=args.repository_path,
        )
        print(json.dumps({"aborted": True, "removed": str(path)}, indent=2))
        return

    if args.command == "consume-round":
        campaign = model.consume_round(campaign, kind=args.kind)
    elif args.command == "sync-head":
        campaign = model.supersede_active_guards(
            campaign,
            current_head_oid=args.head_oid,
            at=args.server_time,
        )
    elif args.command == "terminate":
        campaign = model.terminate(
            campaign,
            status=args.status,
            at=args.at,
            detail=args.detail,
            guard_head_oid=args.guard_head,
        )

    _save(campaign, args.repo, args.pr, args.repository_path)
    print(json.dumps({"ok": True, "campaign": campaign}, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(storage.error_text(error), file=sys.stderr)
        raise SystemExit(1) from error
