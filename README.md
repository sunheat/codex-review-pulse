# Codex Review Pulse

[![CI](https://github.com/sunheat/codex-review-pulse/actions/workflows/ci.yml/badge.svg)](https://github.com/sunheat/codex-review-pulse/actions/workflows/ci.yml)

Codex Review Pulse is a GitHub-specific Codex skill for transaction-safe,
thread-aware remediation of pull requests reviewed by the GitHub Codex
connector. It treats each authoritative review snapshot as a frozen batch and
applies conservative transaction boundaries around edits, publication, and
exact thread resolution.

This project is intentionally not a generic multi-reviewer or multi-forge
framework.

## Current release-candidate scope

The core state model, immutable installation, supervised preflight, and two
manually reviewed bounded live pilots are complete. Version `0.3.1` is a
public release candidate for repeatable, finite recurring pilots on one PR:

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
  and runner-lease state;
- an explicit run contract that fixes repository, PR, identities, installed
  provenance, mutation scopes, wake budget, optional deadline, and runtime
  paths, with a canonical authority digest persisted across wakes;
- a Windows-compatible PR-scoped renewable lease with owner-checked renew and
  release plus expiration-based crash recovery;
- a pure recurring evaluator with stable next-action and reason-code enums,
  injected time, deterministic wait evidence, and durable recovery latches;
  and
- read-only doctor plus one-wake plan/complete interfaces that never create a
  Codex scheduled task or perform a live review-trigger mutation.

The controller remains an agent skill plus deterministic state and API helpers,
not a standalone daemon. Version `0.3.1` is supported for repeatable,
manually reviewed bounded pilots. Long-term unattended heartbeat execution is
not complete.

The project checks executed by CI are network-free and cover the full test suite
and publication validation on Windows and Ubuntu with Python 3.10 and 3.12.
They do not authenticate to GitHub or exercise live mutation paths.

The first supervised live pilot succeeded on `sunheat/job-hunter#2`: five
frozen Codex threads were fixed and resolved, 45 tests passed, one aggregate
commit was pushed once, and the final targeted unresolved count was zero while
current-head approval remained pending. No trigger, issue, merge, auto-merge,
base change, force-push, recurring task, or non-target resolution occurred.

A subsequent two-wake bounded recurring pilot on the same PR processed four
new exact Codex threads in two frozen batches, produced one aggregate commit
and one push per wake, exhausted its `2/2` wake budget, paused, and removed its
completed scheduled task without a third wake. See the
[operator-provided pilot record](docs/pilots/2026-08-25-job-hunter-pr-2-bounded-recurring.md).

## Requirements

- Git
- authenticated [GitHub CLI](https://cli.github.com/)
- Python 3.10 or later
- a verified independent skill installation
- one operator supervising the first bounded recurring wakes

## Use

Install an exact clean commit into OpenAI's documented user skill location
(`$HOME/.agents/skills`) without linking to the development checkout:

```powershell
$commit = git rev-parse HEAD
python skills/codex-review-pulse/scripts/manage_pilot_install.py install `
  --source-repository . `
  --source-commit $commit
python $env:USERPROFILE\.agents\skills\codex-review-pulse\scripts\manage_pilot_install.py verify `
  --expected-version 0.3.1 `
  --expected-source-commit $commit
```

Then run the read-only readiness check with the canonical repository, PR, exact
installed provenance, configured identities, and an explicit confirmation that
no other runner targets the PR:

```powershell
python $env:USERPROFILE\.agents\skills\codex-review-pulse\scripts\pilot_preflight.py `
  --repo OWNER/REPO --pr NUMBER `
  --expected-skill-version 0.3.1 `
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

For a bounded recurring pilot, first collect the exact PR, reviewer/approval
identities, cadence, maximum wakes, expiration, action-specific authority,
single-trigger choice, and notification/pause preference. Then validate a run
contract and run the read-only doctor described in
[the recurring guide](skills/codex-review-pulse/references/recurring.md). The
repository never creates or updates the Codex scheduled task itself.

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
- Acquire the PR-scoped lease before any recurring checkpoint or GitHub
  mutation and latch failures before releasing it.
- Bind the first recurring state to a canonical digest of the complete
  normalized run contract; any later authority drift pauses without rewriting
  state or retaining the lease.
- Execute at most one frozen batch per wake and stop on budget, deadline,
  concurrency, recovery, API, auth, installation, checkout, or mixed-head
  uncertainty.

No additional quiet interval is required after that terminal condition.

## Project status

See [ROADMAP.md](ROADMAP.md) for deliberately deferred milestones and
[RELATED-WORK.md](RELATED-WORK.md) for the relationship to
`djm204/codex-review`. The durable state contract is documented in
[the core state model](docs/design/core-state-model.md).
Pilot installation and readiness are documented in
[the live-pilot design](docs/design/live-pilot-readiness.md).
Bounded recurring behavior is documented in
[the recurring-heartbeat design](docs/design/recurring-heartbeat-readiness.md).

Contributions are welcome within the documented scope; see
[CONTRIBUTING.md](CONTRIBUTING.md). Report security-sensitive findings through
the private path in [SECURITY.md](SECURITY.md).

## License

Licensed under the [Apache License 2.0](LICENSE).
