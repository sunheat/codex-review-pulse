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
- Do not process review artifacts created by a batch push until the next cycle.
- Persist each frozen thread's outcome before resolving it so recovery retains
  both the exact resolved IDs and their classifications.
- Keep runtime checkpoints outside tracked files, under the target
  repository's Git common directory, and replace them atomically.
- Bind recurring state to a canonical digest of the complete normalized run
  contract. Any cross-wake authority drift fails closed before checkpoint or
  GitHub mutation and must not rewrite state.
- Use authoritative GitHub GraphQL review-thread state and exact node IDs. Do
  not infer resolution from comment text or flat comment lists.

## Code Review Rules

- Persist a canonical digest of the complete normalized run contract before
  the first mutation. Every later wake must recompute and match that digest;
  authority drift fails closed before any checkpoint or GitHub mutation,
  without rewriting state, and releases any held lease.
- Accept review work only from a head-OID-bracketed stable GraphQL snapshot
  whose unresolved thread root author is a configured Codex identity. Freeze
  that head and those exact thread IDs, revalidate ownership, and resolve only
  the corresponding exact GraphQL nodes.
- Execute mutating pilot commands only from an independently copied,
  commit-pinned installation whose manifest, inventory, hashes, and resolved
  executing path all verify. Never fall back to a mutable checkout, a default
  installation, or a symlinked path when that binding fails.

## Authorization boundaries

User authorization is action-specific. Code edits do not imply permission to
commit, push, create or resolve issues, resolve GitHub threads, post trigger
comments, start recurring execution, or merge. Never force-push, enable
auto-merge, change a PR base, or include unrelated work. Stage explicit paths.

Tests must not perform live GitHub mutations. Use fixtures and injected GraphQL
callables for mutation-path coverage.

## Validation

For changes to the skill or scripts, run the Skill Creator validator, the full
network-free test suite, Python compilation and CLI help checks, PowerShell AST
parsing, Markdown local-link and fence checks, `git diff --check`, and a final
worktree/staged-path audit. Report focused and complete results separately.

## Current phase boundary

The core model, immutable Windows installation, read-only supervised preflight,
and manually reviewed one- and two-wake live pilots are complete. Version
`0.3.1` supports repeatable bounded pilots with an immutable cross-wake
authority digest and alternate-install self-location. It does not approve
long-term unattended operation, infer unknown connector capability, package a
plugin, validate Pi, add generic reviewer/multi-forge support, or decide
integration/reuse/vendor policy for `gh-address-comments`.
