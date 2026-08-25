# ADR 0001: Limit automatic remediation to Codex root-author threads

- Status: Accepted
- Date: 2026-08-25

## Decision

The remediation batch contains only unresolved review threads whose root
comment author matches a configured Codex reviewer login. Reviewer logins are
configured separately from approval logins, are repeatable, and default to the
GitHub Codex connector identity `chatgpt-codex-connector`. Matching is
case-insensitive and tolerates `[bot]`.

Missing or untrustworthy root-author evidence fails closed. Non-target
unresolved threads are returned separately, reported to the operator, and
never automatically resolved. The terminal predicate describes completion of
the targeted Codex loop only. A stable snapshot persists the complete targeted
set; freezing must match it, and exact resolution revalidates root authors
before mutation.

## Consequences

Human and other automated-review threads cannot be swept into a Codex batch.
The loop may terminate while non-target threads remain, so output must not call
that state globally clean or merge-ready. Supporting additional reviewer types
requires a later explicit design rather than widening this default.
