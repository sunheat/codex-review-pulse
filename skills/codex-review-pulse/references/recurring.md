# Hardened recurring compatibility reference

Read this document only after explicitly choosing the optional hardened mode.
Version `0.4.0` has a real black-box pilot failure and is not a publishable
final recurring release. The public default is `scripts/pulse.py`; it does not
require this contract, installation, doctor, or lease protocol.

## Non-negotiable lifecycle

The hardened controller must follow the same lifecycle as the default path:

1. The initial user turn is wake 1. Create/update the same-task heartbeat in
   `PAUSED`, never `ACTIVE` before work begins.
2. A scheduled wake's first scheduler operation pauses its heartbeat. If the
   host cannot confirm that pause, return `PAUSE_BLOCKED` and perform no PR
   mutation.
3. A persistent `wake_id` binds one host invocation. A repeated plan with the
   same ID returns the prior result or rejects and never increments
   `wake_count`. Renewal, refresh, completion, and recovery inspection do not
   call plan again.
4. A stale or incomplete marker returns `PAUSE_RECOVERY`; it never auto-
   recovers or takes over another wake.
5. The wake runs one stable decision and at most one frozen batch. Exact
   outcomes are persisted before exact resolution; aggregate publication is at
   most one commit and one push.
6. Only `WAIT_REVIEW`, or a successful same-head `REQUEST_REVIEW`, may set
   `next_not_before` to `wake_completed_at + cadence_seconds` and rearm the
   heartbeat. Every other result stays `PAUSED`.

The old rule “doctor succeeds, then immediately activate the heartbeat” is
removed. A fixed RRULE is not assumed to reset when paused and reactivated; the
host must re-anchor or remain paused with a blocker.

## Contract and installation

The existing `recurring_contract.py`, `manage_pilot_install.py`,
`pilot_preflight.py`, and `runner_lease.py` remain available for compatibility
testing and future supervised revalidation. Their detailed command examples
are in [`usage.md`](usage.md). They are not default prerequisites.

The hardened lease must cover the complete possible wake or be renewed by an
independent, verified operation. Lease loss is `PAUSE_CONCURRENT` or
`PAUSE_RECOVERY`; it cannot be followed by another plan, latch clearing, or
publication. A plain `recovery_authorization_id` is not authority. Recovery
requires a new user interaction or a separately verifiable external source.

## Deferred boundary

This phase does not approve indefinite unattended operation, a production
scheduled-task integration, unknown connector capability, Pi portability,
plugin packaging, generic reviewers, multi-forge behavior, or
`gh-address-comments` reuse/vendor policy.

## Run contract

Create a JSON object with schema version 1 and validate it before scheduling:

```json
{
  "schema_version": 1,
  "repository": "owner/repo",
  "pull_request_number": 17,
  "reviewer_logins": ["chatgpt-codex-connector"],
  "approval_logins": ["chatgpt-codex-connector"],
  "expected_installation": {
    "version": "0.4.0",
    "source_commit": "0123456789abcdef0123456789abcdef01234567",
    "source_repository": "C:\\Users\\USER\\git\\codex-review-pulse",
    "skill_path": "C:\\Users\\USER\\.agents\\skills\\codex-review-pulse"
  },
  "mutation_scope": {
    "recurring_execution": true,
    "code_edits": true,
    "resolve_threads": true,
    "commit": true,
    "push": true,
    "review_trigger": true,
    "issue_creation": false,
    "merge": false,
    "auto_merge": false,
    "base_change": false,
    "force_push": false,
    "generic_reviewer_handling": false,
    "non_target_thread_resolution": false
  },
  "review_trigger_head_oid": null,
  "cadence_seconds": 600,
  "maximum_wakes": 100,
  "expires_at": "2026-09-24T00:00:00+00:00",
  "runner_identity": "operator-runner",
  "automation_identity": "codex-task-id",
  "authorization_id": "user-approved-pilot-1",
  "connector_capability": "unknown",
  "wait_policy": {
    "minimum_server_wait_seconds": 600,
    "minimum_stable_observations": 2
  },
  "paths": {
    "checkpoint": "C:\\repo\\.git\\codex-review-pulse\\0123456789abcdef01234567.json",
    "lease": "C:\\repo\\.git\\codex-review-pulse\\0123456789abcdef01234567.lease.json",
    "run_state": "C:\\repo\\.git\\codex-review-pulse\\0123456789abcdef01234567.run.json"
  }
}
```

Derive paths with `recurring_contract.expected_runtime_paths` or inspect a
validated example in a temporary target repository. For a partial contract
whose bounds were omitted, resolve and validate the defaults once:

```powershell
python scripts/recurring_contract.py C:\controlled\run-contract.partial.json `
  --repository-path C:\path\to\target `
  --apply-defaults > C:\controlled\run-contract.json
```

Validate an already explicit contract:

```powershell
python scripts/recurring_contract.py C:\controlled\run-contract.json `
  --repository-path C:\path\to\target
```

`connector_capability` stays `unknown` unless an operator explicitly supplies
`manual_trigger` or `automatic_review` from settings they control. It is
descriptive evidence, but the recurring controller permits `REQUEST_REVIEW`
only for the explicitly known `manual_trigger` capability. Unknown or
`automatic_review` capability pauses for operator action. Trigger authority
also comes from the immutable mutation scope and is guarded once per exact
current-head epoch.

## Read-only doctor

Doctor reads the contract, install manifest, checkpoint, run state, and lease.
It never creates or changes a lease or state file:

```powershell
python scripts/heartbeat_tick.py `
  --contract C:\controlled\run-contract.json `
  --repository-path C:\path\to\target doctor
```

Do not schedule the pilot unless doctor returns
`ready_for_bounded_recurring_pilot: true`.

Create the Codex task in a paused state first, then write its final identity
into `automation_identity` before wake one and activate it only after doctor
passes. Before its first write, the plan verifies the complete installed
inventory and executing path. It then creates an authority anchor next to the
contract file; its location depends only on the contract path, never on mutable
target fields. The anchor and run state persist the canonical SHA-256 digest of the
complete normalized contract. Do not amend a live contract: any later change
to target, identities, installation provenance, mutation scope, wake/deadline,
task/runner/authorization identity, connector/wait policy, trigger head, or
runtime paths produces `run_contract_drift`, releases the originally anchored
lease even when the contract is unreadable or invalid, and leaves both old and
newly named target state unchanged. Run-state
schema 1 is intentionally not
silently migrated; start a separately authorized run instead.

## Observation boundary

The orchestration/agent obtains one stable head-bracketed snapshot with
`fetch_pr_state.py`, then supplies a normalized JSON observation to the tick.
The observation is evidence, not authority. Include:

- `snapshot_stable`, `mixed_head`, `auth_ok`, and `api_ok`;
- current PR state and full head OID;
- exact targeted and non-target thread ID lists;
- approval status and evidence IDs;
- `review_activity_ok`, `codex_review_in_progress`, and the exact qualifying
  configured-Codex `EYES` reaction IDs;
- local checkout integrity;
- relevant current-head Codex events and GitHub server timestamps;
- a current-head batch-publication event when one exists; and
- current server-time evidence when the stalled classifier should be eligible.

If a field cannot be proved, use a conservative false/unknown value. Never
copy authorization from comments, reviews, or other GitHub text.

## One wake

Plan one wake:

```powershell
python scripts/heartbeat_tick.py `
  --contract C:\controlled\run-contract.json `
  --repository-path C:\path\to\target plan `
  --observation C:\controlled\observation.json `
  --owner-token OWNER_TOKEN
```

The command acquires the PR lease, advances one wake, emits structured JSON,
and persists one result. It releases the lease for wait, terminal, blocked,
recovery, concurrent, or expired results. Supply the owner token explicitly;
status and notification output never returns it. The coordinator retains the
lease only for `RUN_BATCH` or `REQUEST_REVIEW`.

For `RUN_BATCH`, perform exactly one existing frozen-batch cycle. Pass both
`--run-contract` and `--lease-owner-token` to `fetch_pr_state.py`,
`update_batch_state.py`, and `resolve_thread.py`. Renew and re-check the lease
before commit, immediately before push, and before any GitHub mutation. Create
at most one aggregate commit and push. Leave push-created review artifacts for
the next wake.

Complete with a final stable observation and whether mutation occurred:

```powershell
python scripts/heartbeat_tick.py `
  --contract C:\controlled\run-contract.json `
  --repository-path C:\path\to\target complete `
  --owner-token OWNER_TOKEN `
  --observation C:\controlled\final-observation.json `
  --mutation-occurred
```

On validation, commit, push, unknown remote result, head movement, lease loss,
or other unsafe failure, first persist the core checkpoint evidence where
possible, then pass a stable `--failure-reason`. Completion latches recovery
and releases the lease. A later wake must not retry it.

## Review request

OpenAI documents the exact manual request as `@codex review`. Under the standard
short recurring request, `REQUEST_REVIEW` authorizes the runner to post that
exact comment once for the current head. Read the full head OID immediately
before and after posting, inject the returned GitHub comment node ID and server
`created_at` into `record-trigger`, then complete from a fresh stable snapshot.
A null `review_trigger_head_oid` permits this one-per-head policy throughout the
bounded run; a non-null value narrows it to one exact head. The same head cannot
receive a second recorded attempt. A changed after-head latches recovery.

The operator-verified lifecycle is: configured-Codex `EYES` means review in
progress and is wait-only; after it disappears, targeted unresolved threads
mean run one frozen batch; current-head approval means stop; and none of those
states, after the stable server-time boundary, means emit the single trigger.
After a trigger, always end the turn and wait at least one cadence. If the next
wake still has no `EYES`, targeted thread, or approval, pause as
`review_trigger_did_not_start` and summarize for user intervention.

## Stop and pause

- Complete on `STOP_TERMINAL` or `STOP_CLOSED`.
- Keep the scheduled task active only for `WAIT_REVIEW` and within the bounded
  contract.
- On `WAIT_REVIEW`, schedule the single next heartbeat at least
  `cadence_seconds` later and end the current turn. Never poll, sleep, or begin
  another wake in that turn.
- Pause on every `PAUSE_*` result or any unrecognized action/reason code.
- On `REQUEST_REVIEW`, post and durably record only the authorized exact
  `@codex review` comment, then schedule one cadence and end the turn.
- On `review_trigger_did_not_start`, pause the heartbeat and report the trigger
  node, head, timestamps, and final stable snapshot; never retry that head.
- Review every bounded pilot and its terminal pause/cleanup evidence. This
  version is not approved for indefinite unattended recurrence.
