# Codex Review Pulse

[![CI](https://github.com/sunheat/codex-review-pulse/actions/workflows/ci.yml/badge.svg)](https://github.com/sunheat/codex-review-pulse/actions/workflows/ci.yml)

Codex Review Pulse is a GitHub-specific Codex skill for transaction-safe,
thread-aware remediation of pull requests reviewed by the GitHub Codex
connector. It treats each authoritative review snapshot as a frozen batch and
applies conservative transaction boundaries around edits, publication, and
exact thread resolution.

This project is intentionally not a generic multi-reviewer or multi-forge
framework.

## Current product boundary

The public product is the Codex-first default path in
`skills/codex-review-pulse/scripts/pulse.py`. A normal request such as
“Automatically fix this pull request's Codex review issues until no new issues
appear” needs one PR-scoped wake at a time, not a run contract, doctor,
preflight, immutable installation, owner token, or renewable lease. The path
uses authoritative head-bracketed GraphQL state, Codex-only targeting, frozen
batches, exact resolution, one aggregate publication, and unrelated-work
protection.

Version `0.4.0` has a real black-box pilot failure and is not a publishable
final recurring release. The old heartbeat became active before wake
completion, used fixed cadence and overlapped a 26-minute wake, let a
300-second lease expire during validation, counted a second plan for one host
wake, and continued after `PAUSE_BLOCKED` by clearing a latch with a generated
recovery authorization. The old scheduled-task semantics must not be used as
the default or described as production-ready.

Version `0.7.1` is the current Codex-first default automation-policy candidate.
Its real scheduled-task and live GitHub integration remains unverified until an
independent forward test completes.

Default checkpoint writes use atomic file replacement under the target
repository's Git common directory. The checkpoint includes `wake_id`, wake
phase/timestamps, completion-relative `next_not_before`, heartbeat disposition,
review epoch, targeted batch, per-thread outcomes, resolution evidence, and
publication/recovery status. Only a final `WAIT_REVIEW`, `WAIT_RETRY`, or
successful same-head `REQUEST_REVIEW` may rearm a next wake; all stop and pause
results remain `PAUSED`.

The optional hardened files remain for compatibility testing and future
supervised revalidation. They are documented in
[`hardened.md`](skills/codex-review-pulse/references/hardened.md) and are not
default prerequisites. The default policy is designed to make unattended
operation possible, but live scheduled-task integration remains a pilot item
until independently forward-tested.

The project checks executed by CI are network-free and cover the full test suite
and publication validation on Windows and Ubuntu with Python 3.11 and 3.12.
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
- Python 3.11 or later

## Use

For the default path, invoke the installed skill normally. The installation
and preflight commands below are optional hardened-mode operations only; they
must not be presented as a hardened prerequisite for using the default policy.

To select a commit-pinned user-level default installation from Git Bash after
committing this repository, run:

```bash
commit=$(git rev-parse HEAD)
python skills/codex-review-pulse/scripts/manage_pilot_install.py update \
  --source-repository . \
  --source-commit "$commit"
python "$HOME/.agents/skills/codex-review-pulse/scripts/manage_pilot_install.py" verify \
  --expected-version 0.7.1 \
  --expected-source-commit "$commit"
```

The installed copy is independent and immutable; the target repository may
continue to evolve without changing the running default until another explicit
update is performed.

To opt into that advanced mode, install an exact clean commit into OpenAI's
documented user skill location (`$HOME/.agents/skills`) without linking to the
development checkout:

```powershell
$commit = git rev-parse HEAD
python skills/codex-review-pulse/scripts/manage_pilot_install.py install `
  --source-repository . `
  --source-commit $commit
python $env:USERPROFILE\.agents\skills\codex-review-pulse\scripts\manage_pilot_install.py verify `
  --expected-version 0.7.1 `
  --expected-source-commit $commit
```

Then run the read-only readiness check with the canonical repository, PR, exact
installed provenance, configured identities, and an explicit confirmation that
no other runner targets the PR:

```powershell
python $env:USERPROFILE\.agents\skills\codex-review-pulse\scripts\pilot_preflight.py `
  --repo OWNER/REPO --pr NUMBER `
  --expected-skill-version 0.7.1 `
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

The default short request authorizes the autonomous PR-scoped loop described
below. The optional hardened path still requires its own explicit scope. See
[the usage guide](skills/codex-review-pulse/references/usage.md) for install,
preflight, update, uninstall, and supervised-cycle commands.

For the default path, this short request selects the autonomous mutation scope
and one-wake-at-a-time lifecycle. It remains active across completion-relative
wakes until the review reaches a Codex-specific stop condition:

```text
Automatically fix this PR's Codex review issues until no new issues appear:
https://github.com/OWNER/REPO/pull/NUMBER
```

When the CLI is used directly from that PR checkout, `pulse.py` can infer the
repository and PR number, so `--repo` and `--pr` are optional. It resolves the
target before computing the Git-common-directory checkpoint path and rejects a
later target mismatch instead of drifting to another PR. An ambiguous checkout
must be stopped and supplied an explicit `--repo OWNER/REPO --pr NUMBER`.

The host keeps the heartbeat `PAUSED` while a wake is running and reanchors
only a later `WAIT_REVIEW`, `WAIT_RETRY`, or successful same-head
`REQUEST_REVIEW` to the first scheduler-representable instant at or after
`wake_completed_at + cadence_seconds`. The persisted first run may be equal to
or at most one second later than that ordered target; unordered timestamp
comparisons allow one second on either side. The default policy has no
wake/deadline/retry budget; prompt-supplied limits are persisted and stop with
`STOP_POLICY_LIMIT`. All other stop, pause, recovery, closed, expired, and
unknown results remain paused. See the
[default Skill](skills/codex-review-pulse/SKILL.md).

Any number of pushes completed before the next wake are intentionally
coalesced: the next stable snapshot processes only the latest observed head.
Once a batch freezes its head and exact thread IDs, any later head change
pauses recovery instead of being folded into that active batch.

## Safety model

- Fetch GraphQL review-thread state at the start of every cycle.
- Bracket connection reads with matching head-OID snapshots; discard mixed-head
  evidence.
- Treat a PR-level `EYES` reaction from the configured Codex identity as
  wait-only evidence that review is in progress. Wait for its removal before
  freezing a batch; never treat it as approval.
- Freeze only targeted Codex thread IDs and the current head OID.
- Report human, unknown-author, and other-reviewer threads without mutating
  them or claiming global merge readiness.
- Keep unrelated and pre-existing work out of the batch.
- Focused-validate and resolve each exact frozen thread before aggregate
  validation and publication. In autonomous mode, repair a stale PR-scoped
  test or implementation defect and retry recoverable failures instead of
  stopping at the first red check; repeated no-progress failures still pause.
- Publish at most one commit and one push for the aggregate batch.
- Refuse to overwrite an unexpectedly advanced PR head.
- Stop immediately when no targeted Codex threads remain and either a
  commit-bound `APPROVED` review or the reaction epoch proves current-head
  approval.
- Persist one PR-scoped in-progress marker per default wake through atomic
  checkpoint replacement; it is not a cross-process lock or compare-and-set
  mechanism. Default operation assumes one host/task runner per PR, and stale
  or incomplete markers pause for recovery rather than being claimed
  automatically.
- The hardened PR-scoped lease and canonical run-contract digest remain
  optional advanced-mode controls, not default prerequisites. Default wake,
  deadline, and retry limits are unbounded unless the prompt supplies them.
- Execute at most one frozen batch per wake and stop on recovery, API,
  authentication, checkout, or mixed-head uncertainty. Unattended operation
  also depends on the host granting network, workspace, and non-interactive
  approval capabilities.
- After the default idle boundary, allow at most one safely bracketed
  `@codex review` request per head. End that turn and wait a
  completion-relative cadence; if the next wake still has no Codex `EYES`,
  targeted thread, or current-head approval, pause and report the evidence.

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
