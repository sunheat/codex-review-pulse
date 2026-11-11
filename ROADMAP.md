# Roadmap

## Current state

The first public release, `1.0.0`, is being prepared for packaged Agent Skills
and native Codex Automations in the Codex app. The version is recorded in
[`skills/codex-review-pulse/VERSION`](skills/codex-review-pulse/VERSION).
“v2” names the second-generation internal architecture; it is independent of
the public version and the runtime state formats.

Implemented deterministic paths include:

- owned campaign creation/rollover and worker decisions, with correctness-critical
  routing in `campaign_model.decide()` and `owned.run_worker_decision()`;
- the remediation transaction: preparation freezes exact targets and releases
  ownership, the model performs speculative semantic/code work without permanent
  ownership, and one finalizer revalidates authority, commits the round, orders
  external mutations, classifies results, persists bookkeeping, and disposes ownership;
- review-request decision and reservation, including round/per-head allowance
  commitment, followed by a deterministic executor that revalidates the committed
  request, checks final authority, posts at most once, brackets the mutation with
  fresh observations, persists its classified result, and owns lock disposition;
- reaction/review evaluation: PR-level Codex `THUMBS_UP`, commit-bound approved
  reviews, eligible `EYES`, creation/request baselines, temporal eligibility,
  completed reviews, Codex comments, ambiguous temporal evidence, unattributable
  lifecycle reactions, response grace, and terminal decisions, with S2 confirmation
  of observation-derived terminal decisions;
- fail-closed permanent ownership and mutation boundaries, retaining ownership
  when a product mutation's outcome cannot be established.

Semantic judgment and actual code repair remain model work. Native Automation
configuration and lifecycle operations also retain host/model-mediated seams;
the runtime is not fully deterministic end to end.

## Reassess remaining seams

Reassess residual nondeterminism against concrete failure modes. Change only
model-mediated decisions whose failure can materially affect external
Git/GitHub mutation, round accounting, permanent lock disposition, or erroneous
terminalization. Leave stable deterministic paths alone unless a concrete
failure mode justifies a change. Do not pre-commit to a broad architecture
migration.

## Validation

Network-free tests cover runtime contracts and safety invariants; local and CI
checks do not exercise native Codex Automation integration or live GitHub
mutations. A live run requires explicit user authorization. See
[CONTRIBUTING.md](CONTRIBUTING.md) for development validation and
[`AGENTS.md`](AGENTS.md) for the product and canary boundaries.

## Deferred scope

- native Automation lifecycle hardening: creation/configuration verification,
  terminal cleanup, and retained-lock quarantine/pause remain partly host/model-mediated;
  native Automations are already the delivery mechanism, with no repository-side scheduler;
- fork pull-request publication and cross-host/distributed coordination;
- long-term operational notification/pause integrations beyond native controls;
- marketplace distribution and plugin packaging;
- public-API connectors beyond the GitHub CLI/GraphQL evidence surface;
- generic reviewer or multi-forge support.

The retained [`legacy/v0.3.1/`](legacy/v0.3.1/README.md), ADRs 0001–0007,
and historical `docs/design/` materials remain evidence of earlier designs.
[ADR 0008](docs/adr/0008-v2-runtime-architecture-reset.md) and
[`AGENTS.md`](AGENTS.md) govern the v2 architecture. Historical phase names and
pilot records are preserved. The `github_api.py` CLI help still says “Phase 3”;
that executable wording debt is deferred.
