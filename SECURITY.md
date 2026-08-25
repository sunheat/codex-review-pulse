# Security Policy

## Reporting a vulnerability

Do not disclose security-sensitive findings in a public issue or pull request.
Email the maintainer at `sunheatyy@gmail.com` with a concise impact summary,
affected version or commit, reproduction steps, and any suggested mitigation.
Do not include real GitHub tokens, credentials, or third-party private data.

An acknowledgement should normally arrive within seven days. Coordinated
disclosure timing will be agreed after the report is reproduced and scoped.

## Supported scope

Security reports are accepted for the current `0.3.1` public release candidate,
especially authorization-boundary bypasses, incorrect Codex thread targeting,
unsafe GraphQL mutation scope, checkpoint or lease integrity failures, and
commit-pinned installation verification failures.

Long-term unattended operation, automatic connector detection, generic
reviewers, non-GitHub forges, Raspberry Pi portability, and plugin marketplace
packaging are not supported capabilities.
