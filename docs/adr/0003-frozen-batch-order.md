# ADR 0003: Resolve the frozen batch before aggregate publication

- Status: Superseded by [ADR 0008](0008-v2-runtime-architecture-reset.md)
- Date: 2026-08-25

> Superseded on 2026-09-13 by
> [ADR 0008](0008-v2-runtime-architecture-reset.md) in the v2 architecture
> reset. Retained as historical evidence for the legacy 0.3.1 implementation;
> it is current v2 authority only where restated in `AGENTS.md`, ADR 0008, or a
> later accepted v2 decision.

## Decision

Each cycle freezes the targeted Codex thread IDs and head OID. For each frozen
thread, the agent implements and focused-validates the smallest repair, or
records its no-fix/deferred classification. It persists that outcome, then
resolves that exact thread. It does not commit or push between threads.

Only after every frozen thread is resolved does the agent run aggregate
validation. If files changed, it performs both remote-head advancement checks,
then creates at most one commit and pushes at most once. Resolution is not
moved after publication.

Artifacts created by the batch push belong to the next cycle. If validation,
commit, or push fails after resolution, the agent pauses and preserves the
frozen head, exact resolved IDs, pending changes or commit, and failure phase in
the recovery checkpoint.

## Consequences

GitHub thread resolution and Git publication are not one atomic transaction.
The durable recovery record makes partial completion explicit and prevents an
unreported second publication attempt. This order deliberately accepts that
resolved threads may temporarily precede their aggregate commit.
