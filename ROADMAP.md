# Roadmap

## Completed core model

The core state phase implements Codex-only thread targeting, current-head
approval checkpoints, commit-bound approved-review evidence, frozen-batch
recovery, and PR-scoped exact resolution.

## Controlled live-pilot readiness

The current release candidate adds a clean-commit, independently copied pilot
installation and a read-only readiness command. Its next operational milestone
is one manually supervised pilot with explicit inputs, a single confirmed
runner, and mutation-by-mutation authorization.

This is not an unattended recurring heartbeat release.

## Deferred milestones

- deterministic stalled-review and broader event classification;
- connector detection and server-timestamp review triggering;
- Codex plugin packaging and marketplace distribution;
- Pi portability validation;
- generic reviewer and multi-forge support; and
- evaluation of whether to integrate, reuse, or vendor OpenAI
  `gh-address-comments`.

These remain deferred and are not claims about current functionality.
