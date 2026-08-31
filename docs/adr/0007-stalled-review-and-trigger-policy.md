# ADR 0007: Fail closed on stalled review and connector triggering

- Status: Accepted
- Date: 2026-08-25

## Decision

Connector capability defaults to `unknown`. Public OpenAI documentation
describes `@codex review` and settings-based automatic review, but not a public
API that binds connector installation or automatic-review configuration to a
current head OID. Connector capability is therefore descriptive and never
independently authorizes a trigger.

A deterministic stalled classification requires a current-head publication
or authorized-trigger event with GitHub server time, healthy API/auth evidence,
no later relevant current-head Codex event, enough identical stable
observations, and the configured wait boundary. The standard short recurring
request authorizes `REQUEST_REVIEW` to post the exact `@codex review` comment
once per current-head epoch. Trigger evidence is persisted with the comment
node ID, server creation time, and bracketed heads. After posting, the runner
must wait one full cadence. A following wake with no configured-Codex `EYES`,
targeted thread, or current-head approval pauses as
`review_trigger_did_not_start` and requires user intervention.

## Consequences

The runner may wait longer than necessary when public evidence is incomplete.
It never infers head binding from PR or commit timestamps or repeats a trigger
for the same head after restart. Stalled remains a local control classification,
not an external-service fault claim. The `EYES` lifecycle is operator-verified
behavior rather than a public OpenAI API guarantee, and its presence is used
only to delay mutation.
