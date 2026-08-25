# Codex Review Pulse

Codex Review Pulse is a GitHub-specific Codex skill for transaction-safe,
thread-aware remediation of pull requests reviewed by the GitHub Codex
connector. It treats
each authoritative review snapshot as a frozen batch and applies conservative
transaction boundaries around edits, publication, and exact thread resolution.

This project is intentionally not a generic multi-reviewer or multi-forge
framework.

## Current baseline

The initial public baseline provides:

- a `codex-review-pulse` Agent Skill with a frozen-batch remediation protocol;
- authoritative GitHub GraphQL retrieval of PR metadata, reviews, inline review
  threads, comments, and PR-level `THUMBS_UP` reactions;
- separate repeatable reviewer and approval identity configuration, with
  Codex-only root-author targeting and non-target reporting;
- durable, atomic current-head approval and frozen-batch recovery checkpoints;
- exact GraphQL review-thread resolution with repository, PR, and frozen-set
  ownership plus root-author verification;
- one-commit/one-push batch rules, unrelated-work protection, and remote-head
  advancement checks;
- recurring execution guidance for Codex heartbeat automations; and
- a PowerShell wrapper for isolated Pi sessions under Windows Task Scheduler.

The controller is currently an agent skill plus deterministic state and API
helpers, not a standalone daemon. Connector detection, broader event
classification, and server-timestamp review triggering remain future work.

## Requirements

- Git
- authenticated [GitHub CLI](https://cli.github.com/)
- Python 3.10 or later
- PowerShell 7 and [Pi](https://github.com/badlogic/pi-mono) for the optional
  Windows scheduled runner

## Use

Copy `skills/codex-review-pulse` into a skill directory recognized by your
agent, then invoke it with an explicit PR and mutation scope:

```text
Use $codex-review-pulse on https://github.com/OWNER/REPO/pull/NUMBER.
I authorize PR-scoped fixes, commits, pushes, exact thread resolution, and one
Codex review trigger comment. Do not merge.
```

The skill requires separate authorization for issue creation, recurring
execution, or other external mutations. See
[the usage guide](skills/codex-review-pulse/references/usage.md) for Codex
heartbeat and Pi scheduling examples.

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
- Stop immediately when no targeted Codex threads remain and a checkpoint
  proves a qualifying PR-level thumbs-up belongs to the current head epoch.

No additional quiet interval is required after that terminal condition.

## Project status

See [ROADMAP.md](ROADMAP.md) for deliberately deferred milestones and
[RELATED-WORK.md](RELATED-WORK.md) for the relationship to
`djm204/codex-review`. The durable state contract is documented in
[the core state model](docs/design/core-state-model.md).

## License

Licensed under the [Apache License 2.0](LICENSE).
