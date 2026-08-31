#!/usr/bin/env python3
"""Pure review-state and frozen-batch checkpoint transitions."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Iterable


SCHEMA_VERSION = 1
DEFAULT_CODEX_LOGINS = ("chatgpt-codex-connector",)


def canonical_repository(repository: str) -> str:
    """Return a stable, case-insensitive OWNER/REPO checkpoint key."""
    value = repository.strip()
    if value.count("/") != 1 or any(not part for part in value.split("/")):
        raise ValueError("Repository must be OWNER/REPO")
    return value.casefold()


def normalize_login(login: object) -> str | None:
    """Normalize GitHub bot and non-bot spellings to one identity."""
    if not isinstance(login, str):
        return None
    value = login.strip().casefold()
    if value.endswith("[bot]"):
        value = value[:-5]
    return value or None


def unique_logins(
    configured: Iterable[str] | None,
    *,
    defaults: Iterable[str] = DEFAULT_CODEX_LOGINS,
    label: str,
) -> list[str]:
    """Normalize and de-duplicate configured identities in stable order."""
    values = list(configured) if configured is not None else list(defaults)
    result: list[str] = []
    seen: set[str] = set()
    for login in values:
        key = normalize_login(login)
        if key is None:
            raise ValueError(f"{label} logins must not be empty")
        if key not in seen:
            result.append(key)
            seen.add(key)
    if not result:
        raise ValueError(f"At least one {label} login is required")
    return result


def _root_author(thread: dict[str, Any]) -> str | None:
    comments = (thread.get("comments") or {}).get("nodes") or []
    if not comments:
        return None
    return normalize_login((comments[0].get("author") or {}).get("login"))


def classify_unresolved_threads(
    review_threads: Iterable[dict[str, Any]], reviewer_logins: Iterable[str]
) -> tuple[list[str], list[dict[str, Any]]]:
    """Partition unresolved threads by trustworthy root-comment identity."""
    reviewer_keys = set(reviewer_logins)
    targeted_ids: list[str] = []
    targeted_seen: set[str] = set()
    non_target: list[dict[str, Any]] = []

    for thread in review_threads:
        if thread.get("isResolved") is True:
            continue
        thread_id = thread.get("id")
        root_author = _root_author(thread)
        if (
            isinstance(thread_id, str)
            and thread_id
            and root_author in reviewer_keys
        ):
            if thread_id not in targeted_seen:
                targeted_ids.append(thread_id)
                targeted_seen.add(thread_id)
            continue

        reason = (
            "unknown_root_author"
            if root_author is None
            else "non_target_root_author"
        )
        non_target.append(
            {
                "id": thread_id,
                "root_author": root_author,
                "reason": reason,
                "path": thread.get("path"),
                "url": _root_comment_url(thread),
            }
        )
    return targeted_ids, non_target


def _root_comment_url(thread: dict[str, Any]) -> str | None:
    comments = (thread.get("comments") or {}).get("nodes") or []
    if not comments:
        return None
    url = comments[0].get("url")
    return url if isinstance(url, str) else None


def classify_approval_reactions(
    reactions: Iterable[dict[str, Any]], approval_logins: Iterable[str]
) -> tuple[list[dict[str, Any]], list[str]]:
    """Return stable qualifying events, excluding conflicting duplicate IDs."""
    approval_keys = set(approval_logins)
    by_id: dict[str, list[dict[str, Any]]] = {}
    invalid_ids: set[str] = set()
    for reaction in reactions:
        reaction_id = reaction.get("id")
        if not isinstance(reaction_id, str) or not reaction_id:
            invalid_ids.add("<missing>")
            continue
        by_id.setdefault(reaction_id, []).append(reaction)

    qualifying: list[dict[str, Any]] = []
    for reaction_id in sorted(by_id):
        items = by_id[reaction_id]
        identities = {
            normalize_login((item.get("user") or {}).get("login")) for item in items
        }
        contents = {item.get("content") for item in items}
        created_at_values = {item.get("createdAt") for item in items}
        if (
            len(identities) != 1
            or len(contents) != 1
            or len(created_at_values) != 1
        ):
            invalid_ids.add(reaction_id)
            continue
        identity = next(iter(identities))
        content = next(iter(contents))
        if identity in approval_keys and content == "THUMBS_UP":
            source = min(
                items,
                key=lambda item: (
                    str(item.get("createdAt") or ""),
                    str((item.get("user") or {}).get("login") or ""),
                ),
            )
            qualifying.append(
                {
                    "id": reaction_id,
                    "content": "THUMBS_UP",
                    "createdAt": source.get("createdAt"),
                    "login": identity,
                }
            )
    return qualifying, sorted(invalid_ids)


def classify_current_head_approval_reviews(
    reviews: Iterable[dict[str, Any]],
    approval_logins: Iterable[str],
    head_oid: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return APPROVED reviews directly bound to the current head commit."""
    approval_keys = set(approval_logins)
    qualifying: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    by_id: dict[str, list[dict[str, Any]]] = {}
    for review in reviews:
        review_id = review.get("id")
        if not isinstance(review_id, str) or not review_id:
            excluded.append({"id": review_id, "reason": "missing_review_id"})
            continue
        by_id.setdefault(review_id, []).append(review)

    for review_id in sorted(by_id):
        items = by_id[review_id]
        signatures = {
            (
                normalize_login((item.get("author") or {}).get("login")),
                item.get("state"),
                (item.get("commit") or {}).get("oid"),
            )
            for item in items
        }
        if len(signatures) != 1:
            excluded.append({"id": review_id, "reason": "conflicting_duplicate_review_id"})
            continue
        review = items[0]
        login, state, commit_oid = next(iter(signatures))
        if login not in approval_keys:
            excluded.append({"id": review_id, "reason": "non_approval_author"})
        elif state != "APPROVED":
            excluded.append({"id": review_id, "reason": "review_not_approved"})
        elif not isinstance(commit_oid, str) or not commit_oid:
            excluded.append({"id": review_id, "reason": "missing_review_commit"})
        elif commit_oid != head_oid:
            excluded.append({"id": review_id, "reason": "review_commit_not_current_head"})
        else:
            qualifying.append(
                {
                    "id": review_id,
                    "login": login,
                    "state": "APPROVED",
                    "commit_oid": commit_oid,
                    "submittedAt": review.get("submittedAt"),
                    "url": review.get("url"),
                }
            )
    return qualifying, excluded


def _event_timestamp(item: dict[str, Any], *fields: str) -> str | None:
    for field in fields:
        value = item.get(field)
        if isinstance(value, str) and value:
            return value
    return None


def _event_author(item: dict[str, Any]) -> str | None:
    author = item.get("author")
    return normalize_login(author.get("login") if isinstance(author, dict) else None)


def _derive_batch_publication_event(
    checkpoint: dict[str, Any] | None,
    *,
    head_oid: str,
    observed_at: str | None,
) -> dict[str, Any] | None:
    """Bind a published batch to its first authoritative current-head observation."""
    if not isinstance(checkpoint, dict):
        return None
    previous_snapshot = checkpoint.get("latest_target_snapshot")
    previous_event = (
        previous_snapshot.get("batch_publication_event")
        if isinstance(previous_snapshot, dict)
        else None
    )
    if (
        isinstance(previous_event, dict)
        and previous_event.get("head_oid") == head_oid
        and isinstance(previous_event.get("created_at"), str)
        and previous_event["created_at"]
    ):
        return deepcopy(previous_event)

    batch = checkpoint.get("active_batch")
    publication = batch.get("publication") if isinstance(batch, dict) else None
    if not isinstance(publication, dict):
        return None
    if publication.get("status") != "succeeded":
        return None
    if publication.get("published_commit") != head_oid:
        return None
    if not isinstance(observed_at, str) or not observed_at:
        return None
    return {
        "kind": "batch_publication",
        "head_oid": head_oid,
        "commit_oid": head_oid,
        "created_at": observed_at,
    }


def _derive_relevant_codex_events(
    *,
    review_threads: Iterable[dict[str, Any]],
    reviews: Iterable[dict[str, Any]],
    conversation_comments: Iterable[dict[str, Any]],
    reviewer_logins: Iterable[str],
    approval_logins: Iterable[str],
    head_oid: str,
) -> list[dict[str, Any]]:
    """Normalize current-head Codex activity from the authoritative snapshot."""
    actor_keys = set(reviewer_logins) | set(approval_logins)
    events: list[dict[str, Any]] = []

    for review in reviews:
        if not isinstance(review, dict):
            continue
        commit = review.get("commit")
        commit_oid = commit.get("oid") if isinstance(commit, dict) else None
        event_id = review.get("id")
        author = _event_author(review)
        created_at = _event_timestamp(review, "submittedAt", "updatedAt")
        if (
            commit_oid == head_oid
            and isinstance(event_id, str)
            and event_id
            and author in actor_keys
            and created_at is not None
        ):
            events.append(
                {
                    "kind": "review",
                    "id": event_id,
                    "head_oid": head_oid,
                    "login": author,
                    "created_at": created_at,
                }
            )

    for thread in review_threads:
        if not isinstance(thread, dict) or thread.get("isOutdated") is True:
            continue
        thread_id = thread.get("id")
        comments_connection = thread.get("comments")
        comments = (
            comments_connection.get("nodes")
            if isinstance(comments_connection, dict)
            else []
        ) or []
        for comment in comments:
            if not isinstance(comment, dict):
                continue
            event_id = comment.get("id")
            author = _event_author(comment)
            created_at = _event_timestamp(comment, "createdAt", "updatedAt")
            if (
                isinstance(event_id, str)
                and event_id
                and author in actor_keys
                and created_at is not None
            ):
                events.append(
                    {
                        "kind": "review_thread",
                        "id": event_id,
                        "thread_id": thread_id,
                        "head_oid": head_oid,
                        "login": author,
                        "created_at": created_at,
                    }
                )

    for comment in conversation_comments:
        if not isinstance(comment, dict):
            continue
        event_id = comment.get("id")
        author = _event_author(comment)
        created_at = _event_timestamp(comment, "createdAt", "updatedAt")
        if (
            isinstance(event_id, str)
            and event_id
            and author in actor_keys
            and created_at is not None
        ):
            events.append(
                {
                    "kind": "conversation_comment",
                    "id": event_id,
                    "head_oid": head_oid,
                    "login": author,
                    "created_at": created_at,
                }
            )

    unique: dict[tuple[str, str], dict[str, Any]] = {}
    for event in events:
        unique[(event["kind"], event["id"])] = event
    return [unique[key] for key in sorted(unique)]


def empty_checkpoint(repository: str, pr_number: int) -> dict[str, Any]:
    if pr_number < 1:
        raise ValueError("Pull request number must be positive")
    return {
        "schema_version": SCHEMA_VERSION,
        "repository": canonical_repository(repository),
        "pull_request_number": pr_number,
        "approval_epoch": None,
        "latest_target_snapshot": None,
        "active_batch": None,
    }


def validate_checkpoint(
    checkpoint: dict[str, Any], repository: str, pr_number: int
) -> None:
    if checkpoint.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Unsupported checkpoint schema version")
    if checkpoint.get("repository") != canonical_repository(repository):
        raise ValueError("Checkpoint repository does not match requested repository")
    if checkpoint.get("pull_request_number") != pr_number:
        raise ValueError("Checkpoint pull request does not match requested pull request")


def evaluate_snapshot(
    *,
    repository: str,
    pr_number: int,
    head_oid: str,
    pull_request_state: str,
    review_threads: Iterable[dict[str, Any]],
    reactions: Iterable[dict[str, Any]],
    reviews: Iterable[dict[str, Any]] = (),
    conversation_comments: Iterable[dict[str, Any]] = (),
    reviewer_logins: Iterable[str] | None = None,
    approval_logins: Iterable[str] | None = None,
    checkpoint: dict[str, Any] | None = None,
    observed_at: str | None = None,
    head_repository: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Evaluate one network snapshot and return result plus next checkpoint."""
    if not isinstance(head_oid, str) or not head_oid:
        raise ValueError("Current head OID is required")
    review_threads = list(review_threads)
    reviews = list(reviews)
    conversation_comments = list(conversation_comments)
    reviewers = unique_logins(reviewer_logins, label="reviewer")
    approvers = unique_logins(approval_logins, label="approval")
    targeted_ids, non_target = classify_unresolved_threads(
        review_threads, reviewers
    )
    qualifying, invalid_reaction_ids = classify_approval_reactions(
        reactions, approvers
    )
    qualifying_reviews, excluded_approval_reviews = (
        classify_current_head_approval_reviews(reviews, approvers, head_oid)
    )
    batch_publication_event = _derive_batch_publication_event(
        checkpoint,
        head_oid=head_oid,
        observed_at=observed_at,
    )
    relevant_codex_events = _derive_relevant_codex_events(
        review_threads=review_threads,
        reviews=reviews,
        conversation_comments=conversation_comments,
        reviewer_logins=reviewers,
        approval_logins=approvers,
        head_oid=head_oid,
    )
    current_ids = {reaction["id"] for reaction in qualifying}

    cold_start = checkpoint is None
    next_checkpoint = (
        empty_checkpoint(repository, pr_number)
        if checkpoint is None
        else deepcopy(checkpoint)
    )
    validate_checkpoint(next_checkpoint, repository, pr_number)
    previous_epoch = next_checkpoint.get("approval_epoch")

    if previous_epoch is None or previous_epoch.get("head_oid") != head_oid:
        epoch = {
            "head_oid": head_oid,
            "baseline_reaction_ids": sorted(current_ids),
            "observed_reaction_ids": sorted(current_ids),
            "proven_reaction_ids": [],
            "started_at": observed_at,
            "last_observed_at": observed_at,
        }
        epoch_transition = "cold_start" if previous_epoch is None else "head_changed"
    else:
        baseline_ids = set(previous_epoch.get("baseline_reaction_ids") or [])
        previously_observed = set(previous_epoch.get("observed_reaction_ids") or [])
        previously_proven = set(previous_epoch.get("proven_reaction_ids") or [])
        newly_observed = current_ids - previously_observed - baseline_ids
        proven = (previously_proven | newly_observed) & current_ids
        epoch = {
            "head_oid": head_oid,
            "baseline_reaction_ids": sorted(baseline_ids),
            "observed_reaction_ids": sorted(current_ids),
            "proven_reaction_ids": sorted(proven),
            "started_at": previous_epoch.get("started_at"),
            "last_observed_at": observed_at,
        }
        epoch_transition = "unchanged"

    next_checkpoint["approval_epoch"] = epoch
    proven_ids = set(epoch["proven_reaction_ids"])
    if qualifying_reviews:
        approval_status = "approved_current_head"
        approval_proof = "pull_request_review"
    elif proven_ids:
        approval_status = "approved_current_head"
        approval_proof = "reaction_epoch"
    elif current_ids:
        approval_status = "ambiguous_existing_reaction"
        approval_proof = None
    else:
        approval_status = "awaiting_current_head_approval"
        approval_proof = None

    codex_terminal = (
        pull_request_state == "OPEN"
        and not targeted_ids
        and approval_status == "approved_current_head"
    )
    next_checkpoint["latest_target_snapshot"] = {
        "head_oid": head_oid,
        "targeted_unresolved_thread_ids": targeted_ids,
        "non_target_thread_ids": sorted(
            item["id"]
            for item in non_target
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        ),
        "reviewer_logins": reviewers,
        "head_repository": head_repository or canonical_repository(repository),
        "pull_request_state": pull_request_state,
        "approval_status": approval_status,
        "codex_review_in_progress": False,
        "review_activity_ok": True,
        "review_in_progress_reaction_ids": [],
        "batch_publication_event": batch_publication_event,
        "relevant_codex_events": relevant_codex_events,
        "snapshot_stable": True,
        "mixed_head": False,
        "auth_ok": True,
        "api_ok": True,
        "server_time": observed_at,
    }
    result = {
        "repository": canonical_repository(repository),
        "pull_request_number": pr_number,
        "head_oid": head_oid,
        "reviewer_logins": reviewers,
        "approval_logins": approvers,
        "targeted_unresolved_thread_ids": targeted_ids,
        "non_target_unresolved_threads": non_target,
        "qualifying_approval_reactions": qualifying,
        "qualifying_current_head_approval_reviews": qualifying_reviews,
        "excluded_approval_reviews": excluded_approval_reviews,
        "invalid_reaction_ids": invalid_reaction_ids,
        "approval_status": approval_status,
        "approval_proof": approval_proof,
        "approval_diagnostic": (
            "A qualifying APPROVED review is directly bound to the current head commit."
            if approval_proof == "pull_request_review"
            else "A new reaction node was observed after the current head epoch was established."
            if approval_proof == "reaction_epoch"
            else "Existing PR-level reactions cannot be ordered reliably against the current head."
            if approval_status == "ambiguous_existing_reaction"
            else "No current-head approval evidence is available."
        ),
        "proven_current_head_reaction_ids": sorted(proven_ids),
        "approval_epoch_transition": epoch_transition,
        "cold_start": cold_start,
        "codex_terminal": codex_terminal,
        "batch_publication_event": batch_publication_event,
        "relevant_codex_events": relevant_codex_events,
    }
    return result, next_checkpoint


def freeze_batch(
    checkpoint: dict[str, Any], head_oid: str, targeted_thread_ids: Iterable[str]
) -> dict[str, Any]:
    """Record the immutable target set for one remediation batch."""
    result = deepcopy(checkpoint)
    thread_ids = _stable_unique_ids(targeted_thread_ids)
    existing = result.get("active_batch")
    if isinstance(existing, dict):
        publication_status = (existing.get("publication") or {}).get("status")
        same_batch = (
            existing.get("frozen_head_oid") == head_oid
            and existing.get("targeted_thread_ids") == thread_ids
        )
        if same_batch and publication_status != "succeeded":
            return result
        if publication_status != "succeeded":
            raise ValueError("An unfinished or failed batch must be recovered first")
    result["active_batch"] = {
        "frozen_head_oid": head_oid,
        "targeted_thread_ids": thread_ids,
        "reviewer_logins": list(
            (result.get("latest_target_snapshot") or {}).get("reviewer_logins")
            or DEFAULT_CODEX_LOGINS
        ),
        "thread_outcomes": {},
        "resolved_thread_ids": [],
        "publication": {"status": "not_started"},
    }
    return result


def validate_freeze_request(
    checkpoint: dict[str, Any], head_oid: str, targeted_thread_ids: Iterable[str]
) -> list[str]:
    """Require a freeze to equal the latest stable evaluator target set."""
    requested_ids = _stable_unique_ids(targeted_thread_ids)
    epoch = checkpoint.get("approval_epoch") or {}
    snapshot = checkpoint.get("latest_target_snapshot") or {}
    observed_ids = snapshot.get("targeted_unresolved_thread_ids") or []
    if epoch.get("head_oid") != head_oid or snapshot.get("head_oid") != head_oid:
        raise ValueError("Frozen head does not match the latest checkpoint head")
    if set(observed_ids) != set(requested_ids) or len(observed_ids) != len(requested_ids):
        raise ValueError("Frozen thread IDs do not match the latest targeted snapshot")
    return requested_ids


def record_thread_outcome(
    checkpoint: dict[str, Any],
    *,
    thread_id: str,
    classification: str,
    reference: str | None = None,
) -> dict[str, Any]:
    """Persist a frozen thread's reviewed outcome before exact resolution."""
    if classification not in {"fix-now", "no-fix", "defer"}:
        raise ValueError("Unsupported thread outcome classification")
    result = deepcopy(checkpoint)
    batch = _require_active_batch(result)
    if thread_id not in batch["targeted_thread_ids"]:
        raise ValueError("Thread outcome is not part of the frozen target set")
    outcomes = batch.setdefault("thread_outcomes", {})
    outcomes[thread_id] = {
        "classification": classification,
        "reference": reference,
    }
    return result


def record_resolved_thread(
    checkpoint: dict[str, Any], thread_id: str
) -> dict[str, Any]:
    """Record one exact resolution from the frozen set."""
    result = deepcopy(checkpoint)
    batch = _require_active_batch(result)
    if thread_id not in batch["targeted_thread_ids"]:
        raise ValueError("Resolved thread is not part of the frozen target set")
    if thread_id not in batch.get("thread_outcomes", {}):
        raise ValueError("Record the thread outcome before exact resolution")
    batch["resolved_thread_ids"] = _stable_unique_ids(
        [*batch.get("resolved_thread_ids", []), thread_id]
    )
    return result


def record_publication_failure(
    checkpoint: dict[str, Any],
    *,
    phase: str,
    pending_paths: Iterable[str] = (),
    pending_commit: str | None = None,
) -> dict[str, Any]:
    """Preserve recovery evidence after resolutions but before publication."""
    if phase not in {"validation", "commit", "push"}:
        raise ValueError("Failure phase must be validation, commit, or push")
    result = deepcopy(checkpoint)
    batch = _require_active_batch(result)
    batch["publication"] = {
        "status": "failed",
        "phase": phase,
        "pending_paths": sorted(set(pending_paths)),
        "pending_commit": pending_commit,
    }
    return result


def record_publication_success(
    checkpoint: dict[str, Any], *, published_commit: str | None = None
) -> dict[str, Any]:
    """Mark aggregate validation and any required publication complete."""
    result = deepcopy(checkpoint)
    batch = _require_active_batch(result)
    unresolved = set(batch["targeted_thread_ids"]) - set(
        batch.get("resolved_thread_ids", [])
    )
    if unresolved:
        raise ValueError("Cannot complete publication while frozen threads remain unresolved")
    batch["publication"] = {
        "status": "succeeded",
        "published_commit": published_commit,
    }
    return result


def _stable_unique_ids(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not value:
            raise ValueError("Thread IDs must not be empty")
        if value not in seen:
            result.append(value)
            seen.add(value)
    return result


def _require_active_batch(checkpoint: dict[str, Any]) -> dict[str, Any]:
    batch = checkpoint.get("active_batch")
    if not isinstance(batch, dict):
        raise ValueError("No active frozen batch exists")
    return batch
