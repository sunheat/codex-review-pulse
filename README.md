# Codex Review Pulse

[![CI](https://github.com/sunheat/codex-review-pulse/actions/workflows/ci.yml/badge.svg)](https://github.com/sunheat/codex-review-pulse/actions)

Codex Review Pulse runs bounded, unattended remediation of GitHub pull-request
review feedback authored by the configured Codex reviewer identity, inside the
Codex app using native Codex Automations and packaged Agent Skills.

The project is deliberately narrow: one target PR, one small repository-local
campaign record, one permanent PR-scoped ownership lock, a bounded number of
effective rounds, and one automatic `@codex review` request per campaign and
head. It is not a generic workflow engine, scheduler, multi-host platform, or
multi-reviewer framework.

The canonical behavior contract is [`AGENTS.md`](AGENTS.md);
[ADR 0008](docs/adr/0008-v2-runtime-architecture-reset.md) records the v2
architecture reset.

## Repository status: v2 implemented, unreleased, awaiting canary

The tree now contains the first usable v2 implementation:

- a skill-driven launcher and scheduled-worker path for the Codex app;
- deterministic helpers for observation, ownership, round accounting,
  publication, deferred issues, exact thread resolution, and review requests;
- network-free tests for the high-frequency branches and safety invariants.

`VERSION` is `2.0.0`, but **no v2 release has been published or tagged**. This
is an unreleased development build. Native Codex Automation creation and
scheduled execution have **not** been exercised in the Codex app; that requires
a separately authorized live canary. Network-free tests do not prove native
host integration, and discovery metadata never authorizes a live run.

The previous 0.3.1 runtime is retained as quarantined historical evidence under
[`legacy/v0.3.1/`](legacy/v0.3.1/README.md). It is not an active entry point and
is not the v2 architecture.

## One-sentence invocation

In the Codex app, with this skill installed and the target repository open as a
project:

```text
Run Codex Review Pulse on https://github.com/OWNER/REPO/pull/NUMBER for at most
6 effective rounds, using MODEL with REASONING_LEVEL every 30 minutes.
```

Required parameters: target PR, 1–10 effective rounds, worker model, reasoning
level, and minutes between deliveries. None may be silently substituted. The
launcher validates as far as the native host permits, creates the campaign
record, and configures one fixed recurring native Codex Automation bound to the
local repository project. Full procedures:

- [skill entry point](skills/codex-review-pulse/SKILL.md);
- [launcher guide](skills/codex-review-pulse/references/launcher.md);
- [scheduled worker guide](skills/codex-review-pulse/references/worker.md);
- [setup and host requirements](skills/codex-review-pulse/references/setup.md);
- [manual lock recovery](skills/codex-review-pulse/references/recovery.md).

## Requirements

- the Codex app / Codex desktop with native Codex Automations and Agent Skills;
- Git, with a local clone of the target repository (same-repository PRs only);
- authenticated [GitHub CLI](https://cli.github.com/);
- Python 3.11 or newer.

Campaign state and the ownership lock live under the repository's Git common
directory (`<git-common-dir>/codex-review-pulse/v2/`), shared by all worktrees,
never tracked. Remediation runs in detached temporary worktrees; the user's
primary worktree is never modified.

## Safety model in brief

- Permanent PR-scoped atomic lock: no TTL, heartbeat, renewal, automatic stale
  detection, or stealing; explicit user-authorized recovery only.
- Effective rounds are consumed only after ownership and authoritative
  revalidation establish that an effective action is required.
- Applicable evidence requires stable Codex identity attribution and current-head
  applicability; unknown or incomplete evidence is never treated as absence.
- Each remediation attempt is one bounded in-memory batch: at most one commit
  and one push, never force-push, fixing state published before any thread is
  resolved, Fix-later issues created with deterministic marker identity.
- One automatic `@codex review` attempt per campaign and head; the round and
  allowance commit before the POST and are never restored; post-request
  completion and no-response conclusions require temporally eligible, complete
  evidence.
- Ambiguous external mutation fails closed and retains the lock.

See [`AGENTS.md`](AGENTS.md) for the full contract. The deterministic mechanisms
live under [`skills/codex-review-pulse/scripts/`](skills/codex-review-pulse/scripts/).

## Development and verification

```text
python -m unittest discover -s tests -v
python -m compileall -q skills/codex-review-pulse/scripts scripts tests
python scripts/validate_repository.py
```

CI runs these network-free checks on Windows and Ubuntu with Python 3.11 and
3.12. They do not authenticate to GitHub or exercise native Codex Automations.

Contributions are welcome within the documented scope; see
[CONTRIBUTING.md](CONTRIBUTING.md). Report security-sensitive findings through
the private path in [SECURITY.md](SECURITY.md). Project direction and deferred
work are in [ROADMAP.md](ROADMAP.md); historical context is in
[RELATED-WORK.md](RELATED-WORK.md).

## License

Licensed under the [Apache License 2.0](LICENSE).
