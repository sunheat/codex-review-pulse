#!/usr/bin/env python3
"""Deterministic local Git operations: fetch, temporary worktrees, publication.

The user's primary worktree is never modified. Remediation runs in detached
temporary worktrees under the repository-associated v2 state directory. A batch
creates at most one commit and one push, never an empty commit, never a
force-push, and a remote head advancement aborts publication.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
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
    storage.ensure_owner(
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
    storage.ensure_owner(
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
    storage.ensure_owner(
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


def _inside(worktree: Path, path: str) -> Path:
    """Resolve a publication path strictly inside the worktree."""
    wt = _canonical(worktree)
    candidate = Path(os.path.realpath(os.path.abspath(str(worktree / path))))
    if not _canonical(candidate).startswith(wt + os.sep):
        raise RuntimeError(f"Publication path escapes the worktree: {path}")
    return candidate


def publish_batch(
    *,
    worktree: str | Path,
    branch: str,
    paths: list[str],
    commit_message: str,
    expected_head: str,
    repository: str,
    pr_number: int,
    owner_token: str,
    repository_path: str | Path = ".",
    remote: str = "origin",
    runner: Callable[..., subprocess.CompletedProcess[str]] = git,
) -> dict[str, Any]:
    """Publish one batch: at most one commit, one push, never force.

    Returns a dict with published status. Ambiguous publication fails closed.
    """
    storage.ensure_owner(
        repository, pr_number, owner_token, repository_path=repository_path
    )
    wt = Path(worktree).resolve()
    if not paths:
        raise ValueError("At least one explicit path is required for publication")
    resolved_paths = [
        os.path.relpath(str(_inside(wt, p)), str(wt)) for p in paths
    ]

    def g(*args: str) -> subprocess.CompletedProcess[str]:
        return runner(*args, cwd=wt)

    before = remote_head(repository_path, branch, remote=remote)
    if before != expected_head:
        return {
            "published": False,
            "status": "remote_head_advanced",
            "expected_head": expected_head,
            "remote_head": before,
        }

    staged = g("add", "--", *resolved_paths)
    if staged.returncode != 0:
        raise RuntimeError(staged.stderr.strip())
    diff = g("diff", "--cached", "--quiet")
    if diff.returncode == 0:
        return {"published": False, "status": "no_changes"}
    if diff.returncode != 1:
        raise RuntimeError(diff.stderr.strip())

    committed = g("commit", "-m", commit_message)
    if committed.returncode != 0:
        raise RuntimeError(committed.stderr.strip())
    local_head = g("rev-parse", "HEAD").stdout.strip()

    second = remote_head(repository_path, branch, remote=remote)
    if second != expected_head:
        return {
            "published": False,
            "status": "remote_head_advanced_after_commit",
            "expected_head": expected_head,
            "remote_head": second,
            "local_commit": local_head,
        }

    push = runner(
        "push", remote, f"HEAD:refs/heads/{branch}", cwd=wt
    )
    if push.returncode != 0:
        # Re-observe to classify a potentially-accepted push instead of guessing.
        observed = remote_head(repository_path, branch, remote=remote)
        if observed == local_head:
            return {"published": True, "status": "pushed", "commit": local_head}
        if observed == expected_head:
            return {
                "published": False,
                "status": "push_failed_clean",
                "local_commit": local_head,
                "remote_head": observed,
                "error": push.stderr.strip(),
            }
        return {
            "published": False,
            "status": "ambiguous_publication",
            "local_commit": local_head,
            "remote_head": observed,
            "error": push.stderr.strip(),
        }

    confirmed = remote_head(repository_path, branch, remote=remote)
    if confirmed != local_head:
        return {
            "published": False,
            "status": "ambiguous_publication",
            "local_commit": local_head,
            "remote_head": confirmed,
        }
    return {"published": True, "status": "pushed", "commit": local_head}


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

    publish_parser = subparsers.add_parser("publish", parents=[owned])
    publish_parser.add_argument("--worktree", required=True)
    publish_parser.add_argument("--branch", required=True)
    publish_parser.add_argument("--path", action="append", required=True, dest="paths")
    publish_parser.add_argument("--message", required=True)
    publish_parser.add_argument("--expected-head", required=True)

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
    elif args.command == "publish":
        print(json.dumps(
            publish_batch(
                worktree=args.worktree,
                branch=args.branch,
                paths=args.paths,
                commit_message=args.message,
                expected_head=args.expected_head,
                repository=args.repo,
                pr_number=args.pr,
                owner_token=args.owner_token,
                repository_path=args.repository_path,
            ),
            indent=2,
        ))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(storage.error_text(error), file=sys.stderr)
        raise SystemExit(1) from error
