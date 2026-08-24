#!/usr/bin/env python3
"""Resolve one exact GitHub review thread and verify the returned state."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from typing import Any


MUTATION = """
mutation($threadId: ID!) {
  resolveReviewThread(input: {threadId: $threadId}) {
    thread { id isResolved }
  }
}
"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("thread_id")
    args = parser.parse_args()
    process = subprocess.run(
        ["gh", "api", "graphql", "-F", "query=@-", "-F", f"threadId={args.thread_id}"],
        input=MUTATION,
        capture_output=True,
        text=True,
    )
    if process.returncode != 0:
        raise RuntimeError(process.stderr.strip())
    payload: dict[str, Any] = json.loads(process.stdout)
    if payload.get("errors"):
        raise RuntimeError(json.dumps(payload["errors"]))
    thread = payload["data"]["resolveReviewThread"]["thread"]
    if thread["id"] != args.thread_id or thread["isResolved"] is not True:
        raise RuntimeError("GitHub did not confirm the requested thread as resolved")
    print(json.dumps(thread))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from error
