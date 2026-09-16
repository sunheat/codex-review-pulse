#!/usr/bin/env python3
"""GitHub observation and mutation primitives for the v2 worker.

All evidence comes from authoritative GitHub responses through the authenticated
GitHub CLI. Connections are fully paginated and head-bracketed; an error, a
partial connection, or a head move during observation raises instead of being
normalized into "absent".
"""

from __future__ import annotations

import argparse
from email.utils import parsedate_to_datetime
import json
from datetime import UTC
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Callable

import storage
from campaign_model import normalize_login


def run(command: list[str], stdin: str | None = None) -> str:
    # GitHub API JSON and HTTP text are UTF-8. Locale-dependent decoding turned
    # non-ASCII evidence into UnicodeDecodeError on e.g. cp936 hosts, so the
    # transport is decoded strictly as UTF-8 and invalid bytes fail closed.
    process = subprocess.run(
        command,
        input=stdin,
        capture_output=True,
        encoding="utf-8",
        errors="strict",
    )
    if process.returncode != 0:
        stderr = (process.stderr or "").strip()
        raise RuntimeError(f"Command failed: {' '.join(command)}\n{stderr}")
    return process.stdout


def run_json(command: list[str], stdin: str | None = None) -> Any:
    try:
        return json.loads(run(command, stdin))
    except json.JSONDecodeError as error:
        raise RuntimeError(f"Command did not return JSON: {' '.join(command)}") from error


def _graphql_arguments(variables: dict[str, object]) -> list[str]:
    """Build gh arguments whose forms match the GraphQL variable types.

    gh treats an ``@value`` passed through ``-F`` as a file reference, so
    string-like variables (including bodies such as ``@codex review``) must use
    raw ``-f``. Integer and boolean variables use typed ``-F``; null variables
    are omitted. The query itself is supplied through stdin with ``query=@-``.
    """
    arguments: list[str] = []
    for name, value in variables.items():
        if value is None:
            continue
        if isinstance(value, bool):
            arguments.extend(["-F", f"{name}={'true' if value else 'false'}"])
        elif isinstance(value, int):
            arguments.extend(["-F", f"{name}={value}"])
        else:
            arguments.extend(["-f", f"{name}={value}"])
    return arguments


def graphql(query: str, variables: dict[str, object] | None = None) -> dict[str, Any]:
    command = ["gh", "api", "graphql", "-F", "query=@-"]
    command.extend(_graphql_arguments(variables or {}))
    payload = run_json(command, query)
    if isinstance(payload, dict) and payload.get("errors"):
        raise RuntimeError(f"GitHub GraphQL errors: {json.dumps(payload['errors'])}")
    return payload


def graphql_timed(
    query: str,
    owner: str,
    repo: str,
    number: int,
    cursor: str | None = None,
) -> dict[str, Any]:
    """GraphQL call that also parses the authoritative response Date header."""
    variables = {
        "owner": owner,
        "repo": repo,
        "number": number,
    }
    if cursor:
        variables["cursor"] = cursor
    return graphql_timed_variables(query, variables)


def graphql_timed_variables(
    query: str, variables: dict[str, object]
) -> dict[str, Any]:
    command = ["gh", "api", "graphql", "--include", "-F", "query=@-"]
    command.extend(_graphql_arguments(variables))
    response = run(command, query)
    header_end = re.search(r"\r?\n\r?\n", response)
    if header_end is None:
        raise RuntimeError("GitHub GraphQL response did not include HTTP headers")
    date_match = re.search(
        r"(?im)^date:\s*(?P<value>[^\r\n]+)", response[: header_end.start()]
    )
    if date_match is None:
        raise RuntimeError("GitHub GraphQL response did not include a Date header")
    server_time = parsedate_to_datetime(date_match.group("value"))
    if server_time.tzinfo is None:
        raise RuntimeError("GitHub GraphQL Date header has no timezone")
    try:
        payload = json.loads(response[header_end.end():])
    except json.JSONDecodeError as error:
        raise RuntimeError("GitHub GraphQL response body is not JSON") from error
    if not isinstance(payload, dict):
        raise RuntimeError("GitHub GraphQL response root must be an object")
    if payload.get("errors"):
        raise RuntimeError(f"GitHub GraphQL errors: {json.dumps(payload['errors'])}")
    payload["_github_server_time"] = server_time.astimezone(UTC).isoformat()
    return payload


def fetch_connection(
    query: str,
    connection_name: str,
    owner: str,
    repo: str,
    number: int,
    *,
    graphql_call: Callable[..., dict[str, Any]] = graphql_timed_variables,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Return all nodes plus the authoritative server times observed."""
    nodes: list[dict[str, Any]] = []
    server_times: list[str] = []
    cursor: str | None = None
    while True:
        variables: dict[str, object] = {
            "owner": owner,
            "repo": repo,
            "number": number,
        }
        if cursor:
            variables["cursor"] = cursor
        payload = graphql_call(query, variables)
        server_times.append(payload["_github_server_time"])
        pull_request = payload["data"]["repository"]["pullRequest"]
        if pull_request is None:
            raise RuntimeError(f"Pull request not found: {owner}/{repo}#{number}")
        connection = pull_request[connection_name]
        nodes.extend(connection.get("nodes") or [])
        page_info = connection["pageInfo"]
        if not page_info["hasNextPage"]:
            return nodes, server_times
        cursor = page_info["endCursor"]
        if not cursor:
            raise RuntimeError(f"{connection_name} pagination did not return a cursor")


META_QUERY = """
query($owner: String!, $repo: String!, $number: Int!) {
  repository(owner: $owner, name: $repo) {
    nameWithOwner
    pullRequest(number: $number) {
      id number url title state
      headRefName headRefOid
      headRepository { nameWithOwner }
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
          id isResolved isOutdated path
          comments(first: 100) {
            nodes { id body createdAt updatedAt author { login } url }
          }
        }
      }
    }
  }
}
"""

REACTIONS_QUERY = """
query($owner: String!, $repo: String!, $number: Int!, $cursor: String, $content: ReactionContent!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      reactions(first: 100, after: $cursor, content: $content) {
        pageInfo { hasNextPage endCursor }
        nodes { id content createdAt user { login } }
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
        nodes {
          id state body submittedAt updatedAt author { login } url
          commit { oid }
        }
      }
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
        nodes { id body createdAt updatedAt author { login } url }
      }
    }
  }
}
"""


def _meta(graphql_call: Callable[..., dict[str, Any]], owner: str, repo: str, number: int) -> tuple[str, dict[str, Any], str]:
    payload = graphql_call(
        META_QUERY, {"owner": owner, "repo": repo, "number": number}
    )
    server_time = payload["_github_server_time"]
    repository_node = payload["data"].get("repository")
    if repository_node is None:
        raise RuntimeError(f"Repository not found: {owner}/{repo}")
    pull_request = repository_node.get("pullRequest")
    if pull_request is None:
        raise RuntimeError(f"Pull request not found: {owner}/{repo}#{number}")
    return repository_node["nameWithOwner"], pull_request, server_time


def fetch_reactions(
    content: str,
    owner: str,
    repo: str,
    number: int,
    *,
    graphql_call: Callable[..., dict[str, Any]] = graphql_timed_variables,
) -> tuple[list[dict[str, Any]], list[str]]:
    nodes: list[dict[str, Any]] = []
    server_times: list[str] = []
    cursor: str | None = None
    while True:
        variables: dict[str, object] = {
            "owner": owner,
            "repo": repo,
            "number": number,
            "content": content,
        }
        if cursor:
            variables["cursor"] = cursor
        payload = graphql_call(REACTIONS_QUERY, variables)
        server_times.append(payload["_github_server_time"])
        connection = payload["data"]["repository"]["pullRequest"]["reactions"]
        nodes.extend(connection.get("nodes") or [])
        page_info = connection["pageInfo"]
        if not page_info["hasNextPage"]:
            return nodes, server_times
        cursor = page_info["endCursor"]
        if not cursor:
            raise RuntimeError("reactions pagination did not return a cursor")


def _require_same_head(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    if before.get("headRefOid") != after.get("headRefOid"):
        raise RuntimeError("Pull request head moved while fetching state; discard the observation")
    if before.get("number") != after.get("number"):
        raise RuntimeError("GitHub returned a different pull request number")
    return after


def normalize_snapshot(
    *,
    repository: str,
    pull_request: dict[str, Any],
    threads: list[dict[str, Any]],
    reactions: list[dict[str, Any]],
    reviews: list[dict[str, Any]],
    comments: list[dict[str, Any]],
    server_time: str,
    viewer: str,
) -> dict[str, Any]:
    """Convert raw GraphQL nodes into the pure model's snapshot shape."""
    normalized_threads: list[dict[str, Any]] = []
    for thread in threads:
        nodes = (thread.get("comments") or {}).get("nodes") or []
        root = nodes[0] if nodes else {}
        normalized_threads.append(
            {
                "id": thread.get("id"),
                "is_resolved": thread.get("isResolved") is True,
                "is_outdated": thread.get("isOutdated") is True,
                "path": thread.get("path"),
                "root_login": normalize_login((root.get("author") or {}).get("login")),
                "body": root.get("body"),
                "url": root.get("url"),
            }
        )
    normalized_reactions = [
        {
            "id": item.get("id"),
            "content": item.get("content"),
            "login": normalize_login((item.get("user") or {}).get("login")),
            "created_at": item.get("createdAt"),
        }
        for item in reactions
    ]
    normalized_reviews = [
        {
            "id": item.get("id"),
            "state": item.get("state"),
            "login": normalize_login((item.get("author") or {}).get("login")),
            "commit_oid": (item.get("commit") or {}).get("oid"),
            "submitted_at": item.get("submittedAt"),
            "body": item.get("body"),
            "url": item.get("url"),
        }
        for item in reviews
    ]
    normalized_comments = [
        {
            "id": item.get("id"),
            "login": normalize_login((item.get("author") or {}).get("login")),
            "created_at": item.get("createdAt"),
            "body": item.get("body"),
            "url": item.get("url"),
        }
        for item in comments
    ]
    return {
        "complete": True,
        "server_time": server_time,
        "repository": repository,
        "pr_number": pull_request.get("number"),
        "pr_state": pull_request.get("state"),
        "head_oid": pull_request.get("headRefOid"),
        "head_ref_name": pull_request.get("headRefName"),
        "head_repository": (pull_request.get("headRepository") or {}).get("nameWithOwner"),
        "node_id": pull_request.get("id"),
        "viewer": viewer,
        "threads": normalized_threads,
        "reactions": normalized_reactions,
        "reviews": normalized_reviews,
        "comments": normalized_comments,
    }


def fetch_snapshot(
    repository: str,
    number: int,
    *,
    graphql_call: Callable[..., dict[str, Any]] = graphql_timed_variables,
    viewer_call: Callable[[], str] | None = None,
) -> dict[str, Any]:
    """Read-only, head-bracketed, complete PR observation."""
    owner, repo = repository.split("/", 1)
    before_name, before, _ = _meta(graphql_call, owner, repo, number)
    threads, _ = fetch_connection(
        THREADS_QUERY, "reviewThreads", owner, repo, number, graphql_call=graphql_call
    )
    thumbs_up, _ = fetch_reactions(
        "THUMBS_UP", owner, repo, number, graphql_call=graphql_call
    )
    eyes, _ = fetch_reactions(
        "EYES", owner, repo, number, graphql_call=graphql_call
    )
    reviews, _ = fetch_connection(
        REVIEWS_QUERY, "reviews", owner, repo, number, graphql_call=graphql_call
    )
    comments, _ = fetch_connection(
        COMMENTS_QUERY, "comments", owner, repo, number, graphql_call=graphql_call
    )
    after_name, after, final_server_time = _meta(graphql_call, owner, repo, number)
    if before_name != after_name:
        raise RuntimeError("Canonical repository changed while fetching state")
    pull_request = _require_same_head(before, after)
    viewer = viewer_call() if viewer_call is not None else run(
        ["gh", "api", "user", "--jq", ".login"]
    ).strip()
    return normalize_snapshot(
        repository=after_name,
        pull_request=pull_request,
        threads=threads,
        reactions=thumbs_up + eyes,
        reviews=reviews,
        comments=comments,
        server_time=final_server_time,
        viewer=normalize_login(viewer) or viewer,
    )


# ---------------------------------------------------------------------------
# Mutations


ADD_COMMENT_MUTATION = """
mutation($subjectId: ID!, $body: String!) {
  addComment(input: {subjectId: $subjectId, body: $body}) {
    commentEdge { node { id createdAt url body } }
  }
}
"""

RESOLVE_MUTATION = """
mutation($threadId: ID!) {
  resolveReviewThread(input: {threadId: $threadId}) {
    thread { id isResolved }
  }
}
"""

VERIFY_THREADS_QUERY = """
query($owner: String!, $repo: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      number headRefOid
      reviewThreads(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes { id isResolved comments(first: 1) { nodes { author { login } } } }
      }
    }
  }
}
"""


def add_comment(subject_id: str, body: str, *, graphql_call: Callable[..., dict[str, Any]] = graphql) -> dict[str, Any]:
    payload = graphql_call(ADD_COMMENT_MUTATION, {"subjectId": subject_id, "body": body})
    node = payload["data"]["addComment"]["commentEdge"]["node"]
    return {
        "node_id": node["id"],
        "created_at": node["createdAt"],
        "url": node.get("url"),
        "body": node.get("body"),
    }


def fetch_all_thread_heads(
    repository: str, number: int, *, graphql_call: Callable[..., dict[str, Any]] = graphql
) -> tuple[str, list[dict[str, Any]]]:
    """Re-observe thread roots with a stable head for pre-resolution checks."""
    owner, repo = repository.split("/", 1)
    nodes: list[dict[str, Any]] = []
    head_oid: str | None = None
    cursor: str | None = None
    while True:
        payload = graphql_call(
            VERIFY_THREADS_QUERY,
            {"owner": owner, "repo": repo, "number": number, "cursor": cursor},
        )
        pull_request = payload["data"]["repository"]["pullRequest"]
        if pull_request is None:
            raise RuntimeError(f"Pull request not found: {repository}#{number}")
        page_head = pull_request["headRefOid"]
        if head_oid is not None and page_head != head_oid:
            raise RuntimeError("Pull request head moved while re-observing threads")
        head_oid = page_head
        connection = pull_request["reviewThreads"]
        nodes.extend(connection.get("nodes") or [])
        page_info = connection["pageInfo"]
        if not page_info["hasNextPage"]:
            break
        cursor = page_info["endCursor"]
        if not cursor:
            raise RuntimeError("reviewThreads pagination did not return a cursor")
    if head_oid is None:
        raise RuntimeError("GitHub did not return a head OID")
    return head_oid, nodes


def verify_and_resolve_thread(
    *,
    repository: str,
    number: int,
    thread_id: str,
    batch_thread_ids: list[str],
    reviewer_logins: list[str],
    owner_token: str,
    repository_path: str | Path = ".",
    graphql_call: Callable[..., dict[str, Any]] = graphql,
) -> dict[str, Any]:
    """Re-observe, verify scope/identity/unresolved state, then resolve exactly."""
    storage.ensure_active_campaign_owner(
        repository, number, owner_token, repository_path=repository_path
    )
    if thread_id not in batch_thread_ids or not batch_thread_ids:
        raise RuntimeError("Thread is not part of this in-memory remediation batch")
    _, nodes = fetch_all_thread_heads(repository, number, graphql_call=graphql_call)
    by_id = {node.get("id"): node for node in nodes}
    missing = sorted(set(batch_thread_ids) - set(by_id))
    if missing:
        raise RuntimeError("Batch thread IDs are not all present on this pull request: " + ", ".join(missing))
    target = by_id[thread_id]
    root_nodes = (target.get("comments") or {}).get("nodes") or []
    root_login = normalize_login((root_nodes[0].get("author") or {}).get("login")) if root_nodes else None
    if root_login not in set(reviewer_logins):
        raise RuntimeError("Thread root author is not an applicable Codex identity")
    if target.get("isResolved") is True:
        return {"id": thread_id, "isResolved": True, "already_resolved": True}
    payload = graphql_call(RESOLVE_MUTATION, {"threadId": thread_id})
    thread = payload["data"]["resolveReviewThread"]["thread"]
    if thread.get("id") != thread_id or thread.get("isResolved") is not True:
        raise RuntimeError("GitHub did not confirm the thread resolution")
    return {"id": thread_id, "isResolved": True, "already_resolved": False}


def deferred_issue_marker(repository: str, number: int, thread_id: str) -> str:
    safe_thread = thread_id.replace("--", "-")
    return f"<!-- codex-review-pulse: {repository.casefold()}#{number}/{safe_thread} -->"


def ensure_deferred_issue(
    *,
    repository: str,
    number: int,
    thread_id: str,
    title: str,
    body: str,
    owner_token: str,
    repository_path: str | Path = ".",
    searcher: Callable[[str, str], list[dict[str, Any]]] | None = None,
    creator: Callable[[str, str, str], str] | None = None,
) -> dict[str, Any]:
    """Find an open issue carrying the deterministic marker, or create one.

    Identity is the marker string itself, not model judgment about titles.
    """
    storage.ensure_active_campaign_owner(
        repository, number, owner_token, repository_path=repository_path
    )
    marker = deferred_issue_marker(repository, number, thread_id)
    if searcher is None:
        def searcher(repo: str, query: str) -> list[dict[str, Any]]:
            return run_json(
                ["gh", "issue", "list", "--repo", repo, "--state", "open",
                 "--search", query, "--json", "number,title,body,url"]
            )
    matches = [
        issue
        for issue in searcher(repository, f'"{marker}"')
        if isinstance(issue, dict) and marker in (issue.get("body") or "")
    ]
    if matches:
        issue = matches[0]
        return {"number": issue.get("number"), "url": issue.get("url"), "created": False}
    full_body = body.rstrip() + "\n\n" + marker + "\n"
    if creator is None:
        def creator(repo: str, issue_title: str, issue_body: str) -> str:
            return run(
                ["gh", "issue", "create", "--repo", repo,
                 "--title", issue_title, "--body-file", "-"],
                issue_body,
            ).strip()
    url = creator(repository, title, full_body)
    return {"number": url.rstrip("/").split("/")[-1], "url": url, "created": True}


def normalize_comment_body(body: object) -> str:
    if not isinstance(body, str):
        return ""
    return re.sub(r"\s+", " ", body.strip()).casefold()


def main() -> None:
    parser = argparse.ArgumentParser(description="v2 GitHub observation and mutation helpers")
    subparsers = parser.add_subparsers(dest="command", required=True)

    snapshot = subparsers.add_parser("snapshot", help="Read-only head-bracketed PR observation")
    snapshot.add_argument("--repo", required=True)
    snapshot.add_argument("--pr", required=True, type=int)

    subparsers.add_parser("viewer", help="Print the authenticated GitHub login")

    resolve = subparsers.add_parser("resolve-thread", help="Verify and resolve one batch thread")
    resolve.add_argument("--repo", required=True)
    resolve.add_argument("--pr", required=True, type=int)
    resolve.add_argument("--repository-path", default=".")
    resolve.add_argument("--owner-token", required=True)
    resolve.add_argument("--thread-id", required=True)
    resolve.add_argument("--batch-thread", action="append", required=True, dest="batch_threads")
    resolve.add_argument("--reviewer-login", action="append", dest="reviewer_logins")

    issue = subparsers.add_parser("ensure-issue", help="Idempotently create/reuse a deferred issue")
    issue.add_argument("--repo", required=True)
    issue.add_argument("--pr", required=True, type=int)
    issue.add_argument("--repository-path", default=".")
    issue.add_argument("--owner-token", required=True)
    issue.add_argument("--thread-id", required=True)
    issue.add_argument("--title", required=True)
    issue.add_argument("--body-file", required=True, type=Path)

    args = parser.parse_args()
    if args.command == "snapshot":
        print(json.dumps(fetch_snapshot(args.repo, args.pr), indent=2))
    elif args.command == "viewer":
        print(run(["gh", "api", "user", "--jq", ".login"]).strip())
    elif args.command == "resolve-thread":
        from campaign_model import unique_logins

        result = verify_and_resolve_thread(
            repository=args.repo,
            number=args.pr,
            thread_id=args.thread_id,
            batch_thread_ids=args.batch_threads,
            reviewer_logins=unique_logins(args.reviewer_logins, label="reviewer"),
            owner_token=args.owner_token,
            repository_path=args.repository_path,
        )
        print(json.dumps(result, indent=2))
    elif args.command == "ensure-issue":
        body = args.body_file.read_text(encoding="utf-8") if str(args.body_file) != "-" else sys.stdin.read()
        result = ensure_deferred_issue(
            repository=args.repo,
            number=args.pr,
            thread_id=args.thread_id,
            title=args.title,
            body=body,
            owner_token=args.owner_token,
            repository_path=args.repository_path,
        )
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(storage.error_text(error), file=sys.stderr)
        raise SystemExit(1) from error
