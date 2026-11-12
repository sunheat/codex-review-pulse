# Security Policy

## Reporting a vulnerability

Do not disclose security-sensitive findings in a public issue or pull request.
Email the maintainer at `sunheatyy@gmail.com` with a concise impact summary,
affected version or commit, reproduction steps, and any suggested mitigation.
Do not include real GitHub tokens, credentials, or third-party private data.

An acknowledgement should normally arrive within seven days. Coordinated
disclosure timing will be agreed after the report is reproduced and scoped.

## Supported scope

Security reports are accepted for the `1.0.0` runtime. Relevant issues include incorrect Codex account identity
attribution or thread targeting; campaign identity, PR-scoped ownership, or
stale-delivery bypasses; unsafe Git or GitHub mutation scope or ordering; and
failures to preserve ownership when publication or another mutation is
ambiguous. Reports about campaign or lock state integrity, untrusted input
crossing authority boundaries, and unsafe handling of unsupported state are
also welcome.

The supported end-to-end host is the Codex app with native Codex Automations,
using a trusted local repository and same-repository GitHub pull requests.
Long-term unattended operation, cross-host coordination, fork-PR publication,
automatic connector detection, generic reviewers, non-GitHub forges,
Raspberry Pi portability, and plugin marketplace packaging are not supported
capabilities.
