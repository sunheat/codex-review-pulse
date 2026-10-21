# Controller-owned Git execution boundary

## Investigation and decision

Integration HEAD verified by `git fetch origin feature/v2-runtime` on
2026-10-21: `06f8b9b5bc7165d8dd3cdef0f0697f280abe9cc4`. PR #9 remains open;
this refactor is development work, not a live campaign.

The defect was execution policy distributed across business callers. Six
helpers independently created empty hook directories, while storage had a
second subprocess implementation and test runners replaced policy rather than
receiving protected process requests. The baseline `stage_complete_delta`
executed a real `core.fsmonitor` sentinel despite its empty hook directory.

Use one standard-library-only `gitexec.run_controller_git` boundary. `gitlocal`
keeps its `git` import alias for business callers. Storage imports the same
boundary directly, without a dependency cycle. Injectable runners now receive
`subprocess.run`-shaped `(command, **context)` requests **after** policy. They
are trusted test facilities, not a callable sandbox or product bypass.

Every new call through the boundary inherits established common controls
without caller-supplied safety flags. New subcommands or materially different
options still require focused reachability/support review. Shared execution
policy changes belong here, not in business callers. The small reviewed
command surface is not a general Git policy engine.

## Reachable operations inspected

| Caller / operation | Local effects | Network / executable mechanisms | Business authority |
| --- | --- | --- | --- |
| `storage.git_common_directory`: `rev-parse --git-common-dir` | metadata observation; canonical shared namespace | no network, no content conversion | bootstrap, guards, verification/release; no ownership prerequisite |
| `gitlocal.git_text`, registration: `worktree list --porcelain -z` | metadata observation | no checkout | preflight and cleanup decisions |
| `gitlocal.remote_head`: `ls-remote` | remote observation | authenticated transport/helpers | existing authoritative head checks; no round consumption |
| `gitlocal.fetch`: `fetch origin` | refs, objects, FETCH_HEAD | transport, reference hooks, auto maintenance, submodule recursion | active owner; preparation, no remediation commitment |
| `gitlocal.add_worktree`: `worktree add --detach` | registered container, index, checkout | post-checkout, index/reference hooks, filters/fsmonitor | active owner; preparation, no round |
| `gitlocal.remove_worktree`: `worktree remove` | registered temporary container removal | common Git execution controls | active owner, scoped containment; residue is best effort |
| `stage_complete_delta`: `add -A -- .` | complete tracked/non-ignored delta staged | filters, index hooks, fsmonitor | finalizer ownership, before commitment |
| `write_tree`: `write-tree` | local objects / index cache | index hooks, fsmonitor | authoritative proposal tree, before commitment |
| `head_tree_oid`: `rev-parse HEAD^{tree}` | object identity observation | no content filters/signatures | exact tree validation |
| `commit_index_hook_free`: `commit -m`, `rev-parse HEAD` | one local commit and HEAD | commit/reference/index hooks, signing, auto maintenance | deterministic finalizer before round commitment |
| `push_publication_commit`: `push` | intended one remote head update; local tracking behavior | pre-push/reference hooks, signing, transport, recursive push / extra refs | existing finalizer commitment and last ownership check, exactly one attempt |
| `remediation._validate_worktree`: `rev-parse HEAD`, `rev-parse --git-path` | metadata observation | no content conversion | validates registration/head/conflict sentinels; no new authority |
| shared policy: `config`, `remote get-url`, `ls-files`, isolated `read-tree`, `check-attr`, `ls-tree`, `cat-file` | read-only observations except disposable private index | raw plumbing, no textconv; partial clones refused | no campaign knowledge; no external product mutation |

`externalize` uses `remote_head` indirectly; `owned` delegates fetching and
preparation; all these paths inherit the boundary. Product-facing `gitlocal`
CLIs remain fetch, remote-head, worktree-add/remove, with their existing
business checks. No publication CLI was added. `github_api` subprocesses are
`gh`, not Git. The development-only repository validator's `ls-files -s -z`
is not an active product controller and is not a new runtime bypass.

## Common controls and versions

Supported installation baseline remains Git 2.36+. No minimum bump. Tested
locally with Git **2.54.0.windows.1**, Python **3.12.10**, LFS **3.7.1**.
A proposed official Git-for-Windows 2.36.1 compatibility distribution download
was denied by host approval policy and was not executed. Git 2.36 was not
locally exercised. Older/newer host coverage comes from CI; a skipped version-specific test is
not positive evidence for configuration-hook suppression.

- Fresh absolute empty `core.hooksPath` for every invocation, including policy
  observations. One centralized lifetime; cleanup cannot overwrite results.
- Effective `hook.<name>.event` / `.command` names are enumerated through Git
  config (including included configuration and repeated events), with
  `hook.<name>.enabled=false` applied. Git 2.54 supports the second hook
  discovery mechanism; older versions have no configuration-defined hooks.
  `--no-verify` alone does not suppress reference/index/checkout hooks.
- `core.fsmonitor=false` suppresses both executable and builtin monitoring.
- `maintenance.auto=false`, `gc.auto=0`, and nondetaching fallback controls
  prevent implicit maintenance. Fetch additionally uses
  `--no-auto-maintenance`. No managed maintenance service/config is changed.
- Repository/index/object/namespace redirect environment variables are removed;
  caller-selected cwd stays authoritative. Trusted host configuration and
  credential/SSH facilities are retained. UTF-8 with `surrogateescape`, stdout,
  stderr and return code are preserved. No `check=True`, retries, or timeouts
  that manufacture a definite outcome after a possibly completed push.

## Content, signing, and unsupported cases

Feature support checks apply only where those features can run. Safe metadata
observations for storage discovery, ownership, diagnostics and release remain
available even with filters, signing requirements, or unsupported transport
configuration. There is no unprotected subprocess bypass or alternate lock
namespace.

**Filters:** active effective `filter` attributes are unsupported, even when
no driver is currently installed. Refuse, never silently disable conversion.
Before checkout, a disposable private index is populated using `read-tree`
without checkout, then `check-attr --cached` resolves the target commit's
nested/index attributes plus info/global/system attributes. Before staging,
Git enumerates complete tracked plus non-ignored paths and checks both current
worktree and cached attributes. Newly changed nested attributes, info/global
attributes, and indexed fallback cannot evade the check. Git native non-program
conversions (autocrlf, encoding, ident) retain their existing semantics.

Conditional includes can select different execution configuration when cwd,
HEAD or registration changes. They are refused for content/transport operations,
not for safe metadata. Relative `core.attributesFile` is refused before
worktree checkout rather than assuming the old/new cwd have identical files.

**LFS:** no upload protocol added. Every prospective publication tree is
inspected for raw LFS pointer blobs (bounded to LFS's 1024-byte pointer size),
including already tracked pointers with no remaining attributes. Such
publication refuses before round commitment and again before push. Pointer
correctness is not remote object completeness. Real LFS conversion and pointer
validation are tested when installed; no full LFS publication support is claimed.
A batch resolving an already published fix without new publication needs no
LFS upload.

**Signing:** effective `commit.gpgSign` and `push.gpgSign` requirements refuse
before the relevant operation; false-like values retain unsigned behavior.
No required signing semantics are silently discarded. Thus GPG/SSH signing
program selection and default-key commands are unreachable in supported
commit/push execution. No key management.

**Partial clones:** object-consuming operations refuse partial/promisor
configuration rather than launching lazy network fetches inside apparent local
observations. Git's server-advertised promisor acceptance is constrained to
none. Metadata storage/release remains available. Alternate-ref execution
helpers are refused for publication. These are narrow support limits, not
process lifecycle machinery.

## Transport and one-ref publication

Trusted: installed native Git executable and its dependencies; host environment,
system/global credential helpers and SSH configuration/overrides; server-side
services/hooks; stable configuration during an operation. Local included
configuration retains its local scope and cannot select executable transport
helpers. Repository-local credential helpers, SSH commands/variants, Git proxy,
remote uploadpack/receivepack/proxy/vcs helpers are refused for network paths.
URL rewriting is refused at any scope: feeding an expanded URL back into Git
can apply rewriting again and defeat the single-destination proof.

Supported URL forms are native HTTPS, SSH and local/file bare remotes, not
custom remote-helper schemes. One effective fetch URL and one identical push
URL are required. Multiple push URLs and distinct fetch/push destinations are
refused before mutation; the latter would otherwise invalidate post-push
classification by observation of the wrong remote.

Publication passes the verified literal URL, not a configured remote name,
plus the existing single `HEAD:refs/heads/<branch>` refspec. It explicitly uses
no-mirror, no-follow-tags, no-signed, no recursive submodules, and clears
multi-valued configured push options. Configured default/refspec/mirror/tag
settings cannot add refs or destinations. The final ownership callback runs
**after** preflight immediately before the subprocess; campaign authority is
still the existing deterministic owner's responsibility.

Full-access execution is **not** a hard sandbox. This does not defend against
arbitrary malicious same-user processes, concurrent hostile configuration
edits, tampered native binaries, malicious remote services, or model-executed
speculative code. Those require separate architecture decisions.

## Business semantics and validation

Preflight is not a receipt, authorization, or durable state. Preparation remains
zero-round local work behind ownership; filters refuse before checkout. The
finalizer checks publication support on its own prospective tree before local
commit and round commitment. Push repeats checks; a late refusal after
commitment retains existing fail-closed semantics and never refunds the round.
Remote-head advancement, nonzero push/re-observation, one attempt, ambiguous
publication retention, CAS, mutation order, exhaustion and scheduler semantics
remain in the existing controllers.

Removed six independently maintained hook-directory lifetimes plus storage's
raw process path. No new persistent state, scheduler, transaction phases,
credential service or dependency.

Tests use real temporary repositories/bare remotes and executable positive
controls: baseline/protected fsmonitor, traditional/configuration/included hooks,
clean/smudge filters despite hook suppression, target nested attributes,
speculatively changed/info/global attributes, real LFS conversion, required
signing, implicit maintenance trace and recent-object hook, expanded push
settings/multiple destinations, transport helper refusal, and release with
unsupported features. Finalizer tests prove zero-round clean refusal for new
filters/LFS/signing. Injection and AST process-call inventory guard the common
boundary. The older locale test now patches the shared subprocess layer rather
than the removed `storage.subprocess` implementation; hook fixtures now have
executable permissions and positive controls.

CI retains Ubuntu/macOS/Windows × Python 3.11/3.12, logs actual Git/LFS versions,
and adds one Windows Git 2.54+ policy job which fails rather than silently skips
when its Git lacks configured hooks. The timeout is 25 minutes because the
full local suite with additional protected config observations takes about
15 minutes; this is CI resource allowance, not runtime lifecycle machinery.

Automated tests do not establish unattended native Codex Desktop operation.
A live canary remains separately authorized and was not run.
