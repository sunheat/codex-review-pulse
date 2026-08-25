# Bounded recurring pilot: `sunheat/job-hunter#2`

## Evidence status

This record summarizes structured evidence reported by the supervising
operator after a live Codex Review Pulse `0.3.0` pilot on 2026-08-25. The
repository maintainers did not independently replay the live GitHub mutations
or scheduled-task event stream. The evidence is therefore suitable for the
bounded release claim below, but not proof of indefinite unattended safety or
of a public connector-status contract.

The pilot used the commit-pinned source
`b91eb0d751bcc2261f2be8821723523b870e5e7b`. Its alternate installation passed
manifest, inventory, version, source-commit, and file-hash verification and the
runtime reported that it was executing from that verified installation. An
older default installation remained unchanged.

## Fixed contract and boundaries

- Target: `sunheat/job-hunter#2`
- Head branch: `codex/phase-2-workflow-core`
- Maximum wakes: `2`
- Connector capability: `unknown`
- Review trigger authorized: no
- Target/non-target counts: `2/0` in each wake
- Prohibited throughout: issue creation, review-trigger comment, merge,
  auto-merge, base change, force-push, non-target resolution, and mutation of
  the configured checkout

No owner token, credential, raw task notification, or user-home installation
path is included in this public record.

## Wake 1

The initial and frozen head was
`061ee01e542c65b839473cb59db4a2ce5284787f`. Preflight and the initial doctor
reported ready with no blockers. Planning returned
`RUN_BATCH / targeted_work_available` at wake `1/2`, acquired and retained the
PR-scoped lease, and did not recover a stale owner.

The frozen batch contained two exact Codex thread IDs:

- `PRRT_kwDOT9qum86b8CrG`: allow an approved but unsubmitted job to return to
  `needs_review` after its job description changes.
- `PRRT_kwDOT9qum86b8CrM`: return schema version zero only when the migrations
  table is absent, while propagating other SQLite operational failures.

Both outcomes were recorded, focused tests passed, and both exact threads were
resolved. Aggregate validation reported Ruff check and format success,
configuration validation success, `47 passed`, and a clean `git diff --check`.
The batch produced commit `99a95ccaa0367ec1162440cf43312ccb0eaed09b`
with one push; GitHub PR head, branch ref, and `git ls-remote` were reported at
that commit after publication.

Completion returned `WAIT_REVIEW / connector_capability_unknown`, cleared the
inflight action, left no failure latch or recovery requirement, and released
the lease. Review artifacts caused by the push were deliberately deferred to
the next wake.

## Wake 2 and terminal cleanup

The final task was reported as `codex-review-pulse-pr-2-final-wake`. It was
created at `2026-08-25T08:28:07.451Z`, scheduled for
`2026-08-25T08:40:31.267Z`, and woke the same Codex task at
`2026-08-25T08:51:22.105Z`.

The initial and frozen head was
`99a95ccaa0367ec1162440cf43312ccb0eaed09b`. Preflight and doctor again reported
ready with no blockers. Planning returned
`RUN_BATCH / targeted_work_available` at wake `2/2`, acquired and retained the
lease, and did not recover a stale owner.

The second frozen batch contained:

- `PRRT_kwDOT9qum86b_BDD`: fence persistence operations so an expired run owner
  cannot continue writing the database, checkpoint, or run completion.
- `PRRT_kwDOT9qum86b_BDG`: update job-description text and its hash atomically
  as one persisted version.

Both outcomes were recorded, focused tests passed, and both exact threads were
resolved. Aggregate validation reported Ruff check and format success,
configuration validation success, `49 passed`, and a clean `git diff --check`.
The batch produced commit `c9e2529918f1f30690b467ea9f68340a2a301fe0`
with one push; the same three remote-head checks were reported at that commit.

Completion returned `PAUSE_EXPIRED / wake_budget_exhausted` with pause
disposition, cleared the inflight action, left no failure latch or recovery
requirement, and released the lease. Final doctor then reported the expected
single blocker `wake_budget_exhausted`. The task entered `PAUSED` at
`2026-08-25T09:04:52.251Z` and was deleted after lifecycle verification. The
operator explicitly verified that no third wake remained.

## Bounded conclusion

Across two wakes, the pilot processed four exact targeted threads in two
frozen batches, with two aggregate commits and two pushes total: at most one of
each per wake. Both task-owned worktrees were removed cleanly, runtime contract,
checkpoint, and run state were retained, and the lease was absent at the end.

This supports repeatable, manually reviewed bounded pilots with explicit wake
budgets and terminal task cleanup. It does not support claims of indefinite
unattended operation, automatic connector-capability detection, automatic
review triggering, generic reviewers, other forges, or broader crash recovery.
