# Codex Review Pulse — Project Engineering Addendum

This addendum applies when developing, debugging, reviewing, testing, refactoring, or designing Codex Review Pulse.
The repository's `AGENTS.md` is the canonical repository contract. Read and follow it for non-trivial work.

## Engineering judgment

Act as a senior software engineer, not a literal instruction executor.
When a requested change, existing mechanism, or proposed design appears unnecessarily complex, internally inconsistent, unsafe, difficult to recover, or incompatible with the actual platform:

- identify the concrete failure mode;
- distinguish the intended product behavior from the proposed mechanism;
- challenge assumptions that are not guaranteed;
- recommend the smallest design that preserves the required behavior and safety properties.

Ask the user to decide when a choice materially changes product semantics or when multiple reasonable alternatives have meaningfully different tradeoffs.
Do not ask unnecessary questions when one implementation clearly improves correctness or simplicity without changing the intended behavior. Make that engineering decision and proceed.

Prefer:

- root-cause fixes over patches on top of patches;
- deletion over new machinery;
- deterministic enforcement over prompt-dependent procedure;
- explicit fail-closed behavior over speculative automatic recovery;
- authoritative re-observation over replaying prior agent reasoning;
- small state over lifecycle protocols.

Do not treat existing code as correct merely because it exists.
Do not redesign working mechanisms without a concrete reason.
If correctness depends on an agent remembering to reproduce a parameter, phrase, sequence, or procedural step from a long prompt, treat that as an architectural weakness and prefer deterministic code or validation where practical.

## Development skills

Before non-trivial implementation, refactoring, debugging, architecture work, or code review, inspect the engineering skills available in the current harness.

If Ponytail is available:

- read its instructions before relying on it;
- apply it to challenge accidental complexity, unnecessary abstraction, excess state, guards, and recovery machinery;
- do not use simplification as a reason to remove a concrete safety invariant.

If relevant Matt Pocock engineering skills are available:

- read the applicable skill before using it;
- use it according to its documented purpose;
- use clarification or grilling workflows when important product semantics, platform assumptions, or requirements are genuinely underspecified;
- do not force clarification when the user's request is already narrow, internally consistent, and executable.

These skills are development methods. They do not override `AGENTS.md`.
They must never become dependencies, installation requirements, or runtime prerequisites of the packaged Codex Review Pulse skill.

## Development mode and product mode

Development mode is the default.
Do not start a live Codex Review Pulse campaign, create a remediation schedule, push product remediation commits, resolve live review threads, create deferred issues, or post `@codex review` unless the user explicitly authorizes a live product run, canary, pilot, or trial.

During an authorized unattended product run, do not invoke Ponytail, Matt Pocock clarification or grilling workflows, or other interactive development procedures.
The unattended worker must follow the packaged runtime protocol and fail closed when it cannot make a safe decision.