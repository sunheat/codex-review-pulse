# Repository Guidance

## Purpose and public truth

Codex Review Pulse is a GitHub-specific Codex skill for safely remediating
recurring review batches created by the GitHub Codex connector. Tracked files
in this repository are the public source of truth.

When `notes/context.md` exists, read it before starting repository work. It is
local working context only: promote durable decisions into tracked
documentation, and never make public behavior depend solely on ignored notes.

## Stable invariants

- Target only unresolved review threads whose root comment author matches a
  configured Codex reviewer identity. Unknown authors fail closed. Report but
  never automatically mutate non-target threads.
- Normalize configured GitHub identities case-insensitively and treat a
  trailing `[bot]` suffix as equivalent.
- Treat terminal approval as a Codex-specific loop result, not a claim that a
  pull request is globally merge-ready.
- A qualifying approval must be proven for the current head OID. Existing
  reactions on cold start or first observation of a new head are ambiguous.
- Freeze the targeted thread IDs and head OID before a batch. Resolve each
  exact frozen thread after its focused local outcome passes, then validate and
  publish the aggregate batch at most once.
- Bracket connection retrieval with head-OID reads and discard a snapshot if
  the head changes. Freeze only the exact targeted set persisted by that stable
  snapshot, and revalidate root-author identity before resolution.
- In that same bracket, treat PR-level `EYES` from a configured Codex identity
  as wait-only review activity. It may delay a batch but never proves approval;
  malformed or conflicting reaction-node evidence fails closed.
- Do not process review artifacts created by a batch push until the next cycle.
- Persist each frozen thread's outcome before resolving it so recovery retains
  both the exact resolved IDs and their classifications.
- Keep runtime checkpoints outside tracked files, under the target
  repository's Git common directory, and replace them atomically.
- Use authoritative GitHub GraphQL review-thread state and exact node IDs. Do
  not infer resolution from comment text or flat comment lists.

These core rules apply to both product modes. The public Codex-first default
does not require the hardened contract, installation, or lease ceremony.

## Default and hardened modes

The public default control surface is
`skills/codex-review-pulse/scripts/pulse.py`. It uses a small PR-scoped
checkpoint with a persisted in-progress marker stored through atomic checkpoint
replacement. The marker is not a cross-process lock or compare-and-set
mechanism; the default path assumes one host/task runner per PR. It must persist
`wake_id`, `wake_phase`, `wake_started_at`, `wake_completed_at`,
`next_not_before`, `scheduled_task_disposition`, and `wake_count`. One host wake
may plan once; duplicate plan/snapshot calls return the prior result or reject
without incrementing the count. A stale marker returns `PAUSE_RECOVERY` and
never auto-takes over.

The initial user turn is wake 1. Heartbeat creation/update stays `PAUSED`.
Each scheduled wake pauses the heartbeat before PR work and stops if that pause
cannot be confirmed. Final `WAIT_REVIEW`, `WAIT_RETRY`, or successful same-head
`REQUEST_REVIEW` may reanchor a next wake at
`wake_completed_at + cadence_seconds`; `STOP_*`, `PAUSE_*`, recovery, closed,
expired, lease-loss, and unknown results remain paused. Pause is absorbing and
the default path has no automatic recovery-latch clearing operation.

The existing immutable installation, pilot preflight, canonical run-contract
digest, renewable lease, doctor/plan/complete protocol, and detailed recovery
operations are optional hardened mode only. If hardened code remains
reachable, it must obey the same P0 lifecycle and fail closed on duplicate
wakes, early fixed-cadence wakes, pause failure, lease loss, and unverified
recovery. Version `0.4.0` has a real black-box pilot failure and is not a
publishable final recurring release.

The default automation policy is persisted in the checkpoint. Its default
profile is autonomous/unattended with automatic PR-scoped edits, stale-test
repair, exact resolution, aggregate publication, review triggering, and
recoverable retries. `max_wakes`, `deadline_at`, and `retry_wake_limit` are
unbounded (`null`) by default; prompt-derived limits are normalized, persisted,
and enforced as `STOP_POLICY_LIMIT`. `validation_failure=repair`,
`allow_test_changes=true`, and `no_progress_limit=3` make ordinary validation
failures recoverable while preserving a pause boundary for repeated no-progress
or explicit policy opt-outs. Prompt policy overrides take precedence over the
default and can select `supervised` or `observe-only`.

The prompt does not grant host capabilities. Unattended execution still
requires the host to provide network access, full workspace access, and a
non-interactive approval policy.

`pulse.py` is the sole control surface for ongoing default-path development.
Treat the hardened controller, including `heartbeat_tick.py`, as a frozen
compatibility layer rather than a second product surface. Do not mirror new
default features or lifecycle behavior into hardened mode. Hardened changes are
limited to:

- shared-core defects that block the default path;
- security, data-integrity, or compatibility fixes; and
- fixes required to keep existing hardened regression coverage operational.

When a shared module changes, validate both modes where relevant, but do not
expand hardened behavior merely to preserve feature parity. Reopening hardened
feature development requires an explicit tracked architecture decision or new
pilot evidence that changes this phase boundary.

## Code Review Rules

- In hardened mode only, persist a canonical digest of the complete normalized
  run contract before the first mutation. Every later wake must recompute and
  match it; authority drift fails closed without rewriting state.
- Accept review work only from a head-OID-bracketed stable GraphQL snapshot
  whose unresolved thread root author is a configured Codex identity. Freeze
  that head and those exact thread IDs, revalidate ownership, and resolve only
  the corresponding exact GraphQL nodes.
- Execute hardened pilot commands only from an independently copied,
  commit-pinned installation whose manifest, inventory, hashes, and resolved
  executing path all verify. The default path does not call the installer or
  preflight.

## Authorization boundaries

The standard autonomous default request authorizes PR-scoped implementation and
test edits, repair of stale PR-scoped expectations, recoverable retries, commit
and push per aggregate batch, exact target-thread resolution, one review
trigger per head, and creation/update/pause/reanchor of one same-task heartbeat
until a Codex-specific stop or hard blocker. Prompt policy can narrow this
scope. It never authorizes issue creation, merge, auto-merge, changing a PR
base, force-push, non-target mutations, or unrelated work. Stage explicit paths.

Tests must not perform live GitHub mutations. Use fixtures and injected GraphQL
callables for mutation-path coverage.

## Validation

For changes to the skill or scripts, run the Skill Creator validator, the full
network-free test suite, Python compilation and CLI help checks, PowerShell AST
parsing, Markdown local-link and fence checks, `git diff --check`, and a final
worktree/staged-path audit. Report focused and complete results separately.

## Current phase boundary

The core model, immutable Windows installation, read-only supervised preflight,
and manually reviewed one- and two-wake live pilots are historical completed
artifacts. The 0.4.0 real black-box pilot failed on scheduled lifecycle and
must not be described as a publishable final release. The active phase is the
Codex-first default vertical slice plus network-free regression coverage; real
Codex scheduled-task/live GitHub integration remains unverified. It does not
approve indefinite unattended operation, infer unknown connector capability,
package a plugin, validate Pi, add generic reviewer/multi-forge support, or
decide integration/reuse/vendor policy for `gh-address-comments`.
