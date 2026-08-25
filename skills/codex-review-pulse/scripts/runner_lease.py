#!/usr/bin/env python3
"""Repository/PR-scoped renewable runner leases with owner comparison."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import sys
from typing import Any, Iterator

from checkpoint_store import load_checkpoint, save_checkpoint
from state_model import canonical_repository


LEASE_SCHEMA_VERSION = 1


def _utc(value: str | datetime) -> datetime:
    parsed = (
        value
        if isinstance(value, datetime)
        else datetime.fromisoformat(value.replace("Z", "+00:00"))
    )
    if parsed.tzinfo is None:
        raise ValueError("Lease time must include a timezone")
    return parsed.astimezone(UTC)


def _format(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


@contextmanager
def _operation_lock(lease_path: Path) -> Iterator[None]:
    """Serialize lease changes with an OS-released advisory byte-range lock."""
    lease_path.parent.mkdir(parents=True, exist_ok=True)
    guard = lease_path.with_name(lease_path.name + ".guard")
    stream = guard.open("a+b")
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


def _validate_lease(
    payload: dict[str, Any], *, repository: str, pr_number: int
) -> dict[str, Any]:
    if payload.get("schema_version") != LEASE_SCHEMA_VERSION:
        raise ValueError("Unsupported lease schema version")
    if payload.get("repository") != canonical_repository(repository):
        raise ValueError("Lease repository mismatch")
    if payload.get("pull_request_number") != pr_number:
        raise ValueError("Lease pull request mismatch")
    if not isinstance(payload.get("owner_token"), str) or not payload["owner_token"]:
        raise ValueError("Lease owner token is invalid")
    for field in ("acquired_at", "renewed_at", "expires_at"):
        _utc(payload.get(field, ""))
    return payload


def inspect_lease(
    path: str | Path,
    *,
    repository: str,
    pr_number: int,
    now: str | datetime,
) -> dict[str, Any]:
    """Read lease status without creating, renewing, recovering, or deleting it."""
    lease_path = Path(path)
    if not lease_path.exists():
        return {"status": "absent", "exists": False}
    try:
        payload = load_checkpoint(lease_path)
        if payload is None:
            return {"status": "absent", "exists": False}
        lease = _validate_lease(payload, repository=repository, pr_number=pr_number)
        expired = _utc(lease["expires_at"]) <= _utc(now)
        return {
            "status": "expired" if expired else "active",
            "exists": True,
            **{key: value for key, value in lease.items() if key != "owner_token"},
        }
    except (OSError, ValueError, json.JSONDecodeError) as error:
        return {"status": "invalid", "exists": True, "error": str(error)}


def acquire_lease(
    path: str | Path,
    *,
    repository: str,
    pr_number: int,
    owner_token: str,
    now: str | datetime,
    duration_seconds: int,
) -> dict[str, Any]:
    if not owner_token:
        raise ValueError("Lease owner token is required")
    if not isinstance(duration_seconds, int) or not 30 <= duration_seconds <= 3600:
        raise ValueError("Lease duration must be between 30 and 3600 seconds")
    lease_path = Path(path)
    current_time = _utc(now)
    with _operation_lock(lease_path):
        existing = load_checkpoint(lease_path)
        recovered = False
        if existing is not None:
            lease = _validate_lease(existing, repository=repository, pr_number=pr_number)
            if _utc(lease["expires_at"]) > current_time:
                return {"acquired": False, "stale_recovered": False, "lease": lease}
            recovered = True
        lease = {
            "schema_version": LEASE_SCHEMA_VERSION,
            "repository": canonical_repository(repository),
            "pull_request_number": pr_number,
            "owner_token": owner_token,
            "acquired_at": _format(current_time),
            "renewed_at": _format(current_time),
            "expires_at": _format(current_time + timedelta(seconds=duration_seconds)),
        }
        save_checkpoint(lease_path, lease)
        return {"acquired": True, "stale_recovered": recovered, "lease": lease}


def renew_lease(
    path: str | Path,
    *,
    repository: str,
    pr_number: int,
    owner_token: str,
    now: str | datetime,
    duration_seconds: int,
) -> dict[str, Any]:
    lease_path = Path(path)
    current_time = _utc(now)
    with _operation_lock(lease_path):
        existing = load_checkpoint(lease_path)
        if existing is None:
            raise RuntimeError("Lease does not exist")
        lease = _validate_lease(existing, repository=repository, pr_number=pr_number)
        if lease["owner_token"] != owner_token:
            raise RuntimeError("Lease owner token mismatch")
        if _utc(lease["expires_at"]) <= current_time:
            raise RuntimeError("Lease has expired and cannot be renewed")
        renewed = dict(lease)
        renewed["renewed_at"] = _format(current_time)
        renewed["expires_at"] = _format(
            current_time + timedelta(seconds=duration_seconds)
        )
        save_checkpoint(lease_path, renewed)
        return renewed


def assert_lease_owner(
    path: str | Path,
    *,
    repository: str,
    pr_number: int,
    owner_token: str,
    now: str | datetime | None = None,
) -> dict[str, Any]:
    payload = load_checkpoint(path)
    if payload is None:
        raise RuntimeError("Recurring mutation requires an acquired lease")
    lease = _validate_lease(payload, repository=repository, pr_number=pr_number)
    if lease["owner_token"] != owner_token:
        raise RuntimeError("Lease owner token mismatch")
    current_time = _utc(now or datetime.now(UTC))
    if _utc(lease["expires_at"]) <= current_time:
        raise RuntimeError("Lease has expired")
    return lease


def release_lease(
    path: str | Path,
    *,
    repository: str,
    pr_number: int,
    owner_token: str,
) -> dict[str, Any]:
    lease_path = Path(path)
    with _operation_lock(lease_path):
        existing = load_checkpoint(lease_path)
        if existing is None:
            raise RuntimeError("Lease does not exist")
        lease = _validate_lease(existing, repository=repository, pr_number=pr_number)
        if lease["owner_token"] != owner_token:
            raise RuntimeError("Lease owner token mismatch")
        lease_path.unlink()
        return {"released": True, "owner_token": owner_token}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Inspect or manage a PR-scoped runner lease")
    parser.add_argument("--lease-file", required=True, type=Path)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pr", required=True, type=int)
    commands = parser.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser("inspect")
    inspect.add_argument("--now", required=True)
    for name in ("acquire", "renew"):
        command = commands.add_parser(name)
        command.add_argument("--owner-token", required=True)
        command.add_argument("--now", required=True)
        command.add_argument("--duration-seconds", type=int, default=300)
    release = commands.add_parser("release")
    release.add_argument("--owner-token", required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    common = {"repository": args.repo, "pr_number": args.pr}
    if args.command == "inspect":
        result = inspect_lease(args.lease_file, now=args.now, **common)
    elif args.command == "acquire":
        result = acquire_lease(
            args.lease_file,
            owner_token=args.owner_token,
            now=args.now,
            duration_seconds=args.duration_seconds,
            **common,
        )
    elif args.command == "renew":
        result = renew_lease(
            args.lease_file,
            owner_token=args.owner_token,
            now=args.now,
            duration_seconds=args.duration_seconds,
            **common,
        )
    else:
        result = release_lease(args.lease_file, owner_token=args.owner_token, **common)
    if isinstance(result.get("lease"), dict):
        result["lease"].pop("owner_token", None)
    result.pop("owner_token", None)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from error
