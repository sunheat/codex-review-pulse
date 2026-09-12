# ADR 0007: Fail closed on stalled review and connector triggering

- Status: Superseded by [ADR 0008](0008-v2-runtime-architecture-reset.md)
- Date: 2026-08-25

> Superseded on 2026-09-13 by
> [ADR 0008](0008-v2-runtime-architecture-reset.md) in the v2 architecture
> reset. Retained as historical evidence for the legacy 0.3.1 implementation;
> it is current v2 authority only where restated in `AGENTS.md`, ADR 0008, or a
> later accepted v2 decision.

## Decision

Connector capability defaults to `unknown`. Public OpenAI documentation
describes `@codex review` and settings-based automatic review, but not a public
API that binds connector installation, auto-review configuration, or expected
post-push activity to a current head OID. Unknown capability therefore cannot
be classified stalled and cannot trigger a comment.

A deterministic stalled classification requires a current-head publication
or authorized-trigger event with GitHub server time, healthy API/auth evidence,
no later relevant current-head Codex event, enough identical stable
observations, and the configured wait boundary. This phase returns
`REQUEST_REVIEW` for human confirmation; it does not post a live comment.
Injected evidence for a separately authorized trigger is persisted once per
head, including comment node ID, server creation time, and bracketed heads.

## Consequences

The runner may wait longer than necessary when public evidence is incomplete.
It never labels an unknown connector stalled, infers head binding from PR or
commit timestamps, or repeats a trigger after restart. Stalled remains a local
control classification, not an external-service fault claim.
