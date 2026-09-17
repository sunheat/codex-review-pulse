# Manual recovery

The ownership lock is **permanent**: no TTL, expiry, heartbeat, renewal, or
automatic stale detection. It is removed only by its owner after a clean
outcome, or by an explicit user-authorized recovery.

## Inspect

```text
python scripts/lock.py inspect --repo OWNER/REPO --pr NUMBER
```

- `absent` — no owner;
- `active` — a live lock exists (the inspect output never shows the token);
- `invalid` — a corrupt or incomplete lock exists. It still blocks every
  worker and is recovered through the same human process.

## When recovery is safe

Clearing a lock is safe only when the previous owner can no longer mutate the
campaign, scheduler, Git, or GitHub state. Confirm that no Codex delivery,
shell, worktree process, or automation for this campaign is still running.

An interrupted attempt has already consumed its round; recovery does not
refund rounds or restore a per-head review-request allowance. The next delivery
starts from fresh authoritative GitHub and Git state. It does not resume an
interrupted batch, worktree, or reasoning.

## Recover

```text
python scripts/lock.py recover --repo OWNER/REPO --pr NUMBER --user-authorized-recovery --expected-campaign-id CRPCAMPAIGNID
```

The expected-campaign guard is mandatory for any lock with a readable
campaign identity — valid or corrupt-but-parseable — and prevents
accidentally clearing a replacement owner's lock. Recovery deletes only the
lock; it never modifies the campaign record. There is no non-interactive
shortcut and this command must never be called automatically by a worker.

Exact rules:

- Lock absent: a harmless no-op; no expected identity is needed.
- Valid lock: `--expected-campaign-id` is required and must match exactly.
- Corrupt but parseable lock with a readable campaign id: the raw id grants
  no authority and need not satisfy campaign-id syntax; it is used only as a
  conservative mismatch guard, so `--expected-campaign-id` is still required
  and must equal the raw id exactly.
- Unreadable lock (not JSON, not an object, or no usable campaign id): a
  supplied `--expected-campaign-id` can never be verified, so recovery
  refuses. Omitting it removes the lock only after you established that the
  previous owner can no longer mutate.

Partial rollover state (`lock = C2`, `campaign = terminal C1`) recovers with
`--expected-campaign-id C2`, because the expected identity refers to the lock
being removed. The terminal C1 record stays in place afterwards; it may then
be preserved, rolled over, or retired.

If the campaign record itself is malformed or unsupported, leave the file in
place for inspection (`<git-common-dir>/codex-review-pulse/v2/`). A runtime
that does not understand the state must not overwrite it.

## After recovery

Recovery does not by itself resume anything. What happens next depends on the
durable campaign state.

- **Active scheduler-setup ambiguity**: when the campaign stays active, the
  only ambiguity concerns the native recurring Automation, no durable
  RESERVED ambiguity or other campaign-level blocker exists, the previous
  launcher and every mutation-capable child can no longer continue, the
  intended compliant recurring Automation exists, the required effective
  configuration is established, and the earlier ambiguous setup cannot later
  create or reconfigure another Automation, lock-only recovery is a resume:
  remove only the expected lock, preserve the active campaign, and a later
  matching delivery acquires and continues.
- **Terminal `ambiguous_interruption`**: the campaign is durably terminal and
  there is no ordinary resume. Lock-only recovery removes only the expected
  lock; it does not reactivate the campaign, refund a round, restore request
  allowance, or authorize another effective action. To start a new campaign,
  explicitly retire the terminal one, then perform normal setup.
- **Active RESERVED ambiguity**: an active campaign that carries an
  unresolved durable RESERVED request guard is not safely resumed merely by
  deleting its lock. The next worker will acquire, detect the campaign-wide
  RESERVED ambiguity, terminalize as `ambiguous_interruption`, and retain
  ownership again. Prefer explicit campaign retirement after quiescence
  rather than a pointless recover/acquire/reterminalize cycle; retirement is
  never automatic.

There is no automatic choice between resume and retirement. If you want to
stop all deliveries, also pause or delete the bound native Codex Automation
using the Codex app controls.

## Campaign retirement

Retirement is the explicit human handling path for a valid supported campaign
that must not continue: an active interrupted campaign, an active
fully-consumed campaign the user chooses not to continue, `ambiguous_interruption`,
`manual_intervention_required`, `request_creation_failed`, `target_unavailable`,
`hard_failed`, an early `succeeded` campaign with unused rounds, or an unused
active scheduler-setup orphan. It is never automatic.

Before retiring, confirm that every previous owner and product-mutating
operation for this campaign can no longer continue. Then choose one mode:

Retained-lock retirement, when the campaign's valid matching permanent lock
still exists:

```text
python scripts/campaign.py retire --repo OWNER/REPO --pr NUMBER --expected-campaign-id CRPCAMPAIGNID --retained-lock --user-authorized-retirement
```

Lock-absent retirement, after lock recovery or when no permanent lock remains:

```text
python scripts/campaign.py retire --repo OWNER/REPO --pr NUMBER --expected-campaign-id CRPCAMPAIGNID --lock-absent --user-authorized-retirement
```

Retirement deletes the campaign record (and, in retained-lock mode, the
matching lock after it). Afterwards old or late deliveries are stale: they
cannot recreate the retired campaign or regain ownership, and a later launcher
may create a new campaign through normal setup. Retiring a rolled-over C2
campaign never restores or reconstructs C1.

Retirement must not be used to bypass protection boundaries:

- An invalid lock, a lock belonging to another campaign, or a
  partial-transition mismatch is refused; recover the lock explicitly first.
- Lock-absent retirement refuses while any permanent lock file exists.
- Malformed or unsupported campaign state is preserved for inspection and is
  never silently retired.
- Native Automation cleanup remains a separate best-effort step in the Codex
  app; no retirement step depends on it succeeding.

## Abandoned worktrees

A temporary worktree left by an interrupted delivery is residue, not
transactional state:

```text
git worktree list
python scripts/gitlocal.py worktree-remove --path PATH --repo OWNER/REPO --pr NUMBER --owner-token OWNER_TOKEN
```

A later delivery creates a fresh worktree and never resumes an abandoned one.

## Campaign terminal states

| Status | Meaning | Lock after reaching it |
| --- | --- | --- |
| `succeeded` | Applicable current-head approval and zero applicable unresolved threads | released |
| `review_completed_without_approval` | Eligible post-request completion, no approval or feedback | released |
| `codex_review_service_unresponsive` | Response grace elapsed (at least the configured interval, never under 20 minutes), complete evidence shows no Codex response | released |
| `rounds_exhausted` | No effective round remains and no asynchronous result is pending | released |
| `request_creation_failed` | Request creation definitively failed with proven absence of a comment | released |
| `manual_intervention_required` | E.g. unbracketed request, inconclusive temporal evidence, no automatic action remains | released |
| `target_unavailable` | PR closed/merged or another definite platform condition | released |
| `hard_failed` | Positively identified unsupported execution environment (`unsupported_execution_mode`, `insufficient_effective_access`) or explicit host authorization denial (`host_authorization_denied`); entire remaining round budget forfeited | released |
| `ambiguous_interruption` | A product mutation may or may not have happened | **retained** |
