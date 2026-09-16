#!/usr/bin/env python3
"""Phase 3 externalization boundaries for committed remediation actions.

Phase 2 (``owned.py``) commits effective actions before any external work.
This module externalizes exactly those committed remediation actions under the
same permanent ownership scope:

- ``publish-fix-now``: the one-commit, one-push Fix-now publication boundary;
- ``ensure-issue``: the deferred-issue ensure/create boundary (deterministic
  marker plus versioned evidence-fingerprint identity, cooperative provenance,
  at most one creation attempt);
- ``resolve-thread``: the independent target-specific review-thread resolution
  boundary.

Every boundary performs the same safety-critical sequence itself: load the
committed-action inputs and Python-owned frozen evidence; perform the required
fresh authoritative external observation; compare current target/head evidence
with the frozen evidence; revalidate matching local ownership as the final
local authority operation immediately before the mutation; perform at most one
external mutation attempt; classify the result (``confirmed_success``,
``definitive_failure``, ``ambiguous``, or a pre-mutation ``refused``); and
return one closed structured outcome.

No boundary consumes a round, reserves a request, reconstructs a lost batch
from campaign state, or retries after an ambiguous result. The committed
remediation handoff is delivery-local and ephemeral: the caller must pass the
exact ``remediation_committed`` campaign id, round count, and the S1 snapshot
path that received the structured result. Missing, malformed, or incomplete
frozen evidence grants no mutation authority.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Callable

import campaign_model as model
import gitlocal
import github_api
import storage


ISSUE_FINGERPRINT_VERSION = "crpf1"

OUTCOMES = ("fix_now", "fix_later", "no_fix_required")


class FrozenEvidenceError(RuntimeError):
    """The delivery-local frozen evidence is absent, malformed, or foreign."""


def load_frozen_evidence(snapshot_path: str | Path) -> dict[str, Any]:
    """Load the Python-owned S1 snapshot backing the committed batch."""
    snapshot = storage.load_json(snapshot_path)
    if snapshot is None:
        raise FrozenEvidenceError(f"Frozen evidence does not exist: {snapshot_path}")
    if snapshot.get("complete") is not True:
        raise FrozenEvidenceError("Frozen evidence is incomplete")
    if not isinstance(snapshot.get("head_oid"), str) or not snapshot.get("head_oid"):
        raise FrozenEvidenceError("Frozen evidence has no head OID")
    if not isinstance(snapshot.get("repository"), str) or not snapshot.get(
        "repository"
    ):
        raise FrozenEvidenceError("Frozen evidence has no repository")
    return snapshot


def _frozen_thread(snapshot: dict[str, Any], thread_id: str) -> dict[str, Any]:
    """Derive one target's frozen identity fields from the snapshot itself.

    The caller may only select a thread ID from the frozen batch; every
    identity field is derived here. Missing or incomplete fields grant no
    mutation authority.
    """
    thread = next(
        (
            item
            for item in snapshot.get("threads", [])
            if isinstance(item, dict) and item.get("id") == thread_id
        ),
        None,
    )
    if thread is None:
        raise FrozenEvidenceError(
            f"Thread is not part of the frozen batch: {thread_id}"
        )
    if thread.get("is_resolved") is True:
        raise FrozenEvidenceError(f"Frozen thread is already resolved: {thread_id}")
    root_comment_id = thread.get("root_comment_id")
    root_author = thread.get("root_login")
    root_updated_at = thread.get("root_updated_at")
    if not isinstance(root_comment_id, str) or not root_comment_id:
        raise FrozenEvidenceError(f"Frozen thread has no root comment ID: {thread_id}")
    if not isinstance(root_author, str) or not root_author:
        raise FrozenEvidenceError(
            f"Frozen thread has no applicable root author: {thread_id}"
        )
    if not isinstance(root_updated_at, str) or not root_updated_at:
        raise FrozenEvidenceError(f"Frozen thread has no root updatedAt: {thread_id}")
    if not isinstance(thread.get("body"), str) or not isinstance(thread.get("path"), str):
        raise FrozenEvidenceError(
            f"Frozen thread is missing its body or path: {thread_id}"
        )
    return {
        "id": thread_id,
        "path": thread["path"],
        "root_comment_id": root_comment_id,
        "root_author": root_author,
        "body": thread["body"],
        "root_updated_at": root_updated_at,
    }


def _establish_authority(
    repository: str,
    pr_number: int,
    owner_token: str,
    *,
    repository_path: str | Path,
    campaign_id: str,
    rounds_used: int,
) -> dict[str, Any]:
    """Prove the delivery-local remediation handoff against durable state.

    Requires the semantically valid permanent lock, the exact owner token, a
    valid supported active campaign, the exact committed campaign identity, and
    a round count that still matches the ``remediation_committed`` handoff.
    """
    metadata = storage.ensure_active_campaign_owner(
        repository, pr_number, owner_token, repository_path=repository_path
    )
    path = storage.campaign_path(repository, pr_number, repository_path=repository_path)
    campaign = storage.load_json(path)
    if campaign is None:
        raise RuntimeError(f"Campaign record does not exist: {path}")
    model.validate_campaign(campaign, repository=repository, pull_request_number=pr_number)
    if campaign["campaign_id"] != metadata["campaign_id"]:
        raise RuntimeError(
            "Ownership lock belongs to a different campaign; refusing mutation"
        )
    if campaign["campaign_id"] != campaign_id:
        raise RuntimeError(
            "Committed remediation handoff belongs to a different campaign"
        )
    if campaign["rounds_used"] != rounds_used:
        raise RuntimeError(
            "Campaign round count no longer matches the committed remediation "
            "handoff"
        )
    return campaign


def _result(boundary: str, classification: str, **fields: Any) -> dict[str, Any]:
    """One closed structured outcome. ``ambiguous`` requires retained ownership."""
    return {
        "boundary": boundary,
        "classification": classification,
        "ownership": "retain" if classification == "ambiguous" else "delivery_disposition",
        **fields,
    }


def _frozen_consistent(
    target: dict[str, Any], current: dict[str, Any]
) -> bool:
    """True when the live thread still matches every frozen identity field."""
    return (
        current.get("is_resolved") is False
        and current.get("path") == target["path"]
        and current.get("root_comment_id") == target["root_comment_id"]
        and current.get("root_login") == target["root_author"]
        and current.get("body") == target["body"]
        and current.get("root_updated_at") == target["root_updated_at"]
    )


def _thread_by_id(snapshot: dict[str, Any], thread_id: str) -> dict[str, Any] | None:
    return next(
        (
            item
            for item in snapshot.get("threads", [])
            if isinstance(item, dict) and item.get("id") == thread_id
        ),
        None,
    )


def _validate_current_repository(
    current: dict[str, Any],
    *,
    canonical: str,
    pr_number: int,
    expected_head: str,
    require_open: bool = True,
) -> None:
    """Fail when the fresh observation no longer supports the mutation target."""
    if current.get("complete") is not True:
        raise FrozenEvidenceError("Fresh observation is incomplete")
    if current.get("repository") != canonical:
        raise FrozenEvidenceError("Fresh observation belongs to a different repository")
    if current.get("pr_number") != pr_number:
        raise FrozenEvidenceError(
            "Fresh observation belongs to a different pull request"
        )
    if require_open and current.get("pr_state") != "OPEN":
        raise FrozenEvidenceError("Pull request is no longer open")
    if current.get("head_oid") != expected_head:
        raise FrozenEvidenceError(
            "Current head does not match the expected evidence head"
        )


def _validate_applicable_target(
    current: dict[str, Any], target: dict[str, Any]
) -> None:
    live = _thread_by_id(current, target["id"])
    if live is None:
        raise FrozenEvidenceError(
            f"Target thread is no longer present: {target['id']}"
        )
    if not _frozen_consistent(target, live):
        raise FrozenEvidenceError(
            f"Target thread evidence changed since classification: {target['id']}"
        )


# ---------------------------------------------------------------------------
# Fix-now publication boundary


def publish_fix_now(
    *,
    repository: str,
    pr_number: int,
    owner_token: str,
    snapshot_path: str | Path,
    thread_ids: list[str],
    expected_head: str,
    worktree: str | Path,
    paths: list[str],
    commit_message: str,
    campaign_id: str,
    rounds_used: int,
    repository_path: str | Path = ".",
    fetch_snapshot: Callable[[], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Externalize Fix-now publication for the committed remediation batch.

    Owns frozen Fix-now target revalidation, current PR and remote-head
    validation, the final active ownership check immediately before the push,
    at most one non-force push, and the authoritative remote-result
    classification. Refuses before any mutation when the frozen evidence or
    current head/thread evidence no longer matches. The head ref is derived
    from the boundary's own fresh observation, never from the caller.
    """
    canonical = model.canonical_repository(repository)
    if fetch_snapshot is None:
        fetch_snapshot = lambda: github_api.fetch_snapshot(canonical, pr_number)  # noqa: E731

    frozen = load_frozen_evidence(snapshot_path)
    if frozen.get("repository") != canonical or frozen.get("pr_number") != pr_number:
        raise FrozenEvidenceError("Frozen evidence belongs to a different target")
    if frozen.get("head_oid") != expected_head:
        raise FrozenEvidenceError(
            "Frozen evidence head does not match the expected publication head"
        )
    if not thread_ids:
        raise FrozenEvidenceError("No Fix-now target was selected for publication")
    campaign = _establish_authority(
        repository,
        pr_number,
        owner_token,
        repository_path=repository_path,
        campaign_id=campaign_id,
        rounds_used=rounds_used,
    )
    reviewers = set(campaign["config"]["reviewer_logins"])
    targets = [_frozen_thread(frozen, thread_id) for thread_id in thread_ids]
    for target in targets:
        if target["root_author"] not in reviewers:
            raise FrozenEvidenceError(
                f"Frozen thread root author is not an applicable Codex identity: "
                f"{target['id']}"
            )

    try:
        current = fetch_snapshot()
        _validate_current_repository(
            current, canonical=canonical, pr_number=pr_number, expected_head=expected_head
        )
        for target in targets:
            _validate_applicable_target(current, target)
    except FrozenEvidenceError as error:
        return _result("publish_fix_now", "refused", reason=str(error))
    branch = current.get("head_ref_name")
    if not isinstance(branch, str) or not branch:
        return _result(
            "publish_fix_now",
            "refused",
            reason="the fresh observation does not expose a supported head ref",
        )

    def final_authority_check() -> None:
        """The final local authority operation before the push attempt."""
        _establish_authority(
            repository,
            pr_number,
            owner_token,
            repository_path=repository_path,
            campaign_id=campaign_id,
            rounds_used=rounds_used,
        )

    published = gitlocal.publish_batch(
        worktree=worktree,
        branch=branch,
        paths=paths,
        commit_message=commit_message,
        expected_head=expected_head,
        repository=canonical,
        pr_number=pr_number,
        owner_token=owner_token,
        repository_path=repository_path,
        before_push=final_authority_check,
    )
    status = published.get("status")
    if published.get("published") is True and status == "pushed":
        return _result(
            "publish_fix_now",
            "confirmed_success",
            published_head=published["commit"],
            expected_head=expected_head,
        )
    if status == "ambiguous_publication":
        return _result(
            "publish_fix_now",
            "ambiguous",
            status=status,
            local_commit=published.get("local_commit"),
            remote_head=published.get("remote_head"),
        )
    # no_changes, remote_head_advanced, remote_head_advanced_after_commit, and
    # push_failed_clean all authoritatively prove no publication occurred.
    return _result(
        "publish_fix_now",
        "definitive_failure",
        status=status,
        local_commit=published.get("local_commit"),
        remote_head=published.get("remote_head"),
    )


# ---------------------------------------------------------------------------
# Deferred-issue boundary


def deferred_issue_marker(repository: str, pr_number: int, thread_id: str) -> str:
    safe_thread = thread_id.replace("--", "-")
    return (
        f"<!-- codex-review-pulse: "
        f"{model.canonical_repository(repository)}#{pr_number}/{safe_thread} -->"
    )


def deferred_issue_fingerprint(
    *, repository: str, pr_number: int, triage_head: str, target: dict[str, Any]
) -> str:
    """Versioned SHA-256 evidence fingerprint from canonical frozen evidence.

    Deterministic Python computes it; the model never supplies it. Changing the
    expected triage head or any frozen identity field changes the fingerprint.
    """
    canonical = json.dumps(
        {
            "version": ISSUE_FINGERPRINT_VERSION,
            "repository": model.canonical_repository(repository),
            "pull_request": pr_number,
            "triage_head": triage_head,
            "thread_id": target["id"],
            "root_comment_id": target["root_comment_id"],
            "root_author": target["root_author"],
            "root_body": target["body"],
            "root_updated_at": target["root_updated_at"],
            "thread_path": target["path"],
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"{ISSUE_FINGERPRINT_VERSION}-{digest}"


def deferred_issue_body(
    *, repository: str, pr_number: int, triage_head: str, target: dict[str, Any], body: str
) -> str:
    """Deterministic issue body: the model's prose plus the identity markers."""
    fingerprint = deferred_issue_fingerprint(
        repository=repository, pr_number=pr_number, triage_head=triage_head, target=target
    )
    return (
        body.rstrip()
        + "\n\n"
        + deferred_issue_marker(repository, pr_number, target["id"])
        + "\n"
        + f"<!-- codex-review-pulse-fingerprint: {fingerprint} -->\n"
    )


def _find_matching_issue(
    list_issues: Callable[[], list[dict[str, Any]]],
    *,
    marker: str,
    fingerprint: str,
    viewer: str,
) -> dict[str, Any] | None:
    """Cooperative provenance match: exact marker, fingerprint, author, open."""
    for issue in list_issues():
        if not isinstance(issue, dict):
            continue
        issue_body = issue.get("body") or ""
        if marker not in issue_body or fingerprint not in issue_body:
            continue
        author = model.normalize_login((issue.get("author") or {}).get("login"))
        if author != viewer:
            continue
        return issue
    return None


def ensure_deferred_issue(
    *,
    repository: str,
    pr_number: int,
    owner_token: str,
    snapshot_path: str | Path,
    thread_id: str,
    triage_head: str,
    title: str,
    body: str,
    campaign_id: str,
    rounds_used: int,
    repository_path: str | Path = ".",
    fetch_snapshot: Callable[[], dict[str, Any]] | None = None,
    list_issues: Callable[[], list[dict[str, Any]]] | None = None,
    create_issue: Callable[[str, str], str] | None = None,
    viewer_call: Callable[[], str] | None = None,
) -> dict[str, Any]:
    """Externalize one Fix-later deferred issue for the committed batch.

    Owns the exact marker/fingerprint/provenance search, current head/thread
    revalidation when creation is needed, the final active ownership check
    immediately before creation, at most one issue-creation attempt, and the
    authoritative creation-or-reuse classification. A trustworthy exact
    current-evidence open issue is reused without any mutation.
    """
    canonical = model.canonical_repository(repository)
    if fetch_snapshot is None:
        fetch_snapshot = lambda: github_api.fetch_snapshot(canonical, pr_number)  # noqa: E731
    if list_issues is None:
        def list_issues() -> list[dict[str, Any]]:
            return github_api.run_json(
                [
                    "gh", "issue", "list", "--repo", canonical,
                    "--state", "open", "--limit", "1000",
                    "--json", "number,url,body,author",
                ]
            )
    if create_issue is None:
        def create_issue(issue_title: str, issue_body: str) -> str:
            return github_api.run(
                [
                    "gh", "issue", "create", "--repo", canonical,
                    "--title", issue_title, "--body-file", "-",
                ],
                issue_body,
            ).strip()
    if viewer_call is None:
        viewer_call = lambda: github_api.run(  # noqa: E731
            ["gh", "api", "user", "--jq", ".login"]
        ).strip()

    frozen = load_frozen_evidence(snapshot_path)
    if frozen.get("repository") != canonical or frozen.get("pr_number") != pr_number:
        raise FrozenEvidenceError("Frozen evidence belongs to a different target")
    campaign = _establish_authority(
        repository,
        pr_number,
        owner_token,
        repository_path=repository_path,
        campaign_id=campaign_id,
        rounds_used=rounds_used,
    )
    target = _frozen_thread(frozen, thread_id)
    if target["root_author"] not in set(campaign["config"]["reviewer_logins"]):
        raise FrozenEvidenceError(
            f"Frozen thread root author is not an applicable Codex identity: {thread_id}"
        )

    marker = deferred_issue_marker(canonical, pr_number, thread_id)
    fingerprint = deferred_issue_fingerprint(
        repository=canonical, pr_number=pr_number, triage_head=triage_head, target=target
    )
    try:
        viewer = model.normalize_login(viewer_call() or "")
        if viewer is None:
            raise FrozenEvidenceError("Authenticated GitHub identity is unavailable")
        match = _find_matching_issue(
            list_issues, marker=marker, fingerprint=fingerprint, viewer=viewer
        )
    except Exception as error:  # noqa: BLE001 - provenance cannot be established
        return _result(
            "ensure_deferred_issue",
            "refused",
            reason=f"issue provenance could not be established: {storage.error_text(error)}",
        )
    if match is not None:
        return _result(
            "ensure_deferred_issue",
            "confirmed_success",
            action="reused",
            issue_number=match.get("number"),
            issue_url=match.get("url"),
        )

    try:
        current = fetch_snapshot()
        _validate_current_repository(
            current, canonical=canonical, pr_number=pr_number, expected_head=triage_head
        )
        _validate_applicable_target(current, target)
    except FrozenEvidenceError as error:
        return _result("ensure_deferred_issue", "refused", reason=str(error))

    def final_authority_check() -> None:
        _establish_authority(
            repository,
            pr_number,
            owner_token,
            repository_path=repository_path,
            campaign_id=campaign_id,
            rounds_used=rounds_used,
        )

    final_authority_check()
    full_body = deferred_issue_body(
        repository=canonical,
        pr_number=pr_number,
        triage_head=triage_head,
        target=target,
        body=body,
    )
    try:
        url = create_issue(title, full_body)
    except Exception as creation_error:  # noqa: BLE001 - classification follows
        # A transport-level failure is never definitive: the creation may still
        # complete. A visible matching issue proves the creation happened.
        try:
            confirmed = _find_matching_issue(
                list_issues, marker=marker, fingerprint=fingerprint, viewer=viewer
            )
        except Exception:  # noqa: BLE001 - outcome cannot be established
            confirmed = None
            observed = False
        else:
            observed = True
        if confirmed is not None:
            return _result(
                "ensure_deferred_issue",
                "confirmed_success",
                action="created",
                issue_number=confirmed.get("number"),
                issue_url=confirmed.get("url"),
                creation_error=storage.error_text(creation_error),
            )
        if not observed:
            return _result(
                "ensure_deferred_issue",
                "ambiguous",
                reason="issue creation failed and the re-observation failed",
                creation_error=storage.error_text(creation_error),
            )
        return _result(
            "ensure_deferred_issue",
            "ambiguous",
            reason=(
                "issue creation failed and no matching issue is visible; the "
                "creation may still complete"
            ),
            creation_error=storage.error_text(creation_error),
        )
    number: int | str | None
    try:
        number = int(url.rstrip("/").split("/")[-1])
    except ValueError:
        number = None
    return _result(
        "ensure_deferred_issue",
        "confirmed_success",
        action="created",
        issue_number=number,
        issue_url=url,
    )


# ---------------------------------------------------------------------------
# Independent thread-resolution boundary


def resolve_review_thread(
    *,
    repository: str,
    pr_number: int,
    owner_token: str,
    snapshot_path: str | Path,
    thread_id: str,
    expected_head: str,
    outcome: str,
    campaign_id: str,
    rounds_used: int,
    published_commit: str | None = None,
    issue_number: int | None = None,
    repository_path: str | Path = ".",
    fetch_snapshot: Callable[[], dict[str, Any]] | None = None,
    remote_head_call: Callable[[str], str] | None = None,
    list_issues: Callable[[], list[dict[str, Any]]] | None = None,
    viewer_call: Callable[[], str] | None = None,
    resolve_call: Callable[[str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Externalize one thread resolution for the committed remediation batch.

    Validates each target independently against the frozen evidence; sibling
    threads are never prerequisites. Enforces the ordering rules
    deterministically: Fix-now requires the confirmed publication commit (a
    different, currently-pushed, current head); Fix-later requires a
    trustworthy exact current-evidence open issue; No-fix requires only the
    final revalidation.
    """
    canonical = model.canonical_repository(repository)
    if fetch_snapshot is None:
        fetch_snapshot = lambda: github_api.fetch_snapshot(canonical, pr_number)  # noqa: E731
    if remote_head_call is None:
        remote_head_call = lambda ref: gitlocal.remote_head(repository_path, ref)  # noqa: E731
    if resolve_call is None:
        resolve_call = lambda thread: github_api.resolve_thread(thread)  # noqa: E731
    if list_issues is None:
        def list_issues() -> list[dict[str, Any]]:
            return github_api.run_json(
                [
                    "gh", "issue", "list", "--repo", canonical,
                    "--state", "open", "--limit", "1000",
                    "--json", "number,url,body,author",
                ]
            )
    if viewer_call is None:
        viewer_call = lambda: github_api.run(  # noqa: E731
            ["gh", "api", "user", "--jq", ".login"]
        ).strip()

    frozen = load_frozen_evidence(snapshot_path)
    if frozen.get("repository") != canonical or frozen.get("pr_number") != pr_number:
        raise FrozenEvidenceError("Frozen evidence belongs to a different target")
    campaign = _establish_authority(
        repository,
        pr_number,
        owner_token,
        repository_path=repository_path,
        campaign_id=campaign_id,
        rounds_used=rounds_used,
    )
    target = _frozen_thread(frozen, thread_id)
    if target["root_author"] not in set(campaign["config"]["reviewer_logins"]):
        raise FrozenEvidenceError(
            f"Frozen thread root author is not an applicable Codex identity: {thread_id}"
        )
    if outcome not in OUTCOMES:
        raise RuntimeError(f"Unknown remediation outcome: {outcome!r}")

    try:
        current = fetch_snapshot()
        _validate_current_repository(
            current,
            canonical=canonical,
            pr_number=pr_number,
            expected_head=expected_head,
            require_open=False,
        )
    except FrozenEvidenceError as error:
        return _result("resolve_review_thread", "refused", reason=str(error))

    live = _thread_by_id(current, target["id"])
    if live is None:
        return _result(
            "resolve_review_thread",
            "refused",
            reason=f"target thread is no longer present: {thread_id}",
        )
    if live.get("is_resolved") is True:
        return _result(
            "resolve_review_thread",
            "confirmed_success",
            already_resolved=True,
        )
    if not _frozen_consistent(target, live):
        return _result(
            "resolve_review_thread",
            "refused",
            reason=f"target thread evidence changed since classification: {thread_id}",
        )

    if outcome == "fix_now":
        if not isinstance(published_commit, str) or not published_commit:
            return _result(
                "resolve_review_thread",
                "refused",
                reason="Fix-now resolution requires the confirmed published commit",
            )
        if published_commit == frozen.get("head_oid"):
            return _result(
                "resolve_review_thread",
                "refused",
                reason="the published commit must advance the pre-publication head",
            )
        if published_commit != expected_head:
            return _result(
                "resolve_review_thread",
                "refused",
                reason="the published commit does not match the expected current head",
            )
        remote = remote_head_call(current.get("head_ref_name") or "")
        if remote != published_commit:
            return _result(
                "resolve_review_thread",
                "refused",
                reason="the remote branch head does not match the published commit",
            )
    elif outcome == "fix_later":
        if not isinstance(issue_number, int):
            return _result(
                "resolve_review_thread",
                "refused",
                reason="Fix-later resolution requires the confirmed deferred issue",
            )
        marker = deferred_issue_marker(canonical, pr_number, thread_id)
        fingerprint = deferred_issue_fingerprint(
            repository=canonical,
            pr_number=pr_number,
            triage_head=expected_head,
            target=target,
        )
        try:
            viewer = model.normalize_login(viewer_call() or "")
            match = (
                _find_matching_issue(
                    list_issues, marker=marker, fingerprint=fingerprint, viewer=viewer or ""
                )
                if viewer
                else None
            )
        except Exception as error:  # noqa: BLE001 - prerequisite unprovable
            return _result(
                "resolve_review_thread",
                "refused",
                reason=(
                    "issue provenance could not be established: "
                    + storage.error_text(error)
                ),
            )
        if match is None or match.get("number") != issue_number:
            return _result(
                "resolve_review_thread",
                "refused",
                reason="no trustworthy exact current-evidence open issue matches",
            )

    def final_authority_check() -> None:
        _establish_authority(
            repository,
            pr_number,
            owner_token,
            repository_path=repository_path,
            campaign_id=campaign_id,
            rounds_used=rounds_used,
        )

    final_authority_check()
    try:
        resolve_call(thread_id)
    except github_api.GithubRejectionError as error:
        return _classify_resolution_failure(
            fetch_snapshot=fetch_snapshot,
            target=target,
            definitive=True,
            error=error,
        )
    except Exception as error:  # noqa: BLE001 - transport-level classification
        return _classify_resolution_failure(
            fetch_snapshot=fetch_snapshot,
            target=target,
            definitive=False,
            error=error,
        )
    return _result("resolve_review_thread", "confirmed_success", already_resolved=False)


def _classify_resolution_failure(
    *,
    fetch_snapshot: Callable[[], dict[str, Any]],
    target: dict[str, Any],
    definitive: bool,
    error: Exception,
) -> dict[str, Any]:
    """Classify a failed resolution attempt from bounded re-observation."""
    try:
        post = fetch_snapshot()
        live = _thread_by_id(post, target["id"])
    except Exception:  # noqa: BLE001 - outcome cannot be established
        return _result(
            "resolve_review_thread",
            "ambiguous",
            reason="resolution failed and the re-observation failed",
            error=storage.error_text(error),
        )
    if live is not None and live.get("is_resolved") is True:
        return _result("resolve_review_thread", "confirmed_success", already_resolved=False)
    if definitive:
        # A server-authoritative rejection proves the attempt did not resolve
        # the target and cannot later complete from that attempt.
        return _result(
            "resolve_review_thread",
            "definitive_failure",
            error=storage.error_text(error),
        )
    return _result(
        "resolve_review_thread",
        "ambiguous",
        reason="resolution failed for transport-level reasons and may still complete",
        error=storage.error_text(error),
    )


# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Phase 3 externalization boundaries for committed remediation "
            "actions (publish-fix-now, ensure-issue, resolve-thread)"
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--repo", required=True)
    common.add_argument("--pr", required=True, type=int)
    common.add_argument("--repository-path", default=".")
    common.add_argument("--owner-token", required=True)
    common.add_argument("--snapshot", required=True, type=Path)
    common.add_argument("--campaign-id", required=True)
    common.add_argument("--rounds-used", required=True, type=int)

    publish = subparsers.add_parser(
        "publish-fix-now", help="One-commit one-push Fix-now publication"
    )
    publish.add_argument("--thread-id", action="append", required=True, dest="thread_ids")
    publish.add_argument("--expected-head", required=True)
    publish.add_argument("--worktree", required=True)
    publish.add_argument("--path", action="append", required=True, dest="paths")
    publish.add_argument("--message", required=True)

    issue = subparsers.add_parser(
        "ensure-issue", help="Reuse or create the deferred Fix-later issue"
    )
    issue.add_argument("--thread-id", required=True)
    issue.add_argument("--expected-head", required=True)
    issue.add_argument("--title", required=True)
    issue.add_argument("--body-file", required=True, type=Path)

    resolve = subparsers.add_parser(
        "resolve-thread", help="Resolve one independently validated review thread"
    )
    resolve.add_argument("--thread-id", required=True)
    resolve.add_argument("--expected-head", required=True)
    resolve.add_argument(
        "--outcome",
        choices=list(OUTCOMES),
        required=True,
    )
    resolve.add_argument("--published-commit")
    resolve.add_argument("--issue-number", type=int)

    args = parser.parse_args()
    if args.command == "publish-fix-now":
        result = publish_fix_now(
            repository=args.repo,
            pr_number=args.pr,
            owner_token=args.owner_token,
            snapshot_path=args.snapshot,
            thread_ids=list(args.thread_ids),
            expected_head=args.expected_head,
            worktree=args.worktree,
            paths=list(args.paths),
            commit_message=args.message,
            campaign_id=args.campaign_id,
            rounds_used=args.rounds_used,
            repository_path=args.repository_path,
        )
    elif args.command == "ensure-issue":
        body = (
            args.body_file.read_text(encoding="utf-8")
            if str(args.body_file) != "-"
            else sys.stdin.read()
        )
        result = ensure_deferred_issue(
            repository=args.repo,
            pr_number=args.pr,
            owner_token=args.owner_token,
            snapshot_path=args.snapshot,
            thread_id=args.thread_id,
            triage_head=args.expected_head,
            title=args.title,
            body=body,
            campaign_id=args.campaign_id,
            rounds_used=args.rounds_used,
            repository_path=args.repository_path,
        )
    else:
        result = resolve_review_thread(
            repository=args.repo,
            pr_number=args.pr,
            owner_token=args.owner_token,
            snapshot_path=args.snapshot,
            thread_id=args.thread_id,
            expected_head=args.expected_head,
            outcome=args.outcome,
            campaign_id=args.campaign_id,
            rounds_used=args.rounds_used,
            published_commit=args.published_commit,
            issue_number=args.issue_number,
            repository_path=args.repository_path,
        )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(storage.error_text(error), file=sys.stderr)
        raise SystemExit(1) from error
