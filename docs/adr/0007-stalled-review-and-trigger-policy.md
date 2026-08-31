# ADR 0007: Fail closed on stalled review and connector triggering

- Status: Accepted
- Date: 2026-08-25

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
