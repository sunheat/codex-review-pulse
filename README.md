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
- configurable matching for the Codex approval identity;
- exact GraphQL review-thread resolution;
- one-commit/one-push batch rules, unrelated-work protection, and remote-head
  advancement checks;
- recurring execution guidance for Codex heartbeat automations; and
- a PowerShell wrapper for isolated Pi sessions under Windows Task Scheduler.

The controller is currently an agent skill plus deterministic API helpers, not
a standalone daemon. Durable checkpoints, a tested event classifier, connector
detection, and server-timestamp review triggering remain future work.

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
- Freeze unresolved thread IDs before making decisions.
- Keep unrelated and pre-existing work out of the batch.
- Publish at most one commit and one push for the batch.
- Refuse to overwrite an unexpectedly advanced PR head.
- Resolve only the exact frozen GraphQL thread IDs after successful publication.
- Stop immediately when no unresolved review threads remain and a PR-level
  thumbs-up comes from a configured Codex approval identity.

No additional quiet interval is required after that terminal condition.

## Project status

See [ROADMAP.md](ROADMAP.md) for deliberately deferred milestones and
[RELATED-WORK.md](RELATED-WORK.md) for the relationship to
`djm204/codex-review`.

## License

Licensed under the [Apache License 2.0](LICENSE).
