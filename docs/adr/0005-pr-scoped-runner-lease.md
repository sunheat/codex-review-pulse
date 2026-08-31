# ADR 0005: Require a PR-scoped renewable runner lease

- Status: Accepted
- Date: 2026-08-25

## Decision

Every recurring tick must acquire a repository/PR-scoped lease before writing
recurring or core checkpoint state or performing a GitHub mutation. The lease
uses an opaque owner token, explicit acquisition/renewal/expiration times, and
compare-before-renew/release. Lease operations are serialized by an
OS-released advisory file lock. Expiration, not PID liveness, defines staleness.

Preflight and doctor inspect without acquiring, renewing, recovering, or
deleting. A failure or recovery condition is durably latched before the owner
releases the lease.

## Consequences

Only one runner can hold mutation authority for a PR. A crash cannot hold the
lease forever, and a non-owner cannot release it. Lease availability alone is
not retry authorization; checkpoint and recurring recovery latches still
block a subsequent wake.
