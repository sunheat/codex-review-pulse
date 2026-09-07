---
name: codex-review-pulse
description: Run a Codex-first, PR-scoped review remediation loop with stable snapshots, frozen Codex batches, exact thread resolution, one aggregate publication, and safe completion-relative wakes. Use for GitHub pull requests reviewed by Codex; generic reviewers and other forges are unsupported.
---

# Codex Review Pulse

Use the Codex-first default path for requests such as:

```text
Automatically fix this pull request's Codex review issues until no new issues appear.
```

The default path is the public product surface. It identifies the current PR
when the checkout identifies one, asks one short question only when the target
is ambiguous, and runs one recoverable wake at a time. Its default policy is
aggressively autonomous and unbounded: it may repair PR-scoped code and tests,
retry recoverable failures, publish the aggregate batch, resolve exact target
threads, and schedule a new standalone task/conversation until a Codex-specific stop condition or
an explicit safety boundary is reached. It does not require a run contract,
observation JSON, doctor, pilot preflight, immutable installation, runner
identity, authority digest, owner token, or renewable lease.

Version `0.4.0` has a real black-box pilot failure. It is not a publishable
final recurring release. The failure evidence is preserved in the repository:
the old heartbeat activated too early, used fixed-cadence overlap, allowed a
300-second lease to expire during a long wake, double-planned one host wake,
and continued after `PAUSE_BLOCKED`. Do not describe `0.4.0` as production
ready or use its old scheduled-task protocol as the default.

Version `0.8.9` is the Codex-first default clean-context scheduling candidate. Its
real scheduled-task and live GitHub integration remains unverified until an
independent forward test completes; do not describe that integration as proven
before then.

## Default control surface

Use `scripts/pulse.py` adjacent to this loaded `SKILL.md` as the single main
entry point. In an installed skill, invoke that installed copy by its resolved
path; never substitute a similarly named script from the target repository.

```text
<loaded-skill-directory>/scripts/pulse.py
```

Its small subcommands are `standalone-task-prompt`, `heartbeat-prompt` (legacy
alias), `begin-wake`, `snapshot`,
`freeze`, `record`, `resolve`, `retry`, `trigger-result`, `confirm-policy`,
`prepare-publication`, `publication-result`, `configure-policy`, and
`restore-repair`, `authorize-successor`, `complete-wake`, and
`reconcile-successor`.
`snapshot` returns an agent-facing normalized object with top-level
`head_oid`, PR state, targeted and non-target threads, Codex review activity,
approval evidence, review-epoch state, and head-bracketing server evidence.
Do not manually translate `fetch_pr_state.py` output into another observation
schema.

The CLI resolves `OWNER/REPO` and the PR number before calculating the default
checkpoint path. From a checkout whose current branch identifies one PR, the
default commands do not need `--repo` or `--pr`; explicit values still take
priority. A missing or ambiguous current PR stops with an actionable error
instead of guessing. The checkpoint remains under the target repository's Git
common directory. After initialization, later commands use the checkpoint's
bound repository and PR and reject any explicit target that differs.

## Default automation policy

The first user request is translated into a normalized policy and persisted in
the PR-scoped checkpoint. Unless the request says otherwise, the policy is:

- `profile=autonomous`, `execution_mode=unattended`;
- `cadence_seconds=600`;
- `max_wakes=null`, `deadline_at=null`, and `retry_wake_limit=null` (no
  artificial wake, time, or retry budget);
- `validation_failure=repair` and `allow_test_changes=true`;
- `publication=auto`, `thread_resolution=auto`, and `review_trigger=auto`;
- `inline_retry_limit=3`, `no_progress_limit=3`, and
  `notifications=blockers-and-terminal`.
- `model=gpt-5.6-luna` and `reasoning_effort=xhigh`.

The policy is a control contract, not a request to bypass the hard invariants
below. Codex-only targeting, current-head proof, frozen exact thread IDs,
explicit-path staging, one aggregate publication per batch, no force-push or
merge, and pause-on-uncertainty always remain in force. `null` limits are
deliberately unbounded; if a user supplies a wake count or deadline, it is
persisted and enforced at the next wake.

`pulse.py` enforces lifecycle-level limits and mutation gates; the executing
agent applies `execution_mode`, `allow_test_changes`, `inline_retry_limit`, and
`notifications` while choosing local edits, validation, and reporting.

Prompt instructions override these defaults. The host agent must convert
explicit model, reasoning, and wake-limit settings from the user's initial
prompt to the corresponding JSON fields before rendering the initial handoff
and before the initial `begin-wake`, for example:

```json
{"max_wakes": 5, "deadline_at": "2026-08-27T10:00:00+10:00"}
```

`--policy-json` accepts the same object on the initial wake. Outside an active
wake, `configure-policy --policy-json '{...}'` updates the persisted policy;
policy changes are never made halfway through a frozen batch. Useful explicit
profiles are `supervised` (confirm publication, resolution, and triggers) and
`observe-only` (no PR mutations). A prompt such as “keep working unattended,
update stale PR-scoped tests when the implementation is correct, and retry
transient failures until the review is clean” selects the default autonomous
profile and needs no extra flags. When a supervised operation pauses a frozen
batch, `confirm-policy --operation` records only that exact continuation
authority. The next fresh wake resumes the frozen batch; a generic latch
clearing or policy rewrite cannot resume it. A `never` operation is an
absorbing opt-out and cannot be resumed through `confirm-policy`.

### Task model configuration

The default path persists the scheduled task model in the same normalized
`automation_policy` as the lifecycle policy. The default is deliberately
`gpt-5.6-luna` with `reasoning_effort=xhigh`; the model identifier is passed
through to the host and is not restricted to a repository-side allowlist.
The host remains responsible for rejecting a model or reasoning level that it
does not support.

Set these fields in the initial handoff or update them outside an active wake:

```json
{"model":"gpt-5.6-luna","reasoning_effort":"xhigh"}
```

For the CLI, pass that object through `--policy-json` to
`standalone-task-prompt`, `begin-wake`, or `configure-policy`. The rendered
standalone handoff includes `model` and `reasoning_effort`; the host maps them
to its task-creation fields (`model` and `reasoningEffort`). Every successor
must receive the persisted values and must pass them back in normalized task
readback. A `configure-policy` change applies to later successor tasks; it
does not mutate a task that has already been created.

The program does not guess model settings from arbitrary prose. A user can
write, for example, `use model gpt-5.6-terra with reasoning_effort medium` in
the initial request; the host agent converts that explicit request to
`--policy-json`. The generated canonical prompt intentionally does not embed
these mutable values; the persisted policy and task metadata are authoritative,
and every successor reuses that persisted configuration.

The host adapter may pass `--pause-confirmed` to `begin-wake` and
`--schedule-reanchored` to `complete-wake` only after successful host-tool
calls. The supported cadence-only successor path creates the task paused,
verifies its ID and metadata, then confirms activation. The latter flag must
include
`--scheduled-created-at` from the persisted successor record,
`--scheduled-first-run` derived from that creation anchor plus the persisted
cadence, and `--scheduled-task-id`. These flags do not pause or schedule a
Codex task themselves; a success boolean is never evidence that the host
operation or completion-relative successor handoff succeeded.

### Codex task status updates

For a Codex cron task, a pause or activation must preserve the task's complete
persisted definition. Read its task metadata first, then submit a full update
containing its kind, name, prompt, recurrence, model, reasoning effort, project,
execution environment, and destination, changing only `status`. Do not submit
a status-only update: the local host rejects it before the pause can be
confirmed. Reading task metadata is not a scheduler mutation; the full pause
update remains the first scheduler mutation of a delivered wake.

### Host invocation boundary

A host invocation is the unit of execution and may consume at most one pulse
wake. Wake 1 is created only by the user's initial explicit request. Every
later wake requires a genuinely new host invocation delivered by the scheduler;
a tool result, todo rollover, notebook entry, model decision, or already
registered future task is not a new wake and must not start one.

When a genuinely new host invocation arrives, generate one fresh opaque
`wake_id` from the host/runtime. Do not copy or derive it from the checkpoint,
logs, task description, notebook, todo state, or a failed attempt. Reuse that
ID only for commands in this invocation's wake; the next scheduler-delivered
invocation must generate another one.

For every scheduled invocation, after the initial task-pause operation and
before calling `begin-wake`, read the checkpoint directly and inspect the
actual result. A non-empty `active_wake_id`, a non-empty `failure_latch`, or a
`next_not_before` later than the current time is a stop condition. A missing or
unreadable checkpoint on a scheduled invocation is also a stop condition. Do
not create a replacement checkpoint or use memory, a task summary, or natural
language to override any of these results. Stop the invocation before
`begin-wake`, report the corresponding pause/recovery outcome, and leave the
task paused.

### Default CLI/host sequence

The host owns the scheduled-task operations; `pulse.py` only records their
confirmed results. The reusable ordering guard in
`scripts/standalone_orchestration.py` accepts those host operations through an
injected adapter; it does not call a scheduler or Codex API itself. The
following is one complete host invocation. The marker
`END_INVOCATION` means return the final report immediately; it is not a prompt
to continue with another scheduler or pulse operation.

```text
PULSE = "python <loaded-skill-directory>/scripts/pulse.py"
CONFIGURED_CHECKOUT = host project checkout, used only as a read-only locator
TARGET = "--repository-path WAKE_WORKTREE --repo OWNER/REPO --pr NUMBER"
CHECKPOINT_TARGET = (
    "--repository-path CONFIGURED_CHECKOUT --repo OWNER/REPO --pr NUMBER"
)

# On the initial user turn, first convert explicit settings from the user's
# request to POLICY_JSON, then render the target-bound standalone-task prompt
# with that policy and
# pass its `prompt` field unchanged when creating the standalone scheduler task.
# Do not paraphrase or reorder its batch protocol.
STANDALONE_HANDOFF = PULSE CHECKPOINT_TARGET --policy-json POLICY_JSON standalone-task-prompt
TASK_MODEL = STANDALONE_HANDOFF.model
TASK_REASONING_EFFORT = STANDALONE_HANDOFF.reasoning_effort

# A genuinely delivered invocation gets one fresh ID. Never derive it from
# checkpoint, logs, task text, notebook, todo state, or an earlier attempt.
WAKE_ID = host.new_opaque_wake_id()

if this is the initial explicit user request:
    host.create_standalone_task(
        kind=cron, conversation=standalone, target_thread_id=absent,
        prompt=STANDALONE_HANDOFF.prompt,
        model=TASK_MODEL, reasoning_effort=TASK_REASONING_EFFORT,
        disposition=PAUSED
    ) -> success
    retain the exact setup-task ID and verify its persisted definition. Before
    creating a successor, delete that exact paused setup task and require
    confirmed deletion; if deletion cannot be confirmed, keep it paused,
    persist a cleanup blocker, and end without creating another task.
    # Wake 1 may initialize an absent checkpoint.
else if this is a scheduler-delivered invocation:
    # This must be the first scheduler operation in this invocation. The
    # delivered task is distinct from every earlier and later task.
    DELIVERED_TASK = host.read_task_definition(delivered_task_id)
    pause_result = host.update_task(DELIVERED_TASK, status=PAUSED)
    if pause_result is not confirmed:
        PULSE CHECKPOINT_TARGET --wake-id WAKE_ID begin-wake \
          --delivered-task-id DELIVERED_TASK_ID
        report the returned pause result
        END_INVOCATION
    checkpoint = host.read_checkpoint_directly()
    if checkpoint is missing or unreadable:
        report PAUSE_RECOVERY / checkpoint_unavailable
        END_INVOCATION
    if checkpoint.active_wake_id is non-empty:
        report PAUSE_RECOVERY / incomplete_wake
        END_INVOCATION
    if checkpoint.failure_latch is non-empty:
        report PAUSE_RECOVERY / failure_latched
        END_INVOCATION
    preflight = host.scheduled_preflight(
        checkpoint, delivered_task_id, current time
    )
    if preflight is not ready:
        # This persists identity, disposition, malformed-state, and early
        # cadence failures before ending the invocation. It uses the existing
        # configured checkout because WAKE_WORKTREE does not exist yet.
        PULSE CHECKPOINT_TARGET --wake-id WAKE_ID begin-wake \
          --delivered-task-id DELIVERED_TASK_ID
        report the persisted result
        END_INVOCATION
else:
    report PAUSE_RECOVERY / invocation_not_scheduler_delivered
    END_INVOCATION

# Persist the wake before fallible remote/worktree setup. This writes only
# Git-common-dir state, not configured checkout files.
if this is the initial explicit user request:
    begin_result = PULSE CHECKPOINT_TARGET --wake-id WAKE_ID --policy-json POLICY_JSON begin-wake \
      --pause-confirmed
else:
    begin_result = PULSE CHECKPOINT_TARGET --wake-id WAKE_ID begin-wake \
      --pause-confirmed --delivered-task-id DELIVERED_TASK_ID
require begin_result.next_action == WAKE_STARTED or END_INVOCATION

# After the scheduled pause/checkpoint preflight above (or initial task
# creation), independently read the authoritative remote PR head. Create a new
# clean linked worktree at that exact commit for this wake. Never reuse an old
# wake worktree or switch/reset/clean/modify CONFIGURED_CHECKOUT. All remaining
# pulse commands, repository edits, validation, commit, and push run with
# WAKE_WORKTREE as --repository-path. Linked worktrees share the repository's
# Git-common-dir checkpoint without sharing checkout files.
REMOTE_PR_HEAD = host.read_authoritative_pr_head()
WAKE_WORKTREE = host.create_clean_linked_worktree(
    repository=CONFIGURED_CHECKOUT, commit=REMOTE_PR_HEAD, unique_per_wake=true
)

# Before END_INVOCATION, verify this fresh task-owned worktree is clean, remove
# it, and prune its administrative entry. Never remove CONFIGURED_CHECKOUT. If
# cleanup cannot be confirmed, keep the next task paused and report the blocker.

# A resumed batch must restore the verified patch into this worktree.
if begin_result.resume_pending_batch and checkpoint.active_batch.pending_repair:
    PULSE TARGET --wake-id WAKE_ID restore-repair
    run focused validation of the restored repair

snapshot = PULSE TARGET --wake-id WAKE_ID snapshot

if snapshot.decision.next_action == RUN_BATCH:
    require git -C WAKE_WORKTREE rev-parse HEAD == snapshot.head_oid
    # The freeze command repeats this guard and persists a recovery pause on
    # mismatch, before any outcome, edit, or exact resolution is allowed.

if snapshot.decision.next_action == RUN_BATCH:
    # Freeze the exact stable snapshot before processing any thread.
    PULSE TARGET --wake-id WAKE_ID freeze
    for each frozen thread:
        PULSE TARGET --wake-id WAKE_ID record --thread-id ID \
          --classification fix-now|no-fix|defer|ambiguous
        # Apply and focused-validate a fix when classification is fix-now.
        # In autonomous mode, repair stale PR-scoped tests or implementation
        # defects when the behavior contract is correct. If a recoverable
        # failure remains, persist it and end this wake:
        PULSE TARGET --wake-id WAKE_ID retry \
          --reason-code validation_retry --signature FAILURE_SIGNATURE \
          [--pending-repair PENDING_REPAIR_MANIFEST]
        # If any fix-now work is uncommitted, PENDING_REPAIR_MANIFEST must
        # identify an immutable Git-common-dir patch, its SHA-256, and the
        # frozen head. The next clean worktree verifies and applies it before
        # focused validation.
        # Otherwise, after the focused check passes:
        PULSE TARGET --wake-id WAKE_ID resolve --thread-id ID
    # Only after every exact resolution succeeds in this wake, run aggregate
    # validation and obtain a same-head positive authorization before commit.
    PULSE TARGET --wake-id WAKE_ID prepare-publication
    explicitly stage intended paths and create at most one aggregate commit
    # Re-run the same authoritative gate immediately before push.
    PULSE TARGET --wake-id WAKE_ID prepare-publication
    push at most once and verify the remote PR head equals the pushed commit
    PULSE TARGET --wake-id WAKE_ID publication-result --status succeeded \
      --published-commit PUSHED_COMMIT

if snapshot.decision.next_action == REQUEST_REVIEW:
    # Perform one authorized, same-head bracketed trigger and write its evidence.
    PULSE TARGET --wake-id WAKE_ID trigger-result --evidence trigger.json

if snapshot.decision.next_action is PAUSE_* or STOP_*:
    leave the host task PAUSED
    report the final result
    END_INVOCATION

# A scheduled invocation is a fresh process: none of the initial-wake
# handoff variables survive from an earlier task. Before creating a successor,
# reload the handoff from the persisted policy without policy overrides, then
# bind its canonical prompt to the task that delivered this wake. A policy
# change may intentionally update the successor's model or reasoning settings,
# but a prompt or prompt-digest mismatch is unverified and must pause.
if this is a scheduler-delivered invocation:
    PERSISTED_HANDOFF = PULSE TARGET standalone-task-prompt
    require PERSISTED_HANDOFF.prompt == DELIVERED_TASK.prompt
    require PERSISTED_HANDOFF.prompt_sha256 == sha256(DELIVERED_TASK.prompt)
    STANDALONE_HANDOFF = PERSISTED_HANDOFF
    TASK_MODEL = STANDALONE_HANDOFF.model
    TASK_REASONING_EFFORT = STANDALONE_HANDOFF.reasoning_effort

# WAIT_REVIEW, WAIT_RETRY, or a successfully recorded same-head REQUEST_REVIEW
# may re-anchor. WAIT_RETRY resumes the same frozen batch on the next wake.
# Cleanup is part of the completion boundary. It must finish before taking the
# host clock anchor and before creating the next task. The host accepts a
# cadence-only recurring schedule; do not inject DTSTART or raw directives.
PENDING_REPAIR = (
    checkpoint.active_batch.pending_repair
    if COMPLETION_ACTION == WAIT_RETRY
    else absent
)
require host.cleanup_worktree(pending_repair=PENDING_REPAIR) is confirmed
COMPLETION_NOW = host.now_utc()
successor_result = host.create_standalone_task(
    kind=cron, conversation=standalone, target_thread_id=absent,
    prompt=STANDALONE_HANDOFF.prompt, cadence_seconds=cadence_seconds,
    model=TASK_MODEL, reasoning_effort=TASK_REASONING_EFFORT,
    disposition=PAUSED
)
if successor_result is success:
    SUCCESSOR_ID = absent
    try:
        SUCCESSOR_ID = require_nonempty_id(successor_result)
        SUCCESSOR = host.read_task(SUCCESSOR_ID)
        require SUCCESSOR.prompt == STANDALONE_HANDOFF.prompt
        require SUCCESSOR.prompt_sha256 == sha256(STANDALONE_HANDOFF.prompt)
        require SUCCESSOR.scheduler_kind == cron
        require SUCCESSOR.conversation_mode == standalone
        require SUCCESSOR.target_thread_id is absent
        require SUCCESSOR.model == TASK_MODEL
        require SUCCESSOR.reasoning_effort == TASK_REASONING_EFFORT
        require SUCCESSOR.cadence_seconds == cadence_seconds
        require SUCCESSOR.disposition == PAUSED
        require SUCCESSOR.created_at is at or after COMPLETION_NOW at scheduler precision
        EXPECTED_FIRST_RUN = truncate_to_scheduler_precision(
            SUCCESSOR.created_at + cadence_seconds
        )
        require SUCCESSOR.first_run is present
        require SUCCESSOR.first_run matches EXPECTED_FIRST_RUN at scheduler precision
        # Authorization is setup evidence only. The wake remains active and
        # the task remains paused until complete-wake durably finalizes it.
        PULSE TARGET --wake-id WAKE_ID --now COMPLETION_NOW authorize-successor \
          --schedule-reanchored --scheduled-created-at SUCCESSOR.created_at \
          --scheduled-first-run SUCCESSOR.first_run --scheduled-task-id SUCCESSOR_ID
        require result.next_action == SUCCESSOR_AUTHORIZED
        completion = PULSE CHECKPOINT_TARGET --wake-id WAKE_ID --now COMPLETION_NOW complete-wake \
          --schedule-reanchored --scheduled-created-at SUCCESSOR.created_at \
          --scheduled-first-run SUCCESSOR.first_run \
          --scheduled-task-id SUCCESSOR_ID
        require completion.next_action in {WAIT_REVIEW, WAIT_RETRY, REQUEST_REVIEW}
        # Activation is the final host mutation. Do not call pulse, cleanup,
        # validation, or ordinary work after this operation.
        require host.activate_task(SUCCESSOR_ID) is confirmed
        report completion
        END_INVOCATION
    except Exception:
        # Creation is atomic in PAUSED state. If ID extraction failed, the
        # unknown task cannot run. If the ID is known, explicitly re-pause it
        # before persisting the absorbing recovery state.
        if SUCCESSOR_ID is present:
            try:
                cleanup_result = host.pause_task(SUCCESSOR_ID)
            except Exception:
                cleanup_result = None
            PAUSE_CONFIRMED = cleanup_result is success
        else:
            PAUSE_CONFIRMED = true
        FAILURE_FILE = write_json(
            {
                "reason_code": (
                    "successor_readback_failed"
                    if PAUSE_CONFIRMED
                    else "successor_cleanup_unconfirmed"
                ),
                "evidence": {
                    "successor_task_id": SUCCESSOR_ID,
                    "pause_confirmed": PAUSE_CONFIRMED,
                    "created_paused": true,
                },
            }
        )
        completion = PULSE CHECKPOINT_TARGET --wake-id WAKE_ID --now COMPLETION_NOW complete-wake \
          --completion-failure FAILURE_FILE
        report completion
        END_INVOCATION
else:
    # Do not pass --schedule-reanchored. This persists PAUSE_BLOCKED /
    # scheduled_task_reanchor_unavailable.
    completion = PULSE CHECKPOINT_TARGET --wake-id WAKE_ID --now COMPLETION_NOW complete-wake
    report completion
    END_INVOCATION
```

The host must inspect the actual tool response before passing either
confirmation flag. A boolean, model statement, or successful-looking command
line is not a host-tool result. For the initial user turn, create the
standalone task in `PAUSED` state first, then follow the same `begin-wake`
confirmation sequence. After durable `complete-wake` and the final activation
(or a fail-closed failure path), report the final wake state and end the
invocation immediately. Do not call scheduler list, reread the checkpoint, roll
another successor, verify that a successor was consumed, or call `begin-wake`
again in that invocation. The CLI options are
also visible in `pulse.py standalone-task-prompt --help`, `pulse.py begin-wake
--help`, and `pulse.py complete-wake --help`.

The default path reuses the core state evaluator, head-bracketed GraphQL
retrieval, atomic Git-common-directory checkpoint, frozen-batch transitions,
and exact GraphQL resolver. It does not duplicate those implementations and
does not import the hardened authority machinery.

For the default clean-context path, the Git-common-dir checkpoint is the
canonical durable lifecycle and control authority. Checkpoint-referenced
immutable repair patches/manifests and verified scheduler task metadata may
persist across wakes as auxiliary recovery or evidence artifacts, but they are
not independent workflow authorities. If a host exposes or uses a native
execution plan, it is ephemeral user-facing telemetry rather than durable
workflow authority; this skill does not claim native plan support in the
current implementation.

## Native execution plan contract

Native execution plans are user-visible execution telemetry, not durable
workflow state. The supported and validated scope for this capability is the
Codex Desktop scheduled-task workflow only. Do not detect Desktop versus CLI,
add frontend-specific branches, or add CLI compatibility, fallback, prompt,
or display behavior.

The setup conversation must create its Desktop-native execution plan before
the first scheduler mutation and update it as setup proceeds, including task
creation and metadata readback. It may finish the plan only after task
creation and successful metadata readback. If either operation fails, report
the actual blocker, do not claim completion, and do not wait, poll, or track
the first scheduled delivery. The setup conversation does not own that
delivery or the worker's PR/review work.

Each scheduled delivery, including the first one, starts a new standalone
worker. After startup and before its first PR/review operation, that worker
must create and maintain its own native execution plan. Do not inherit or
reuse the setup plan or a plan from another standalone wake. A worker plan
should contain only a few outcome-oriented steps, for example:

1. Inspect current PR and review state.
2. Classify actionable findings.
3. Implement the required repair.
4. Run focused and aggregate verification.
5. Publish/rearm or record the final blocked/terminal result.

While a plan is executing, exactly one step must be `in_progress`. After all
steps complete, or after a terminal or blocked outcome, the plan may have no
`in_progress` step. Update it promptly after the snapshot, classification,
repair, focused or aggregate verification, publication, rearm, and final
result; update it immediately when the approach changes. When pausing or
blocking, report the concrete blocker and do not fabricate completion.

Use the native `update_plan` tool when it is available in the supported
Desktop harness. If it is unavailable, continue the workflow normally and
report that native plan telemetry was unavailable; do not simulate it with a
Python API, MCP API, text/database Todo, checkpoint field, custom plan ID, or
another task-state subsystem. Do not persist or reuse a native plan across
wakes. GitHub, Git, the checkpoint, and existing lifecycle state remain the
only workflow facts.

## One standalone delivery, one wake, one plan

Treat the initial user turn as wake 1. Create its standalone scheduler task in
`PAUSED` state; never activate it before wake 1 starts. Every later rearm
creates one new standalone task/conversation with the same canonical prompt.
No task may point at or continue another task's Codex conversation.

The scheduler's configured project checkout is only a read-only repository
locator. After the required scheduled-task pause and checkpoint preflight, each
wake must create a new task-owned clean linked worktree at the independently
verified remote PR head. After begin-wake persists via the configured locator,
it must run every remaining pulse command, repository mutation,
validation, commit, and push from that worktree with the worktree passed as
`--repository-path`. Never reuse an earlier wake's worktree, and never switch,
reset, clean, or modify the configured/main checkout. A linked worktree retains
the required Git-common-dir checkpoint continuity while isolating checkout
files across standalone wakes.

The host invocation is the hard boundary: one invocation can successfully
begin and plan at most one wake. Only a scheduler-delivered new invocation can
start a later wake. Results from tools, todo rollover, notebooks, model
decisions, and future-task registration never authorize another `wake_id` in
the current invocation.

At the start of every scheduled wake, the first scheduler operation must pause
the delivered standalone task. Continue only after the host confirms the pause. If pause
confirmation is unavailable or fails, persist `PAUSE_BLOCKED`, keep the
delivered task paused, and end the turn without snapshot, freeze, resolve, commit,
push, trigger, or another plan.

Persist an opaque `wake_id` and at least these fields in the default checkpoint:

- `active_wake_id`
- `wake_phase`
- `wake_started_at`
- `wake_completed_at`
- `next_not_before`
- `scheduled_task_disposition`
- `scheduled_task_kind` (`standalone`)
- `scheduled_task_id` (the latest successor task when active)
- `wake_count`

Generate a fresh opaque `wake_id` when each real invocation arrives, and never
reuse an ID from checkpoint, logs, task text, notebook, todo state, or a failed
attempt. Repeating `plan` or `snapshot` with the same ID returns the prior
result or rejects without incrementing `wake_count`; that idempotence does not
make the repeated call a new wake. Lease renewal, snapshot refresh,
completion, and recovery inspection are not new wakes. A stale or incomplete
marker produces `PAUSE_RECOVERY`; it never auto-takes over the marker.

Before every scheduled `begin-wake`, directly inspect the current checkpoint:
stop on a non-empty `active_wake_id`, `failure_latch`, or a future
`next_not_before`; stop on a missing or unreadable checkpoint. These checks use
the actual checkpoint/tool output and cannot be satisfied by remembered state,
natural-language summaries, or a registered future task. Do not snapshot,
freeze, mutate, or start a successor after such a stop.

After any `complete-wake` call, immediately report that wake's final result and
end the host invocation. Do not inspect scheduler state, reread the
checkpoint, roll a successor todo, verify successor consumption, or call
`begin-wake` again. A successful re-anchor and a failed re-anchor follow the
same immediate-end rule; the latter uses the unconfirmed `complete-wake` path
and remains paused with its blocker.

Run at most one stable snapshot/decision and one frozen batch per wake. While a
configured Codex identity has PR-level `EYES`, return `WAIT_REVIEW` and do not
freeze partial threads. After `EYES` disappears, freeze all targeted unresolved
Codex root-author thread IDs from the stable current head. Process each thread
as `fix-now`, `no-fix`, `defer`, or `ambiguous`; persist its outcome before
resolving that exact GraphQL node. Ambiguous/conflicting evidence pauses.

In the autonomous profile, a failed focused check is a repair signal, not an
automatic stop. If the behavior contract is correct, update a stale
PR-scoped test or fixture when `allow_test_changes` is true, rerun the focused
check, and continue. For a transient external or environment failure, use
`retry` to persist the failure signature and end the wake with `WAIT_RETRY`;
the next completion-relative wake resumes the same frozen batch. A repeated
unchanged signature reaches `no_progress_limit` and pauses. If the policy sets
`validation_failure=pause` or disallows test changes, stop at that boundary.

After all exact resolutions, run aggregate validation and call
`prepare-publication` before creating the aggregate commit and again
immediately before the push. Continue only when each call returns
`PUBLISH_BATCH` for the frozen head. Then publish at most one aggregate commit
and one push. A no-fix-only batch does not create an empty commit. Do not
process review artifacts created by that push until a later wake.

The following are default-path safety rules, not optional hardened ceremony:

- Each wake uses a newly created clean linked worktree at the verified remote
  PR head. The scheduler/project checkout remains read-only and is never the
  repair workspace, even when the host binds a standalone task to that project.
- For `fix-now`, focused validation must pass before resolving that exact
  thread. Repair the implementation or a stale PR-scoped test first when the
  autonomous policy permits; otherwise leave the thread unresolved and pause.
- Never commit or push while any frozen thread lacks a recorded outcome or an
  exact confirmed resolution. A `prepare-publication` pause is absorbing for
  that wake; do not bypass it with raw Git commands.
- Before creating the aggregate commit, re-read the authoritative remote PR
  head and require it to equal the frozen head OID.
- Stage only explicit intended paths and inspect the staged diff; never stage
  the whole worktree.
- Immediately before the single push, re-read the remote PR head again and
  require it still equals the frozen head OID. If the branch advanced, stop
  and do not overwrite it.
- After pushing, verify that the remote PR head equals the new pushed commit;
  a mismatch is a publication failure. Never force-push or change the PR base.

## Completion-relative scheduling

Only a final `WAIT_REVIEW`, `WAIT_RETRY`, or a successful `REQUEST_REVIEW` whose
trigger evidence proves the same head before and after the trigger, may rearm
the next wake. Set:

```text
next_not_before = ceil_to_scheduler_precision(wake_completed_at + cadence_seconds)
```

For a persisted-created-at successor, the scheduler's represented first run
(`truncate_to_scheduler_precision(created_at + cadence_seconds)`) is also the
checkpoint's `next_not_before`. This keeps delivery and the lifecycle deadline
aligned when completion and task creation fall in different fractional seconds.

Never rely on pausing and reactivating a fixed recurring task to reset its
clock, and do not submit `DTSTART` during immediate automation creation. Create
one new paused cadence-only successor immediately after choosing
`wake_completed_at`.
Read back its persisted ID, prompt, cadence, and `created_at`; require the
creation anchor to be at or after `wake_completed_at` at the scheduler's
representable precision, then derive:

```text
next_not_before = truncate_to_scheduler_precision(created_at + cadence_seconds)
```

The host-supported scheduler represents task metadata by truncating fractional
seconds. The controller applies that same whole-second representation to the
creation anchor, expected first run, and read-back first run; this accepts a
sub-second representation difference without adding an arbitrary early-wake
tolerance. A first run in an earlier represented second still fails closed.
The creation anchor must still be at or after `wake_completed_at` at the
scheduler's representable precision. If the host cannot prove the creation
anchor and cadence, keep the disposition `PAUSED` and report the corresponding
re-anchor/readback blocker.
The public standalone rearm path must include the persisted task creation
anchor. A missing or pre-completion anchor remains paused; the persisted first
run must equal the creation-anchor-plus-cadence expectation or be at most one
second later. Older injected direct-first-run integrations remain compatible
with the legacy completion callback path.

Multiple pushes that finish before the next wake naturally coalesce into the
latest head accepted by that wake's stable GraphQL snapshot. A head change
after the snapshot is frozen remains a recovery pause: do not add later pushes
or their review artifacts to the active batch.

`STOP_*`, every `PAUSE_*`, recovery, closed/merged, expired, publication
failure, lease loss, and unknown results remain `PAUSED` and do not schedule a
next wake. `STOP_POLICY_LIMIT` is the explicit result for a configured wake,
deadline, or retry bound. Persisted `terminal` or `closed` phases are absorbing
across later wake IDs too: `begin-wake` rejects them without incrementing
`wake_count`. Reopening requires an explicit new user instruction. Pause is
absorbing for the current turn: persist the reason and evidence, stop
immediately, and never clear its latch in the same turn. A
non-empty string called `recovery_authorization_id` is not proof of user or
external authorization. The default path has no automatic latch-clearing
operation; recovery starts only in a new user turn or from a separately
verifiable external authority.

## Standalone scheduled-task handoff

The Python entry point does not call a private Codex automation API. The
executing agent owns the following single handoff sequence for one host
invocation:

1. Treat the user's initial task as wake 1.
2. Create the initial standalone scheduled task while it is `PAUSED`; its
   `prompt` is the exact output of `standalone-task-prompt`, its model and
   reasoning settings come from that handoff, its scheduler kind is `cron`,
   its conversation mode is standalone, and it has no `target_thread_id`.
3. On each genuinely new scheduler-delivered invocation, generate a fresh
   opaque `wake_id`; never copy one from checkpoint, logs, task text, notebook,
   todo state, or a failed attempt. Do not treat an in-invocation tool result,
   rollover, model decision, or future task as delivery of another wake.
4. At the start of every scheduled wake, call the task tool to pause the
   delivered standalone task.
5. Read the checkpoint directly before `begin-wake`; stop with the task paused
   if it is missing/unreadable, has an active wake, has a failure latch, or has
   a future `next_not_before`. Do not replace it from memory or a summary.
6. If the delivered-task pause cannot be confirmed, call `begin-wake` without
   pause confirmation to persist the pause result and end before remote-head or
   worktree setup. Otherwise, persist begin-wake via the configured checkout
   immediately after preflight, then independently verify the
   remote PR head and create a
   new task-owned clean linked worktree at that exact commit. Treat the
   scheduler/project checkout as a read-only locator; never reuse an earlier
   wake worktree or switch, reset, clean, or modify the configured checkout.
   Use the new worktree as `--repository-path` for remaining pulse commands and run
   all edits, validation, commit, and push from it.
7. Submit `pause confirmed` to `begin-wake` only after the pause tool call
   returns success and the direct preflight passes. A pause failure may use an
   unconfirmed `begin-wake` only to persist `PAUSE_BLOCKED`, then ends this
   invocation. For every scheduled delivery, pass the delivered task's ID as
   `--delivered-task-id`; `pulse.py` compares it with the checkpoint's persisted
   active successor before starting the wake. The initial user wake has no
   delivered successor ID and omits this option.
8. Run this wake's snapshot, frozen batch, repair/retry, outcome/resolve, and
   aggregate publication work. Before freezing, require the wake worktree's
   `git rev-parse HEAD` to equal the stable snapshot head; `pulse.py freeze`
   persists `PAUSE_RECOVERY / worktree_head_mismatch` otherwise. If a fix-now
   repair is uncommitted when a recoverable retry is needed, persist an
   immutable Git-common-dir patch and manifest, pass it to `pulse.py retry
   --pending-repair`, and run restore-repair in the next clean worktree before
   focused validation. Stop immediately on any `PAUSE_*` or `STOP_*`.
 9. For `WAIT_REVIEW`, `WAIT_RETRY`, or successful same-head `REQUEST_REVIEW`, finish
    all publication, remote-head, validation, and task-owned worktree cleanup first.
    Only then read the host's current UTC time as the completion anchor. Do not use a
    timestamp captured before cleanup or before the final durable close.
10. On a scheduled delivery, reload the target-bound handoff with
   `standalone-task-prompt` from the persisted policy and without
   `--policy-json`. Require its prompt and SHA-256 to match the delivered
   task's persisted canonical prompt, then use that handoff's model and
   reasoning settings for the successor. Do not carry `POLICY_JSON`,
   `STANDALONE_HANDOFF`, or task-setting variables from an earlier invocation.
   Create one new standalone successor task in `PAUSED` state with the
   verified handoff and a cadence-only recurring schedule. Do not submit
   `DTSTART` or hand-write a raw scheduling directive. Extract its ID inside
   the cleanup boundary, then read back the successor's persisted ID,
   prompt and SHA-256, scheduler kind (`cron`), conversation mode
   (`standalone`), absent `target_thread_id`, model, reasoning settings,
    cadence, paused disposition, and creation timestamp. Require every field to
    match the canonical handoff, require its creation timestamp to be at or after
    the completion anchor at scheduler precision, and derive its first run as the
    whole-second-truncated creation time plus cadence. Persist `authorize-successor`
    with those verified values while the task is paused; authorization is setup
    evidence and does not close the wake. Then call `complete-wake` exactly once
    from the configured checkpoint-capable checkout with the same anchor,
    `--schedule-reanchored`, `--scheduled-created-at`, the derived
    `--scheduled-first-run`, and `--scheduled-task-id`. Require a successful
    rearmable result before activation. The checkpoint remains `AUTHORIZED` after
    durable finalization so a delivered task consumes the handoff.
 11. After durable finalization succeeds, activate only that verified successor
     with the full metadata-preserving update. Activation is the final host
     mutation in this invocation: do not call pulse, cleanup, validation, or
     ordinary work afterward. If activation fails, pause the exact successor and
     persist a recovery latch before ending.
 12. If successor creation, readback, authorization, cleanup, clock anchoring, or
     finalization fails, keep the successor paused and call `complete-wake` once
     without reanchor confirmation (or with `--completion-failure`) to persist the
     blocker. Never activate an unverified or non-finalized successor.
 13. If a host restart occurs while `active_wake_id` is still present, do not
     activate from authorization alone; keep the exact task paused and preserve
     the active-wake guard. After durable finalization, reconciliation requires
     exact task-status readback and explicit host evidence that no delivery was
     observed. A delivered task must go directly through `begin-wake` with its
     exact ID and must never be reactivated.
 14. Immediately report the result after the final host mutation and end this host
     invocation. Do not list scheduler tasks, reread the checkpoint, roll or verify
     a successor, or call `begin-wake` again. On any other tool failure, `PAUSE_*`,
     terminal result, or unproven success, leave the task `PAUSED` and end the
     invocation. Never treat a model assertion, boolean argument, or natural-
     language claim as proof that pause, delivery gating, or re-scheduling succeeded.

    A recurring cron task cannot guarantee that its first invocation is paused
    before a model starts. A `usage_limit_exceeded` or equivalent failure before
    the first tool call can therefore create another invocation even though the
    checkpoint active-wake guard prevents duplicate PR mutation. The host adapter
    must provide a one-shot/pre-model delivery gate for unattended safety; if it
    exposes only ordinary recurring cron with no such gate, fail closed and report
    that host limitation rather than claiming that self-pause prevents duplicate
    task creation.

## Review termination

Stop only for a Codex-specific result, never as a claim of global merge
readiness:

1. an `APPROVED` review by a configured Codex identity whose
   `review.commit.oid == headRefOid` and no targeted threads;
2. a newly proven current-head Codex `THUMBS_UP` reaction epoch and no targeted
   threads; or

Cold-start or first-observation historical reactions remain ambiguous.
`EYES` is review activity, never approval. A head with targeted threads is
always processed even if an earlier epoch saw `EYES`. After one safely
bracketed `@codex review` trigger for a head, a following empty wake pauses
with evidence instead of triggering again.

## Authorized default scope

The standard short request authorizes autonomous PR-scoped implementation and
test edits, repair of stale PR-scoped expectations, recoverable retries,
targeted Codex thread exact resolution including recorded no-fix outcomes, one
aggregate commit and push per batch, one trigger per head, and creation of one
standalone successor task per rearmable wake. It remains active across
scheduled wakes until a Codex-specific terminal result or a hard blocker. A prompt can
narrow this scope by selecting `supervised`, `observe-only`, explicit limits,
`allow_test_changes=false`, or confirmation policies. It never authorizes issue
creation, merge, auto-merge, base changes, force-pushes, generic reviewers,
non-target threads, or unrelated changes.

Host permissions are a separate boundary: unattended operation requires the
host task/thread to have network access, full workspace access, and a
non-interactive approval policy. The prompt can narrow behavior, but cannot
grant capabilities that the host has not granted.

## Optional hardened mode

The existing immutable installation, pilot preflight, run contract, authority
digest, renewable lease, doctor/plan/complete, and recovery-latch machinery is
retained only as an explicit advanced/hardened mode. Read
[`references/hardened.md`](references/hardened.md) before choosing it. It is
not a prerequisite for the default path, and the 0.4.0 black-box pilot failure
means it is not currently a publishable final recurring mode.

Codex-only targeting, stable head snapshots, frozen batches, exact resolution,
one aggregate publication, and unrelated-work protection apply in both modes.
Pi portability, plugin packaging, generic reviewers, multi-forge support, live
long-term unattended integration evidence, and `gh-address-comments`
integration remain deferred.
