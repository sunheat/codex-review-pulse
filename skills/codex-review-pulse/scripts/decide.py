#!/usr/bin/env python3
"""CLI wrapper around the pure worker decision (read-only)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import campaign_model as model
import storage


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Decide the next worker directive from campaign state plus a snapshot"
    )
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pr", required=True, type=int)
    parser.add_argument("--repository-path", default=".")
    parser.add_argument("--snapshot", required=True, type=Path)
    args = parser.parse_args()

    path = storage.campaign_path(
        args.repo, args.pr, repository_path=args.repository_path
    )
    campaign = storage.load_json(path)
    if campaign is None:
        raise RuntimeError(f"Campaign record does not exist: {path}")
    model.validate_campaign(
        campaign, repository=args.repo, pull_request_number=args.pr
    )
    snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
    directive = model.decide(campaign, snapshot)
    print(json.dumps(directive, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(storage.error_text(error), file=sys.stderr)
        raise SystemExit(1) from error
