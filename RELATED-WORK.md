# Related work

## djm204/codex-review

[`djm204/codex-review`](https://github.com/djm204/codex-review) is a focused
Claude Code plugin for driving the GitHub Codex connector through a review
loop. Its documented mechanical responsibilities are connector availability
detection, review triggering, polling GitHub's review channels, and
classification of the result as working, findings, or clean.

Codex Review Pulse addresses a different part of the workflow:

| Area | `djm204/codex-review` | Codex Review Pulse |
| --- | --- | --- |
| Primary concern | Start and observe a Codex review | Safely remediate recurring Codex review batches |
| Review state | Poll and classify Codex output across GitHub channels | Fetch authoritative GraphQL review threads and preserve exact thread IDs |
| Mutation boundary | Leaves remediation judgement to the calling agent | Defines frozen-batch, one-commit, and one-push transaction semantics |
| Concurrency safety | Not its documented focus | Protect unrelated work and stop on unexpected remote-head advancement |
| Recovery | Poll until a review outcome is classified | Preserve enough live/thread mapping to pause and recover after partial publication or resolution failure |
| Approval signal | A documented clean-result classification | A proven current-head PR-level `THUMBS_UP` from a configured Codex identity, combined with zero targeted Codex threads |
| Recurrence | Review loop polling | Codex heartbeat execution and Pi scheduled execution |

The projects are complementary rather than interchangeable. Future work will
evaluate whether Codex Review Pulse should adopt compatible connector detection
and server-timestamp triggering ideas without broadening into a generic review
framework.

At the time this document was written (2026-08-25), the related project's
README stated `MIT`, while its repository root did not contain a complete
`LICENSE` file. It is therefore treated only as related work. No source code
from that repository has been copied into this project.
