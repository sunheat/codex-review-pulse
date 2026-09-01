# Optional hardened mode

This reference is opt-in only. Version `0.4.0` has a real black-box pilot
failure and is not a publishable final recurring release. Do not use the
hardened controller as the default user path or claim that its old pilot
protocol is production-ready.

The hardened machinery remains available for focused compatibility tests and
future supervised revalidation:

- `manage_pilot_install.py` for immutable installation provenance;
- `pilot_preflight.py` for read-only diagnostics;
- `recurring_contract.py` for explicit authority and bounds;
- `runner_lease.py` for a PR-scoped renewable lease; and
- `heartbeat_tick.py` for an explicit `wake_id`-bound plan/complete cycle.

When explicitly choosing hardened mode, apply the same P0 lifecycle as the
default path:

1. The first user turn is wake 1 and the heartbeat remains `PAUSED` before it
   begins.
2. The scheduled wake pauses its heartbeat before PR work and must stop if
   that pause cannot be confirmed.
3. The host supplies a persistent `wake_id`; a duplicate plan returns its
   persisted result and does not increment `wake_count`.
4. A wake uses a lease long enough for the whole operation or an independently
   verified renewal. Lease loss is `PAUSE_CONCURRENT`/`PAUSE_RECOVERY` and
   cannot be retried by planning again.
5. Only `WAIT_REVIEW` or a successful same-head `REQUEST_REVIEW` may receive a
   completion-relative `next_not_before`. Every other result remains paused.
6. A plain recovery ID never clears a latch. Recovery requires a new user turn
   or separately verifiable external authority.

The detailed installation and one-cycle commands remain in
[`usage.md`](usage.md), and the contract-specific design remains in
[`recurring.md`](recurring.md). Those documents describe implementation
details, not default prerequisites, and must not be used to reintroduce the
old fixed-cadence activation lifecycle.
