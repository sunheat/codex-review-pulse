#!/usr/bin/env python3
"""The deterministic remediation subprotocol (Phase 1 control-plane migration).

Once the existing Phase-2-unmigrated campaign routing selects a remediation
candidate, it hands off into this module and every remaining remediation
decision is deterministic:

- ``prepare_remediation_owned`` runs while preparation ownership is still
  held: it freezes the exact prepared batch, captures the campaign-source
  witness, fetches remote Git state, creates the isolated H1 worktree, writes
  the controller-owned work packet, and disposes preparation ownership before
  returning. It consumes no remediation round and performs no external
  product mutation. A successful preparation result means no permanent
  PR-scoped ownership remains held on behalf of the semantic worker, and the
  packet carries no owner token.

- ``finalize_remediation`` is the one model-visible finalizer invocation. It
  acquires ownership internally, validates the preparation artifact and the
  bounded semantic proposal, derives the controller-owned proposal-bound
  tree, performs the campaign-source compare-and-swap, creates the hook-free
  local commit when publication is required, validates the final remote
  head, durably commits the remediation round, executes the fixed external
  mutation order (push, issues, thread resolutions), stops the external
  suffix on the first definitive failure or ambiguity, performs the
  mandatory campaign bookkeeping (including direct remediation exhaustion
  when deterministically applicable), and disposes ownership before
  returning an informational result.

The model owns only semantic judgment, code editing, and speculative
repository validation inside the registered speculative worktree. It never
receives finalization ownership, never infers the remediation phase, never
supplies authoritative tree identity, and never carries receipts between
authoritative operations. If a successful finalizer's stdout is lost, the
authoritative work is already complete: campaign truth is persisted and
ownership is already disposed before the result is printed.

Trust model: the packet and the derived tree are controller-owned. The final
result is informational, not an authoritative receipt.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import sys
from typing import Any, Callable

import admission
import campaign_model as model
import externalize
import gitlocal
import github_api
import storage


PACKET_SCHEMA_VERSION = 1
PROPOSAL_SCHEMA_VERSION = 1
PACKET_KIND = "crp-remediation-packet"
PROPOSAL_KIND = "crp-remediation-proposal"
COMMIT_MESSAGE = "codex review pulse: remediation"

TITLE_LIMIT = 512
BODY_LIMIT = 65536
RATIONALE_LIMIT = 65536

# Conflict-state sentinels that make complete-worktree publication unsafe.
_CONFLICT_SENTINELS = (
    "MERGE_HEAD",
    "CHERRY_PICK_HEAD",
    "REVERT_HEAD",
    "rebase-merge",
    "rebase-apply",
)


class RemediationRefused(RuntimeError):
    """A pre-mutation whole-proposal refusal: no round, no external mutation."""


class RemediationStale(RuntimeError):
    """The prepared authority is stale: the whole proposal is rejected."""


def _utc_now() -> str:
    """Local wall-clock stamp for lock acquisition metadata only.

    Acquisition timestamps are diagnostic; no correctness decision orders on
    them.
    """
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _worktree_name() -> str:
    """Unique speculative worktree name; abandoned paths never collide."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"batch-{stamp}-{secrets.token_hex(2)}"


def _canonical(path: str | Path) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(str(path))))


def _try_release(
    repository: str,
    pr_number: int,
    owner_token: str,
    *,
    repository_path: str | Path,
) -> bool:
    try:
        released = storage.release_lock(
            repository, pr_number, owner_token, repository_path=repository_path
        )
        return released.get("released") is True
    except Exception:  # noqa: BLE001 - release must never be guessed
        return False


# ---------------------------------------------------------------------------
# Deterministic remediation preparation


def prepare_remediation_owned(
    *,
    repository: str,
    pr_number: int,
    owner_token: str,
    snapshot: dict[str, Any],
    directive_threads: list[dict[str, Any]],
    campaign: dict[str, Any],
    repository_path: str | Path = ".",
    fetch_remote: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Prepare one exact remediation work packet while ownership is held.

    Called by the owned-worker decision boundary after it selected a
    remediation candidate. This helper freezes the projected batch snapshot,
    captures the campaign-source witness, fetches remote Git state, creates
    the isolated worktree at the prepared head, and writes the
    controller-owned packet. It performs no GitHub-visible product mutation,
    pushes nothing, consumes no remediation round, and creates no durable
    in-progress phase. Ownership is disposed before any successful return:
    if the model disappears immediately afterwards, the only remaining state
    is disposable speculative local artifacts (plus an abandoned worktree
    that never blocks fresh preparation).

    On failure the ownership is released when possible and a structured
    refusal is returned; when release itself cannot be confirmed the caller
    must fail closed instead of running semantic work.
    """
    canonical = model.canonical_repository(repository)
    try:
        projected = model.project_remediation_batch(snapshot, directive_threads)
        batch_snapshot_path = admission.write_private_snapshot(projected)
        targets = [
            externalize._frozen_thread(projected, thread_id["id"])
            for thread_id in directive_threads
        ]
        if fetch_remote is None:
            def fetch_remote() -> None:
                gitlocal.fetch(
                    repository_path,
                    repository=canonical,
                    pr_number=pr_number,
                    owner_token=owner_token,
                )

        fetch_remote()
        added = gitlocal.add_worktree(
            canonical,
            pr_number,
            snapshot["head_oid"],
            owner_token=owner_token,
            repository_path=repository_path,
            name=_worktree_name(),
        )
        packet = {
            "schema_version": PACKET_SCHEMA_VERSION,
            "kind": PACKET_KIND,
            "campaign_id": campaign["campaign_id"],
            "repository": canonical,
            "pr_number": pr_number,
            "prepared_head": snapshot["head_oid"],
            "head_ref_name": snapshot["head_ref_name"],
            "source_digest": model.campaign_source_digest(campaign),
            "batch_snapshot_path": batch_snapshot_path,
            "worktree_path": added["path"],
            "targets": targets,
        }
        packet_path = admission.write_private_snapshot(packet)
    except Exception as error:  # noqa: BLE001 - clean pre-commitment refusal
        released = _try_release(
            canonical, pr_number, owner_token, repository_path=repository_path
        )
        return {"prepared": False, "released": released, "reason": storage.error_text(error)}

    released = _try_release(
        canonical, pr_number, owner_token, repository_path=repository_path
    )
    if not released:
        return {
            "prepared": False,
            "released": False,
            "reason": "preparation completed but ownership release could not be confirmed",
        }
    return {"prepared": True, "released": True, "packet_path": packet_path, "packet": packet}


# ---------------------------------------------------------------------------
# Packet and proposal validation


def _require_nonempty_str(packet: dict[str, Any], key: str) -> str:
    value = packet.get(key)
    if not isinstance(value, str) or not value:
        raise RemediationRefused(f"preparation packet field {key} is missing or invalid")
    return value


def load_packet(packet_path: str | Path) -> dict[str, Any]:
    """Load and structurally validate the controller-owned preparation packet.

    Missing, malformed, or unsupported packets refuse the whole proposal
    before any ownership is acquired.
    """
    packet = storage.load_json(packet_path)
    if packet is None:
        raise RemediationRefused(
            f"preparation packet does not exist: {packet_path}"
        )
    if packet.get("kind") != PACKET_KIND or packet.get(
        "schema_version"
    ) != PACKET_SCHEMA_VERSION:
        raise RemediationRefused("unsupported preparation packet schema")
    campaign_id = _require_nonempty_str(packet, "campaign_id")
    if not model.CAMPAIN_ID_RE.fullmatch(campaign_id):
        raise RemediationRefused("preparation packet campaign identity is invalid")
    try:
        model.canonical_repository(_require_nonempty_str(packet, "repository"))
    except ValueError as error:
        raise RemediationRefused(
            "preparation packet repository is invalid"
        ) from error
    pr_number = packet.get("pr_number")
    if not isinstance(pr_number, int) or isinstance(pr_number, bool) or pr_number < 1:
        raise RemediationRefused("preparation packet pull request is invalid")
    _require_nonempty_str(packet, "prepared_head")
    _require_nonempty_str(packet, "head_ref_name")
    digest = _require_nonempty_str(packet, "source_digest")
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise RemediationRefused("preparation packet source witness is invalid")
    _require_nonempty_str(packet, "batch_snapshot_path")
    _require_nonempty_str(packet, "worktree_path")
    targets = packet.get("targets")
    if not isinstance(targets, list) or not targets:
        raise RemediationRefused("preparation packet selects no target")
    seen: set[str] = set()
    for target in targets:
        if not isinstance(target, dict):
            raise RemediationRefused("preparation packet target is malformed")
        for key in (
            "id",
            "path",
            "root_comment_id",
            "root_author",
            "body",
            "root_updated_at",
        ):
            if not isinstance(target.get(key), str) or not target.get(key):
                raise RemediationRefused(
                    f"preparation packet target field {key} is missing or invalid"
                )
        if target["id"] in seen:
            raise RemediationRefused("preparation packet selects a duplicate target ID")
        seen.add(target["id"])
    return packet


def _bounded(value: object, limit: int, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RemediationRefused(f"proposal {label} must be a non-empty string")
    if len(value) > limit:
        raise RemediationRefused(f"proposal {label} exceeds the bounded size limit")
    return value


def parse_proposal(proposal_text: str) -> list[dict[str, Any]]:
    """Parse and structurally validate the bounded semantic proposal.

    The proposal is semantic disposition data only: it may never carry
    campaign identity, heads, tree witnesses, ownership values, or any other
    authoritative field. Unknown fields, unknown outcomes, unknown Fix-now
    submodes, unbounded content, and duplicate or missing coverage refuse the
    whole proposal before any external mutation.
    """
    try:
        data = json.loads(proposal_text)
    except json.JSONDecodeError as error:
        raise RemediationRefused(f"proposal is not valid JSON: {error}") from error
    if not isinstance(data, dict):
        raise RemediationRefused("proposal root must be an object")
    if data.get("kind") != PROPOSAL_KIND or data.get(
        "schema_version"
    ) != PROPOSAL_SCHEMA_VERSION:
        raise RemediationRefused("unsupported semantic proposal schema")
    if set(data.keys()) != {"kind", "schema_version", "dispositions"}:
        raise RemediationRefused(
            "proposal must not carry fields beyond kind, schema_version, and "
            "dispositions; authoritative values are controller-owned"
        )
    dispositions = data.get("dispositions")
    if not isinstance(dispositions, list) or not dispositions:
        raise RemediationRefused("proposal contains no dispositions")
    parsed: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in dispositions:
        if not isinstance(item, dict):
            raise RemediationRefused("proposal disposition is malformed")
        thread_id = item.get("thread_id")
        if not isinstance(thread_id, str) or not thread_id:
            raise RemediationRefused("proposal disposition has no target ID")
        if thread_id in seen:
            raise RemediationRefused(
                f"proposal covers target {thread_id} more than once"
            )
        seen.add(thread_id)
        outcome = item.get("outcome")
        if outcome == "fix_now":
            allowed = {"thread_id", "outcome", "mode"}
            if set(item.keys()) != allowed:
                raise RemediationRefused(
                    "Fix-now disposition has unexpected or missing fields"
                )
            if item.get("mode") not in externalize.FIX_NOW_MODES:
                raise RemediationRefused("Fix-now disposition has an unknown submode")
            parsed.append(
                {
                    "thread_id": thread_id,
                    "outcome": "fix_now",
                    "mode": item["mode"],
                }
            )
        elif outcome == "fix_later":
            allowed = {"thread_id", "outcome", "issue_title", "issue_body"}
            if set(item.keys()) != allowed:
                raise RemediationRefused(
                    "Fix-later disposition has unexpected or missing fields"
                )
            parsed.append(
                {
                    "thread_id": thread_id,
                    "outcome": "fix_later",
                    "issue_title": _bounded(
                        item.get("issue_title"), TITLE_LIMIT, "issue title"
                    ),
                    "issue_body": _bounded(
                        item.get("issue_body"), BODY_LIMIT, "issue body"
                    ),
                }
            )
        elif outcome == "no_fix_required":
            allowed = {"thread_id", "outcome", "rationale"}
            if set(item.keys()) != allowed:
                raise RemediationRefused(
                    "No-fix disposition has unexpected or missing fields"
                )
            parsed.append(
                {
                    "thread_id": thread_id,
                    "outcome": "no_fix_required",
                    "rationale": _bounded(
                        item.get("rationale"), RATIONALE_LIMIT, "rationale"
                    ),
                }
            )
        else:
            raise RemediationRefused(f"proposal disposition has an unknown outcome")
    return parsed


def _validate_coverage(
    packet: dict[str, Any], dispositions: list[dict[str, Any]]
) -> None:
    """The proposal must cover the exact prepared target set one-for-one."""
    prepared = [target["id"] for target in packet["targets"]]
    proposed = [item["thread_id"] for item in dispositions]
    if sorted(prepared) != sorted(proposed):
        raise RemediationRefused(
            "proposal coverage does not match the exact prepared target set"
        )


def _validate_worktree(
    packet: dict[str, Any], *, repository_path: str | Path
) -> Path:
    """Validate the registered speculative worktree against the packet.

    The worktree must live under this campaign's v2 state root, be a
    registered worktree of this repository, stand at the prepared head, and
    carry no merge/rebase/cherry-pick conflict state.
    """
    worktree = Path(packet["worktree_path"])
    root = storage.worktree_root(
        packet["repository"],
        packet["pr_number"],
        repository_path=repository_path,
    )
    if not _canonical(worktree).startswith(_canonical(root) + os.sep):
        raise RemediationStale("registered worktree escapes the campaign worktree root")
    if not gitlocal.worktree_is_registered(repository_path, worktree):
        raise RemediationStale("worktree is not a registered worktree of this repository")
    try:
        head = gitlocal.git_text("rev-parse", "HEAD", cwd=worktree)
    except RuntimeError as error:
        raise RemediationStale(
            f"worktree head could not be observed: {storage.error_text(error)}"
        ) from error
    if head != packet["prepared_head"]:
        raise RemediationStale("worktree base does not equal the prepared head")
    for sentinel in _CONFLICT_SENTINELS:
        located = gitlocal.git("rev-parse", "--git-path", sentinel, cwd=worktree)
        if located.returncode != 0:
            continue
        indicator = Path(located.stdout.strip())
        if not indicator.is_absolute():
            indicator = worktree / indicator
        if indicator.exists():
            raise RemediationRefused(
                f"worktree carries unsupported conflict state ({sentinel})"
            )
    return worktree


def _validate_packet_against_batch(
    packet: dict[str, Any], *, canonical: str, pr_number: int
) -> dict[str, Any]:
    """Cross-validate the packet targets against the frozen batch snapshot."""
    try:
        batch = externalize.load_frozen_evidence(packet["batch_snapshot_path"])
    except externalize.FrozenEvidenceError as error:
        raise RemediationRefused(storage.error_text(error)) from error
    if batch.get("repository") != canonical or batch.get("pr_number") != pr_number:
        raise RemediationRefused("frozen batch belongs to a different target")
    if batch.get("head_oid") != packet["prepared_head"]:
        raise RemediationRefused(
            "frozen batch head does not match the prepared head"
        )
    batch_ids = sorted(
        item["id"]
        for item in batch.get("threads", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    )
    packet_ids = sorted(target["id"] for target in packet["targets"])
    if batch_ids != packet_ids:
        raise RemediationRefused(
            "packet target set does not match the frozen batch membership"
        )
    for target in packet["targets"]:
        try:
            frozen = externalize._frozen_thread(batch, target["id"])
        except (
            externalize.FrozenEvidenceError,
            externalize.TargetSelectionError,
        ) as error:
            raise RemediationRefused(storage.error_text(error)) from error
        if frozen != target:
            raise RemediationRefused(
                f"packet identity evidence is inconsistent for {target['id']}"
            )
    return batch


# ---------------------------------------------------------------------------
# Deterministic finalization


def finalize_remediation(
    *,
    repository: str,
    pr_number: int,
    packet_path: str | Path,
    proposal_text: str,
    proposal_origin_path: str | Path | None = None,
    repository_path: str | Path = ".",
    fetch_snapshot: Callable[[], dict[str, Any]] | None = None,
    list_issues: Callable[[], list[dict[str, Any]]] | None = None,
    create_issue: Callable[[str, str], str] | None = None,
    viewer_call: Callable[[], str] | None = None,
    resolve_call: Callable[[str], dict[str, Any]] | None = None,
    remote_head_call: Callable[[str], str] | None = None,
    runner: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """The single deterministic remediation finalization boundary.

    Accepts the bounded semantic proposal for one prepared remediation packet
    and owns the complete authoritative lifecycle: internal ownership
    acquisition, packet and proposal validation, worktree and frozen-evidence
    revalidation, campaign-source compare-and-swap, proposal-bound tree
    derivation, hook-free local commit construction when publication is
    required, final remote-head validation, durable remediation-round
    commitment, the fixed external mutation order, suffix stopping on the
    first definitive failure or ambiguity, mandatory campaign bookkeeping,
    and the ownership disposition. The returned result is informational.

    The model supplies no owner token, no tree witness, no publication path
    selection, and no receipt. Unknown or malformed input refuses cleanly
    before any mutation; ambiguity retains the permanent lock.
    """
    canonical = model.canonical_repository(repository)
    if fetch_snapshot is None:
        fetch_snapshot = lambda: github_api.fetch_snapshot(canonical, pr_number)  # noqa: E731
    if remote_head_call is None:
        remote_head_call = lambda ref: gitlocal.remote_head(repository_path, ref)  # noqa: E731
    if runner is None:
        runner = gitlocal.git

    # Pre-acquisition validation: no ownership exists yet, so a refusal here
    # is reported as not_acquired and mutates nothing.
    try:
        packet = load_packet(packet_path)
        if packet["repository"] != canonical or packet["pr_number"] != pr_number:
            raise RemediationRefused(
                "preparation packet belongs to a different target"
            )
        dispositions = parse_proposal(proposal_text)
        _validate_coverage(packet, dispositions)
    except (RemediationRefused, RemediationStale) as refusal:
        kind = (
            "remediation_refused"
            if isinstance(refusal, RemediationRefused)
            else "remediation_stale"
        )
        return {
            "outcome": kind,
            "reason": str(refusal),
            "round_committed": False,
            "ownership": "not_acquired",
            "scheduler_cleanup_authorized": False,
        }

    acquisition = storage.acquire_worker_lock(
        canonical,
        pr_number,
        campaign_id=packet["campaign_id"],
        acquired_at=_utc_now(),
        repository_path=repository_path,
    )
    if not acquisition.get("acquired"):
        return {
            "outcome": "remediation_refused",
            "reason": (
                "ownership could not be acquired "
                f"({acquisition.get('status')}); no mutation, no round"
            ),
            "round_committed": False,
            "ownership": "not_acquired",
            "scheduler_cleanup_authorized": False,
        }
    token = acquisition["owner_token"]
    committed = False
    stopped: tuple[str, str] | None = None
    results: dict[str, Any] = {
        "publication": None,
        "issues": {},
        "resolutions": {},
    }

    def refuse(kind: str, reason: str) -> dict[str, Any]:
        released = _try_release(
            canonical, pr_number, token, repository_path=repository_path
        )
        if not released:
            return {
                "outcome": "local_fail_closed",
                "reason": reason + " (and release could not be confirmed)",
                "round_committed": committed,
                "ownership": "retained",
                "scheduler_cleanup_authorized": False,
            }
        return {
            "outcome": kind,
            "reason": reason,
            "round_committed": committed,
            "ownership": "released",
            "scheduler_cleanup_authorized": False,
        }

    try:
        # 1. Durable authority and campaign-source CAS.
        metadata = storage.ensure_active_campaign_owner(
            canonical, pr_number, token, repository_path=repository_path
        )
        campaign_path = storage.campaign_path(
            canonical, pr_number, repository_path=repository_path
        )
        campaign = storage.load_json(campaign_path)
        if campaign is None:
            raise RemediationRefused("campaign record does not exist")
        model.validate_campaign(
            campaign, repository=canonical, pull_request_number=pr_number
        )
        if campaign["campaign_id"] != metadata["campaign_id"]:
            raise RemediationRefused(
                "ownership lock belongs to a different campaign"
            )
        if campaign["campaign_id"] != packet["campaign_id"]:
            raise RemediationRefused(
                "preparation packet belongs to a different campaign"
            )
        if campaign["rounds_used"] >= campaign["config"]["max_rounds"]:
            raise RemediationRefused("effective-round budget is exhausted")
        if model.campaign_source_digest(campaign) != packet["source_digest"]:
            raise RemediationStale(
                "campaign source changed since preparation "
                "(campaign-source compare-and-swap failed)"
            )

        # 2. Worktree, batch snapshot, and proposal validation.
        worktree = _validate_worktree(packet, repository_path=repository_path)
        if proposal_origin_path is not None and _canonical(proposal_origin_path).startswith(
            _canonical(worktree) + os.sep
        ):
            raise RemediationRefused(
                "the proposal file must live outside the registered worktree"
            )
        batch = _validate_packet_against_batch(
            packet, canonical=canonical, pr_number=pr_number
        )

        # 3. Whole-proposal staleness against fresh authoritative evidence.
        current = fetch_snapshot()
        try:
            externalize._validate_current_repository(
                current,
                canonical=canonical,
                pr_number=pr_number,
                expected_head=packet["prepared_head"],
            )
            for target in packet["targets"]:
                externalize._validate_applicable_target(current, target)
        except externalize.FrozenEvidenceError as error:
            raise RemediationStale(storage.error_text(error)) from error

        # 4. Controller-owned proposal-bound tree derivation.
        gitlocal.stage_complete_delta(worktree, runner=runner)
        prospective_tree = gitlocal.write_tree(worktree, runner=runner)
        base_tree = gitlocal.head_tree_oid(worktree, runner=runner)
        delta_empty = prospective_tree == base_tree
        publication_required = any(
            item["outcome"] == "fix_now"
            and item["mode"] == externalize.FIX_NOW_PROSPECTIVE
            for item in dispositions
        )
        if publication_required and delta_empty:
            raise RemediationRefused(
                "publication is required but the complete worktree delta is empty; "
                "no empty commit is manufactured"
            )
        if not publication_required and not delta_empty:
            raise RemediationRefused(
                "the worktree holds a non-ignored delta but no disposition "
                "authorizes prospective-tree publication"
            )

        # 5. Hook-free local commit and final remote-head validation.
        local_commit: str | None = None
        if publication_required:
            local_commit = gitlocal.commit_index_hook_free(
                worktree, message=COMMIT_MESSAGE, runner=runner
            )
            committed_tree = gitlocal.head_tree_oid(worktree, runner=runner)
            if committed_tree != prospective_tree:
                raise RemediationRefused(
                    "the created commit does not carry the proposal-bound tree"
                )
            if remote_head_call(packet["head_ref_name"]) != packet["prepared_head"]:
                raise RemediationStale(
                    "final remote-head validation failed: the remote branch head "
                    "no longer equals the prepared head"
                )

        def final_authority_check() -> None:
            storage.ensure_active_campaign_owner(
                canonical, pr_number, token, repository_path=repository_path
            )

        # 6. Durable remediation-round commitment (the commitment point).
        committed_record = storage.apply_campaign_transition_if_current(
            canonical,
            pr_number,
            owner_token=token,
            expected_source=campaign,
            transition=lambda record: model.consume_round(record, kind="remediation"),
            repository_path=repository_path,
        )
        committed = True
        rounds_used_now = committed_record["rounds_used"]
        campaign_now = committed_record

        # 7. Fixed external mutation order: push, issues, resolutions.
        published_head: str | None = None
        if publication_required:
            push = gitlocal.push_publication_commit(
                worktree=worktree,
                branch=packet["head_ref_name"],
                expected_head=packet["prepared_head"],
                local_commit=local_commit,
                repository=canonical,
                pr_number=pr_number,
                owner_token=token,
                repository_path=repository_path,
                runner=runner,
                before_push=final_authority_check,
            )
            results["publication"] = push
            if push.get("published") is True:
                published_head = push["commit"]
            elif push.get("status") == "ambiguous_publication":
                stopped = ("ambiguous", "Fix-now publication is ambiguous")
            else:
                stopped = (
                    "definitive",
                    f"Fix-now publication failed ({push.get('status')})",
                )

        if stopped is None:
            triage_head = published_head or packet["prepared_head"]
            issue_numbers: dict[str, int | str | None] = {}
            for item in dispositions:
                if item["outcome"] != "fix_later":
                    continue
                issue = externalize.ensure_deferred_issue(
                    repository=canonical,
                    pr_number=pr_number,
                    owner_token=token,
                    snapshot_path=packet["batch_snapshot_path"],
                    thread_id=item["thread_id"],
                    triage_head=triage_head,
                    title=item["issue_title"],
                    body=item["issue_body"],
                    campaign_id=campaign_now["campaign_id"],
                    rounds_used=rounds_used_now,
                    repository_path=repository_path,
                    fetch_snapshot=fetch_snapshot,
                    list_issues=list_issues,
                    create_issue=create_issue,
                    viewer_call=viewer_call,
                )
                results["issues"][item["thread_id"]] = issue
                if issue.get("classification") == "confirmed_success":
                    issue_numbers[item["thread_id"]] = issue.get("issue_number")
                else:
                    stopped = (
                        "ambiguous" if issue.get("classification") == "ambiguous"
                        else "definitive",
                        f"deferred issue for {item['thread_id']} did not confirm",
                    )
                    break

        if stopped is None:
            triage_head = published_head or packet["prepared_head"]
            for target in packet["targets"]:
                item = next(
                    d for d in dispositions if d["thread_id"] == target["id"]
                )
                published_commit: str | None = None
                fix_now_mode: str | None = None
                if item["outcome"] == "fix_now":
                    if published_head is not None:
                        fix_now_mode = externalize.FIX_NOW_PROSPECTIVE
                        published_commit = published_head
                    else:
                        fix_now_mode = externalize.FIX_NOW_ALREADY_PRESENT
                resolution = externalize.resolve_review_thread(
                    repository=canonical,
                    pr_number=pr_number,
                    owner_token=token,
                    snapshot_path=packet["batch_snapshot_path"],
                    thread_id=target["id"],
                    expected_head=triage_head,
                    outcome=item["outcome"],
                    campaign_id=campaign_now["campaign_id"],
                    rounds_used=rounds_used_now,
                    published_commit=published_commit,
                    issue_number=(
                        issue_numbers.get(target["id"])
                        if item["outcome"] == "fix_later"
                        else None
                    ),
                    fix_now_mode=fix_now_mode,
                    repository_path=repository_path,
                    fetch_snapshot=fetch_snapshot,
                    remote_head_call=remote_head_call,
                    list_issues=list_issues,
                    viewer_call=viewer_call,
                    resolve_call=resolve_call,
                )
                results["resolutions"][target["id"]] = resolution
                if resolution.get("classification") != "confirmed_success":
                    stopped = (
                        "ambiguous"
                        if resolution.get("classification") == "ambiguous"
                        else "definitive",
                        f"resolution of {target['id']} did not confirm",
                    )
                    break

        # 8. Best-effort speculative cleanup when the disposition is known and
        #    unambiguous. Cleanup failure is residue and never retains
        #    ownership or alters the outcome.
        if stopped is None or stopped[0] != "ambiguous":
            try:
                gitlocal.remove_worktree(
                    worktree,
                    repository=canonical,
                    pr_number=pr_number,
                    owner_token=token,
                    repository_path=repository_path,
                    force=True,
                )
            except Exception:  # noqa: BLE001 - residue, never a failure cause
                results["cleanup"] = "residue"

    except (RemediationRefused, RemediationStale) as refusal:
        kind = (
            "remediation_refused"
            if isinstance(refusal, RemediationRefused)
            else "remediation_stale"
        )
        return refuse(kind, str(refusal))
    except Exception as error:  # noqa: BLE001 - classified below
        if not committed:
            return refuse(
                "remediation_refused",
                "finalization failed before commitment: " + storage.error_text(error),
            )
        return {
            "outcome": "local_fail_closed",
            "reason": (
                "finalization failed after round commitment and the campaign "
                "state is uncertain: " + storage.error_text(error)
            ),
            "round_committed": True,
            "ownership": "retained",
            "scheduler_cleanup_authorized": False,
        }

    printable = {
        "campaign_id": packet["campaign_id"],
        "prepared_head": packet["prepared_head"],
        "prospective_tree": prospective_tree,
        "published_head": published_head,
        "publication": results["publication"],
        "issues": results["issues"],
        "resolutions": results["resolutions"],
        "round_committed": True,
        "rounds_used": rounds_used_now,
    }

    # 9. Ambiguity keeps the existing fail-closed disposition: only known
    #    facts are persisted (the durable round), no terminal transition may
    #    overwrite the ambiguity, and ownership stays retained.
    if stopped is not None and stopped[0] == "ambiguous":
        return {
            **printable,
            "outcome": "remediation_failed_ambiguous",
            "detail": stopped[1],
            "ownership": "retained",
            "scheduler_cleanup_authorized": False,
        }

    # 10. Mandatory bookkeeping: apply the direct deterministic exhaustion
    #     transition when durable state proves it, then dispose ownership.
    import owned  # local import: owned.py hands off into this module

    finalization = owned.finalize_exhaustion_owned(
        repository=canonical,
        pr_number=pr_number,
        owner_token=token,
        repository_path=repository_path,
    )
    if finalization.get("finalized"):
        printable["exhaustion_finalized"] = True
        if finalization.get("ownership") == "released":
            return {
                **printable,
                "outcome": (
                    "remediation_completed"
                    if stopped is None
                    else "remediation_failed_definitive"
                ),
                "detail": stopped[1] if stopped else None,
                "ownership": "released",
                "scheduler_cleanup_authorized": True,
            }
        return {
            **printable,
            "outcome": (
                "remediation_completed"
                if stopped is None
                else "remediation_failed_definitive"
            ),
            "detail": stopped[1] if stopped else None,
            "ownership": "retained",
            "scheduler_cleanup_authorized": False,
            "reason": "exhaustion terminalized but release could not be confirmed",
        }
    if finalization.get("outcome") == "local_fail_closed":
        return {
            **printable,
            "outcome": "local_fail_closed",
            "reason": finalization.get("reason"),
            "ownership": "retained",
            "scheduler_cleanup_authorized": False,
        }

    released = _try_release(
        canonical, pr_number, token, repository_path=repository_path
    )
    return {
        **printable,
        "outcome": (
            "remediation_completed"
            if stopped is None
            else "remediation_failed_definitive"
        ),
        "detail": stopped[1] if stopped else None,
        "exhaustion_finalized": False,
        "ownership": "released" if released else "release_unconfirmed",
        "scheduler_cleanup_authorized": False,
    }


# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Deterministic remediation finalizer: acquire ownership, validate "
            "the prepared packet and semantic proposal, commit the round, "
            "execute the fixed mutation order, and dispose ownership"
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    finalize = subparsers.add_parser(
        "finalize",
        help=(
            "Finalize one prepared remediation packet against one bounded "
            "semantic proposal (submit the proposal through stdin or a file "
            "outside the registered worktree)"
        ),
    )
    finalize.add_argument("--repo", required=True)
    finalize.add_argument("--pr", required=True, type=int)
    finalize.add_argument("--repository-path", default=".")
    finalize.add_argument("--packet", required=True, type=Path)
    finalize.add_argument(
        "--proposal-file",
        required=True,
        help="Path of the semantic proposal JSON file, or - for standard input",
    )
    args = parser.parse_args()

    if str(args.proposal_file) == "-":
        proposal_text = sys.stdin.read()
        origin: Path | None = None
    else:
        origin = Path(args.proposal_file)
        proposal_text = origin.read_text(encoding="utf-8")

    try:
        result = finalize_remediation(
            repository=args.repo,
            pr_number=args.pr,
            packet_path=args.packet,
            proposal_text=proposal_text,
            proposal_origin_path=origin,
            repository_path=args.repository_path,
        )
    except (RemediationRefused, RemediationStale) as refusal:
        result = {
            "outcome": (
                "remediation_refused"
                if isinstance(refusal, RemediationRefused)
                else "remediation_stale"
            ),
            "reason": str(refusal),
            "round_committed": False,
            "ownership": "not_acquired",
            "scheduler_cleanup_authorized": False,
        }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
