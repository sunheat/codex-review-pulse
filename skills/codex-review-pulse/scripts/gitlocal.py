#!/usr/bin/env python3
"""Deterministic local Git operations: fetch, temporary worktrees, publication.

The user's primary worktree is never modified. Remediation runs in detached
temporary worktrees under the repository-associated v2 state directory. A batch
creates at most one commit and one push, never an empty commit, never a
force-push, and a remote head advancement aborts publication. Authoritative
commit creation never executes Git hooks (``core.hooksPath`` is pointed at an
empty directory for the one commit invocation).

The publication primitives (``stage_complete_delta``, ``write_tree``,
``head_tree_oid``, ``commit_index_hook_free``, ``push_publication_commit``)
are internal library boundaries of the deterministic remediation finalizer
(``remediation.py``); they are not alternate product-facing push paths.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Callable

import storage
from storage import worktree_root


def git(
    *args: str, cwd: str | Path | None = None, stdin: str | None = None
) -> subprocess.CompletedProcess[str]:
    command = ["git"] + list(args)
    # Git emits UTF-8 for commit messages, paths, and porcelain output.
    # Locale-dependent decoding corrupted non-ASCII evidence on e.g. cp936
    # hosts, and strict decoding would crash on bytes Git legitimately emits.
    # surrogateescape round-trips unknown bytes through os.path operations.
    process = subprocess.run(
        command, cwd=str(cwd) if cwd is not None else None,
        input=stdin, capture_output=True,
        encoding="utf-8", errors="surrogateescape",
    )
    return process


def git_text(*args: str, cwd: str | Path | None = None) -> str:
    process = git(*args, cwd=cwd)
    if process.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed:\n{process.stderr.strip()}"
        )
    return process.stdout.strip()


def fetch(
    repository_path: str | Path,
    *,
    repository: str,
    pr_number: int,
    owner_token: str,
    remote: str = "origin",
) -> None:
    # Fetching updates shared refs and happens only behind the ownership lock.
    storage.ensure_active_campaign_owner(
        repository, pr_number, owner_token, repository_path=repository_path
    )
    output = git("fetch", remote, cwd=repository_path)
    if output.returncode != 0:
        raise RuntimeError(output.stderr.strip())


def remote_head(
    repository_path: str | Path, branch: str, *, remote: str = "origin"
) -> str:
    process = git(
        "ls-remote", remote, f"refs/heads/{branch}", cwd=repository_path
    )
    if process.returncode != 0 or not process.stdout.strip():
        raise RuntimeError(
            "Unable to determine remote head for "
            f"{remote}/refs/heads/{branch}: {process.stderr.strip()}"
        )
    return process.stdout.split()[0].strip()


def _canonical(path: str | Path) -> str:
    """Platform-native canonical form for containment and identity checks.

    Git reports Windows paths with forward slashes while Python resolves to
    backslashes; comparisons therefore normalize through the OS semantics
    (absolute, symlink-resolved, case-normalized where the platform is).
    """
    return os.path.normcase(os.path.realpath(os.path.abspath(str(path))))


def _same_location(a: Path, b: Path) -> bool:
    """True when both paths denote the same existing directory or file."""
    try:
        if a.samefile(b):
            return True
    except OSError:
        pass
    return _canonical(a) == _canonical(b)


def _registered_worktrees(repository_path: str | Path) -> list[Path]:
    # NUL-delimited porcelain (Git 2.36+) never quotes or mangles paths.
    listing = git_text(
        "worktree", "list", "--porcelain", "-z", cwd=repository_path
    )
    paths: list[Path] = []
    for record in listing.split("\0"):
        for line in record.splitlines():
            if line.startswith("worktree "):
                paths.append(Path(line[len("worktree "):].strip()))
    return paths


def add_worktree(
    repository: str,
    pr_number: int,
    commit: str,
    *,
    owner_token: str,
    repository_path: str | Path = ".",
    name: str | None = None,
) -> dict[str, str]:
    storage.ensure_active_campaign_owner(
        repository, pr_number, owner_token, repository_path=repository_path
    )
    root = worktree_root(repository, pr_number, repository_path=repository_path)
    root.mkdir(parents=True, exist_ok=True)
    suffix = name or commit[:12]
    target = root / suffix
    if target.exists():
        raise RuntimeError(f"Temporary worktree path already exists: {target}")
    process = git(
        "worktree", "add", "--detach", str(target), commit, cwd=repository_path
    )
    if process.returncode != 0:
        raise RuntimeError(process.stderr.strip())
    return {"path": str(target), "commit": commit}


def remove_worktree(
    path: str | Path,
    *,
    repository: str,
    pr_number: int,
    owner_token: str,
    repository_path: str | Path = ".",
    force: bool = False,
) -> dict[str, Any]:
    """Remove a v2 temporary worktree. Refuses anything outside v2 state."""
    storage.ensure_active_campaign_owner(
        repository, pr_number, owner_token, repository_path=repository_path
    )
    target = Path(path).resolve()
    root = _canonical(worktree_root(repository, pr_number, repository_path=repository_path))
    if not _canonical(target).startswith(root + os.sep):
        raise RuntimeError(
            f"Refusing to remove a worktree outside the v2 state directory: {target}"
        )
    registered = _registered_worktrees(repository_path)
    if not any(_same_location(target, candidate) for candidate in registered):
        raise RuntimeError(f"Not a registered worktree of this repository: {target}")
    args = ["worktree", "remove"]
    if force:
        args.append("--force")
    args.append(str(target))
    process = git(*args, cwd=repository_path)
    if process.returncode != 0:
        raise RuntimeError(process.stderr.strip())
    return {"removed": str(target)}


def worktree_is_registered(repository_path: str | Path, path: str | Path) -> bool:
    """True when the path is a registered worktree of this repository."""
    target = Path(path).resolve()
    return any(
        _same_location(target, candidate)
        for candidate in _registered_worktrees(repository_path)
    )


def stage_complete_delta(
    worktree: str | Path, *, runner: Callable[..., subprocess.CompletedProcess[str]] = git
) -> None:
    """Stage the complete non-ignored worktree delta (complete `git add -A`).

    Tracked modifications, tracked deletions, and non-ignored untracked files
    are staged; ignored files are excluded by Git's deterministic ignore
    rules. Unmerged index entries fail here and refuse publication.
    """
    staged = runner("add", "-A", "--", ".", cwd=str(worktree))
    if staged.returncode != 0:
        raise RuntimeError(staged.stderr.strip())


def write_tree(
    worktree: str | Path, *, runner: Callable[..., subprocess.CompletedProcess[str]] = git
) -> str:
    """Return the exact Git tree OID of the current worktree index."""
    written = runner("write-tree", cwd=str(worktree))
    if written.returncode != 0:
        raise RuntimeError(written.stderr.strip())
    return written.stdout.strip()


def head_tree_oid(
    worktree: str | Path, *, runner: Callable[..., subprocess.CompletedProcess[str]] = git
) -> str:
    """Return the tree OID of the worktree's HEAD commit."""
    resolved = runner("rev-parse", "HEAD^{tree}", cwd=str(worktree))
    if resolved.returncode != 0:
        raise RuntimeError(resolved.stderr.strip())
    return resolved.stdout.strip()


def commit_index_hook_free(
    worktree: str | Path,
    *,
    message: str,
    runner: Callable[..., subprocess.CompletedProcess[str]] = git,
) -> str:
    """Create one commit from the current index without executing Git hooks.

    ``core.hooksPath`` is pointed at a fresh empty directory for this one
    invocation, so no repository- or user-configured hook can run. The caller
    verifies the resulting commit's tree against the bound tree.
    """
    hooks_dir = tempfile.mkdtemp(prefix="crp-no-hooks-")
    try:
        committed = runner(
            "-c", f"core.hooksPath={hooks_dir}", "commit", "-m", message,
            cwd=str(worktree),
        )
        if committed.returncode != 0:
            raise RuntimeError(committed.stderr.strip())
        resolved = runner("rev-parse", "HEAD", cwd=str(worktree))
        if resolved.returncode != 0:
            raise RuntimeError(resolved.stderr.strip())
        return resolved.stdout.strip()
    finally:
        shutil.rmtree(hooks_dir, ignore_errors=True)


def push_publication_commit(
    *,
    worktree: str | Path,
    branch: str,
    expected_head: str,
    local_commit: str,
    repository: str,
    pr_number: int,
    owner_token: str,
    repository_path: str | Path = ".",
    remote: str = "origin",
    runner: Callable[..., subprocess.CompletedProcess[str]] = git,
    before_push: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Internal one-push publication tail, never force. Ambiguity fails closed.

    This is not a product-facing boundary: the deterministic remediation
    finalizer wraps it with round commitment and downstream mutation ordering.
    ``before_push`` runs as the final local authority operation immediately
    before the push attempt. The caller has already created ``local_commit``
    and validated the remote head; this helper re-checks the remote head,
    pushes exactly once, and classifies the outcome from re-observation.
    """
    storage.ensure_active_campaign_owner(
        repository, pr_number, owner_token, repository_path=repository_path
    )

    second = remote_head(repository_path, branch, remote=remote)
    if second != expected_head:
        return {
            "published": False,
            "status": "remote_head_advanced_after_commit",
            "expected_head": expected_head,
            "remote_head": second,
            "local_commit": local_commit,
        }

    if before_push is not None:
        before_push()
    push = runner(
        "push", remote, f"HEAD:refs/heads/{branch}", cwd=str(worktree)
    )
    if push.returncode != 0:
        # Re-observe to classify a potentially-accepted push instead of guessing.
        observed = remote_head(repository_path, branch, remote=remote)
        if observed == local_commit:
            return {"published": True, "status": "pushed", "commit": local_commit}
        if observed == expected_head:
            return {
                "published": False,
                "status": "push_failed_clean",
                "local_commit": local_commit,
                "remote_head": observed,
                "error": push.stderr.strip(),
            }
        return {
            "published": False,
            "status": "ambiguous_publication",
            "local_commit": local_commit,
            "remote_head": observed,
            "error": push.stderr.strip(),
        }

    confirmed = remote_head(repository_path, branch, remote=remote)
    if confirmed != local_commit:
        return {
            "published": False,
            "status": "ambiguous_publication",
            "local_commit": local_commit,
            "remote_head": confirmed,
        }
    return {"published": True, "status": "pushed", "commit": local_commit}


def main() -> None:
    parser = argparse.ArgumentParser(description="v2 local Git and publication helpers")
    subparsers = parser.add_subparsers(dest="command", required=True)

    owned = argparse.ArgumentParser(add_help=False)
    owned.add_argument("--repository-path", default=".")
    owned.add_argument("--repo", required=True)
    owned.add_argument("--pr", required=True, type=int)
    owned.add_argument("--owner-token", required=True)

    subparsers.add_parser("fetch", parents=[owned])

    head_parser = subparsers.add_parser("remote-head")
    head_parser.add_argument("--repository-path", default=".")
    head_parser.add_argument("--branch", required=True)

    add_parser = subparsers.add_parser("worktree-add", parents=[owned])
    add_parser.add_argument("--commit", required=True)
    add_parser.add_argument("--name")

    remove_parser = subparsers.add_parser("worktree-remove", parents=[owned])
    remove_parser.add_argument("--path", required=True)
    remove_parser.add_argument("--force", action="store_true")

    args = parser.parse_args()
    import json

    if args.command == "fetch":
        fetch(
            args.repository_path,
            repository=args.repo,
            pr_number=args.pr,
            owner_token=args.owner_token,
        )
        print("fetched")
    elif args.command == "remote-head":
        print(remote_head(args.repository_path, args.branch))
    elif args.command == "worktree-add":
        print(json.dumps(
            add_worktree(
                args.repo, args.pr, args.commit,
                owner_token=args.owner_token,
                repository_path=args.repository_path, name=args.name,
            ),
            indent=2,
        ))
    elif args.command == "worktree-remove":
        print(json.dumps(
            remove_worktree(
                args.path,
                repository=args.repo,
                pr_number=args.pr,
                owner_token=args.owner_token,
                repository_path=args.repository_path, force=args.force,
            ),
            indent=2,
        ))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(storage.error_text(error), file=sys.stderr)
        raise SystemExit(1) from error
