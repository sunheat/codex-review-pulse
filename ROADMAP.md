# Roadmap

## v2 first usable implementation (current, unreleased)

The first usable v2 runtime is implemented in this tree:

- skill-driven launcher for one-sentence campaign creation in the Codex app,
  configuring one fixed recurring native Codex Automation;
- scheduled worker path that independently reconstructs authority on every
  delivery;
- repository-associated small campaign record and one permanent PR-scoped
  ownership lock under the Git common directory;
- effective-round accounting, in-memory remediation batches, temporary
  worktrees, one commit/one push publication, deterministic deferred-issue
  identity, and exact re-observed thread resolution;
- applicable 👀 / 👍 observation with stable Codex identity attribution and
  bounded current-head temporal eligibility;
- one automatic `@codex review` per campaign and head, reserved before the
  POST, with response-window superseding, completion-without-approval,
  service-unresponsive, definitive-failure, and ambiguous fail-closed outcomes;
- network-free unit tests for the high-frequency branches and safety
  invariants.

`VERSION` reads `2.0.0`, but no v2 release is published or tagged. Native Codex
Automation creation and scheduled execution in the Codex app await a
separately authorized live canary; until then the host boundary is documented,
not claimed verified.

## Next: authorized Codex-app canary

- exercise the exact installed build (pinned commit/package) end to end on one
  explicitly authorized same-repository pull request;
- verify native model, reasoning level, interval, and project/folder binding are
  actually applied by the automation;
- verify delivery behavior with no inherited conversation or working directory;
- verify terminal best-effort automation cleanup and stale-delivery safety.

## Retained 0.3.1 lineage (historical evidence)

The frozen-batch, renewable-lease, checkpoint, and heartbeat machinery of the
retained 0.3.1 release candidate lives under
[`legacy/v0.3.1/`](legacy/v0.3.1/README.md) as implementation and incident
evidence. ADR 0001 through ADR 0007 and the `docs/design/` materials describe
that lineage; they are superseded as governing architecture by
[ADR 0008](docs/adr/0008-v2-runtime-architecture-reset.md) and
[`AGENTS.md`](AGENTS.md). The two manually supervised 2026 pilots remain
recorded in [`docs/pilots/`](docs/pilots/2026-08-25-job-hunter-pr-2-bounded-recurring.md).

## Deferred scope

- fork pull-request publication;
- cross-host or distributed campaign coordination;
- long-term operational notification/pause integrations beyond native
  automation controls;
- marketplace distribution and plugin packaging;
- public-API connectors beyond the GitHub CLI/GraphQL evidence surface;
- generic reviewer or multi-forge support.

These remain deferred and are not claims about current functionality.
