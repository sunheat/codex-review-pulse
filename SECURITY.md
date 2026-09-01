# Security Policy

## Reporting a vulnerability

Do not disclose security-sensitive findings in a public issue or pull request.
Email the maintainer at `sunheatyy@gmail.com` with a concise impact summary,
affected version or commit, reproduction steps, and any suggested mitigation.
Do not include real GitHub tokens, credentials, or third-party private data.

An acknowledgement should normally arrive within seven days. Coordinated
disclosure timing will be agreed after the report is reproduced and scoped.

## Supported scope

Security reports are accepted for the current `0.6.0` Codex-first development
baseline, especially authorization-boundary bypasses, incorrect Codex thread
targeting, unsafe GraphQL mutation scope, checkpoint or wake-lifecycle
integrity failures, lease failures in the optional hardened mode, and
commit-pinned installation verification failures. The real 0.4.0 black-box
pilot failed; it is not a publishable final recurring release.

Live long-term unattended operation remains a forward-pilot capability rather
than a proven service guarantee. Automatic connector detection, generic
reviewers, non-GitHub forges, Raspberry Pi portability, and plugin marketplace
packaging are not supported capabilities.
