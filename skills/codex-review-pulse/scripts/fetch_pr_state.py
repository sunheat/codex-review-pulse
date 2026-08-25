#!/usr/bin/env python3
"""Fetch GitHub PR evidence and evaluate Codex-specific review state."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
from pathlib import Path
import subprocess
import sys
from typing import Any

from checkpoint_store import checkpoint_path, load_checkpoint, save_checkpoint
from state_model import DEFAULT_CODEX_LOGINS, evaluate_snapshot


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
        "gh", "api", "graphql", "-F", "query=@-",
        "-F", f"owner={owner}", "-F", f"repo={repo}",
        "-F", f"number={number}",
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
        pull_request = payload["data"]["repository"]["pullRequest"]
        if pull_request is None:
            raise RuntimeError(f"Pull request not found: {owner}/{repo}#{number}")
        connection = pull_request[connection_name]
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
    nameWithOwner
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
        "--reviewer-login", action="append", dest="reviewer_logins", metavar="LOGIN",
        help="Targeted Codex root-author login; repeat for multiple identities",
    )
    parser.add_argument(
        "--approval-login", action="append", dest="approval_logins", metavar="LOGIN",
        help="Allowed Codex approval login; repeat for multiple identities",
    )
    parser.add_argument(
        "--state-file", type=Path,
        help="Override the checkpoint path (primarily for controlled testing)",
    )
    parser.add_argument(
        "--repository-path", default=".",
        help="Target worktree used to locate the Git common directory",
    )
    return parser.parse_args()


def resolve_target(repo_arg: str | None, pr_arg: int | None) -> tuple[str, str, int]:
    repository = repo_arg
    if repository is None:
        repository = run_json(["gh", "repo", "view", "--json", "nameWithOwner"])[
            "nameWithOwner"
        ]
    if repository.count("/") != 1:
        raise RuntimeError("--repo must be OWNER/REPO")
    owner, repo = repository.split("/", 1)
    number = pr_arg
    if number is None:
        number = int(run_json(["gh", "pr", "view", "--json", "number"])["number"])
    if number < 1:
        raise RuntimeError("--pr must be positive")
    return owner, repo, number


def verify_stable_head(
    initial_repository: dict[str, Any],
    final_repository: dict[str, Any],
    pr_number: int,
) -> dict[str, Any]:
    """Reject connection data collected across a PR head transition."""
    if initial_repository.get("nameWithOwner") != final_repository.get("nameWithOwner"):
        raise RuntimeError("Canonical repository changed while fetching PR state")
    initial_pr = initial_repository.get("pullRequest")
    final_pr = final_repository.get("pullRequest")
    if initial_pr is None or final_pr is None:
        raise RuntimeError(
            f"Pull request disappeared while fetching state: "
            f"{initial_repository.get('nameWithOwner')}#{pr_number}"
        )
    if initial_pr.get("number") != pr_number or final_pr.get("number") != pr_number:
        raise RuntimeError("GitHub returned a different pull request number")
    if initial_pr.get("headRefOid") != final_pr.get("headRefOid"):
        raise RuntimeError("Pull request head advanced while fetching state; retry the snapshot")
    return final_pr


def main() -> None:
    args = parse_args()
    run(["gh", "auth", "status"])
    owner, repo, number = resolve_target(args.repo, args.pr)

    initial_meta = graphql(META_QUERY, owner, repo, number)
    initial_repository = initial_meta["data"].get("repository")
    if initial_repository is None:
        raise RuntimeError(f"Repository not found: {owner}/{repo}")
    if initial_repository.get("pullRequest") is None:
        raise RuntimeError(f"Pull request not found: {owner}/{repo}#{number}")

    review_threads = fetch_connection(THREADS_QUERY, "reviewThreads", owner, repo, number)
    thumbs_up_reactions = fetch_connection(REACTIONS_QUERY, "reactions", owner, repo, number)
    conversation_comments = fetch_connection(COMMENTS_QUERY, "comments", owner, repo, number)
    reviews = fetch_connection(REVIEWS_QUERY, "reviews", owner, repo, number)
    final_meta = graphql(META_QUERY, owner, repo, number)
    final_repository = final_meta["data"].get("repository")
    if final_repository is None:
        raise RuntimeError(f"Repository disappeared while fetching state: {owner}/{repo}")
    pull_request = verify_stable_head(initial_repository, final_repository, number)
    canonical_repo = final_repository["nameWithOwner"]

    state_path = args.state_file or checkpoint_path(
        canonical_repo, number, repository_path=args.repository_path
    )
    previous_checkpoint = load_checkpoint(state_path)
    evaluation, next_checkpoint = evaluate_snapshot(
        repository=canonical_repo,
        pr_number=number,
        head_oid=pull_request["headRefOid"],
        pull_request_state=pull_request["state"],
        review_threads=review_threads,
        reactions=thumbs_up_reactions,
        reviewer_logins=args.reviewer_logins or DEFAULT_CODEX_LOGINS,
        approval_logins=args.approval_logins or DEFAULT_CODEX_LOGINS,
        checkpoint=previous_checkpoint,
        observed_at=datetime.now(UTC).isoformat(),
    )
    save_checkpoint(state_path, next_checkpoint)

    result = {
        "viewer_login": run(["gh", "api", "user", "--jq", ".login"]).strip(),
        "repository": canonical_repo,
        "pull_request": pull_request,
        "checkpoint_path": str(state_path),
        "reviewer_logins": evaluation["reviewer_logins"],
        "approval_logins": evaluation["approval_logins"],
        "conversation_comments": conversation_comments,
        "reviews": reviews,
        "review_threads": review_threads,
        "targeted_unresolved_thread_ids": evaluation["targeted_unresolved_thread_ids"],
        "non_target_unresolved_threads": evaluation["non_target_unresolved_threads"],
        "thumbs_up_reactions": thumbs_up_reactions,
        "qualifying_approval_reactions": evaluation["qualifying_approval_reactions"],
        "invalid_reaction_ids": evaluation["invalid_reaction_ids"],
        "approval_status": evaluation["approval_status"],
        "proven_current_head_reaction_ids": evaluation["proven_current_head_reaction_ids"],
        "approval_epoch_transition": evaluation["approval_epoch_transition"],
        "codex_terminal": evaluation["codex_terminal"],
    }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from error
