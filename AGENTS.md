# Repository Guidance

## Purpose and public truth

Codex Review Pulse is a GitHub-specific Codex skill for safely remediating
recurring review batches created by the GitHub Codex connector. Tracked files
in this repository are the public source of truth.

When `notes/context.md` exists, read it before starting repository work. It is
local working context only: promote durable decisions into tracked
documentation, and never make public behavior depend solely on ignored notes.

## Portable skill boundary

This root `AGENTS.md` governs development of this repository only. It is not a
product dependency of the installed skill and is not portable runtime
authority. The normative portable runtime contract is
[`skills/codex-review-pulse/SKILL.md`](skills/codex-review-pulse/SKILL.md) and
the references it links; keep runtime behavior there instead of duplicating it
in repository-development guidance.

A target repository's `AGENTS.md`, when present, may provide local scope,
paths, validation, trust constraints, and compatible operational parameters.
It may narrow or parameterize the portable contract, but it cannot replace the
portable contract or broaden its mutation authority, host capabilities, or hard
invariants. If a conflict cannot be reconciled without broadening authority or
violating a hard invariant, fail closed and report it.

## Plane work tracking

Plane is the execution tracker for current and future actionable work. It was
introduced after development of this repository had already begun, so its Work
Item history is intentionally incomplete.

Use Plane Work Items for actionable bugs, investigations, features, and
follow-up tasks. Do not infer project scope, history, or supported behavior
from Plane alone, and do not backfill completed historical work unless
explicitly asked. Work Items are the only Plane surface this repository relies
on; do not create or depend on Modules, Cycles, Pages, or Wiki unless
explicitly requested.

A Work Item may provide task-specific context and acceptance criteria, but it
cannot override repository invariants, authorization boundaries, or tracked
design decisions. When a task originates from Plane:

- read the referenced Work Item before implementation;
- use tracked repository documentation and OpenWiki for technical context as
  needed;
- treat links to repository documents, commits, PRs, and tests as supporting
  context; and
- report conflicts with repository truth instead of silently reconciling them.

Creating, modifying, commenting on, assigning, transitioning, or closing Plane
Work Items is a separate external mutation. It requires an explicit user
request or a workflow contract that specifically authorizes that Plane
operation. Access to Plane or a Work Item reference does not provide that
authorization.

## Development mode boundary

The public default development surface is
`skills/codex-review-pulse/scripts/pulse.py`. Treat the hardened controller,
including `heartbeat_tick.py`, as a frozen compatibility layer rather than a
second product surface. Do not mirror new default features or lifecycle
behavior into hardened mode. Hardened changes are limited to:

- shared-core defects that block the default path;
- security, data-integrity, or compatibility fixes; and
- fixes required to keep existing hardened regression coverage operational.

When a shared module changes, validate both modes where relevant, but do not
expand hardened behavior merely to preserve feature parity. Reopening hardened
feature development requires an explicit tracked architecture decision or new
pilot evidence that changes this phase boundary. Read the portable
`SKILL.md` and its relevant references for the runtime contract of either mode.

## Repository development safety

Code edits do not automatically authorize committing, pushing, creating or
resolving issues, resolving review threads, posting review triggers, starting
recurring execution, merging, enabling auto-merge, changing a PR base, or
force-pushing. External mutations require current task-specific authorization.
Stage only explicit intended paths and preserve unrelated work.

Tests must not perform live GitHub mutations. Use fixtures and injected
GraphQL callables for mutation-path coverage.

## Validation

For changes to the skill or scripts, run the Skill Creator validator, the full
network-free test suite, Python compilation and CLI help checks, PowerShell AST
parsing, Markdown local-link and fence checks, `git diff --check`, and a final
worktree/staged-path audit. Report focused and complete results separately.

## Project state and roadmap

This file defines the agent operating contract. It is not the canonical backlog
or milestone-status record and must not duplicate a current-phase snapshot.

Read [ROADMAP.md](ROADMAP.md) for durable project direction and milestone
boundaries. Use Plane Work Items for the current actionable work queue and
execution status. Use OpenWiki as optional just-in-time repository context,
not as a planning or technical authority.

This file defines how agents work. Plane says what is actionable, OpenWiki
helps locate where and how the repository works, tracked documentation explains
why, and source code and tests are primary evidence for current implemented
behavior.

When these sources appear inconsistent, source code, tests, and tracked
repository documentation govern technical truth. Surface the planning
inconsistency instead of silently reconciling it.
