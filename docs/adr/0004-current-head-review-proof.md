# ADR 0004: Accept commit-bound approved reviews as current-head proof

- Status: Accepted
- Date: 2026-08-25

## Decision

Keep the existing PR-level reaction epoch unchanged. Repeated observation of a
reaction node, including after an idempotent add attempt, is not a new approval.
Cold-start and head-change reactions remain ambiguous.

Also accept a `PullRequestReview` as current-head approval evidence when all of
the following are true in one stable, head-bracketed snapshot:

- its normalized author is in the configured approval identity set;
- its current state is `APPROVED`;
- it has an associated commit; and
- that commit's full OID exactly equals the PR's current `headRefOid`.

The Codex-specific terminal predicate still additionally requires an open PR
and zero targeted unresolved Codex threads. It remains distinct from global
merge readiness.

## Rationale

GitHub documents the review's `commit` as the commit associated with that
review and `APPROVED` as a review allowing the pull request to merge. This is a
direct object-level relation that PR-level reaction nodes do not provide.
Reaction IDs and timestamps identify the reaction object and creation time,
but do not identify a reviewed head.

## Consequences

A qualifying approved review can prove current-head approval on cold start
without weakening the reaction epoch. Reviews tied to old commits, missing a
commit, dismissed, informational, pending, change-requesting, or authored by
an unconfigured identity do not qualify. Diagnostic output identifies whether
proof came from a commit-bound review or the established reaction epoch.
