# Setup and host requirements

## Runtime host

The end-to-end product runs only in the Codex app / Codex desktop runtime:

- a packaged Agent Skill installation of Codex Review Pulse;
- native **Codex Automations** for the single fixed recurring delivery;
- a Codex app project (folder/workspace) bound to the local clone of the target
  repository.

Codex CLI, Pi, OMP, ChatGPT scheduled tasks, cron, GitHub Actions, Windows Task
Scheduler, and generic schedulers are not supported hosts. Do not substitute
one when native Automations are unavailable.

## Local requirements

- Git 2.36 or newer, with the target repository cloned locally and an `origin`
  remote that matches the PR base repository. Same-repository pull requests
  only; fork-PR publication is not implemented. (The worktree helpers use
  `git worktree list --porcelain -z`, added in Git 2.36.)
- Python 3.11 or newer. Commands are written with `python`; on hosts where only
  `python3` exists, substitute it.
- Authenticated [GitHub CLI](https://cli.github.com/): run `gh auth status`.
  The authenticated identity needs read access to the PR and permission to push
  the PR head branch, resolve review threads, create issues, and post comments.
- This skill installed into the Codex app's Agent Skills location.

## Runtime artifacts

All campaign state is stored under the repository's Git common directory and is
shared by every worktree of that installation:

```text
<git-common-dir>/codex-review-pulse/v2/<digest>.campaign.json
<git-common-dir>/codex-review-pulse/v2/<digest>.lock
<git-common-dir>/codex-review-pulse/v2/worktrees/<digest>/...
```

Nothing is stored per worktree, in tracked files, or inside the installed skill
directory. Remediation runs in detached temporary worktrees; the user's primary
worktree is never modified.

## Native configuration that must really exist

A campaign may start only when the native Codex Automation can be configured
with all of the following and the Codex app accepts that configuration:

1. the worker model;
2. the reasoning / thinking level;
3. a recurring schedule at the requested interval, recurring indefinitely —
   never an occurrence limit (RRULE `COUNT`, end date, or similar) derived
   from the effective-round budget, because delivery count is not round count;
4. the project/folder binding to the intended local repository installation;
5. the packaged Codex Review Pulse skill available to the delivery; and
6. a fixed delivery prompt whose first nonblank line is
   `$codex-review-pulse`, then names the campaign, repository, PR number, and
   the worker guide (see the launcher guide).

The `$codex-review-pulse` marker is the supported explicit skill invocation for
a scheduled task. A native Automation need not expose a separate skill-
attachment field when it accepts that exact prompt. If a host neither accepts
the explicit invocation nor exposes an equivalent validated attachment, stop
and report the native-host limitation.

Storing a model or reasoning string in the campaign record is only a record of
what was requested. It is not proof that the Automation will run with those
settings. If the host does not expose a setting, cannot validate it, or rejects
it, stop and report the exact native-host limitation instead of creating the
Automation.

## Native setup-result classification

The launcher classifies the recurring-Automation setup only from authoritative
native-host operation results:

- **Confirmed compliant** — the host definitively reports successful creation
  and every required setting is established by a validated input to the
  successful native operation or by authoritative output/readback. A
  successful validated native creation is authoritative without a mandatory
  separate readback.
- **Definitive failure** — the host definitively rejects creation, or
  authoritative output proves a required setting is wrong, unsupported, or
  bound to the wrong target.
- **Ambiguous** — the result cannot establish whether creation occurred, the
  operation may still complete later, host output is incomplete or
  contradictory, or a required effective setting is neither established by the
  operation contract nor observable. Locally stored requested values,
  conversation text, model confidence, UI assumptions, or reconstructed
  configuration are not authoritative native-host evidence.

There is no scheduler adapter, receipt, polling, retry phase, or
reconciliation mechanism that turns ambiguity into confirmation. Ambiguous
setup preserves the campaign and lock fail closed for explicit human recovery.

## Verification boundary

The deterministic helpers, state machine, owned boundaries, and Phase 3
externalization boundaries are covered by network-free unit tests. Native
Automation creation, scheduled delivery in the Codex app, and live GitHub
mutation paths require a separately authorized Codex-app canary. No such
canary has been run from this development tree, so the runtime is not yet
canary-validated.
