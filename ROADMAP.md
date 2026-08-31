# Roadmap

## Completed core model

The core state phase implements Codex-only thread targeting, current-head
approval checkpoints, commit-bound approved-review evidence, frozen-batch
recovery, and PR-scoped exact resolution.

## Controlled live-pilot readiness

The clean-commit independent installation, read-only preflight, and first
manually supervised live pilot are complete. The pilot processed five exact
threads in one frozen batch and one push without widening mutation scope.

## Historical bounded pilot and hardened machinery

Version `0.3.1` records a successful two-wake supervised pilot and retains the
immutable installation, preflight, authority digest, and renewable-lease
machinery as an optional hardened mode. Hardened mode is now a frozen
compatibility layer: maintenance is limited to default-blocking shared-core
defects, security/data-integrity/compatibility fixes, and repairs required by
its existing regression coverage. Default features are not mirrored into
`heartbeat_tick.py`. That evidence is historical and does not establish
unattended production readiness. Reopening hardened feature development
requires an explicit tracked architecture decision or new pilot evidence.

## 0.4.0 black-box failure and Codex-first default

The real 0.4.0 black-box pilot failed before it could be a publishable final
release. Evidence included early heartbeat activation, fixed-cadence overlap
during a 26-minute wake, expiry of the 300-second lease during validation,
duplicate planning on one host wake, and continued publication after
`PAUSE_BLOCKED` through generated recovery authorization. The old scheduled-task
semantics are retired from the default product and must not be described as
release-ready.

Version `0.5.0` made `scripts/pulse.py` the Codex-first default black-box
candidate. It preserves Codex-only targeting, stable head snapshots, frozen
batches, exact resolution, one aggregate publication, and unrelated-work
protection while keeping the hardened machinery opt-in. Its lifecycle is
paused-before-work, one wake/one plan, completion-relative cadence, and
absorbing pause/stop outcomes.

## 0.6.0 autonomous default policy

Version `0.6.0` keeps `pulse.py` as the sole default development surface and
adds a persisted, prompt-overridable automation policy. The default profile is
unattended and unbounded: it repairs PR-scoped implementation and stale test
expectations, retries recoverable validation/external failures, resumes the
same frozen batch on the next completion-relative wake, publishes and resolves
automatically, and stops only for Codex-specific completion, explicit policy
limits, no-progress, or hard safety blockers. `supervised` and `observe-only`
profiles remain available, and host permissions remain an independent
capability boundary.

The adjacent `job-hunter#3` forward test exposed two stale exact-error
assertions after the first successful wake. The prior default stopped at those
focused failures; 0.6.0 records the failure as repairable and is intended to
resume the frozen batch so the agent can update the PR-scoped expectations,
rerun validation, and continue. Live scheduled-task and GitHub integration are
still pending this new forward test.

## 0.7.0 canonical host handoff and publication gate

Version `0.7.0` keeps the one-sentence autonomous request as the public entry
point while making the scheduled-wake handoff deterministic. The loaded
user-directory installation renders one target-bound heartbeat prompt; later
wakes must preserve it unchanged and must execute that installed controller,
not a similarly named script in the target repository.

For a frozen batch, the default controller now issues positive publication
authority only after every exact thread resolution is recorded and the remote
PR head still equals the frozen head. The host must obtain that authority
before commit and again immediately before push. A premature publication
attempt or head change pauses recovery instead of allowing the host to record a
successful batch after reordering resolution and publication.

## Deferred milestones

- public-API connector and automatic-review detection bound to a head OID;
- independent real scheduled-task integration and long-term unattended
  heartbeat evidence;
- broader production notification/pause integration and multi-wake recovery
  history beyond the completed bounded evidence;
- Codex plugin packaging and marketplace distribution;
- Pi portability validation;
- generic reviewer and multi-forge support; and
- evaluation of whether to integrate, reuse, or vendor OpenAI
  `gh-address-comments`.

These remain deferred and are not claims about current functionality.
