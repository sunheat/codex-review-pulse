# Contributing

Codex Review Pulse welcomes focused fixes that preserve its GitHub-specific,
Codex-only, fail-closed safety model. Discuss broad scope changes before
implementation; generic reviewers, other forges, plugin packaging, Pi
portability, and long-term unattended operation remain deferred.

## Development

Use Python 3.11 or later. Keep runtime dependencies minimal and preserve
network-free tests by injecting GitHub GraphQL reads and mutations. Tests must
never perform a live GitHub mutation.

Choose development checks from the changed behavior and boundary conditions,
direct callers and consumers, changed interfaces or state transitions, and
relevant safety invariants. A test filename is a starting point, not proof of
coverage. Focused discovery can help with a localized change; this example is
not a universal test mapping or sufficient coverage for every change:

```powershell
python -m unittest discover -s tests -p test_runtime_contract.py -v
```

For a batch of edits or review findings, collect the relevant test scope and
validate the final intended state together. Run focused tests during debugging
when useful, but do not rerun a module solely because several findings affect
it. Rerun a passed check when later changes could affect its result; otherwise,
the actual diff and known dependencies can justify retaining that result.
Editing, committing, or pushing does not itself require a full-suite run.

Expand regression coverage for shared interfaces, campaign state, lifecycle
transitions, ownership, locking, concurrency, Git execution, recovery,
authorization, idempotency, and external mutation boundaries. Run the complete
local suite when the affected scope cannot reasonably be bounded, an applicable
requirement mandates it, or necessary acceptance cannot otherwise be
established. Never omit relevant safety-invariant coverage to save time.

Ordinary prose changes do not automatically need runtime-wide tests. Changes
to agent instructions, runtime contracts, executable examples, packaging
rules, or authority boundaries need appropriate targeted validation.

Use the following commands according to their relevance and any applicable
mandatory acceptance requirements:

```powershell
python -m unittest discover -s tests -v
python -m compileall -q skills/codex-review-pulse/scripts scripts tests
python scripts/validate_repository.py
python path/to/skill-creator/scripts/quick_validate.py skills/codex-review-pulse
git diff --check
```

The unittest command runs the complete regression suite. `compileall` checks
Python compilation. `validate_repository.py` checks repository structure,
Markdown, tracked-file hygiene, workflows, and CLI help. Skill Creator's
`quick_validate.py` checks the skill package; it is not part of the current CI
workflow. `git diff --check` checks whitespace and patch hygiene.

Replace `path/to/skill-creator` with the installed Skill Creator directory for
your Codex environment.

Local development checks and final PR acceptance are separate. Report focused
local results, a complete local-suite pass for its specific environment, the
complete CI matrix for the exact final revision, and other applicable
acceptance requirements separately. Successful final-head CI normally removes
the need for a redundant local full-suite run for a localized change. A local
full-suite pass on one platform does not establish cross-platform CI success;
pending, cancelled, failed, or earlier-revision CI cannot establish final-head
acceptance. If CI is unavailable, perform necessary local validation and
report complete CI acceptance as pending. Local success does not waive known
CI failures or other mandatory requirements.

## Pull requests

Keep changes small, document behavioral or authority-boundary changes, and add
focused regression coverage. Use short Conventional Commit subjects where
practical. Do not commit local notes, credentials, caches, runtime checkpoints,
installation manifests, or unrelated files.

Authorization is action-specific. A code change does not authorize committing,
pushing, creating or resolving issues, resolving review threads, posting a
review trigger, starting recurring execution, merging, enabling auto-merge,
changing a PR base, or force-pushing. State every requested external mutation
explicitly and keep it scoped to the target PR.
