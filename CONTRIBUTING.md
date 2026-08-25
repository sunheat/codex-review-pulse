# Contributing

Codex Review Pulse welcomes focused fixes that preserve its GitHub-specific,
Codex-only, fail-closed safety model. Discuss broad scope changes before
implementation; generic reviewers, other forges, plugin packaging, Pi
portability, and long-term unattended operation remain deferred.

## Development

Use Python 3.11 or later. Keep runtime dependencies minimal and preserve
network-free tests by injecting GitHub GraphQL reads and mutations. Tests must
never perform a live GitHub mutation.

Before opening a pull request, run:

```powershell
python -m unittest discover -s tests -v
python -m compileall -q skills/codex-review-pulse/scripts scripts tests
python scripts/validate_repository.py
python path/to/skill-creator/scripts/quick_validate.py skills/codex-review-pulse
git diff --check
```

Replace `path/to/skill-creator` with the installed Skill Creator directory for
your Codex environment.

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
