# Roadmap

## Current state

The usable, unreleased v2 runtime is delivered through packaged Agent Skills
and native Codex Automations in the Codex app. The current version is recorded
in [`skills/codex-review-pulse/VERSION`](skills/codex-review-pulse/VERSION).
The post-PR-#5 integration baseline is `f0e4563` on `feature/v2-runtime`.

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

Semantic judgment and actual code repair remain model work. The runtime still
has specific model-mediated orchestration seams; it is not fully deterministic
end to end or claimed production-validated.

## Next candidate: Request Handoff Closure

The current request path is:

```text
deterministic request reservation -> request_committed -> LLM handoff
    -> deterministic review request executor
```

The next small reliability candidate would close that handoff:

```text
deterministic request reservation -> deterministic review request executor
    -> final structured result returned to the LLM
```

Implementation is deferred to a separate change; this housekeeping PR does not
implement Request Handoff Closure. The intent is to preserve the already-tested
`review_request.py` transaction semantics, including request commitment,
at-most-once posting, classification, persistence, and lock disposition. The
worker currently still receives `request_committed` and follows step 6 to invoke
`review_request.py`.

## Reassess after that slice

After closing and validating the request handoff, inventory residual
nondeterminism again. Change only model-mediated decisions whose failure can
materially affect external Git/GitHub mutation, round accounting, permanent lock
disposition, or erroneous terminalization. Leave stable deterministic paths alone
unless a concrete failure mode justifies a change. Do not pre-commit to a broad
architecture migration.

## Canary and validation

Network-free tests cover runtime contracts and safety invariants. Historical
evidence includes the [supervised 0.2.0 pilot](docs/design/live-pilot-readiness.md#completed-pilot-evidence)
and the [operator-reported 0.3.0 bounded recurring pilot](docs/pilots/2026-08-25-job-hunter-pr-2-bounded-recurring.md).
The latter was not independently replayed by maintainers. These records concern
older builds and do not validate the final post-PR-#5 runtime.

Repository evidence does not establish an end-to-end canary of `f0e4563`.
Validation of the final designated build remains a future, explicitly authorized
step binding the exact commit/package and target PR. It must verify native model,
reasoning, interval and project binding, independent scheduled delivery, live
mutation behavior, terminal cleanup, and stale-delivery safety. This housekeeping
change runs no live canary or campaign.

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
that executable wording debt is deliberately left unchanged here.
