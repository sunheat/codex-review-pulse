# ADR 0002: Prove approval within the current head epoch

- Status: Accepted
- Date: 2026-08-25

## Decision

A qualifying PR-level `THUMBS_UP` approves only the head epoch in which its
reaction ID is first proven to appear. The checkpoint is keyed by canonical
repository and PR number and persists the head OID, baseline observed reaction
IDs, current observed IDs, and proven current-head IDs.

Existing reactions at cold start and reactions first seen in the same snapshot
as a head change are ambiguous. They cannot satisfy the terminal predicate.
Changing the head invalidates all prior proof. A later new qualifying reaction
ID on the unchanged head proves approval, and that proof survives restart while
the reaction remains present.

## Consequences

Historical reactions cannot approve newer code. Some real approvals will be
reported as ambiguous when event ordering is unavailable; that false negative
is the intentional fail-closed behavior. Once proof exists and targeted thread
count is zero, termination is immediate without a quiet interval.

ADR 0004 later adds a separate direct proof for an `APPROVED` review whose
associated commit exactly equals the current head. It does not relax this
reaction-epoch decision.
