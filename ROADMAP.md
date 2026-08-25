# Roadmap

The core state phase implements Codex-only thread targeting, current-head
approval checkpoints, frozen-batch recovery, and PR-scoped exact resolution.
It intentionally continues to defer:

- broader event and stalled-review classification beyond the core evaluator;
- connector detection and server-timestamp review triggering;
- Codex plugin packaging and installation;
- Pi portability validation;
- generic reviewer and multi-forge support; and
- evaluation of whether to reuse or vendor OpenAI `gh-address-comments`.

These are candidate later milestones, not claims about current functionality.
