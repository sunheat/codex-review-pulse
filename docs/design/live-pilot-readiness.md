# Controlled live-pilot readiness

## Scope

This phase turned the completed core state model into a release candidate for
one manually supervised GitHub pilot. That historical pilot completed
successfully, but the later real 0.4.0 black-box recurring pilot failed. The
failure showed early heartbeat activation, fixed-cadence overlap during a
26-minute wake, expiry of a 300-second lease during validation, duplicate plan
counting for one host wake, and continued publication after `PAUSE_BLOCKED`.
Version `0.4.0` is therefore not a publishable final recurring release.

The public product now uses the Codex-first `scripts/pulse.py` control surface.
This design remains an opt-in hardened compatibility reference, not a default
prerequisite, and it must obey the same paused-before-work,
one-wake/one-plan, completion-relative, fail-closed lifecycle.

The historical supervised phase added three boundaries:

1. approval evidence is audited against GitHub's documented object model;
2. the pilot runs from a commit-pinned independent skill copy; and
3. a read-only preflight reports whether the environment is safe to enter the
   existing mutation workflow.

## Approval evidence audit

GitHub documents reaction creation as idempotent: a `200` response means the
reaction already exists and a `201` means it was created. A later snapshot can
therefore contain the same reaction node even if an actor tried to add the same
reaction again. GraphQL exposes a reaction node ID, its creation time, author,
content, and reactable subject, but no pull-request head OID.

Consequently:

- the same reaction ID seen again is the same event, not a fresh approval;
- `createdAt` proves when GitHub created that reaction object, not which head
  the author reviewed or intended to approve;
- deleting a reaction and later observing a newly created reaction with a new
  ID is a new event, but it is current-head proof only when the unchanged head
  epoch had already been established before that ID appeared; and
- PR `updatedAt`, a commit's `authoredDate`, and an old reaction cannot order a
  reaction reliably against the moment a head became current.

The reaction path therefore retains the existing fail-closed approval epoch.
Cold-start reactions and reactions first observed with a changed head remain
ambiguous and are exposed as such in preflight output.

GitHub GraphQL also exposes a `PullRequestReview.commit`, described as the
commit associated with the review, and a review `state`. A review state of
`APPROVED` is explicit approval. This supplies a stronger independent proof:
an approval-author identity's non-dismissed `APPROVED` review qualifies only
when its `commit.oid` exactly equals the bracketed current `headRefOid`.
Missing commits, old commits, informational reviews, dismissed reviews, and
other authors fail closed. See [ADR 0004](../adr/0004-current-head-review-proof.md).

Official sources:

- [GitHub REST reaction endpoints](https://docs.github.com/en/rest/reactions/reactions)
- [GitHub GraphQL Reaction](https://docs.github.com/en/graphql/reference/reactions)
- [GitHub GraphQL PullRequestReview](https://docs.github.com/en/graphql/reference/pulls#pullrequestreview)
- [GitHub GraphQL PullRequestReviewState](https://docs.github.com/en/graphql/reference/pulls#pullrequestreviewstate)

## Commit-pinned pilot installation

OpenAI documents user-level local skills under `$HOME/.agents/skills`, automatic
detection of skill changes, and a restart fallback when changes do not appear.
The pilot manager therefore defaults to:

```text
$HOME/.agents/skills/codex-review-pulse
```

See [OpenAI's Build skills documentation](https://learn.chatgpt.com/docs/build-skills#where-codex-loads-local-skills).

`scripts/manage_pilot_install.py` implements `install`, `verify`, `update`, and
`uninstall`. Install and update require a clean source repository and an
explicit commit that contains `skills/codex-review-pulse/VERSION`. They read
the skill tree from Git objects rather than copying the mutable checkout. The
installed directory is a normal independent directory, never a symlink.

Its private installation manifest records:

- manifest schema and skill name;
- skill version;
- full source commit;
- source repository used for the operation; and
- the SHA-256 digest of every installed skill file.

Verify detects missing or modified files and expected version/commit mismatch.
It rejects symlinks anywhere in the installed tree and reconstructs the trusted
inventory from the pinned commit's Git objects, instead of treating the
mutable adjacent manifest as its own trust root.
Update refuses an already damaged or foreign target before swapping in the new
commit snapshot. Uninstall removes only the fixed skill directory after its
ownership and inventory verify; it refuses an unverified directory. Tests
override the skill root with temporary directories.

This is immutable provenance, not a filesystem write-protection promise. A
local edit is detected by `verify` and invalidates pilot readiness.

## Read-only preflight

`scripts/pilot_preflight.py` performs no GitHub mutation and does not write a
checkpoint. It checks and emits JSON for:

- Python, Git, GitHub CLI, and `gh auth status`;
- requested and canonical repository identity, PR number, state, draft flag,
  and a head-bracketed `headRefOid`;
- a clean target checkout whose local HEAD equals that `headRefOid` and whose
  `origin` fetch and push URLs identify the PR head repository;
- normalized reviewer and approval identities;
- targeted and non-target unresolved threads;
- checkpoint path, schema, active batch, and recovery state;
- reaction-epoch or current-head-review approval status, including cold-start
  and existing-reaction ambiguity;
- installed skill version, source commit, file inventory, and hashes;
- proof that the running preflight script is inside that verified installation;
- the operator's explicit single-runner confirmation.

The original `--single-runner-confirmed` check is intentionally an operator
assertion for supervised mode. Version `0.3.1` additionally makes preflight
inspect the recurring PR lease without acquiring or changing it; recurring
mutation authority comes only from the real lease described in
[the recurring-heartbeat design](recurring-heartbeat-readiness.md).

When no installation-root override is supplied, preflight self-locates and
verifies the exact skill directory containing the executing script. Explicit
`--install-root` and `--skills-root` aliases identify the parent directory and
must agree when both are present. This preserves fail-closed hash, manifest,
inventory, symlink, and runtime-path checks without consulting an unrelated
default installation.

Preflight may compute what the next checkpoint would contain, but reports
`checkpoint_would_change` instead of saving it. Establishing or advancing an
approval epoch remains an explicit write performed by the formal state-fetch
command.

An absent checkpoint and ambiguous approval do not by themselves prevent a
supervised pilot from starting; they prevent a terminal approval claim. An
invalid checkpoint, unfinished/failed active batch, unverified installation,
closed/draft PR, failed dependency/API check, or missing single-runner
confirmation blocks the pilot.

## Pilot entry and stop boundaries

A supervised pilot may begin only after:

- the release-candidate commit is locally complete and clean;
- an independent copy of that exact commit verifies at the expected version;
- preflight returns `ready_for_supervised_pilot: true`;
- the operator confirms the canonical repository, PR number, reviewer and
  approval identities, and that no other runner targets the PR; and
- the operator separately authorizes the intended code changes, commit, push,
  exact thread resolution, issue creation, or trigger comment.

Stop and preserve evidence on a mixed-head snapshot, authentication/API
failure, installation drift, active recovery, unexpected identity/thread
scope, or head advancement. Preflight success is not mutation authority and is
not a global merge-readiness claim.

## Completed pilot evidence

The first supervised pilot used version `0.2.0` from source commit
`f24c172396b0a2b35b05872c570945753bcfcbab` on `sunheat/job-hunter#2`.
Preflight was ready, the frozen head was `b9aaad4`, five targeted threads were
fixed and resolved exactly, Ruff/config checks and 45 tests passed, aggregate
commit `061ee01e542c65b839473cb59db4a2ce5284787f` was pushed once, and the final
targeted count was zero with current-head approval pending. No issue, trigger,
merge, auto-merge, base change, force-push, recurrence, or non-target resolution
occurred.

## Deferred after this phase

The bounded recurring phase now supplies a conservative deterministic wait
classifier while leaving unknown connector capability unclassified. Public API
connector detection, indefinite unattended operation, plugin packaging, Pi
portability, generic reviewer or multi-forge support, and
`gh-address-comments` integration remain deferred.
