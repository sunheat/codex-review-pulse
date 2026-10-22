# Manual recovery

The ownership lock is **permanent**: no TTL, expiry, heartbeat, renewal, or
automatic stale detection. It is removed only by its owner after a clean
outcome, or by an explicit user-authorized recovery.

## Inspect

```text
python scripts/lock.py inspect --repo OWNER/REPO --pr NUMBER
```

- `absent` — no owner;
- `active` — a live lock exists (the inspect output never shows the token)
  and carries `recovery_witness` (`sha256:<hex>` over the exact raw lock
  bytes);
- `invalid` — a corrupt or incomplete lock exists. It still blocks every
  worker and is recovered through the same human process. When its raw
  bytes are readable it also carries `recovery_witness`, even for empty
  bytes, malformed JSON, invalid UTF-8, non-object JSON, or semantically
  invalid lock JSON. Actual raw I/O failure carries no witness.

The witness is a compare-and-delete precondition over raw lock contents,
not a persistent file-instance identity and not ownership authority. It
does not prove the previous owner is dead and never substitutes for human
quiescence determination.

## When recovery is safe

Clearing a lock is safe only when the previous owner can no longer mutate the
campaign, scheduler, Git, or GitHub state. Confirm that no Codex delivery,
shell, worktree process, or automation for this campaign is still running.

An interrupted attempt has already consumed its round; recovery does not
refund rounds or restore a per-head review-request allowance. The next delivery
starts from fresh authoritative GitHub and Git state. It does not resume an
interrupted batch, worktree, or reasoning.

## Retained-lock quarantine states

The original owning delivery may have attempted a best-effort quarantine
before stopping (see the worker guide, section 9). The bound native Automation
may therefore be in any of these states:

- **confirmed paused** by the original delivery's single pause attempt;
- **not paused**, because automatic quarantine was unavailable or the pause
  attempt was definitively rejected;
- **unknown pause state**, because the pause attempt timed out or returned an
  unparseable result;
- **untouched**, because the host never exposed an exact native self identity.

The quarantine never releases or recovers the lock, never modifies the
campaign, and never refunds a round. Before any recovery, you must still
establish that the previous owner and every mutation-capable child or
in-flight operation can no longer continue; a paused Automation is one
possible quiescence signal, but an unknown or unpaused state means you must
establish quiescence by the existing rules.

## Recover

```text
python scripts/lock.py inspect --repo OWNER/REPO --pr NUMBER
python scripts/lock.py recover --repo OWNER/REPO --pr NUMBER --user-authorized-recovery --expected-campaign-id CRPCAMPAIGNID --expected-lock-witness sha256:HEX
```

Manual recovery only removes the authorized abandoned lock. It never
resumes work or creates a successor worker. Any continuation occurs through
a later ordinary scheduled delivery.

The expected-campaign guard and the raw lock-content witness protect
different things: the campaign id proves the operator is acting on the
intended campaign, while the witness proves the current lock contents
still match the lock the operator inspected. The witness is computed from
the exact raw bytes as stored (`sha256:<64 lowercase hex>`), without
parsing, reserialization, whitespace normalization, field selection, or
exposing `owner_token`.

For normally initialized acquisitions each owner persists a fresh random
`owner_token`, so a normally initialized successor owner produces different
raw contents and a different witness. A stale recovery carrying the old
witness therefore refuses against the successor. Content-identical damaged
lock creations (for example two empty files) are outside that guarantee;
their recovery remains an explicit human operation requiring quiescence.

Recovery deletes only the lock; it never modifies the campaign record.
There is no non-interactive shortcut and this command must never be called
automatically by a worker.

Exact rules:

- Lock absent: Absent-lock recovery must not create or modify campaign
  state, create a permanent ownership lock, or trigger work. Creation or
  opening of the existing canonical guard sidecar is permitted. Once it
  observes absence under the guard, that invocation must return without
  scheduling any later deletion or affecting a subsequently acquired lock.
- Valid lock: `--expected-campaign-id` and `--expected-lock-witness` are
  both required and must match exactly.
- Corrupt but parseable lock with a readable campaign id: the raw id grants
  no authority and need not satisfy campaign-id syntax; it is used only as a
  conservative mismatch guard, so `--expected-campaign-id` is still required
  and must equal the raw id exactly, and the matching `--expected-lock-witness`
  is also required.
- Readable raw contents with no usable campaign id (empty file, malformed
  JSON, invalid UTF-8, non-object JSON, or missing/empty campaign id): a
  supplied `--expected-campaign-id` can never be verified, so recovery
  refuses. The matching `--expected-lock-witness` is required; omitting it
  removes nothing. Omitting the campaign id removes the lock only after you
  established that the previous owner can no longer mutate.
- Raw bytes that cannot actually be read (for example permission or other
  `OSError` failures): recovery refuses, preserves the lock, and never
  unlinks, even when a previously captured witness is supplied. A previously
  observed witness cannot authorize deleting contents that recovery cannot
  currently re-observe and compare. Here “unreadable” means actual raw I/O
  failure, not merely invalid JSON content.

Never auto-fill `--expected-lock-witness` from the lock observed at
recovery time. That would silently convert authorization for an old lock
into authorization to delete whatever lock exists now.

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

**When preserving an active campaign** (where the rules above permit a
lock-only recovery as a resume):

1. inspect the exact native Automation and establish that continuation is
   safe;
2. recover the exact permanent lock using the identity rules above;
3. explicitly resume the same exact Automation if the quarantine paused it —
   an explicitly resumed Automation is not an automatic resume;
4. never refund a consumed round and never resume abandoned in-memory
   reasoning, a frozen batch, or an abandoned worktree; the next delivery
   starts from fresh authoritative state.

**When abandoning the campaign**: perform the explicit campaign retirement
below; only after campaign authority is removed, delete or otherwise clean the
exact native Automation best effort using the Codex app controls.

## Campaign retirement

Retirement is the explicit human handling path for a valid supported campaign
that must not continue: an active interrupted campaign, an active
fully-consumed campaign the user chooses not to continue, `ambiguous_interruption`,
`manual_intervention_required`, `request_creation_failed`, `target_unavailable`,
`hard_failed`, an early `succeeded` campaign with unused rounds, or an unused
active scheduler-setup orphan. It is never automatic.

Before retiring, confirm that every previous owner and product-mutating
operation for this campaign can no longer continue. For retained-lock
retirement, inspect the exact lock to be removed and retain its
`recovery_witness`; do not obtain the witness again when running the retirement
command. Then choose one mode:

Retained-lock retirement, when the campaign's valid matching permanent lock
still exists:

```text
python scripts/lock.py inspect --repo OWNER/REPO --pr NUMBER
python scripts/campaign.py retire --repo OWNER/REPO --pr NUMBER --expected-campaign-id CRPCAMPAIGNID --expected-lock-witness sha256:HEX --retained-lock --user-authorized-retirement
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

Retained-lock retirement compares the supplied witness with the current raw
lock contents under the canonical guard before deleting either file. A
replacement owner in the same campaign changes the witness and makes the
retirement refuse without deleting the campaign or lock.

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
| `succeeded` | Codex PR-level THUMBS_UP (no commit linkage required) or a current-head approved review, and zero applicable unresolved threads | released |
| `review_completed_without_approval` | Eligible post-request completion, no approval or feedback | released |
| `codex_review_service_unresponsive` | Response grace elapsed (at least the configured interval, never under 20 minutes), complete evidence shows no Codex response | released |
| `rounds_exhausted` | No effective round remains and no asynchronous result is pending | released |
| `request_creation_failed` | Request creation definitively failed with proven absence of a comment | released |
| `manual_intervention_required` | E.g. unbracketed request, inconclusive temporal evidence, no automatic action remains | released |
| `target_unavailable` | PR closed/merged or another definite platform condition | released |
| `hard_failed` | Positively identified unsupported execution environment (`unsupported_execution_mode`, `insufficient_effective_access`) or explicit host authorization denial (`host_authorization_denied`); entire remaining round budget forfeited | released |
| `ambiguous_interruption` | A product mutation may or may not have happened | **retained** |
