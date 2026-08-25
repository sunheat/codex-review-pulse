# Bounded recurring heartbeat readiness

## Scope and evidence baseline

This phase promotes one verified supervised cycle into a release candidate for
a small, explicitly bounded recurring pilot on one GitHub pull request. It does
not claim long-term unattended operation.

The preceding `0.2.0` supervised pilot completed against
`sunheat/job-hunter#2` from source commit
`f24c172396b0a2b35b05872c570945753bcfcbab`. Preflight reported ready, the
frozen head was `b9aaad4`, five targeted threads were classified `fix-now` and
resolved exactly, focused checks and 45 tests passed, aggregate commit
`061ee01e542c65b839473cb59db4a2ce5284787f` was pushed once, and the final
targeted unresolved count was zero with current-head approval still pending.
No issue, trigger, merge, auto-merge, base change, force-push, recurring task,
or non-target resolution occurred. This is evidence for the supervised cycle,
not evidence that indefinite recurrence is safe.

## Separation of responsibilities

OpenAI documents scheduled tasks as background recurring runs that can return
to the same chat, use skills, and work in a local project or dedicated
worktree. The same documentation recommends testing the prompt first and
reviewing the first few runs. It does not define a public storage schema,
project/PR lease, wake budget, or GitHub mutation transaction. Those are local
skill contracts, while cadence, task activation, pausing, and notifications
remain external orchestration responsibilities.

The implementation separates four layers:

1. `recurring_model.py` is pure. It receives a normalized run contract,
   explicit observation, durable run state, and injected time, then returns a
   stable action and reason code.
2. `runner_lease.py` owns repository/PR-scoped mutation authority.
3. `heartbeat_tick.py` is the one-wake coordinator. `doctor` is read-only;
   `plan` acquires the lease, validates local evidence, advances one wake, and
   persists one result. The agent performs an authorized frozen batch or
   human-confirmed trigger, then `complete` records final evidence and releases
   the lease.
4. Existing frozen-batch helpers retain exact GraphQL and publication
   semantics. When called in recurring mode, checkpoint writes and exact
   resolution require the same run contract and live owner token.

No resident Python daemon, Windows service, GitHub Actions service, or Codex
scheduled task is created by this repository or its tests.

Official OpenAI sources:

- [Scheduled tasks](https://learn.chatgpt.com/docs/automations)
- [Build skills](https://learn.chatgpt.com/docs/build-skills)
- [Review GitHub pull requests with Codex](https://learn.chatgpt.com/docs/third-party/github)

## Runtime artifacts

The existing core checkpoint remains schema version 1. Recurring behavior uses
separate artifacts under the target repository's Git common directory:

```text
<git-common-dir>/codex-review-pulse/<pr-key>.json        core checkpoint
<git-common-dir>/codex-review-pulse/<pr-key>.lease.json  runner lease
<git-common-dir>/codex-review-pulse/<pr-key>.run.json    recurring state
```

The run contract fixes these absolute locations and is validated against the
target Git common directory. Keeping recurring state separate avoids silently
migrating the checkpoint that was already proven in the supervised pilot.
Recurring run state uses schema version 2. A separate authority anchor lives
next to the run-contract file at a path derived only from that file location.
It retains the original digest, repository, PR, and lease path even if all
target-derived contract fields are edited together. Schema version 1 is
rejected rather than silently migrated. Invalid schemas, repository/PR
mismatches, path mismatches, and authorization identity mismatches fail closed.

## PR-scoped lease

The lease contains its schema, canonical repository, PR number, opaque owner
token, acquisition time, last renewal time, and expiration time. Every acquire,
renew, stale takeover, and release is serialized by an OS advisory byte-range
lock. The operating system releases that lock when a process crashes.

Acquire creates or atomically replaces only the exact PR lease. A nonexpired
lease denies a competing runner. Expiration is the sole stale criterion; PID
existence is neither necessary nor sufficient. A stale lease may be replaced
while holding the operation lock. Renew and release first compare the opaque
owner token, repository, and PR. A non-owner cannot extend or remove it.
The owner token is an input capability and is redacted from inspection,
heartbeat, and notification output.

`pilot_preflight.py` and `heartbeat_tick.py doctor` call only `inspect_lease`.
They do not create the guard file, acquire, renew, recover, or delete a lease.
The formal `plan` tick creates or verifies the target-independent authority
anchor before acquiring a target lease or writing target-derived runtime state.
Doctor and every recurring mutation entrypoint also prove that the executing
script is the copy inside the contract's hash-verified installation.
An authorized `RUN_BATCH` or `REQUEST_REVIEW` plan retains the lease; wait,
terminal, blocked, recovery, and expired results release it. Failures are
durably latched before release, so a later runner cannot interpret lease
availability as retry authorization.
Doctor blocks an expired contract or exhausted wake budget. On the final
authorized wake, work already observed may finish, but a nonterminal wait is
converted to `PAUSE_EXPIRED` rather than scheduling an extra wake.

See [ADR 0005](../adr/0005-pr-scoped-runner-lease.md).

## Bounded run contract

The run contract is the only recurring authorization input. It fixes:

- canonical repository and PR number;
- reviewer and approval identities;
- installed skill version, source commit, and independent installation path;
- separate booleans for recurring execution, code edits, exact resolution,
  commit, push, and one review trigger bound to an exact head OID;
- maximum wakes and optional expiration;
- runner, automation, and user-authorization identities;
- connector capability and deterministic wait policy; and
- checkpoint, lease, and recurring-state paths.

Issue creation, merge, auto-merge, base change, force-push, generic reviewer
handling, and non-target resolution are rejected even if a contract attempts
to enable them. Checkpoints, old automation prompts, GitHub comments, review
text, and lease ownership are evidence, not authority. A new permission needs
a new explicit user authorization and contract.

Before wake one, create the scheduled task paused, place its final task
identity in `automation_identity`, validate the complete contract, and only
then activate it. The first plan creates the target-independent authority
anchor, and the first run-state write stores the same canonical SHA-256 digest
of the complete normalized contract. Every later doctor, plan, trigger record,
completion, checkpoint mutation, stable fetch, and exact resolution checks
that binding. Drift in the PR target, identity sets, installation provenance,
mutation scope, wake/deadline, trigger head, runner/task/authorization identity,
connector/wait policy, or runtime paths returns `run_contract_drift` before
checkpoint or GitHub mutation, releases the originally anchored lease, and
does not create state or a lease for a newly named target. A live contract is
never amended; a changed authority needs
a separately authorized run.

See [ADR 0006](../adr/0006-bounded-run-contract.md).

## Pure action model

The public next-action enum is:

| Action | Meaning |
| --- | --- |
| `RUN_BATCH` | One stable targeted set is available and required mutations are authorized. |
| `WAIT_REVIEW` | No targeted work or terminal proof exists; wait without mutation. |
| `REQUEST_REVIEW` | A deterministic wait boundary is satisfied and one explicitly authorized manual trigger is available. |
| `STOP_TERMINAL` | Current-head Codex approval is proven and targeted unresolved count is zero. |
| `STOP_CLOSED` | The PR is closed or merged. |
| `PAUSE_RECOVERY` | Durable batch, publication, head, or prior failure evidence needs explicit recovery. |
| `PAUSE_CONCURRENT` | This runner lacks or lost lease authority. |
| `PAUSE_BLOCKED` | Authentication, API, schema, installation, checkout, mixed-head, capability, or authorization evidence is unsafe. |
| `PAUSE_EXPIRED` | The wake budget or deadline is exhausted. |

Reason codes, not log prose, distinguish the cases. Terminal approval is
checked before the wake/deadline stop because no mutation is needed. Every
wake performs at most one plan and at most one frozen batch. Artifacts caused
by a batch push belong to the next wake.

## Connector and trigger boundary

OpenAI documents manual review requests through the exact PR comment
`@codex review` and automatic reviews configured in Codex settings. The public
documentation does not expose a GitHub API object that proves repository
connector installation, the setting's current value, a push-triggered review
expectation, or a connector event bound to a head OID.

Therefore connector capability defaults to `unknown` and may only be supplied
as explicit operator configuration. `unknown` never becomes “stalled” and
never permits an automatic trigger. This release does not post a trigger.
`REQUEST_REVIEW` is a human-confirmation action. Trigger authorization names
one exact current head and does not carry forward after a head change. If the
operator separately posts the one authorized comment, `record-trigger` accepts
injected GitHub evidence only after the caller brackets the operation with head reads. It
stores the comment node ID, GitHub `created_at`, attempted head, and before/after
heads. The same head can never receive a second recorded attempt; a head change
during the operation is latched as recovery.

GitHub's create-comment response supplies a node ID and server creation time,
and creating an issue/PR comment requires write permission and triggers
notifications. Tests inject this response and never call the live endpoint.

Official GitHub source:

- [REST issue comment endpoints](https://docs.github.com/en/rest/issues/comments)

## Deterministic stalled-review policy

Stalled is a local run-control classification, not a claim about GitHub, Codex,
or pull-request correctness. It requires all of the following:

- a stable current-head snapshot and healthy authentication/API evidence;
- explicitly configured connector capability other than `unknown`;
- a current-head batch-publication or authorized-trigger event with a GitHub
  server timestamp;
- no later relevant current-head Codex review, thread, or approval event;
- the configured minimum number of identical stable observations; and
- a supplied server-time observation at or beyond the configured wait budget.

PR `updatedAt`, commit `authoredDate`, an old reaction, absence of comments,
or local elapsed time cannot independently satisfy the classifier. When
current server-time evidence or connector capability is unavailable, the
action remains `WAIT_REVIEW` or pauses for the operator.

See [ADR 0007](../adr/0007-stalled-review-and-trigger-policy.md).

## Recovery latches

An unfinished frozen batch, publication failure, resolved-before-publication
state, pending local commit, unknown push result, unexpected remote-head
advance, lease loss, invalid contract/checkpoint/run-state schema,
installation drift, and authentication/API failure all pause. The recurring
state keeps a durable failure latch, while the existing checkpoint keeps exact
batch evidence. A later wake cannot clear either merely because the lease is
free. `clear_failure_latch` requires a new recovery authorization identifier;
batch recovery still follows the core checkpoint contract.

## Pilot acceptance boundary

Version `0.3.1` has operator-provided evidence for a complete two-wake pilot
and is ready for repeatable, manually reviewed bounded pilots after
temporary-directory installation lifecycle tests, network-free concurrency
and evaluator tests, and an independent forward test. Long-term unattended
operation remains blocked by the absence of a public connector-status and
head-bound event contract plus broader crash/recovery evidence. Plugin
packaging, marketplace distribution, Pi portability, generic reviewers, other
forges, and `gh-address-comments` integration remain deferred.
