# Roadmap

Codex Review Pulse supports bounded Codex review remediation for same-repository
GitHub pull requests in the Codex app, using native Codex Automations. The
[product contract](AGENTS.md) defines its scope and safety boundaries.

## Deferred scope

- Native Automation lifecycle hardening for creation and configuration
  verification, terminal cleanup, and retained-lock quarantine or pause;
  these operations remain partly host/model-mediated.
- Fork pull-request publication and cross-host coordination.
- Operational notifications and pause integrations beyond native controls.
- Marketplace distribution and plugin packaging.
- Connectors beyond the GitHub CLI and GraphQL evidence surface.
- Generic reviewer and multi-forge support.

Network-free checks do not establish native Automation integration or live
GitHub mutation behavior. A live run requires explicit user authorization; see
[CONTRIBUTING.md](CONTRIBUTING.md) and [AGENTS.md](AGENTS.md) for validation
and canary boundaries.
