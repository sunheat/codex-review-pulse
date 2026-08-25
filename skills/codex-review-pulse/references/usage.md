# Usage

## Release-candidate boundary

The core model, immutable installation, supervised preflight, and manually
reviewed one- and two-wake live pilots are complete. Version `0.3.1` supports
both a supervised single cycle and repeatable bounded recurring pilots. It
does not support indefinite unattended operation, plugin packaging, or
validated Pi portability.

Use a clean source repository and an exact release-candidate commit throughout
the commands below. OpenAI documents `$HOME/.agents/skills` as the user-level
local skill location; the install manager uses it by default. Codex normally
detects skill changes automatically, but OpenAI recommends restarting Codex if
an update does not appear.

Official reference:
[Build skills](https://learn.chatgpt.com/docs/build-skills#where-codex-loads-local-skills).

## Install an immutable-provenance copy

From the clean `codex-review-pulse` source repository on Windows:

```powershell
$commit = git rev-parse HEAD
python skills/codex-review-pulse/scripts/manage_pilot_install.py install `
  --source-repository . `
  --source-commit $commit
```

This extracts `skills/codex-review-pulse` from the named Git commit into
`$env:USERPROFILE\.agents\skills\codex-review-pulse`. It does not copy the
mutable working-tree files and does not create a symlink. The installed
manifest records version `0.3.1`, the full source commit, and SHA-256 file
hashes. Verification independently reconstructs that inventory from the pinned
Git commit, so changing both an installed file and its adjacent manifest does
not reauthorize the modified bytes.

The install fails if the source is dirty, the commit is missing, the commit
does not contain the skill/version, or the target already exists. Use
`--skills-root C:\controlled\temporary\skills` only for isolated testing or an
explicit alternate configured location.

## Verify

```powershell
python $env:USERPROFILE\.agents\skills\codex-review-pulse\scripts\manage_pilot_install.py verify `
  --expected-version 0.3.1 `
  --expected-source-commit $commit
```

Verification fails on version or source-commit mismatch, unavailable pinned
source provenance, manifest/source inventory disagreement, missing/extra or
changed files, an invalid manifest, or a symlink anywhere in the installed
tree. Do not continue a pilot after verification failure.

## Read-only pilot preflight

Confirm manually that no Codex task, terminal, scheduler, or other person is
running this skill against the same pull request. Then run:

```powershell
python $env:USERPROFILE\.agents\skills\codex-review-pulse\scripts\pilot_preflight.py `
  --repo OWNER/REPO `
  --pr NUMBER `
  --repository-path C:\path\to\target-repository `
  --expected-skill-version 0.3.1 `
  --expected-source-commit $commit `
  --reviewer-login chatgpt-codex-connector `
  --approval-login chatgpt-codex-connector `
  --single-runner-confirmed
```

`--single-runner-confirmed` remains operator evidence for the supervised mode;
it is not automatic scheduler discovery. Preflight also inspects and reports
the PR lease without creating, renewing, recovering, or deleting it. Omission
of the supervised confirmation or an active/invalid lease blocks readiness.

Preflight checks Python, Git, GitHub CLI, authentication, canonical repository,
PR/head state, configured identities, targeted and non-target threads,
checkpoint schema and recovery state, approval evidence/ambiguity, installed
provenance, and the single-runner prerequisite. It also requires the supplied
target checkout to be clean, its local HEAD to equal the bracketed PR head OID,
and the `origin` fetch and push URLs to identify the PR head repository. The
running preflight script itself must be inside the verified installation. It
emits JSON and exits nonzero when a readiness blocker exists.

With no root override, preflight verifies the exact skill directory containing
the executing script. This supports isolated commit-pinned installations while
an older default installation remains untouched. `--install-root` and
`--skills-root` are equivalent parent-root overrides; supplying different
values is an error.

Preflight never resolves a thread, posts a comment, creates an issue, commits,
pushes, or writes the checkpoint. If its in-memory evaluation would establish
or advance an epoch, it reports `checkpoint_would_change: true`; the formal
state-fetch command must perform that write after the supervised cycle is
explicitly authorized.

## Start one supervised cycle

Only after preflight returns `ready_for_supervised_pilot: true`, invoke the
installed skill with an exact PR and action-specific authority:

```text
Use $codex-review-pulse on https://github.com/OWNER/REPO/pull/NUMBER for one
manually supervised cycle. Reviewer and approval identities are
chatgpt-codex-connector. I authorize PR-scoped fixes, one aggregate commit and
push, and exact frozen-thread resolution. Do not create issues, post a review
trigger, merge, enable auto-merge, change the base, or start recurring work.
```

Change that scope only when the operator explicitly wants the additional
mutation. Preflight success alone authorizes none of them.

The formal state fetch is:

```powershell
python $env:USERPROFILE\.agents\skills\codex-review-pulse\scripts\fetch_pr_state.py `
  --repo OWNER/REPO `
  --pr NUMBER `
  --repository-path C:\path\to\target-repository `
  --reviewer-login chatgpt-codex-connector `
  --approval-login chatgpt-codex-connector
```

Unlike preflight, this command atomically persists the approval epoch and
latest stable target snapshot in the target repository's Git common directory.
Do not delete the checkpoint to make an existing reaction appear fresh.

Freeze only IDs returned by that stable snapshot, then persist an outcome
before resolving each exact thread:

```powershell
python $env:USERPROFILE\.agents\skills\codex-review-pulse\scripts\update_batch_state.py `
  --repo OWNER/REPO --pr NUMBER `
  --repository-path C:\path\to\target-repository `
  freeze --head-oid HEAD_OID --thread-id THREAD_ID
python $env:USERPROFILE\.agents\skills\codex-review-pulse\scripts\update_batch_state.py `
  --repo OWNER/REPO --pr NUMBER `
  --repository-path C:\path\to\target-repository `
  record-outcome --thread-id THREAD_ID --classification fix-now `
  --reference "focused checks passed"
python $env:USERPROFILE\.agents\skills\codex-review-pulse\scripts\resolve_thread.py `
  THREAD_ID --repo OWNER/REPO --pr NUMBER `
  --repository-path C:\path\to\target-repository
```

If bracketing head reads differ, discard the snapshot and stop. Do not freeze
mixed-head evidence. Report non-target threads without resolving them. Stop on
an unfinished or failed batch and recover it before freezing another.

## Approval diagnostics

A configured identity's `APPROVED` review with `commit.oid == headRefOid` is
direct current-head proof. PR-level thumbs-up reactions remain epoch based:

- a reaction present at cold start is ambiguous;
- a reaction carried into the first observation of a new head is ambiguous;
- seeing the same reaction ID again is not a new event;
- a newly observed ID on an already established unchanged head is proof while
  it remains present; and
- deleting and re-adding a reaction can produce a new observed ID, but only the
  established epoch ordering makes that new object current-head proof.

Do not infer approval ordering from PR `updatedAt`, commit `authoredDate`, or a
reaction `createdAt` alone.

## Update

Commit the new release candidate and return the source repository to a clean
state. Then:

```powershell
$newCommit = git rev-parse HEAD
python $env:USERPROFILE\.agents\skills\codex-review-pulse\scripts\manage_pilot_install.py update `
  --source-repository . `
  --source-commit $newCommit
python $env:USERPROFILE\.agents\skills\codex-review-pulse\scripts\manage_pilot_install.py verify `
  --expected-version NEW_VERSION `
  --expected-source-commit $newCommit
```

Update verifies ownership and integrity of the current installation before an
atomic directory swap. It refuses a dirty source or modified installation.

## Uninstall

```powershell
python $env:USERPROFILE\.agents\skills\codex-review-pulse\scripts\manage_pilot_install.py uninstall
```

Uninstall first verifies the fixed target's manifest and complete inventory.
It removes only the managed `codex-review-pulse` directory and refuses an
unverified or foreign directory.

## Deferred modes

For a finite recurring pilot, follow [recurring.md](recurring.md). Do not start
an indefinite unattended automation or Windows scheduled task. Public-API
connector detection bound to the current head, long-term unattended approval,
plugin packaging, Pi portability, generic reviewers/multi-forge behavior, and
`gh-address-comments` integration remain deferred.
