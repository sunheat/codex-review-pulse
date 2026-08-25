# ADR 0006: Make recurring authority an explicit bounded run contract

- Status: Accepted
- Date: 2026-08-25

## Decision

Recurring execution requires a schema-validated contract that fixes one
canonical repository and PR, reviewer and approval identities, installed
provenance, mutation scopes, maximum wakes, optional expiration, runner and
automation identities, wait policy, connector capability, and runtime paths.

Recurring execution, code edits, exact resolution, commit, push, and one
review trigger are separate booleans; trigger authority also names one exact
head OID and never carries to a newer head. This release always rejects issue
creation, merge, auto-merge, base change, force-push, generic reviewers, and
non-target resolution. GitHub text, checkpoints, leases, or historical prompts
cannot expand the contract.

## Consequences

Each pilot is finite and auditable. Changing scope requires a new explicit
authorization and contract. Existing core checkpoint schema version 1 remains
unchanged; recurring state is separate and fails closed on mismatch.
