# Codex Review Pulse

Codex Review Pulse is a GitHub-specific Codex skill for transaction-safe,
thread-aware remediation of pull requests reviewed by the GitHub Codex
connector. It treats each authoritative review snapshot as a frozen batch and
applies conservative transaction boundaries around edits, publication, and
exact thread resolution.

This project is intentionally not a generic multi-reviewer or multi-forge
framework.

## Current release-candidate scope

The core state model is complete. The current release candidate adds the
controls needed for a first manually supervised live pilot:

- a `codex-review-pulse` Agent Skill with a frozen-batch remediation protocol;
- authoritative GitHub GraphQL retrieval of PR metadata, reviews, inline review
  threads, comments, and PR-level `THUMBS_UP` reactions;
- separate repeatable reviewer and approval identity configuration, with
  Codex-only root-author targeting and non-target reporting;
- durable, atomic current-head approval and frozen-batch recovery checkpoints,
  including commit-bound `APPROVED` review evidence without weakening the
  fail-closed reaction epoch;
- exact GraphQL review-thread resolution with repository, PR, and frozen-set
  ownership plus root-author verification;
- one-commit/one-push batch rules, unrelated-work protection, and remote-head
  advancement checks;
- a commit-pinned, independently copied installation with recorded version,
  source commit, and file hashes; and
- a structured read-only preflight for dependencies, authentication, PR state,
  thread scope, checkpoint recovery, approval ambiguity, installed provenance,
  and the single-runner prerequisite.

The controller remains an agent skill plus deterministic state and API helpers,
not a standalone daemon. A supervised pilot is the next operational step;
unattended recurring heartbeat execution is not complete.

## Requirements

- Git
- authenticated [GitHub CLI](https://cli.github.com/)
- Python 3.10 or later
- a single operator-controlled runner for the supervised pilot

## Use

Install an exact clean commit into OpenAI's documented user skill location
(`$HOME/.agents/skills`) without linking to the development checkout:

```powershell
$commit = git rev-parse HEAD
python skills/codex-review-pulse/scripts/manage_pilot_install.py install `
  --source-repository . `
  --source-commit $commit
python $env:USERPROFILE\.agents\skills\codex-review-pulse\scripts\manage_pilot_install.py verify `
  --expected-version 0.2.0 `
  --expected-source-commit $commit
```

Then run the read-only readiness check with the canonical repository, PR, exact
installed provenance, configured identities, and an explicit confirmation that
no other runner targets the PR:

```powershell
python $env:USERPROFILE\.agents\skills\codex-review-pulse\scripts\pilot_preflight.py `
  --repo OWNER/REPO --pr NUMBER `
  --expected-skill-version 0.2.0 `
  --expected-source-commit $commit `
  --reviewer-login chatgpt-codex-connector `
  --approval-login chatgpt-codex-connector `
  --single-runner-confirmed
```

Preflight never writes the checkpoint or performs a GitHub mutation. Only when
the supplied target checkout is clean, its origin fetch/push URLs match the PR
head repository, its local HEAD equals the PR head OID, and the command itself
is running from the verified installation can it return
`ready_for_supervised_pilot: true`. The operator can then invoke the installed
skill with an explicit PR and mutation scope:

```text
Use $codex-review-pulse on https://github.com/OWNER/REPO/pull/NUMBER for one
manually supervised cycle. I authorize PR-scoped fixes, one aggregate commit
and push, and exact frozen-thread resolution. Do not create issues, post a
review trigger, merge, or start recurring work.
```

The skill requires separate authorization for every external mutation. See
[the usage guide](skills/codex-review-pulse/references/usage.md) for install,
preflight, update, uninstall, and supervised-cycle commands.

## Safety model

- Fetch GraphQL review-thread state at the start of every cycle.
- Bracket connection reads with matching head-OID snapshots; discard mixed-head
  evidence.
- Freeze only targeted Codex thread IDs and the current head OID.
- Report human, unknown-author, and other-reviewer threads without mutating
  them or claiming global merge readiness.
- Keep unrelated and pre-existing work out of the batch.
- Focused-validate and resolve each exact frozen thread before aggregate
  validation and publication.
- Publish at most one commit and one push for the aggregate batch.
- Refuse to overwrite an unexpectedly advanced PR head.
- Stop immediately when no targeted Codex threads remain and either a
  commit-bound `APPROVED` review or the reaction epoch proves current-head
  approval.

No additional quiet interval is required after that terminal condition.

## Project status

See [ROADMAP.md](ROADMAP.md) for deliberately deferred milestones and
[RELATED-WORK.md](RELATED-WORK.md) for the relationship to
`djm204/codex-review`. The durable state contract is documented in
[the core state model](docs/design/core-state-model.md).
Pilot installation and readiness are documented in
[the live-pilot design](docs/design/live-pilot-readiness.md).

## License

Licensed under the [Apache License 2.0](LICENSE).
