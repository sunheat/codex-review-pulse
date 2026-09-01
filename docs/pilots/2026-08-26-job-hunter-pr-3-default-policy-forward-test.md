# Default-policy forward test: `sunheat/job-hunter#3`

## Evidence status

This is an operator-provided live-run record for the `0.5.0` default candidate
and the `0.6.0` repair decision. It is not a claim that scheduled-task or live
GitHub integration is already proven. The next run must use the installed
`0.6.0` user-level default and resume the existing checkpoint rather than
starting a second wake.

## Wake 1

The first wake identified three targeted Codex threads, applied the fixes,
recorded focused and aggregate validation, resolved the exact frozen threads,
and published one aggregate commit (`5fd2dc0`). The reported validation result
was `71 passed`; the heartbeat was re-anchored after publication.

## Wake 2 interruption

The second wake observed the new head and froze three new Codex threads. The
agent modified seven PR-scoped files but made no GitHub resolution, commit, or
push. Focused validation reported `2 failed, 38 passed`; both failures were
stale exact-error assertions that still expected `invalid listing data` while
the implementation now emits the more specific
`Greenhouse/Lever listing is missing a posting ID` error. The old default
instruction treated any focused failure as an immediate pause, so the wake
stopped with the frozen batch retained and the heartbeat paused.

## 0.6.0 repair decision

The failure is a repairable PR-scoped test expectation, not a head mismatch,
ambiguous review, or publication-integrity failure. The autonomous default now
allows stale PR-scoped test updates, persists recoverable retry state, and can
resume the same frozen batch on the next completion-relative wake. Repeated
unchanged validation signatures still pause at the configured no-progress
limit; host permission failures remain independent hard blockers.
