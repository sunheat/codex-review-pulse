#!/usr/bin/env python3
"""GitHub observation and mutation primitives for the v2 runtime.

All evidence comes from authoritative GitHub responses through the authenticated
GitHub CLI. Connections are fully paginated and head-bracketed; an error, a
partial connection, or a head move during observation raises instead of being
normalized into "absent".

The transport functions here are internal implementation details and unit-test
seams. Product-facing external mutations go through the deterministic
remediation finalizer (using ``externalize.py``) or the committed-request
executor (``review_request.py``), never by composing these primitives directly.
"""

from __future__ import annotations

import argparse
from email.utils import parsedate_to_datetime
import json
from datetime import UTC
import re
import subprocess
import sys
from typing import Any, Callable

import storage
from campaign_model import canonical_repository, normalize_login


class GithubRejectionError(RuntimeError):
    """Server-authoritative proof that one mutation attempt did not execute.

    Raised only when the GraphQL mutation response itself proves, via the
    conservative pre-execution request-error predicate, that the target
    mutation did not execute and cannot later complete from this attempt.

    ``GithubRejectionError`` means: there is authoritative evidence that this
    specific target mutation did not execute and cannot later complete from
    this attempt. It never means merely "GitHub returned a GraphQL error".

    Every other mutation outcome -- transport failures, malformed output,
    execution errors carrying ``data`` and/or ``path``, unproven error
    shapes, or a nonzero CLI exit with success-looking JSON -- raises an
    ordinary ``RuntimeError`` (ambiguous, fail closed) instead.
    """


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


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate object key: {key!r}")
        result[key] = value
    return result


def _reject_nonstandard_constant(value: str) -> Any:
    raise ValueError(f"non-standard JSON constant: {value}")


def _parse_mutation_json_document(stdout: str) -> Any:
    # loads accepts surrounding JSON whitespace but rejects a second document.
    return json.loads(
        stdout,
        object_pairs_hook=_reject_duplicate_keys,
        parse_constant=_reject_nonstandard_constant,
    )


def _proven_rejection_detail(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    if set(payload) != {"errors"}:
        return None
    errors = payload["errors"]
    if not isinstance(errors, list) or not errors:
        return None
    for entry in errors:
        if not isinstance(entry, dict):
            return None
        if "message" not in entry or not isinstance(entry["message"], str):
            return None
        # Only understood request-error metadata may authorize non-execution.
        # Paths, extensions, GitHub-specific types and unknown keys stay ambiguous.
        if set(entry) - {"message", "locations"}:
            return None
        if "locations" in entry:
            locations = entry["locations"]
            if not isinstance(locations, list) or not locations:
                return None
            for location in locations:
                if not isinstance(location, dict) or set(location) != {"line", "column"}:
                    return None
                if any(type(value) is not int or value < 1 for value in location.values()):
                    return None
    detail = json.dumps(errors)[:2000]
    return f"GitHub rejected the mutation before execution: {detail}"


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


def _run_graphql_mutation(command: list[str], stdin: str | None = None) -> dict[str, Any]:
    """Run one GraphQL mutation and classify the raw subprocess response.

    Preserves API evidence from both zero and nonzero exits and applies one
    shared rejection predicate to both. Generic run()/run_json() semantics
    are unchanged; this narrow helper is the only path that interprets
    nonzero-exit stdout as protocol evidence. Stderr is diagnostic only.
    """
    try:
        process = subprocess.run(
            command,
            input=stdin,
            capture_output=True,
            encoding="utf-8",
            errors="strict",
        )
    except Exception as error:
        raise RuntimeError(
            f"GitHub GraphQL mutation transport failed: {storage.error_text(error)}"
        ) from error
    stdout = process.stdout if process.stdout is not None else ""
    stderr = (process.stderr or "").strip()
    returncode = process.returncode
    try:
        payload = _parse_mutation_json_document(stdout)
    except ValueError as error:
        diagnostic = f"exit {returncode}"
        if stderr:
            diagnostic += f"; stderr: {stderr[:500]}"
        raise RuntimeError(
            f"GitHub GraphQL mutation returned unparseable output "
            f"({diagnostic}): {error}"
        ) from error
    if not isinstance(payload, dict):
        raise RuntimeError(
            "GitHub GraphQL mutation returned an unexpected JSON root "
            f"(exit {returncode}): {type(payload).__name__}"
        )
    detail = _proven_rejection_detail(payload)
    if detail is not None:
        raise GithubRejectionError(detail)
    if returncode != 0:
        seen = json.dumps(payload)[:2000]
        diagnostic = f"exit {returncode}; response: {seen}"
        if stderr:
            diagnostic += f"; stderr: {stderr[:500]}"
        raise RuntimeError(f"GitHub GraphQL mutation failed ({diagnostic})")
    if "errors" in payload:
        seen = json.dumps(payload["errors"])[:2000]
        raise RuntimeError(
            "GitHub GraphQL mutation returned errors without proving "
            f"non-execution: {seen}"
        )
    if set(payload) != {"data"} or not isinstance(payload["data"], dict):
        raise RuntimeError(
            "GitHub GraphQL mutation returned an unexpected success payload"
        )
    return payload


def graphql_mutation(
    query: str, variables: dict[str, object] | None = None
) -> dict[str, Any]:
    """Execute one GraphQL *mutation* through the safe transport boundary.

    Only the GraphQL-spec pre-execution request-error shape proves
    non-execution (see _proven_rejection_detail): an exactly-one-document,
    strictly parsed object response whose keys are exactly {"errors"} (so
    "data" is absent, not null), with a non-empty list of message-bearing
    error objects, optionally with valid positive line/column locations, and
    no execution "path" marker or other metadata. That predicate comes from
    the GraphQL October 2021 spec: request errors prevent execution from
    beginning and return no "data" entry, while execution errors return an
    execution result containing "data" (possibly partial/null) and normally
    carry "path". Because mutations may side-effect during execution,
    errors/data-null/null-fields/paths/types/messages/stderr alone never
    prove non-execution.

    https://spec.graphql.org/October2021/#sec-Errors

    No stable durable GitHub contract was found proving that a
    GitHub-specific execution-result shape (for example FORBIDDEN with
    "data": {"addComment": null} and a path-bearing error) guarantees no
    side effect, so such shapes stay ambiguous (ordinary RuntimeError,
    fail closed). Only the spec-guaranteed pre-execution subset raises
    GithubRejectionError, identically on zero and nonzero exits.
    """
    command = ["gh", "api", "graphql", "-F", "query=@-"]
    command.extend(_graphql_arguments(variables or {}))
    return _run_graphql_mutation(command, query)


def graphql(query: str, variables: dict[str, object] | None = None) -> dict[str, Any]:
    """Read-only GraphQL observation path (queries, never mutations).

    Observation errors are transport-level RuntimeErrors, never
    GithubRejectionError: rejection proof applies only to the mutation
    boundary (graphql_mutation).
    """
    command = ["gh", "api", "graphql", "-F", "query=@-"]
    command.extend(_graphql_arguments(variables or {}))
    payload = run_json(command, query)
    if isinstance(payload, dict) and payload.get("errors"):
        raise RuntimeError(
            f"GitHub GraphQL errors: {json.dumps(payload['errors'])}"
        )
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
                "root_comment_id": root.get("id"),
                "root_login": normalize_login((root.get("author") or {}).get("login")),
                "body": root.get("body"),
                "root_updated_at": root.get("updatedAt"),
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
        "repository": canonical_repository(repository),
        "pr_number": pull_request.get("number"),
        "pr_state": pull_request.get("state"),
        "head_oid": pull_request.get("headRefOid"),
        "head_ref_name": pull_request.get("headRefName"),
        "head_repository": (
            canonical_repository(pull_request["headRepository"]["nameWithOwner"])
            if (pull_request.get("headRepository") or {}).get("nameWithOwner")
            else None
        ),
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


def add_comment(subject_id: str, body: str, *, graphql_call: Callable[..., dict[str, Any]] = graphql_mutation) -> dict[str, Any]:
    payload = graphql_call(ADD_COMMENT_MUTATION, {"subjectId": subject_id, "body": body})
    try:
        node = payload["data"]["addComment"]["commentEdge"]["node"]
        result = {
            "node_id": node["id"],
            "created_at": node["createdAt"],
            "url": node.get("url"),
            "body": node.get("body"),
        }
    except (KeyError, TypeError, AttributeError) as error:
        raise RuntimeError(
            "GitHub did not return the expected addComment payload"
        ) from error
    if not isinstance(result["node_id"], str) or not result["node_id"]:
        raise RuntimeError("GitHub did not return the expected addComment payload")
    return result


def resolve_thread(thread_id: str, *, graphql_call: Callable[..., dict[str, Any]] = graphql_mutation) -> dict[str, Any]:
    """Internal transport for one review-thread resolution mutation."""
    payload = graphql_call(RESOLVE_MUTATION, {"threadId": thread_id})
    try:
        thread = payload["data"]["resolveReviewThread"]["thread"]
    except (KeyError, TypeError) as error:
        raise RuntimeError(
            "GitHub did not return the expected resolveReviewThread payload"
        ) from error
    if not isinstance(thread, dict):
        raise RuntimeError(
            "GitHub did not return the expected resolveReviewThread payload"
        )
    if thread.get("id") != thread_id or thread.get("isResolved") is not True:
        raise RuntimeError("GitHub did not confirm the thread resolution")
    return {"id": thread_id, "isResolved": True}


def normalize_comment_body(body: object) -> str:
    if not isinstance(body, str):
        return ""
    return re.sub(r"\s+", " ", body.strip()).casefold()


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "v2 GitHub observation helpers. External product mutations go "
            "through the deterministic Phase 3 boundaries "
            "(externalize.py, review_request.py), not this CLI."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    snapshot = subparsers.add_parser("snapshot", help="Read-only head-bracketed PR observation")
    snapshot.add_argument("--repo", required=True)
    snapshot.add_argument("--pr", required=True, type=int)

    subparsers.add_parser("viewer", help="Print the authenticated GitHub login")

    args = parser.parse_args()
    if args.command == "snapshot":
        print(json.dumps(fetch_snapshot(args.repo, args.pr), indent=2))
    elif args.command == "viewer":
        print(run(["gh", "api", "user", "--jq", ".login"]).strip())


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(storage.error_text(error), file=sys.stderr)
        raise SystemExit(1) from error
