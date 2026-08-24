#!/usr/bin/env python3
"""Fetch authoritative GitHub PR thread and Codex approval state via gh."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from typing import Any


DEFAULT_APPROVAL_LOGINS = (
    "chatgpt-codex-connector",
    "chatgpt-codex-connector[bot]",
)


def run(command: list[str], stdin: str | None = None) -> str:
    process = subprocess.run(command, input=stdin, capture_output=True, text=True)
    if process.returncode != 0:
        raise RuntimeError(f"Command failed: {' '.join(command)}\n{process.stderr.strip()}")
    return process.stdout


def run_json(command: list[str], stdin: str | None = None) -> dict[str, Any]:
    try:
        return json.loads(run(command, stdin))
    except json.JSONDecodeError as error:
        raise RuntimeError(f"Command did not return JSON: {' '.join(command)}") from error


def graphql(
    query: str,
    owner: str,
    repo: str,
    number: int,
    cursor: str | None = None,
) -> dict[str, Any]:
    command = [
        "gh",
        "api",
        "graphql",
        "-F",
        "query=@-",
        "-F",
        f"owner={owner}",
        "-F",
        f"repo={repo}",
        "-F",
        f"number={number}",
    ]
    if cursor:
        command.extend(["-F", f"cursor={cursor}"])
    payload = run_json(command, query)
    errors = payload.get("errors")
    if errors:
        raise RuntimeError(f"GitHub GraphQL errors: {json.dumps(errors)}")
    return payload


def fetch_connection(
    query: str,
    connection_name: str,
    owner: str,
    repo: str,
    number: int,
) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        payload = graphql(query, owner, repo, number, cursor)
        connection = payload["data"]["repository"]["pullRequest"][connection_name]
        nodes.extend(connection.get("nodes") or [])
        page_info = connection["pageInfo"]
        if not page_info["hasNextPage"]:
            return nodes
        cursor = page_info["endCursor"]
        if not cursor:
            raise RuntimeError(f"{connection_name} pagination did not return an end cursor")


META_QUERY = """
query($owner: String!, $repo: String!, $number: Int!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      number url title state isDraft mergeable reviewDecision
      headRefName headRefOid baseRefName updatedAt
      author { login }
    }
  }
}
"""

COMMENTS_QUERY = """
query($owner: String!, $repo: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      comments(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes { id databaseId body createdAt updatedAt author { login } url }
      }
    }
  }
}
"""

REVIEWS_QUERY = """
query($owner: String!, $repo: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      reviews(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes { id databaseId state body submittedAt updatedAt author { login } url }
      }
    }
  }
}
"""

THREADS_QUERY = """
query($owner: String!, $repo: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      reviewThreads(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id isResolved isOutdated path line diffSide
          startLine startDiffSide originalLine originalStartLine
          resolvedBy { login }
          comments(first: 100) {
            nodes { id databaseId body createdAt updatedAt author { login } url }
          }
        }
      }
    }
  }
}
"""

REACTIONS_QUERY = """
query($owner: String!, $repo: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      reactions(first: 100, after: $cursor, content: THUMBS_UP) {
        pageInfo { hasNextPage endCursor }
        nodes { id content createdAt user { login } }
      }
    }
  }
}
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", help="Canonical base repository as OWNER/REPO")
    parser.add_argument("--pr", type=int, help="Pull request number")
    parser.add_argument(
        "--approval-login",
        action="append",
        dest="approval_logins",
        metavar="LOGIN",
        help="Allowed Codex approval login; repeat for multiple identities",
    )
    return parser.parse_args()


def resolve_target(repo_arg: str | None, pr_arg: int | None) -> tuple[str, str, int]:
    repository = repo_arg
    if repository is None:
        repository = run_json(["gh", "repo", "view", "--json", "nameWithOwner"])[
            "nameWithOwner"
        ]
    if "/" not in repository:
        raise RuntimeError("--repo must be OWNER/REPO")
    owner, repo = repository.split("/", 1)
    number = pr_arg
    if number is None:
        number = int(run_json(["gh", "pr", "view", "--json", "number"])["number"])
    return owner, repo, number


def unique_logins(configured: list[str] | None) -> list[str]:
    values = configured or list(DEFAULT_APPROVAL_LOGINS)
    result: list[str] = []
    seen: set[str] = set()
    for login in values:
        value = login.strip()
        if not value:
            raise RuntimeError("Approval logins must not be empty")
        key = value.casefold()
        if key not in seen:
            result.append(value)
            seen.add(key)
    return result


def main() -> None:
    args = parse_args()
    run(["gh", "auth", "status"])
    owner, repo, number = resolve_target(args.repo, args.pr)
    approval_logins = unique_logins(args.approval_logins)
    approval_keys = {login.casefold() for login in approval_logins}

    meta_payload = graphql(META_QUERY, owner, repo, number)
    pull_request = meta_payload["data"]["repository"]["pullRequest"]
    if pull_request is None:
        raise RuntimeError(f"Pull request not found: {owner}/{repo}#{number}")

    review_threads = fetch_connection(
        THREADS_QUERY, "reviewThreads", owner, repo, number
    )
    unresolved_thread_ids = [
        thread["id"] for thread in review_threads if not thread["isResolved"]
    ]
    thumbs_up_reactions = fetch_connection(
        REACTIONS_QUERY, "reactions", owner, repo, number
    )
    qualifying_approval_reactions = [
        reaction
        for reaction in thumbs_up_reactions
        if ((reaction.get("user") or {}).get("login") or "").casefold()
        in approval_keys
    ]

    result = {
        "viewer_login": run(["gh", "api", "user", "--jq", ".login"]).strip(),
        "repository": f"{owner}/{repo}",
        "pull_request": pull_request,
        "approval_logins": approval_logins,
        "conversation_comments": fetch_connection(
            COMMENTS_QUERY, "comments", owner, repo, number
        ),
        "reviews": fetch_connection(REVIEWS_QUERY, "reviews", owner, repo, number),
        "review_threads": review_threads,
        "unresolved_review_thread_ids": unresolved_thread_ids,
        "thumbs_up_reactions": thumbs_up_reactions,
        "qualifying_approval_reactions": qualifying_approval_reactions,
        "terminal_approval": (
            not unresolved_thread_ids and bool(qualifying_approval_reactions)
        ),
    }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from error
