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
threads, and rearm the same heartbeat until a Codex-specific stop condition or
an explicit safety boundary is reached. It does not require a run contract,
observation JSON, doctor, pilot preflight, immutable installation, runner
identity, authority digest, owner token, or renewable lease.

Version `0.4.0` has a real black-box pilot failure. It is not a publishable
final recurring release. The failure evidence is preserved in the repository:
the old heartbeat activated too early, used fixed-cadence overlap, allowed a
300-second lease to expire during a long wake, double-planned one host wake,
and continued after `PAUSE_BLOCKED`. Do not describe `0.4.0` as production
ready or use its old scheduled-task protocol as the default.

Version `0.7.1` is the Codex-first default automation-policy candidate. Its
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

Its small subcommands are `heartbeat-prompt`, `begin-wake`, `snapshot`,
`freeze`, `record`, `resolve`, `retry`, `trigger-result`, `confirm-policy`,
`prepare-publication`, `publication-result`, `configure-policy`, and
`complete-wake`.
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

The policy is a control contract, not a request to bypass the hard invariants
below. Codex-only targeting, current-head proof, frozen exact thread IDs,
explicit-path staging, one aggregate publication per batch, no force-push or
merge, and pause-on-uncertainty always remain in force. `null` limits are
deliberately unbounded; if a user supplies a wake count or deadline, it is
persisted and enforced at the next wake.

`pulse.py` enforces lifecycle-level limits and mutation gates; the executing
agent applies `execution_mode`, `allow_test_changes`, `inline_retry_limit`, and
`notifications` while choosing local edits, validation, and reporting.

Prompt instructions override these defaults. The host agent should convert them
to the corresponding JSON fields before the initial `begin-wake`, for example:

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
clearing or policy rewrite cannot resume it.

The host adapter may pass `--pause-confirmed` to `begin-wake` and
`--schedule-reanchored` to `complete-wake` only after successful host-tool
calls. The latter must be accompanied by `--scheduled-first-run` containing
the actual persisted first run read back from the updated task. These flags do
not pause or schedule a Codex task themselves; a success boolean is never
evidence that the host operation or completion-relative re-anchor succeeded.

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
confirmed results. The following is one complete host invocation. The marker
`END_INVOCATION` means return the final report immediately; it is not a prompt
to continue with another scheduler or pulse operation.

```text
PULSE = "python <loaded-skill-directory>/scripts/pulse.py"
TARGET = "--repository-path PR_CHECKOUT --repo OWNER/REPO --pr NUMBER"

# On the initial user turn, render the target-bound scheduled-wake prompt and
# pass its `prompt` field unchanged when creating or updating the same task.
# Do not paraphrase or reorder its batch protocol.
HEARTBEAT_HANDOFF = PULSE TARGET heartbeat-prompt

# A genuinely delivered invocation gets one fresh ID. Never derive it from
# checkpoint, logs, task text, notebook, todo state, or an earlier attempt.
WAKE_ID = host.new_opaque_wake_id()

if this is the initial explicit user request:
    host.create_or_update_task(task_id, disposition=PAUSED) -> success
    # Wake 1 may initialize an absent checkpoint.
else if this is a scheduler-delivered invocation:
    # This must be the first scheduler operation in this invocation.
    pause_result = host.pause_task(task_id)
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
    if checkpoint.next_not_before is later than current time:
        report PAUSE_BLOCKED / cadence_not_elapsed
        END_INVOCATION
else:
    report PAUSE_RECOVERY / invocation_not_scheduler_delivered
    END_INVOCATION

# On wake 1, begin-wake may create the checkpoint. On later wakes, the direct
# preflight above is mandatory immediately before this call.
if this is the initial explicit user request or pause_result is confirmed:
    PULSE TARGET --wake-id WAKE_ID begin-wake \
      --pause-confirmed
else:
    # Do not pass --pause-confirmed. This call only persists PAUSE_BLOCKED.
    PULSE TARGET --wake-id WAKE_ID begin-wake
    report the returned pause result
    END_INVOCATION

snapshot = PULSE TARGET --wake-id WAKE_ID snapshot

if snapshot.decision.next_action == RUN_BATCH:
    PULSE TARGET --wake-id WAKE_ID freeze
    for each frozen thread:
        PULSE TARGET --wake-id WAKE_ID record --thread-id ID \
          --classification fix-now|no-fix|defer|ambiguous
        # Apply and focused-validate a fix when classification is fix-now.
        # In autonomous mode, repair stale PR-scoped tests or implementation
        # defects when the behavior contract is correct. If a recoverable
        # failure remains, persist it and end this wake:
        PULSE TARGET --wake-id WAKE_ID retry \
          --reason-code validation_retry --signature FAILURE_SIGNATURE
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

# WAIT_REVIEW, WAIT_RETRY, or a successfully recorded same-head REQUEST_REVIEW
# may re-anchor. WAIT_RETRY resumes the same frozen batch on the next wake.
# Choose COMPLETION_NOW once and use it for both actions.
COMPLETION_NOW = current UTC time
NEXT_NOT_BEFORE = COMPLETION_NOW + cadence_seconds
reanchor_result = host.reanchor_task(task_id, first_run=NEXT_NOT_BEFORE)
if reanchor_result is success:
    ACTUAL_FIRST_RUN = host.read_task(task_id).first_run
    completion = PULSE TARGET --wake-id WAKE_ID --now COMPLETION_NOW complete-wake \
      --schedule-reanchored --scheduled-first-run ACTUAL_FIRST_RUN
    report completion
    END_INVOCATION
else:
    # Do not pass --schedule-reanchored. This persists PAUSE_BLOCKED /
    # scheduled_task_reanchor_unavailable.
    completion = PULSE TARGET --wake-id WAKE_ID --now COMPLETION_NOW complete-wake
    report completion
    END_INVOCATION
```

The host must inspect the actual tool response before passing either
confirmation flag. A boolean, model statement, or successful-looking command
line is not a host-tool result. For the initial user turn, create or update
the same task in `PAUSED` state first, then follow the same `begin-wake`
confirmation sequence. After either form of `complete-wake`, report the final
wake state and end the invocation immediately. Do not call scheduler list,
re-read the checkpoint, roll a successor todo, verify that a successor was
consumed, or call `begin-wake` again in that invocation. The CLI options are
also visible in `pulse.py begin-wake --help` and `pulse.py complete-wake
--help`.

The default path reuses the core state evaluator, head-bracketed GraphQL
retrieval, atomic Git-common-directory checkpoint, frozen-batch transitions,
and exact GraphQL resolver. It does not duplicate those implementations and
does not import the hardened authority machinery.

## One host wake, one plan

Treat the initial user turn as wake 1. When creating or updating the one
heartbeat for the same task, leave it `PAUSED`; never activate it before wake
1 starts.

The host invocation is the hard boundary: one invocation can successfully
begin and plan at most one wake. Only a scheduler-delivered new invocation can
start a later wake. Results from tools, todo rollover, notebooks, model
decisions, and future-task registration never authorize another `wake_id` in
the current invocation.

At the start of every scheduled wake, the first scheduler operation must pause
that heartbeat. Continue only after the host confirms the pause. If pause
confirmation is unavailable or fails, persist `PAUSE_BLOCKED`, keep the
heartbeat paused, and end the turn without snapshot, freeze, resolve, commit,
push, trigger, or another plan.

Persist an opaque `wake_id` and at least these fields in the default checkpoint:

- `active_wake_id`
- `wake_phase`
- `wake_started_at`
- `wake_completed_at`
- `next_not_before`
- `scheduled_task_disposition`
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
next_not_before = wake_completed_at + cadence_seconds
```

Never rely on pausing and reactivating a fixed RRULE to reset its clock. The
host must re-anchor the next run to `next_not_before`. If the host cannot prove
that completion-relative schedule, keep the disposition `PAUSED` and report
`PAUSE_BLOCKED / scheduled_task_reanchor_unavailable`. If the persisted
first run differs from `next_not_before`, report
`PAUSE_BLOCKED / scheduled_task_reanchor_mismatch`; never reuse an earlier
`DTSTART`.

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

## Scheduled-task handoff

The Python entry point does not call a private Codex automation API. The
executing agent owns the following single handoff sequence for one host
invocation:

1. Treat the user's initial task as wake 1.
2. Create or update the same-task recurring task while it is `PAUSED`.
3. On each genuinely new scheduler-delivered invocation, generate a fresh
   opaque `wake_id`; never copy one from checkpoint, logs, task text, notebook,
   todo state, or a failed attempt. Do not treat an in-invocation tool result,
   rollover, model decision, or future task as delivery of another wake.
4. At the start of every scheduled wake, call the task tool to pause that task.
5. Read the checkpoint directly before `begin-wake`; stop with the task paused
   if it is missing/unreadable, has an active wake, has a failure latch, or has
   a future `next_not_before`. Do not replace it from memory or a summary.
6. Submit `pause confirmed` to `begin-wake` only after the pause tool call
   returns success and the direct preflight passes. A pause failure may use an
   unconfirmed `begin-wake` only to persist `PAUSE_BLOCKED`, then ends this
   invocation.
7. Run this wake's snapshot, frozen batch, repair/retry, outcome/resolve, and
   aggregate publication work. Stop immediately on any `PAUSE_*` or `STOP_*`.
8. For `WAIT_REVIEW`, `WAIT_RETRY`, or successful same-head `REQUEST_REVIEW`, choose one
   completion timestamp and compute
   `next_not_before = wake_completed_at + cadence_seconds` without calling
   `complete-wake` yet.
9. Update the task so its next run is anchored to that timestamp, inspect the
   host-tool result, then read back the task's actual persisted first run.
10. After the task update and readback succeed, call `complete-wake` exactly
   once with the same completion timestamp, `--schedule-reanchored`, and
   `--scheduled-first-run ACTUAL_FIRST_RUN`. A stale or mismatched readback
   persists a blocker and must not activate the checkpoint.
11. If the task update or readback fails, call `complete-wake` exactly once without
   `--schedule-reanchored`; this persists the re-anchor blocker and keeps the
   task paused.
12. Immediately report the result of either `complete-wake` call and end this
    host invocation. Do not list scheduler tasks, reread the checkpoint, roll
    or verify a successor, or call `begin-wake` again. On any other tool
    failure, `PAUSE_*`, terminal result, or unproven success, leave the task
    `PAUSED` and end the invocation. Never treat a model assertion, boolean
    argument, or natural-language claim as proof that pause or re-scheduling
    succeeded.

## Review termination

Stop only for a Codex-specific result, never as a claim of global merge
readiness:

1. an `APPROVED` review by a configured Codex identity whose
   `review.commit.oid == headRefOid` and no targeted threads;
2. a newly proven current-head Codex `THUMBS_UP` reaction epoch and no targeted
   threads; or
3. Codex `EYES` was observed on this head, then disappeared in a newer stable
   snapshot, and targeted unresolved threads are zero.

Cold-start or first-observation historical reactions remain ambiguous.
`EYES` is review activity, never approval. A head with targeted threads is
always processed even if an earlier epoch saw `EYES`. After one safely
bracketed `@codex review` trigger for a head, a following empty wake pauses
with evidence instead of triggering again.

## Authorized default scope

The standard short request authorizes autonomous PR-scoped implementation and
test edits, repair of stale PR-scoped expectations, recoverable retries,
targeted Codex thread exact resolution including recorded no-fix outcomes, one
aggregate commit and push per batch, one trigger per head, and creation/update/
pause/reanchor of one same-task heartbeat. It remains active across scheduled
wakes until a Codex-specific terminal result or a hard blocker. A prompt can
narrow this scope by selecting `supervised`, `observe-only`, explicit limits,
`allow_test_changes=false`, or confirmation policies. It never authorizes issue
creation, merge, auto-merge, base changes, force-pushes, generic reviewers,
non-target threads, or unrelated changes.

Host permissions are a separate boundary: unattended operation requires the
host task/thread to have network access, full workspace access, and a
non-interactive approval policy. The prompt can narrow behavior, but cannot
grant capabilities that the host has not granted.

This repository phase implements and tests the control logic only. Do not
create a real scheduled task, mutate live GitHub, install the skill, commit,
or push while developing or validating this change.

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
