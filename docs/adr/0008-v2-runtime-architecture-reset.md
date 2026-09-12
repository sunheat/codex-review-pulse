# ADR 0008: Reset the runtime architecture for v2

- Status: Accepted
- Date: 2026-09-13

## Decision

The v2 runtime is a greenfield redesign. [`AGENTS.md`](../../AGENTS.md) is the
canonical current repository contract; this ADR records only the reset and does
not restate that contract.

The reset starts from the retained public 0.3.1 `main` baseline
`a697032b4103c1cb909324001add8fb8f429f23e`. The executable 0.3.1
implementation remains in the repository as the retained legacy runtime and
domain-logic reference. PR #2 and PR #3 are historical and incident evidence
only: neither their architecture nor their runtime commits are inherited or
cherry-picked into v2.

The v2 architecture is accepted, but Phase 0 does not implement it. Phase 0 is
a contract, documentation, and skill-discovery reset; the Phase 0 tree is not a
runnable v2 release. Normal repository development must not implicitly invoke
the product against live pull requests. An explicitly authorized later legacy
or v2 canary remains possible only when it binds the exact intended immutable
artifact and receives explicit mutation authorization; discovery metadata never
authorizes a live run.

ADR 0001 through ADR 0007 are superseded together as the governing
architectural decision set for the retained 0.3.1 implementation. They remain
historical records. A principle from them, such as Codex-only targeting,
current-head evidence, or exact thread identity, becomes current v2 authority
only when restated in `AGENTS.md`, this ADR, or a later accepted v2 decision.

## Consequences

- Material describing frozen-batch transactions, renewable leases, recovery
  checkpoints, heartbeat recurrence, or run-contract authority digests
  describes the retained legacy 0.3.1 implementation unless restated as v2
  authority.
- Legacy 0.3.1 runtime and design material stays available as implementation
  and incident evidence but must not masquerade as current v2 architecture or
  implemented v2 behavior.
- v2 runtime work begins later from the small-state design in `AGENTS.md`, not
  from legacy recovery, lease, or scheduler machinery.
