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

## Requirements and installation

- Codex app / Codex desktop with native Codex Automations and Agent Skills;
- Codex mode and Full access when the host exposes the effective mode and
  permissions; see the [environment gate](skills/codex-review-pulse/references/launcher.md#0-environment-gate);
- Git 2.36+, with a local clone of the target repository and a matching `origin`
  remote (same-repository PRs only); see [Git support limits](skills/codex-review-pulse/references/setup.md#controller-git-support);
- authenticated [GitHub CLI](https://cli.github.com/) with access to read the
  PR, push its head branch, resolve threads, create issues, and post comments;
- Python 3.11 or newer.

The complete skill package is [`skills/codex-review-pulse/`](skills/codex-review-pulse/).
Replace `REF` with the branch or tag for the version you want to install:

```text
git clone --branch REF https://github.com/sunheat/codex-review-pulse.git
```

Check [`VERSION`](skills/codex-review-pulse/VERSION) in the cloned package
before installing it.
Copy the entire `skills/codex-review-pulse` directory, including its references
and scripts, into the target project's `.agents/skills/` directory so that
`.agents/skills/codex-review-pulse/SKILL.md` exists. Start a new Codex
app conversation with the target repository open as a project. This follows the
[official manual skill installation guidance](https://developers.openai.com/api/reference/cli#use-the-cli-from-a-coding-agent).

## One-sentence invocation

In the new Codex app conversation, invoke the installed skill:

```text
$codex-review-pulse Run Codex Review Pulse on https://github.com/OWNER/REPO/pull/NUMBER for at most
6 effective rounds, using MODEL with REASONING_LEVEL every 30 minutes.
```

Required parameters: 1–10 effective rounds, worker model, reasoning level, and
minutes between deliveries. The target PR may be explicitly supplied — then it
is authoritative and never substituted — or omitted, in which case the launcher
discovers OPEN pull requests in the bound repository: exactly one is selected
automatically, two or more ask the user to choose, zero reports and stops, and
a discovery failure reports and stops. Nothing is silently substituted. The
launcher validates as far as the native host permits, creates the campaign
record, and configures one fixed recurring native Codex Automation bound to the
local repository project. By default, worker execution begins on the normal
schedule. If explicitly requested, the current conversation may run one worker
attempt immediately after successful Automation setup and confirmed setup-lock
release. It follows the same fresh checks, ownership, and round budget as a
scheduled delivery. Full procedures:

- [skill entry point](skills/codex-review-pulse/SKILL.md);
- [launcher guide](skills/codex-review-pulse/references/launcher.md);
- [scheduled worker guide](skills/codex-review-pulse/references/worker.md);
- [setup and host requirements](skills/codex-review-pulse/references/setup.md);
- [manual lock recovery](skills/codex-review-pulse/references/recovery.md).

## Operating requirements

Campaign state and the ownership lock live under the repository's Git common
directory (`<git-common-dir>/codex-review-pulse/v2/`), shared by all worktrees,
never tracked. Remediation runs in detached temporary worktrees; the user's
primary worktree is never modified.

Use unattended remediation only with repositories and same-repository PR
branches whose code and test dependencies you trust. The full-access worker
runs tests it adds or changes in the current round by default, when the
existing environment can run them directly. To opt in, add “Run the full test
suite before each remediation push” to the campaign setup request. Test code
runs with the worker's effective access. Separate build, lint, and formatting
checks and any remaining test coverage belong to the target repository's CI.
Fix quality depends on the configured model, and publication does not certify
that the fix compiles or passes CI.

## Safety model in brief

- Permanent PR-scoped atomic lock: no TTL, heartbeat, renewal, automatic stale
  detection, or stealing; explicit user-authorized recovery only.
- Effective rounds are consumed only after ownership and authoritative
  revalidation establish that an effective action is required.
- Applicable evidence requires stable Codex identity attribution. A Codex
  THUMBS_UP on the PR itself is PR-wide approval without commit linkage;
  EYES and non-approval completion retain current-lifecycle eligibility rules.
  Unknown or incomplete evidence is never treated as absence.
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

Use focused local tests while developing a bounded change, then cover the
relevant final state. This command is one example of focused discovery, not a
test mapping for every change:

```powershell
python -m unittest discover -s tests -p test_runtime_contract.py -v
```

The complete regression suite and other repository checks are available when
the change or applicable acceptance requirements call for them:

```powershell
python -m unittest discover -s tests -v
python -m compileall -q skills/codex-review-pulse/scripts scripts tests
python scripts/validate_repository.py
```

CI runs these network-free checks on Ubuntu, macOS, and Windows with Python
3.11 and 3.12, plus a separate Windows configuration-hook regression job; see
[the workflow](.github/workflows/ci.yml). They do not authenticate to GitHub
or exercise native Codex Automations. See [CONTRIBUTING.md](CONTRIBUTING.md)
for detailed test-selection guidance and the distinction between local checks
and complete final-head CI acceptance.

Contributions are welcome within the documented scope; see
[CONTRIBUTING.md](CONTRIBUTING.md). Report security-sensitive findings through
the private path in [SECURITY.md](SECURITY.md). Project direction and deferred
work are in [ROADMAP.md](ROADMAP.md).

## License

Licensed under the [Apache License 2.0](LICENSE).
