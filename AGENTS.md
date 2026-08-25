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
- Use authoritative GitHub GraphQL review-thread state and exact node IDs. Do
  not infer resolution from comment text or flat comment lists.

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

The core state phase covers Codex reviewer targeting, current-head approval
epochs, durable checkpoints, frozen-batch recovery, exact PR-scoped thread
resolution, tests, and their documentation. It does not cover connector
detection, plugin packaging, Pi portability, generic reviewer support, or
reuse/vendor decisions for `gh-address-comments`.
