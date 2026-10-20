#!/usr/bin/env python3
"""Common execution controls, not campaign authorization or a process sandbox.

Trusted host Git/configuration/credentials remain available. New calls inherit
common controls; new subcommands/options still need focused reachability review.
No repository configuration is persistently rewritten.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Callable

Executor = Callable[..., subprocess.CompletedProcess[str]]


class GitPolicyRefused(RuntimeError):
    """A known unsupported feature was refused before its operation started."""


COMMON_CONFIG = (
    "core.fsmonitor=false", "maintenance.auto=false", "gc.auto=0",
    "maintenance.autoDetach=false", "gc.autoDetach=false",
    "promisor.acceptFromServer=none",
)
_REDIRECT_ENV = (
    "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_NAMESPACE", "GIT_SHALLOW_FILE", "GIT_QUARANTINE_PATH", "GIT_ATTR_SOURCE",
)


def _environment() -> dict[str, str]:
    env = os.environ.copy()
    for key in _REDIRECT_ENV:
        env.pop(key, None)
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _require(result: subprocess.CompletedProcess[str]) -> str:
    if result.returncode:
        raise GitPolicyRefused(result.stderr.strip() or "Git policy observation failed")
    return result.stdout


def _configuration(call: Callable[..., subprocess.CompletedProcess[str]]) -> list[tuple[str, str, str]]:
    raw = _require(call(["config", "--null", "--show-scope", "--list"]))
    parts = raw.split("\0")
    if parts[-1] != "" or len(parts) % 2 != 1:
        raise GitPolicyRefused("Unable to parse effective Git configuration")
    records = []
    for i in range(0, len(parts) - 1, 2):
        key, sep, value = parts[i + 1].partition("\n")
        records.append((parts[i], key, value if sep else ""))
    return records


def _values(records: list[tuple[str, str, str]], key: str) -> list[str]:
    return [value for _, name, value in records if name == key]


def _signing(records: list[tuple[str, str, str]], key: str) -> None:
    values = _values(records, key)
    if values and values[-1].lower() not in ("false", "no", "off", "0"):
        raise GitPolicyRefused(f"Unsupported required signing: {key}")


def _transport(records: list[tuple[str, str, str]]) -> None:
    for scope, key, _ in records:
        # Passing a get-url-expanded URL back to Git can apply rewriting again.
        # Refuse rewriting rather than promising a single canonical destination.
        if key.startswith("url."):
            raise GitPolicyRefused(f"Unsupported URL rewriting: {key}")
        if scope not in ("local", "worktree"):
            continue
        if (key in ("core.sshcommand", "core.askpass")
                or (key.startswith("credential.") and key.endswith("helper"))
                or re.fullmatch(r"remote\..*\.(uploadpack|receivepack|proxy|vcs)", key)
                or key in ("core.gitproxy", "ssh.variant", "core.alternaterefscommand")):
            raise GitPolicyRefused(f"Unsupported repository-selected transport helper: {key}")


def _destination(call: Callable[..., subprocess.CompletedProcess[str]], remote: str,
                 *, push: bool) -> str:
    args = ["remote", "get-url"] + (["--push"] if push else []) + ["--all", remote]
    output = _require(call(args))
    urls = output.removesuffix("\n").split("\n")
    if len(urls) != 1 or not urls[0] or urls[0].startswith("-") or "\r" in urls[0]:
        raise GitPolicyRefused("Controller requires exactly one remote destination")
    url = urls[0]
    # Native SSH/HTTPS and local bare remotes only, never arbitrary remote
    # helpers (ext::, custom schemes). Host transport/server programs are trusted.
    if "::" in url or (re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", url)
                          and not url.startswith(("https://", "ssh://", "file://"))):
        raise GitPolicyRefused("Unsupported Git transport URL")
    if push:
        fetch_url = _destination(call, remote, push=False)
        if url != fetch_url:
            raise GitPolicyRefused("Distinct fetch/push destinations are unsupported")
    return url


def _paths(call: Callable[..., subprocess.CompletedProcess[str]]) -> list[str]:
    return list(dict.fromkeys(p for p in _require(call([
        "ls-files", "-z", "--cached", "--others", "--exclude-standard"
    ])).split("\0") if p))


def _filters(call: Callable[..., subprocess.CompletedProcess[str]], paths: list[str],
             *, cached: bool, ambiguous_drivers: set[str]) -> None:
    if not paths:
        return
    args = ["check-attr", "-z"] + (["--cached"] if cached else []) + ["--stdin", "filter"]
    result = _require(call(args, stdin="\0".join(paths) + "\0"))
    items = result.split("\0")
    if items[-1] != "" or (len(items) - 1) % 3:
        raise GitPolicyRefused("Unable to parse effective filter attributes")
    for i in range(0, len(items) - 1, 3):
        if items[i + 2] not in ("unspecified", "unset") or items[i + 2] in ambiguous_drivers:
            raise GitPolicyRefused(f"Unsupported content filter on {items[i]}: {items[i + 2]}")


def _lfs_tree(call: Callable[..., subprocess.CompletedProcess[str]], tree: str) -> None:
    listing = _require(call(["ls-tree", "-r", "-z", tree]))
    blobs = list(dict.fromkeys(entry.split("\t", 1)[0].split()[2]
                              for entry in listing.split("\0") if entry
                              and entry.split("\t", 1)[0].split()[1] == "blob"))
    if not blobs:
        return
    # Raw blob plumbing, never textconv/filters. Parse byte sizes after
    # round-tripping surrogateescape, not Unicode character counts.
    sizes = _require(call(["cat-file", "--batch-check"], stdin="\n".join(blobs) + "\n"))
    candidates = []
    for oid, line in zip(blobs, sizes.splitlines(), strict=True):
        fields = line.split()
        if len(fields) != 3 or fields[:2] != [oid, "blob"]:
            raise GitPolicyRefused("Unable to inspect publication blob sizes")
        # LFS pointers are at most 1024 bytes; do not load normal binary blobs.
        if int(fields[2]) <= 1024:
            candidates.append(oid)
    if not candidates:
        return
    raw = _require(call(["cat-file", "--batch"], stdin="\n".join(candidates) + "\n")).encode(
        "utf-8", "surrogateescape")
    offset = 0
    for oid in candidates:
        end = raw.find(b"\n", offset)
        header = raw[offset:end].split()
        if len(header) != 3 or header[0].decode() != oid or header[1] != b"blob":
            raise GitPolicyRefused("Unable to inspect publication blobs")
        size = int(header[2])
        offset = end + 1
        content = raw[offset:offset + size]
        if content.startswith(b"version https://git-lfs.github.com/spec/v1"):
            raise GitPolicyRefused("Unsupported LFS publication: object upload completeness is not established")
        offset += size + 1
    if offset != len(raw):
        raise GitPolicyRefused("Incomplete publication blob inspection")


def run_controller_git(*args: str, cwd: str | Path | None = None,
                       stdin: str | None = None, executor: Executor | None = None,
                       before_execute: Callable[[], None] | None = None
                       ) -> subprocess.CompletedProcess[str]:
    """Execute a reviewed controller operation; injection is below policy.

    Feature preflights apply only to operations exercising those features.
    Metadata/ownership/release never require content/publication support.
    Nonzero operation exits return intact; no retries or check=True.
    """
    executor = executor or subprocess.run
    env = _environment()
    hooks_dir = tempfile.mkdtemp(prefix="crp-no-hooks-")
    config = [f"core.hooksPath={Path(hooks_dir).as_posix()}", *COMMON_CONFIG]

    def call(command: list[str], *, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
        cmd = ["git"]
        for setting in config:
            cmd.extend(("-c", setting))
        cmd.extend(command)
        if (command == ["cat-file", "--batch"] or command[:2] == ["remote", "get-url"]
                or "-z" in command or "--null" in command):
            # Universal-newline translation corrupts byte-counted blobs and
            # NUL-delimited names/config values and remote destinations with
            # literal CR bytes. Binary I/O preserves their identity.
            result = executor(cmd, cwd=str(cwd) if cwd is not None else None,
                              env=env, input=stdin.encode("utf-8", "surrogateescape") if stdin else None,
                              capture_output=True)
            return subprocess.CompletedProcess(
                result.args, result.returncode,
                result.stdout.decode("utf-8", "surrogateescape"),
                result.stderr.decode("utf-8", "surrogateescape"),
            )
        return executor(cmd, cwd=str(cwd) if cwd is not None else None,
                        env=env, input=stdin, capture_output=True,
                        encoding="utf-8", errors="surrogateescape")

    try:
        records = _configuration(call)
        ambiguous_drivers = {name for name in ("unset", "unspecified")
                             if any(key.startswith(f"filter.{name}.") and
                                    key.rsplit(".", 1)[-1] in ("clean", "smudge", "process")
                                    for _, key, _ in records)}
        # Git <=2.53 has no config-defined hooks. >=2.54 honors per-name
        # enabled=false. Enumerate effective names, including includes and
        # repeated events: hooksPath alone does not cover this mechanism.
        names = {key[5:].rsplit(".", 1)[0] for _, key, _ in records
                 if key.startswith("hook.") and key.endswith((".event", ".command"))}
        config.extend(f"hook.{name}.enabled=false" for name in sorted(names))
        command = list(args)
        if not command or command[0].startswith("-"):
            raise GitPolicyRefused("Caller-supplied Git global options are not supported")
        op = command[0]
        if op in ("add", "commit", "push", "fetch", "ls-remote") or (
                op == "worktree" and command[1:2] == ["add"]):
            if any(key.startswith("includeif.") for _, key, _ in records):
                raise GitPolicyRefused("Conditional Git includes are unsupported for content/transport operations")
        if op == "worktree" and command[1:2] == ["add"]:
            attrs = _values(records, "core.attributesfile")
            if attrs and attrs[-1] and not Path(os.path.expanduser(attrs[-1])).is_absolute():
                raise GitPolicyRefused("Relative attributesFile is unsupported during worktree checkout")
        # Small reviewed surface, not a generic command classifier. New calls
        # using these shapes inherit policy; new behavior needs support review.
        if op not in {"config", "rev-parse", "worktree", "ls-files", "read-tree",
                      "check-attr", "ls-tree", "cat-file", "add", "write-tree",
                      "commit", "push", "fetch", "ls-remote", "remote"}:
            raise GitPolicyRefused(f"Unreviewed controller Git command: {op}")
        if op == "worktree" and command[1:2] not in (["list"], ["add"], ["remove"]):
            raise GitPolicyRefused("Unreviewed controller worktree operation")
        if op == "remote" and command[1:2] != ["get-url"]:
            raise GitPolicyRefused("Only remote metadata discovery is supported")
        if op == "config" and command[1:] != ["--null", "--show-scope", "--list"]:
            raise GitPolicyRefused("Only effective configuration observation is supported")
        # Object access in a partial clone can launch lazy fetches, including
        # under local observations. Refuse object-consuming operations, not
        # rev-parse/config/worktree-list used for storage and lock release.
        if op in ("read-tree", "ls-tree", "cat-file", "add", "commit", "push", "fetch") or (
                op == "worktree" and command[1:2] == ["add"]):
            if any(key == "extensions.partialclone" or (
                    re.fullmatch(r"remote\..*\.promisor", key)
                    and value.lower() not in ("false", "no", "off", "0"))
                   for _, key, value in records):
                raise GitPolicyRefused("Partial-clone lazy fetching is unsupported")
        if op == "push" and _values(records, "core.alternaterefscommand"):
            raise GitPolicyRefused("Alternate-ref execution helpers are unsupported")
        if op == "read-tree" and len(command) != 2:
            raise GitPolicyRefused("Only isolated-index read-tree is supported")
        if op == "cat-file" and command[1:] not in (["--batch"], ["--batch-check"]):
            raise GitPolicyRefused("Only raw blob batch inspection is supported")
        if op == "worktree" and command[1:2] == ["add"]:
            # Isolated read-tree index: no checkout/conversion. Git resolves
            # nested target-commit, info and global attributes via --cached.
            with tempfile.TemporaryDirectory(prefix="crp-attribute-index-") as tmp:
                env["GIT_INDEX_FILE"] = str(Path(tmp) / "index")
                try:
                    _require(call(["read-tree", command[-1]]))
                    _filters(call, _paths(call), cached=True, ambiguous_drivers=ambiguous_drivers)
                finally:
                    env.pop("GIT_INDEX_FILE", None)
        elif op == "add":
            paths = _paths(call)
            _filters(call, paths, cached=False, ambiguous_drivers=ambiguous_drivers)
            _filters(call, paths, cached=True, ambiguous_drivers=ambiguous_drivers)
        elif op == "commit":
            _signing(records, "commit.gpgsign")
        elif op == "push":
            _signing(records, "push.gpgsign")
            _transport(records)
            if len(command) != 4 or command[1] != "--no-verify" or not re.fullmatch(
                    r"HEAD:refs/heads/.+", command[3]):
                raise GitPolicyRefused("Unsupported controller push shape")
            _lfs_tree(call, "HEAD")
            url = _destination(call, command[2], push=True)
            config.extend(("push.followTags=false", "push.pushOption=",
                           "push.recurseSubmodules=no", "push.gpgSign=false"))
            command = ["push", "--no-verify", "--no-mirror", "--no-follow-tags", "--no-signed",
                       "--recurse-submodules=no", "--", url, command[3]]
        elif op in ("fetch", "ls-remote"):
            _transport(records)
            if len(command) < 2:
                raise GitPolicyRefused("Missing controller remote")
            url = _destination(call, command[1], push=False)
            if op == "ls-remote":
                command[1] = url
            else:
                # Preserve configured fetch refspecs via the verified remote.
                command = ["fetch", "--no-recurse-submodules", "--no-auto-maintenance", *args[1:]]
        if before_execute is not None:
            before_execute()
        return call(command, stdin=stdin)
    finally:
        shutil.rmtree(hooks_dir, ignore_errors=True)


def preflight_publication(worktree: str | Path, tree: str,
                          *, executor: Executor | None = None) -> None:
    """Check support before round commitment; push repeats before execution.

    Not a receipt/reservation/authorization: tree identity is controller-derived.
    """
    def call(args: list[str], *, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
        return run_controller_git(*args, cwd=worktree, stdin=stdin, executor=executor)

    records = _configuration(call)
    _signing(records, "push.gpgsign")
    _transport(records)
    _destination(call, "origin", push=True)
    _lfs_tree(call, tree)


if __name__ == "__main__":
    import argparse
    argparse.ArgumentParser(description=__doc__).parse_args()
