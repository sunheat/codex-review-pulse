# Retained legacy 0.3.1 runtime (quarantined evidence)

This directory contains the public 0.3.1 release-candidate lineage, moved here
during the v2 reset. It is historical implementation and incident evidence, not
an active runtime entry point:

- `SKILL.md`, `agents/`, `references/` — the legacy skill surface;
- `scripts/` — frozen-batch, checkpoint, renewable-lease, run-contract,
  preflight, installation, and heartbeat helpers;
- `tests/` — tests for that lineage (not part of the current network-free test
  run).

The v2 architecture is governed by [`../../AGENTS.md`](../../AGENTS.md) and
[ADR 0008](../../docs/adr/0008-v2-runtime-architecture-reset.md). Do not run,
install, or cherry-pick this legacy runtime for new campaigns; its recurring
machinery (successor tasks, renewable leases, checkpoints, heartbeats) is not
part of v2.

The retained baseline commit is `a697032b4103c1cb909324001add8fb8f429f23e`.
Pilot evidence remains under [`../../docs/pilots/`](../../docs/pilots/).
