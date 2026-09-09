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

## 0.7.1 verified completion-relative re-anchor

Version `0.7.1` requires the host to read back the task's persisted first run
after every successful re-anchor. `complete-wake` activates the checkpoint
only when that timestamp equals the controller's completion-relative
`next_not_before`; an old or malformed `DTSTART` stays paused with explicit
mismatch evidence.

Pushes completed before the next wake continue to coalesce into the latest
stable observed head. Head changes after a batch is frozen continue to pause
recovery, so this release does not weaken the frozen-head publication gate.

## 0.8.0 standalone clean-context scheduling

Version `0.8.0` changes the default host handoff from a same-task heartbeat to
standalone scheduled tasks. Each scheduler delivery is a new task/conversation
using the canonical handoff produced by `pulse.py`. The installed skill and
target repository instructions, including `AGENTS.md` when present, are runtime
instruction/configuration inputs. The Git-common-dir checkpoint is the
canonical default-path lifecycle and control authority. Checkpoint-referenced
immutable repair patches/manifests and verified scheduler task metadata may
persist as auxiliary recovery or evidence artifacts, but they are not
independent workflow authorities.

If a host exposes or uses a native execution plan, it is ephemeral user-facing
telemetry rather than durable workflow authority. This roadmap does not claim
that the current implementation has native plan support. The injected host
orchestration guard proves one fresh wake per invocation,
pause-before-preflight ordering, one successor per rearmable wake, and
immediate termination after `complete-wake`.

The network-free tests do not create real scheduler tasks, install skills, or
mutate GitHub. Real Codex scheduled-task and live GitHub integration remains
unverified.

## 0.8.6 scheduler timestamp quantization

The host-supported scheduler's task metadata is authoritative at whole-second
precision with fractional seconds truncated. The standalone re-anchor host and
controller use that same quantization for creation-anchor and first-run
validation, accepting representation-only sub-second differences while still
rejecting an earlier represented second. Direct completion callbacks retain
their exact completion-relative ceiling behavior.

## 0.8.7 paused-successor activation boundary

Standalone successors are created paused, identified and read back inside the
cleanup boundary, and activated only after their prompt, model, cadence,
creation anchor, and first run are verified. Missing task IDs therefore leave
only harmless paused records, while callback exceptions or malformed results
re-pause every known activated successor before the invocation ends.

## 0.8.8 metadata-preserving task status updates

Codex cron status transitions now require a metadata read followed by a full
persisted task update that changes only status. This prevents the local host
from rejecting pause or activation attempts that omit required cron fields and
leaving a live task paired with a fail-closed checkpoint.

## 0.8.9 durable wake and repair handoffs

The default lifecycle now persists a wake before fallible worktree setup,
authorizes a verified successor before activation, and restores and verifies
pending repair bytes before resolution or publication. This keeps scheduler
delivery, successor identity, and retry recovery durable across process exits.

## 0.8.10 exact predecessor retirement

For a rearmable default wake, the separately verified setup task or the
authenticated delivered task is registered with begin-wake, then advances from
registered to pending to confirmed retirement before successor creation. The
checkpoint distinguishes no current task (`NONE`) from externally unprovable
state (`UNKNOWN`), retains only bounded predecessor evidence, and permits only
handoff recovery after retirement. It never discovers tasks heuristically or
adds scheduler-wide cleanup.

## 0.8.11 lifecycle hardening

Version `0.8.11` publishes default checkpoint schema v4 and standalone
protocol v13. A wake is counted only after successful admission: `wake_count`
does not include attempted invocations, scheduler delivery, pause failures,
rejected or ambiguous provenance, duplicate calls, or completion. The raw
`--pause-confirmed` input is only a host observation and is not admission
authority.

Scheduler mutations require strict pre/post provenance for the exact task ID
and persisted metadata. Every task-creation attempt first records a durable
creation intent, then reconciles that intent with the exact task ID and
post-creation readback. Ambiguous results remain paused; task names, prompts,
ages, and scheduler-wide listings cannot substitute for exact evidence.

Legacy schema-v3 state with a non-zero `wake_count` or an active task chain
fails closed rather than being silently migrated or replayed; explicit fresh
setup is required. The exact-ID retirement contract remains unchanged:
`NONE` is confirmed retirement with no current task, while `UNKNOWN` is
unavailable exact scheduler truth and is not absence.

The portable Python controller documents and enforces safety boundaries once
invoked, but it cannot guarantee a Desktop pre-model scheduler gate. Public
claims are therefore limited to safety and fail-closed behavior, not
unattended liveness or proven continuous unattended Desktop operation. Real
scheduled-task and live GitHub integration remain unverified.

## 0.8.12 strict scheduled-delivery provenance

Version `0.8.12` aligns the public scheduled-wake handoff with the strict
controller path: read the delivered task before pause, submit its full
persisted definition with only status changed, read it back after the pause,
validate exact structured provenance, and pass that provenance to
`begin-wake`. The CLI rejects a confirmed delivered task when the provenance
file is omitted. This remains a fail-closed contract fix; real scheduled-task
and live GitHub integration remain unverified.

## Deferred milestones

- public-API connector and automatic-review detection bound to a head OID;
- independent real scheduled-task integration and long-term unattended
  clean-context evidence;
- broader production notification/pause integration and multi-wake recovery
  history beyond the completed bounded evidence;
- Codex plugin packaging and marketplace distribution;
- Pi portability validation;
- generic reviewer and multi-forge support; and
- evaluation of whether to integrate, reuse, or vendor OpenAI
  `gh-address-comments`.

These remain deferred and are not claims about current functionality.
