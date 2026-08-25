# Roadmap

## Completed core model

The core state phase implements Codex-only thread targeting, current-head
approval checkpoints, commit-bound approved-review evidence, frozen-batch
recovery, and PR-scoped exact resolution.

## Controlled live-pilot readiness

The clean-commit independent installation, read-only preflight, and first
manually supervised live pilot are complete. The pilot processed five exact
threads in one frozen batch and one push without widening mutation scope.

## Bounded recurring pilot evidence and release hardening

Version `0.3.1` records a successful two-wake live pilot, makes alternate
commit-pinned installations self-verifying in preflight, and binds recurring
state to a canonical digest of the complete normalized run contract. Contract
changes after wake one now fail closed without state rewrite or retained lease.

This supports repeatable, manually reviewed bounded pilots. It remains a
bounded pilot release, not a long-term unattended heartbeat.

## Deferred milestones

- public-API connector and automatic-review detection bound to a head OID;
- long-term unattended heartbeat approval and operational evidence;
- broader production notification/pause integration and multi-wake recovery
  history beyond the completed bounded evidence;
- Codex plugin packaging and marketplace distribution;
- Pi portability validation;
- generic reviewer and multi-forge support; and
- evaluation of whether to integrate, reuse, or vendor OpenAI
  `gh-address-comments`.

These remain deferred and are not claims about current functionality.
