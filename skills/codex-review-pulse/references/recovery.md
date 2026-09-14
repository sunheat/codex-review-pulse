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

The expected-campaign guard prevents accidentally clearing a replacement
owner's lock. Recovery deletes only the lock; it never modifies the campaign
record. There is no non-interactive shortcut and this command must never be
called automatically by a worker.

If the campaign record itself is malformed or unsupported, leave the file in
place for inspection (`<git-common-dir>/codex-review-pulse/v2/`). A runtime
that does not understand the state must not overwrite it.

## After recovery

The next scheduled delivery re-acquires the lock and reconstructs authority.
If you want to stop all deliveries, also pause or delete the bound native Codex
Automation using the Codex app controls.

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
| `codex_review_service_unresponsive` | At least one interval elapsed, complete evidence shows no Codex response | released |
| `rounds_exhausted` | No effective round remains and no asynchronous result is pending | released |
| `request_creation_failed` | Request creation definitively failed with proven absence of a comment | released |
| `manual_intervention_required` | E.g. unbracketed request, inconclusive temporal evidence, no automatic action remains | released |
| `target_unavailable` | PR closed/merged or another definite platform condition | released |
| `ambiguous_interruption` | A product mutation may or may not have happened | **retained** |
