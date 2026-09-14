#!/usr/bin/env python3
"""Repository-associated v2 storage and the permanent PR-scoped ownership lock.

All runtime artifacts live under the Git common directory so every worktree of
one local repository installation shares them. They are never tracked and never
stored per worktree.

The ownership lock is permanent: no TTL, heartbeat, renewal, expiry, or stale
detection. Acquisition is an atomic O_EXCL create. An incomplete or corrupt lock
still blocks; only explicit user-authorized recovery removes it.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import platform
import secrets
import subprocess
import tempfile
from typing import Any, Iterator


LOCK_SCHEMA_VERSION = 1
LOCK_STATE_DIR = "codex-review-pulse"
LOCK_STATE_SUBDIR = "v2"


def git_common_directory(repository_path: str | Path = ".") -> Path:
    process = subprocess.run(
        [
            "git",
            "-C",
            str(repository_path),
            "rev-parse",
            "--path-format=absolute",
            "--git-common-dir",
        ],
        capture_output=True,
        text=True,
    )
    if process.returncode != 0:
        raise RuntimeError(
            process.stderr.strip() or "Unable to locate the Git common directory"
        )
    return Path(process.stdout.strip()).resolve()


def state_directory(repository_path: str | Path = ".") -> Path:
    return (
        git_common_directory(repository_path)
        / LOCK_STATE_DIR
        / LOCK_STATE_SUBDIR
    )


def _key_digest(repository: str, pr_number: int) -> str:
    key = f"{repository.strip().casefold()}#{pr_number}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]


def campaign_path(
    repository: str, pr_number: int, *, repository_path: str | Path = "."
) -> Path:
    return state_directory(repository_path) / (
        f"{_key_digest(repository, pr_number)}.campaign.json"
    )


def lock_path(
    repository: str, pr_number: int, *, repository_path: str | Path = "."
) -> Path:
    return state_directory(repository_path) / f"{_key_digest(repository, pr_number)}.lock"


def worktree_root(
    repository: str, pr_number: int, *, repository_path: str | Path = "."
) -> Path:
    return state_directory(repository_path) / "worktrees" / _key_digest(
        repository, pr_number
    )


def error_text(error: BaseException) -> str:
    """Console-safe diagnostic rendering.

    Diagnostics may carry surrogates from Git's surrogateescape decoding or
    characters a locale console cannot encode; re-encode as printable ASCII so
    reporting never raises on top of the original failure.
    """
    return str(error).encode("utf-8", "backslashreplace").decode("ascii")


def load_json(path: str | Path) -> dict[str, Any] | None:
    """Load JSON; return None when absent. Malformed content fails closed."""
    target = Path(path)
    if not target.exists():
        return None
    with target.open("r", encoding="utf-8") as stream:
        payload = json.load(stream)
    if not isinstance(payload, dict):
        raise ValueError(f"{target}: JSON root must be an object")
    return payload


def save_json(path: str | Path, payload: dict[str, Any]) -> None:
    """Atomically replace a JSON document using a sibling temporary file."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary_name = stream.name
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, target)
        temporary_name = None
    finally:
        if temporary_name is not None:
            try:
                Path(temporary_name).unlink()
            except FileNotFoundError:
                pass


def new_owner_token() -> str:
    return secrets.token_hex(32)


def lock_metadata(
    *,
    repository: str,
    pr_number: int,
    campaign_id: str,
    owner_token: str,
    acquired_at: str,
) -> dict[str, Any]:
    return {
        "schema_version": LOCK_SCHEMA_VERSION,
        "repository": repository.strip().casefold(),
        "pull_request_number": pr_number,
        "campaign_id": campaign_id,
        "owner_token": owner_token,
        "acquired_at": acquired_at,
        "host": platform.node(),
        "pid": os.getpid(),
    }


@contextmanager
def _guard(lock_file: Path) -> Iterator[None]:
    """Serialize local lock changes with an OS advisory lock on a sidecar file."""
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    sidecar = lock_file.with_name(lock_file.name + ".guard")
    stream = sidecar.open("a+b")
    try:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    finally:
        stream.close()


class LockHeld(RuntimeError):
    """Raised when a permanent lock already exists and cannot be acquired."""

    def __init__(self, status: str, metadata: dict[str, Any] | None = None) -> None:
        super().__init__(f"PR-scoped ownership lock is held (status={status})")
        self.status = status
        self.metadata = metadata


def _validate_lock_shape(
    payload: dict[str, Any], *, repository: str, pr_number: int
) -> dict[str, Any]:
    if payload.get("schema_version") != LOCK_SCHEMA_VERSION:
        raise ValueError("Unsupported lock schema version")
    if payload.get("repository") != repository.strip().casefold():
        raise ValueError("Lock repository does not match the requested repository")
    if payload.get("pull_request_number") != pr_number:
        raise ValueError("Lock pull request does not match the requested pull request")
    token = payload.get("owner_token")
    if not isinstance(token, str) or not token:
        raise ValueError("Lock owner token is invalid")
    campaign_id = payload.get("campaign_id")
    if not isinstance(campaign_id, str) or not campaign_id:
        raise ValueError("Lock campaign identity is invalid")
    return payload


def inspect_lock(
    repository: str, pr_number: int, *, repository_path: str | Path = "."
) -> dict[str, Any]:
    """Read lock status without mutating anything. Token is never returned."""
    target = lock_path(repository, pr_number, repository_path=repository_path)
    if not target.exists():
        return {"status": "absent", "exists": False}
    try:
        payload = load_json(target)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        return {"status": "invalid", "exists": True, "error": str(error)}
    if payload is None:
        return {"status": "invalid", "exists": True, "error": "lock file is empty"}
    try:
        metadata = _validate_lock_shape(
            payload, repository=repository, pr_number=pr_number
        )
    except ValueError as error:
        return {"status": "invalid", "exists": True, "error": str(error)}
    return {
        "status": "active",
        "exists": True,
        "campaign_id": metadata["campaign_id"],
        "acquired_at": metadata.get("acquired_at"),
        "host": metadata.get("host"),
        "pid": metadata.get("pid"),
    }


def acquire_lock(
    repository: str,
    pr_number: int,
    *,
    campaign_id: str,
    acquired_at: str,
    repository_path: str | Path = ".",
) -> dict[str, Any]:
    """Atomically acquire the permanent lock. Fails when any lock file exists."""
    target = lock_path(repository, pr_number, repository_path=repository_path)
    token = new_owner_token()
    metadata = lock_metadata(
        repository=repository,
        pr_number=pr_number,
        campaign_id=campaign_id,
        owner_token=token,
        acquired_at=acquired_at,
    )
    rendered = json.dumps(metadata, indent=2, sort_keys=True) + "\n"
    with _guard(target):
        current = inspect_lock(
            repository, pr_number, repository_path=repository_path
        )
        if current["status"] != "absent":
            raise LockHeld(current["status"], current)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        descriptor = os.open(target, flags, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(rendered)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            # The lock was atomically acquired even if metadata writing failed.
            # It stays in place and must be recovered explicitly.
            raise
    return {
        "acquired": True,
        "campaign_id": campaign_id,
        "owner_token": token,
        "lock_path": str(target),
    }


def verify_owner(
    repository: str,
    pr_number: int,
    owner_token: str,
    *,
    repository_path: str | Path = ".",
) -> dict[str, Any]:
    """Return lock metadata only when the caller proves the owner identity."""
    target = lock_path(repository, pr_number, repository_path=repository_path)
    payload = load_json(target)
    if payload is None:
        raise RuntimeError("Ownership lock does not exist")
    metadata = _validate_lock_shape(
        payload, repository=repository, pr_number=pr_number
    )
    if not secrets.compare_digest(metadata["owner_token"], owner_token):
        raise RuntimeError("Ownership token mismatch")
    return metadata


def ensure_owner(
    repository: str,
    pr_number: int,
    owner_token: str,
    *,
    repository_path: str | Path = ".",
) -> dict[str, Any]:
    """Prove lock ownership at a mutation boundary and cross-check campaign id.

    Used by every product-mutating helper so correctness does not depend on the
    worker model remembering the check.
    """
    metadata = verify_owner(
        repository, pr_number, owner_token, repository_path=repository_path
    )
    record = load_json(
        campaign_path(repository, pr_number, repository_path=repository_path)
    )
    if record is not None and record.get("campaign_id") != metadata["campaign_id"]:
        raise RuntimeError(
            "Ownership lock belongs to a different campaign; refusing mutation"
        )
    return metadata


def release_lock(
    repository: str,
    pr_number: int,
    owner_token: str,
    *,
    repository_path: str | Path = ".",
) -> dict[str, Any]:
    target = lock_path(repository, pr_number, repository_path=repository_path)
    with _guard(target):
        verify_owner(
            repository,
            pr_number,
            owner_token,
            repository_path=repository_path,
        )
        target.unlink()
    return {"released": True, "lock_path": str(target)}


def recover_lock(
    repository: str,
    pr_number: int,
    *,
    user_authorized_recovery: bool,
    expected_campaign_id: str | None = None,
    repository_path: str | Path = ".",
) -> dict[str, Any]:
    """Explicit human recovery boundary. Never call this automatically."""
    if not user_authorized_recovery:
        raise RuntimeError(
            "Lock recovery requires explicit user authorization; the permanent "
            "lock has no TTL or automatic stale detection"
        )
    target = lock_path(repository, pr_number, repository_path=repository_path)
    with _guard(target):
        current = inspect_lock(
            repository, pr_number, repository_path=repository_path
        )
        if current["status"] == "absent":
            return {"recovered": False, "status": "absent"}
        if (
            expected_campaign_id is not None
            and current["status"] == "active"
            and current.get("campaign_id") != expected_campaign_id
        ):
            raise RuntimeError(
                "Refusing recovery: the active lock belongs to a different "
                f"campaign ({current.get('campaign_id')})"
            )
        target.unlink()
    return {"recovered": True, "previous_status": current["status"]}


def _library_help() -> None:
    parser = argparse.ArgumentParser(
        description="Repository-associated storage and permanent ownership lock"
    )
    parser.add_argument(
        "--repository-path",
        default=".",
        help="Resolve and print the v2 state directory for this repository",
    )
    args = parser.parse_args()
    print(state_directory(args.repository_path))


if __name__ == "__main__":
    _library_help()
