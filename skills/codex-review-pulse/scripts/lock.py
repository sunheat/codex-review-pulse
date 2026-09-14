#!/usr/bin/env python3
"""CLI for the permanent PR-scoped ownership lock."""

from __future__ import annotations

import argparse
import json
import sys

import campaign_model as model
import storage


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Manage the permanent PR-scoped v2 ownership lock"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--repo", required=True)
    common.add_argument("--pr", required=True, type=int)
    common.add_argument("--repository-path", default=".")

    acquire = subparsers.add_parser("acquire", parents=[common])
    acquire.add_argument("--campaign-id")
    acquire.add_argument(
        "--generate-campaign-id",
        action="store_true",
        help="Deterministically generate the campaign id from --acquired-at",
    )
    acquire.add_argument(
        "--acquired-at",
        required=True,
        help="Authoritative timestamp to record (use the snapshot server time)",
    )

    subparsers.add_parser("inspect", parents=[common])

    verify = subparsers.add_parser("verify", parents=[common])
    verify.add_argument("--owner-token", required=True)

    release = subparsers.add_parser("release", parents=[common])
    release.add_argument("--owner-token", required=True)

    recover = subparsers.add_parser(
        "recover",
        parents=[common],
        help="Explicit user-authorized recovery; there is no automatic expiry",
    )
    recover.add_argument("--user-authorized-recovery", action="store_true", required=True)
    recover.add_argument(
        "--expected-campaign-id",
        help="Refuse when the active lock belongs to a different campaign",
    )

    args = parser.parse_args()
    if args.command == "acquire":
        if bool(args.campaign_id) == bool(args.generate_campaign_id):
            raise SystemExit(
                "Provide exactly one of --campaign-id or --generate-campaign-id"
            )
        campaign_id = args.campaign_id or model.new_campaign_id(args.acquired_at)
        # Stale-delivery guard: a delivery for an obsolete campaign must never
        # drive a newer campaign record targeting the same PR.
        campaign_file = storage.campaign_path(
            args.repo, args.pr, repository_path=args.repository_path
        )
        if campaign_file.exists():
            record = storage.load_json(campaign_file)
            if not isinstance(record, dict) or record.get("campaign_id") != campaign_id:
                print(
                    json.dumps(
                        {
                            "acquired": False,
                            "status": "campaign_identity_mismatch",
                            "delivered_campaign_id": campaign_id,
                        }
                    )
                )
                raise SystemExit(2)
        try:
            result = storage.acquire_lock(
                args.repo,
                args.pr,
                campaign_id=campaign_id,
                acquired_at=args.acquired_at,
                repository_path=args.repository_path,
            )
        except storage.LockHeld as error:
            print(
                json.dumps(
                    {"acquired": False, "status": error.status, "metadata": error.metadata}
                )
            )
            raise SystemExit(2) from error
        print(json.dumps(result, indent=2))
    elif args.command == "inspect":
        print(
            json.dumps(
                storage.inspect_lock(
                    args.repo, args.pr, repository_path=args.repository_path
                ),
                indent=2,
            )
        )
    elif args.command == "verify":
        metadata = storage.verify_owner(
            args.repo,
            args.pr,
            args.owner_token,
            repository_path=args.repository_path,
        )
        metadata.pop("owner_token", None)
        print(json.dumps({"owner": True, **metadata}, indent=2))
    elif args.command == "release":
        print(
            json.dumps(
                storage.release_lock(
                    args.repo,
                    args.pr,
                    args.owner_token,
                    repository_path=args.repository_path,
                ),
                indent=2,
            )
        )
    elif args.command == "recover":
        print(
            json.dumps(
                storage.recover_lock(
                    args.repo,
                    args.pr,
                    user_authorized_recovery=args.user_authorized_recovery,
                    expected_campaign_id=args.expected_campaign_id,
                    repository_path=args.repository_path,
                ),
                indent=2,
            )
        )


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(storage.error_text(error), file=sys.stderr)
        raise SystemExit(1) from error
