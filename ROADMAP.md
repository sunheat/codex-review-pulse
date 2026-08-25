# Roadmap

## Completed core model

The core state phase implements Codex-only thread targeting, current-head
approval checkpoints, commit-bound approved-review evidence, frozen-batch
recovery, and PR-scoped exact resolution.

## Controlled live-pilot readiness

The clean-commit independent installation, read-only preflight, and first
manually supervised live pilot are complete. The pilot processed five exact
threads in one frozen batch and one push without widening mutation scope.

## Bounded recurring heartbeat readiness

Version `0.3.0` adds an explicit finite run contract, a real PR-scoped lease,
a pure next-action evaluator, deterministic server-event wait policy, durable
failure latches, and a one-wake plan/complete interface. The next operational
milestone is a manually observed two-to-five-wake recurring pilot on one PR.

This remains a bounded pilot release, not a long-term unattended heartbeat.

## Deferred milestones

- public-API connector and automatic-review detection bound to a head OID;
- long-term unattended heartbeat approval and operational evidence;
- production notification/pause integration and multi-wake recovery history;
- Codex plugin packaging and marketplace distribution;
- Pi portability validation;
- generic reviewer and multi-forge support; and
- evaluation of whether to integrate, reuse, or vendor OpenAI
  `gh-address-comments`.

These remain deferred and are not claims about current functionality.
