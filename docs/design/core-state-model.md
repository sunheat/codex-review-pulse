# Core state model

## Scope

The core model answers two narrow questions from an authoritative GitHub
snapshot:

1. Which unresolved threads belong to the configured Codex reviewer?
2. Can a qualifying thumbs-up be proven to approve the current head OID?

It also records enough frozen-batch state to recover when validation or
publication fails after threads have already been resolved. It does not decide
whether a pull request is globally merge-ready.

## Separation of concerns

`scripts/fetch_pr_state.py` performs read-only GitHub retrieval. It passes raw
thread and reaction fixtures to `scripts/state_model.py`, whose transitions are
pure and deterministic. `scripts/checkpoint_store.py` locates the target
repository's Git common directory and atomically replaces the resulting JSON
checkpoint. Connection retrieval is bracketed by metadata reads; if the head
OID changes, the mixed snapshot is discarded before evaluation or persistence.
Tests invoke the evaluator directly without network access.

The default state path is:

```text
<git-common-dir>/codex-review-pulse/<repository-pr-key-hash>.json
```

The checkpoint contains a schema version, canonical case-folded `OWNER/REPO`,
PR number, approval epoch, latest stable targeted snapshot with reviewer
identities, and optional active batch. A checkpoint whose key or schema does
not match the request fails closed. A freeze must match the persisted head and
complete targeted ID set, and carries the normalized reviewer identities into
the batch.

## Reviewer scope

Reviewer logins and approval logins are separate repeatable configuration
sets. Both default to `chatgpt-codex-connector`. Login comparison is
case-insensitive and strips one trailing `[bot]` suffix.

For an unresolved review thread, only the first returned thread comment is the
root-author evidence. A missing comment, missing author, missing login, or
missing thread ID is non-target. The evaluator returns exact targeted IDs and
non-target records separately. A non-target thread does not block the
Codex-specific terminal predicate, but its presence must be reported and must
not be described as global merge readiness. Exact resolution re-queries the PR
and root authors before mutation, including when an expected set is supplied
explicitly. Checkpoint-driven resolution also requires the live PR head to
equal the frozen head and inherits the batch's reviewer identities. An explicit
expected set cannot override an active batch.

## Approval epoch

An approval event is identified by its GitHub reaction node ID, content, and
normalized author. Duplicate identical event IDs collapse deterministically;
conflicting duplicates are excluded and reported.

For each head OID the checkpoint records:

- reaction IDs present when the epoch was first observed (`baseline`);
- reaction IDs present at the last observation; and
- reaction IDs proven to have first appeared during the already-established
  epoch.

On cold start, all existing qualifying reactions become baseline evidence and
the result is ambiguous. When a new head OID is first observed, all reactions
in that same snapshot also become baseline evidence because their ordering
relative to the head change cannot be proven. On a later observation of the
same head, a newly observed qualifying reaction ID becomes proven for that
head. Proven IDs survive restart, remain valid only while still present, and
are discarded when the head changes.

The Codex loop is terminal immediately when the PR is open, targeted unresolved
count is zero, and at least one current reaction ID is proven for the current
head. No quiet interval is added. Ambiguous existing reactions are
non-terminal.

## Frozen batch and recovery

An active batch stores the frozen head OID, ordered de-duplicated targeted
thread IDs, per-thread `fix-now`/`no-fix`/`defer` outcomes, exact resolved IDs,
and publication state. `update_batch_state.py` freezes a batch, records outcomes,
and records aggregate publication success or failure. `resolve_thread.py`
requires an outcome before a checkpoint-driven mutation and records the
verified exact resolution.

If aggregate validation, commit, or push fails after resolutions, the failure
record includes its phase, resolved IDs, pending paths, optional local commit,
and frozen head. This checkpoint is recovery evidence; it is not permission to
retry a mutation or create a second commit. A failed or unfinished batch cannot
be overwritten by a different freeze; it must be recovered first.

## Limits

Atomic replacement prevents torn checkpoint files but does not serialize two
independent writers. The surrounding heartbeat or scheduled runner must keep
one active cycle per PR. A cold start with a pre-existing reaction may remain
ambiguous until GitHub exposes a new reaction event after the epoch is
established; the evaluator deliberately prefers a stalled non-terminal result
to attributing historical approval to a newer head.

See [ADR 0001](../adr/0001-codex-only-thread-scope.md),
[ADR 0002](../adr/0002-current-head-approval.md), and
[ADR 0003](../adr/0003-frozen-batch-order.md).
