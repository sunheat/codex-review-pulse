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
3. a recurring schedule at the requested interval;
4. the project/folder binding to the intended local repository installation;
5. the packaged Codex Review Pulse skill available to the delivery;
6. a fixed delivery prompt that names the campaign, repository, PR number, and
   the worker guide (see the launcher guide).

Storing a model or reasoning string in the campaign record is only a record of
what was requested. It is not proof that the Automation will run with those
settings. If the host does not expose a setting, cannot validate it, or rejects
it, stop and report the exact native-host limitation instead of creating the
Automation.

## Verification boundary

The deterministic helpers, state machine, and safety decisions are covered by
network-free unit tests. Native Automation creation, scheduled delivery in the
Codex app, and live GitHub mutation paths require a separately authorized
Codex-app canary. No such canary has been run from this development tree.
