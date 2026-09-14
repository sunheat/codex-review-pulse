#!/usr/bin/env python3
"""Single deterministic read-only admission for one scheduled delivery.

One command performs the whole pre-ownership admission phase: it observes the
pull request through the authoritative GitHub transport, loads the campaign
record, and prints an admission envelope that references a snapshot file
written by this process itself. The worker model never assembles, edits, or
re-saves snapshot evidence; it only passes the printed path to later helpers.

Admission performs no lock, campaign, Git, or GitHub mutation. Its only local
artifact is a fresh private snapshot file in the system temporary directory.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any

import campaign_model as model
import github_api
import storage


def _write_private_snapshot(snapshot: dict[str, Any]) -> str:
    """Persist the observation to a fresh private file owned by this process."""
    descriptor, name = tempfile.mkstemp(prefix="crp-snapshot-", suffix=".json")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(snapshot, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        try:
            Path(name).unlink()
        except OSError:
            pass
        raise
    return name


def run_admission(
    repository: str,
    pr_number: int,
    *,
    repository_path: str | Path = ".",
) -> dict[str, Any]:
    snapshot = github_api.fetch_snapshot(repository, pr_number)
    snapshot_path = _write_private_snapshot(snapshot)

    envelope: dict[str, Any] = {
        "snapshot_path": snapshot_path,
        "snapshot_complete": snapshot.get("complete") is True,
        "head_oid": snapshot.get("head_oid"),
        "head_ref_name": snapshot.get("head_ref_name"),
        "head_repository": snapshot.get("head_repository"),
        "server_time": snapshot.get("server_time"),
        "pr_state": snapshot.get("pr_state"),
    }

    # Read-only campaign and lock inspection; both tolerate absence.
    campaign = storage.load_json(
        storage.campaign_path(repository, pr_number, repository_path=repository_path)
    )
    if campaign is None:
        envelope["campaign_record"] = "absent"
    else:
        # Malformed, foreign, or unsupported records fail closed here: the
        # record is preserved for inspection and this delivery stops.
        model.validate_campaign(
            campaign, repository=repository, pull_request_number=pr_number
        )
        envelope["campaign_record"] = (
            "terminal" if model.is_terminal(campaign) else "active"
        )
        envelope["campaign_id"] = campaign.get("campaign_id")
        envelope["campaign_status"] = campaign.get("status")
        envelope["rounds_used"] = campaign.get("rounds_used")

    envelope["lock_status"] = storage.inspect_lock(
        repository, pr_number, repository_path=repository_path
    ).get("status")
    return envelope


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Read-only admission: observe the PR and report campaign/lock status"
    )
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pr", required=True, type=int)
    parser.add_argument("--repository-path", default=".")
    args = parser.parse_args()

    envelope = run_admission(
        args.repo, args.pr, repository_path=args.repository_path
    )
    print(json.dumps(envelope, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(storage.error_text(error), file=sys.stderr)
        raise SystemExit(1) from error
