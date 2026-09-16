#!/usr/bin/env python3
"""Repository-associated v2 storage and the permanent PR-scoped ownership lock.

All runtime artifacts live under the Git common directory so every worktree of
one local repository installation shares them. They are never tracked and never
stored per worktree.

The ownership lock is permanent: no TTL, heartbeat, renewal, expiry, or stale
detection. Acquisition is an atomic O_EXCL create. A malformed or corrupt lock
still blocks, is classified as invalid (not ordinary contention), and is
removed only through explicit user-authorized recovery.

Every local operation that changes or atomically depends on campaign/lock
identity runs inside one canonical per-PR sidecar guard (``_guard``). The sidecar
guard is short local serialization only: it is not the ownership lock, a lease,
a TTL, a heartbeat, or distributed fencing, and no network operation may run
while it is held. Public operations acquire the guard and delegate to
``*_unlocked`` internal helpers so callers already inside the guard never
re-acquire it.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import secrets
import subprocess
import tempfile
from typing import Any, Callable, Iterator

import campaign_model as model


LOCK_SCHEMA_VERSION = 1
LOCK_STATE_DIR = "codex-review-pulse"
LOCK_STATE_SUBDIR = "v2"

# owner tokens are secrets.token_hex(32): exactly 64 lowercase hex characters.
OWNER_TOKEN_RE = re.compile(r"^[0-9a-f]{64}$")


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
    """Serialize local lock changes with an OS advisory lock on a sidecar file.

    This is the one canonical per-PR guard namespace. Never nest it: public
    operations take the guard and call ``*_unlocked`` internals instead.
    """
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


def _validate_lock_shape(
    payload: dict[str, Any], *, repository: str, pr_number: int
) -> dict[str, Any]:
    """Semantic validation of persisted lock authority fields.

    Diagnostic-only metadata (host, pid) is deliberately not validated: no
    authority decision depends on it.
    """
    version = payload.get("schema_version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise ValueError("Unsupported lock schema version")
    if version != LOCK_SCHEMA_VERSION:
        raise ValueError("Unsupported lock schema version")
    campaign_id = payload.get("campaign_id")
    if not isinstance(campaign_id, str) or not model.CAMPAIN_ID_RE.fullmatch(campaign_id):
        raise ValueError("Lock campaign identity is invalid")
    expected_repository = model.canonical_repository(repository)
    if payload.get("repository") != expected_repository:
        raise ValueError("Lock repository does not match the requested repository")
    number = payload.get("pull_request_number")
    if (
        not isinstance(number, int)
        or isinstance(number, bool)
        or number < 1
        or number != pr_number
    ):
        raise ValueError("Lock pull request does not match the requested pull request")
    token = payload.get("owner_token")
    if not isinstance(token, str) or not OWNER_TOKEN_RE.fullmatch(token):
        raise ValueError("Lock owner token is invalid")
    try:
        model.parse_timestamp(payload.get("acquired_at"))
    except ValueError as error:
        raise ValueError("Lock acquired_at is invalid") from error
    return payload


def _inspect_lock_unlocked(
    repository: str, pr_number: int, *, repository_path: str | Path = "."
) -> dict[str, Any]:
    """Read/classify the permanent lock. Caller holds the canonical guard.

    Because every writer serializes on the same guard, a reader can never
    observe the short O_EXCL-to-metadata-write interval of a normal
    acquisition and misclassify it as corruption.
    """
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


def inspect_lock(
    repository: str, pr_number: int, *, repository_path: str | Path = "."
) -> dict[str, Any]:
    """Stable public lock inspection: read status under the canonical guard.

    Token is never returned. Outcomes: absent / active / invalid.
    """
    with _guard(lock_path(repository, pr_number, repository_path=repository_path)):
        return _inspect_lock_unlocked(
            repository, pr_number, repository_path=repository_path
        )


def _require_supported_campaign_id(campaign_id: str) -> None:
    if not isinstance(campaign_id, str) or not model.CAMPAIN_ID_RE.fullmatch(campaign_id):
        raise ValueError("Campaign id must have the form crp-YYYYMMDDTHHMMSSZ-hex")


def _create_lock_unlocked(
    target: Path,
    *,
    repository: str,
    pr_number: int,
    campaign_id: str,
    acquired_at: str,
) -> dict[str, Any]:
    """O_EXCL-create the permanent lock. Caller holds the guard and has proven
    lock absence plus the purpose-specific campaign predicate."""
    token = new_owner_token()
    metadata = lock_metadata(
        repository=repository,
        pr_number=pr_number,
        campaign_id=campaign_id,
        owner_token=token,
        acquired_at=acquired_at,
    )
    rendered = json.dumps(metadata, indent=2, sort_keys=True) + "\n"
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
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


def _load_valid_campaign_unlocked(
    repository: str, pr_number: int, *, repository_path: str | Path = "."
) -> dict[str, Any]:
    """Load and fully validate the campaign record; absent/malformed fails."""
    path = campaign_path(repository, pr_number, repository_path=repository_path)
    record = load_json(path)
    if record is None:
        raise RuntimeError(f"Campaign record does not exist: {path}")
    model.validate_campaign(record, repository=repository, pull_request_number=pr_number)
    return record


def _setup_refusal_unlocked(
    repository: str, pr_number: int, *, repository_path: str | Path = "."
) -> dict[str, Any] | None:
    """Setup may acquire only when the campaign record is absent."""
    if campaign_path(repository, pr_number, repository_path=repository_path).exists():
        return {"status": "campaign_exists"}
    return None


def _worker_refusal_unlocked(
    repository: str,
    pr_number: int,
    *,
    campaign_id: str,
    repository_path: str | Path = ".",
) -> dict[str, Any] | None:
    """Ordinary workers may acquire only a valid matching active campaign."""
    path = campaign_path(repository, pr_number, repository_path=repository_path)
    if not path.exists():
        return {"status": "campaign_absent"}
    try:
        record = load_json(path)
        if record.get("campaign_id") != campaign_id:
            return {
                "status": "campaign_identity_mismatch",
                "record_campaign_id": record.get("campaign_id"),
            }
        model.validate_campaign(record, repository=repository, pull_request_number=pr_number)
    except ValueError as error:
        return {"status": "campaign_malformed", "detail": str(error)}
    if record["status"] != model.ACTIVE:
        return {"status": "campaign_terminal", "campaign_status": record["status"]}
    return None


def _rollover_refusal_unlocked(
    repository: str,
    pr_number: int,
    *,
    campaign_id: str,
    repository_path: str | Path = ".",
) -> dict[str, Any] | None:
    """Rollover may acquire only a valid allowlisted fully-consumed terminal C1."""
    path = campaign_path(repository, pr_number, repository_path=repository_path)
    if not path.exists():
        return {"status": "campaign_absent"}
    try:
        record = load_json(path)
        if record.get("campaign_id") != campaign_id:
            return {
                "status": "campaign_identity_mismatch",
                "record_campaign_id": record.get("campaign_id"),
            }
        model.validate_campaign(record, repository=repository, pull_request_number=pr_number)
    except ValueError as error:
        return {"status": "campaign_malformed", "detail": str(error)}
    if not model.is_terminal(record):
        return {"status": "campaign_active", "campaign_status": record["status"]}
    if not model.is_rollover_eligible(record):
        return {
            "status": "campaign_not_rolloverable",
            "campaign_status": record["status"],
        }
    return None


def _acquire_lock_with_predicate(
    repository: str,
    pr_number: int,
    *,
    campaign_id: str,
    acquired_at: str,
    refusal: Callable[[], dict[str, Any] | None],
    repository_path: str | Path = ".",
) -> dict[str, Any]:
    """Lock-first acquisition inside one canonical-guard critical section.

    Decision order: classify the permanent lock first; a semantically valid
    lock is an immediate busy without interpreting campaign state; an invalid
    lock fails closed; only when the lock is absent is the purpose-specific
    campaign predicate evaluated, and the O_EXCL creation happens in the same
    critical section. This ordering does not weaken atomic check-and-create.
    """
    _require_supported_campaign_id(campaign_id)
    model.parse_timestamp(acquired_at)
    target = lock_path(repository, pr_number, repository_path=repository_path)
    with _guard(target):
        current = _inspect_lock_unlocked(
            repository, pr_number, repository_path=repository_path
        )
        if current["status"] == "active":
            return {"acquired": False, "status": "busy", "lock": current}
        if current["status"] == "invalid":
            return {"acquired": False, "status": "invalid", "lock": current}
        refusal_result = refusal()
        if refusal_result is not None:
            return {"acquired": False, **refusal_result}
        return _create_lock_unlocked(
            target,
            repository=repository,
            pr_number=pr_number,
            campaign_id=campaign_id,
            acquired_at=acquired_at,
        )


def acquire_setup_lock(
    repository: str,
    pr_number: int,
    *,
    campaign_id: str,
    acquired_at: str,
    repository_path: str | Path = ".",
) -> dict[str, Any]:
    """Launcher setup acquisition: campaign record must still be absent."""
    return _acquire_lock_with_predicate(
        repository,
        pr_number,
        campaign_id=campaign_id,
        acquired_at=acquired_at,
        refusal=lambda: _setup_refusal_unlocked(
            repository, pr_number, repository_path=repository_path
        ),
        repository_path=repository_path,
    )


def acquire_worker_lock(
    repository: str,
    pr_number: int,
    *,
    campaign_id: str,
    acquired_at: str,
    repository_path: str | Path = ".",
) -> dict[str, Any]:
    """Ordinary worker acquisition for a delivered campaign.

    An active campaign remains acquirable at ``rounds_used == max_rounds``:
    that is valid state for later non-counting observation/terminalization.
    """
    return _acquire_lock_with_predicate(
        repository,
        pr_number,
        campaign_id=campaign_id,
        acquired_at=acquired_at,
        refusal=lambda: _worker_refusal_unlocked(
            repository, pr_number, campaign_id=campaign_id, repository_path=repository_path
        ),
        repository_path=repository_path,
    )


def acquire_rollover_lock(
    repository: str,
    pr_number: int,
    *,
    campaign_id: str,
    acquired_at: str,
    repository_path: str | Path = ".",
) -> dict[str, Any]:
    """Rollover acquisition for an allowlisted fully-consumed terminal C1.

    Local eligibility/acquisition only; native C2 Automation creation belongs
    to a later phase.
    """
    return _acquire_lock_with_predicate(
        repository,
        pr_number,
        campaign_id=campaign_id,
        acquired_at=acquired_at,
        refusal=lambda: _rollover_refusal_unlocked(
            repository, pr_number, campaign_id=campaign_id, repository_path=repository_path
        ),
        repository_path=repository_path,
    )


def _verify_owner_unlocked(
    repository: str,
    pr_number: int,
    owner_token: str,
    *,
    repository_path: str | Path = ".",
) -> dict[str, Any]:
    """Prove owner identity against a semantically valid lock. Caller holds guard."""
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


def verify_owner(
    repository: str,
    pr_number: int,
    owner_token: str,
    *,
    repository_path: str | Path = ".",
) -> dict[str, Any]:
    """Return lock metadata only when the caller proves the owner identity."""
    with _guard(lock_path(repository, pr_number, repository_path=repository_path)):
        return _verify_owner_unlocked(
            repository, pr_number, owner_token, repository_path=repository_path
        )


def ensure_active_campaign_owner(
    repository: str,
    pr_number: int,
    owner_token: str,
    *,
    repository_path: str | Path = ".",
) -> dict[str, Any]:
    """Prove complete ordinary product authority at a mutation boundary.

    Requires a semantically valid permanent lock, the exact owner token, a
    valid supported campaign matching the repository/PR and the lock identity,
    and campaign status ``active``. Campaign absence, malformed/unsupported
    state, and terminal campaigns all fail ordinary mutation authority.
    Identity-boundary operations use their dedicated predicates instead.
    """
    with _guard(lock_path(repository, pr_number, repository_path=repository_path)):
        metadata = _verify_owner_unlocked(
            repository, pr_number, owner_token, repository_path=repository_path
        )
        campaign = _load_valid_campaign_unlocked(
            repository, pr_number, repository_path=repository_path
        )
        if campaign["campaign_id"] != metadata["campaign_id"]:
            raise RuntimeError(
                "Ownership lock belongs to a different campaign; refusing mutation"
            )
        if campaign["status"] != model.ACTIVE:
            raise RuntimeError(
                "Campaign is not active "
                f"(status={campaign['status']}); ordinary mutation authority "
                "requires an active campaign"
            )
    return metadata


def release_lock(
    repository: str,
    pr_number: int,
    owner_token: str,
    *,
    repository_path: str | Path = ".",
) -> dict[str, Any]:
    """Ordinary identity-safe owner release, entirely inside one guard section.

    Accepts a matching campaign that is active or terminal. Campaign absence,
    malformed/unsupported state, and identity mismatch refuse and preserve the
    lock.
    """
    target = lock_path(repository, pr_number, repository_path=repository_path)
    with _guard(target):
        metadata = _verify_owner_unlocked(
            repository, pr_number, owner_token, repository_path=repository_path
        )
        campaign = _load_valid_campaign_unlocked(
            repository, pr_number, repository_path=repository_path
        )
        if campaign["campaign_id"] != metadata["campaign_id"]:
            raise RuntimeError(
                "Ownership lock belongs to a different campaign; refusing release"
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
    """Explicit human recovery boundary. Never call this automatically.

    Raw reading, classification, expected-identity checking, and unlink all
    happen inside one canonical-guard critical section.

    - Lock absent: harmless no-op; no expected identity is required.
    - Structurally valid lock: expected campaign id is mandatory and must match
      the stored campaign identity exactly.
    - Invalid lock with a readable raw campaign id: the raw identity grants no
      authority and need not satisfy campaign-id syntax; it is used only as a
      conservative mismatch guard, so expected identity is still mandatory.
    - Invalid lock with no recoverable identity: a supplied expected id can
      never be verified and refuses recovery; only an operator who established
      that the prior owner cannot continue may recover without one.
    """
    if not user_authorized_recovery:
        raise RuntimeError(
            "Lock recovery requires explicit user authorization; the permanent "
            "lock has no TTL or automatic stale detection"
        )
    target = lock_path(repository, pr_number, repository_path=repository_path)
    with _guard(target):
        if not target.exists():
            return {"recovered": False, "status": "absent"}
        try:
            payload = load_json(target)
        except (OSError, ValueError, json.JSONDecodeError):
            payload = None
        metadata: dict[str, Any] | None = None
        if payload is not None:
            try:
                metadata = _validate_lock_shape(
                    payload, repository=repository, pr_number=pr_number
                )
            except ValueError:
                metadata = None
        raw_campaign_id = payload.get("campaign_id") if payload is not None else None
        raw_identity = (
            raw_campaign_id
            if isinstance(raw_campaign_id, str) and raw_campaign_id
            else None
        )
        stored_identity = (
            metadata["campaign_id"] if metadata is not None else raw_identity
        )
        if stored_identity is not None:
            if expected_campaign_id is None:
                raise RuntimeError(
                    "Refusing recovery: this lock carries a readable campaign "
                    "identity, so the expected campaign id is required"
                )
            if stored_identity != expected_campaign_id:
                raise RuntimeError(
                    "Refusing recovery: the lock belongs to a different "
                    f"campaign ({stored_identity})"
                )
        elif expected_campaign_id is not None:
            raise RuntimeError(
                "Refusing recovery: the lock has no readable campaign identity, "
                "so the expected campaign id cannot be verified"
            )
        target.unlink()
    return {
        "recovered": True,
        "previous_status": "active" if metadata is not None else "invalid",
    }


def initialize_campaign(
    repository: str,
    pr_number: int,
    *,
    owner_token: str,
    campaign: dict[str, Any],
    repository_path: str | Path = ".",
) -> dict[str, Any]:
    """Identity-boundary campaign initialization inside one guard section.

    Requires the semantically valid setup lock, the exact owner token, an
    absent campaign record, a proposed campaign id equal to the lock identity,
    and a fully valid proposed campaign.
    """
    path = campaign_path(repository, pr_number, repository_path=repository_path)
    with _guard(lock_path(repository, pr_number, repository_path=repository_path)):
        metadata = _verify_owner_unlocked(
            repository, pr_number, owner_token, repository_path=repository_path
        )
        if path.exists():
            raise RuntimeError(f"Campaign record already exists: {path}")
        if campaign.get("campaign_id") != metadata["campaign_id"]:
            raise RuntimeError(
                "Init campaign id does not match the acquired lock identity"
            )
        model.validate_campaign(
            campaign, repository=repository, pull_request_number=pr_number
        )
        save_json(path, campaign)
    return {"initialized": True, "campaign": campaign}


class StaleCampaignError(RuntimeError):
    """Current durable campaign state differs from the caller's expected source."""


def _transition_campaign_unlocked(
    repository: str,
    pr_number: int,
    owner_token: str,
    *,
    transition: Callable[[dict[str, Any]], dict[str, Any]],
    expected_source: dict[str, Any] | None,
    require_active: bool,
    repository_path: str | Path = ".",
) -> dict[str, Any]:
    """Validate, transition, validate, and atomically replace under the guard."""
    metadata = _verify_owner_unlocked(
        repository, pr_number, owner_token, repository_path=repository_path
    )
    current = _load_valid_campaign_unlocked(
        repository, pr_number, repository_path=repository_path
    )
    if current["campaign_id"] != metadata["campaign_id"]:
        raise RuntimeError(
            "Ownership lock belongs to a different campaign; refusing mutation"
        )
    if expected_source is not None and current != expected_source:
        raise StaleCampaignError(
            "Current campaign state differs from the expected source; "
            "refusing stale write"
        )
    if require_active and current["status"] != model.ACTIVE:
        raise RuntimeError(
            f"Campaign is not active (status={current['status']}); "
            "ordinary campaign transitions require an active campaign"
        )
    result = transition(current)
    model.validate_campaign(
        result, repository=repository, pull_request_number=pr_number
    )
    save_json(
        campaign_path(repository, pr_number, repository_path=repository_path), result
    )
    return result


def transition_active_campaign(
    repository: str,
    pr_number: int,
    *,
    owner_token: str,
    transition: Callable[[dict[str, Any]], dict[str, Any]],
    repository_path: str | Path = ".",
) -> dict[str, Any]:
    """One guarded read/transition/write for a purely local campaign transition.

    Used for transitions without intervening external work (round consumption,
    guard synchronization, terminalization).
    """
    with _guard(lock_path(repository, pr_number, repository_path=repository_path)):
        return _transition_campaign_unlocked(
            repository,
            pr_number,
            owner_token,
            transition=transition,
            expected_source=None,
            require_active=True,
            repository_path=repository_path,
        )


def apply_campaign_transition_if_current(
    repository: str,
    pr_number: int,
    *,
    owner_token: str,
    expected_source: dict[str, Any],
    transition: Callable[[dict[str, Any]], dict[str, Any]],
    repository_path: str | Path = ".",
) -> dict[str, Any]:
    """Same-owner stale-write protection for transitions separated by work.

    Internal primitive for later mutation-boundary phases: possession of one
    owner token (shared by several subprocesses) does not authorize writing
    state derived from an obsolete campaign document. Only operation-specific
    pure transitions are persisted; there is no generic document replacement.
    """
    if not isinstance(expected_source, dict):
        raise ValueError("expected_source must be the exact current campaign record")
    with _guard(lock_path(repository, pr_number, repository_path=repository_path)):
        return _transition_campaign_unlocked(
            repository,
            pr_number,
            owner_token,
            transition=transition,
            expected_source=expected_source,
            require_active=False,
            repository_path=repository_path,
        )


def cancel_setup(
    repository: str,
    pr_number: int,
    *,
    owner_token: str,
    expected_campaign_id: str,
    repository_path: str | Path = ".",
) -> dict[str, Any]:
    """Owner-authorized cancellation of a clean pre-campaign setup.

    Valid only while the campaign record is still absent, the matching setup
    lock is still owned by the caller, and no external product mutation is in
    flight. Whether a native scheduler mutation is safe to cancel is decided
    by later scheduler integration, not here.
    """
    target = lock_path(repository, pr_number, repository_path=repository_path)
    with _guard(target):
        metadata = _verify_owner_unlocked(
            repository, pr_number, owner_token, repository_path=repository_path
        )
        if metadata["campaign_id"] != expected_campaign_id:
            raise RuntimeError(
                "Setup lock belongs to a different campaign; refusing cancellation"
            )
        if campaign_path(repository, pr_number, repository_path=repository_path).exists():
            raise RuntimeError(
                "Campaign record already exists; setup cancellation is no "
                "longer applicable"
            )
        target.unlink()
    return {"cancelled": True, "campaign_id": expected_campaign_id}


def abort_unused_campaign(
    repository: str,
    pr_number: int,
    *,
    owner_token: str,
    repository_path: str | Path = ".",
) -> dict[str, Any]:
    """Owner-authorized abort of a valid unused active campaign.

    Requires active status, zero consumed rounds, and no request guards.
    Deletion order is fixed: campaign first, lock last. A crash between the
    two intentionally leaves campaign-absent + lock-present, which stays
    fail-closed and requires explicit recovery.
    """
    target = lock_path(repository, pr_number, repository_path=repository_path)
    path = campaign_path(repository, pr_number, repository_path=repository_path)
    with _guard(target):
        metadata = _verify_owner_unlocked(
            repository, pr_number, owner_token, repository_path=repository_path
        )
        campaign = _load_valid_campaign_unlocked(
            repository, pr_number, repository_path=repository_path
        )
        if campaign["campaign_id"] != metadata["campaign_id"]:
            raise RuntimeError(
                "Ownership lock belongs to a different campaign; refusing abort"
            )
        if (
            campaign["status"] != model.ACTIVE
            or campaign["rounds_used"] != 0
            or campaign["guards"]
        ):
            raise RuntimeError(
                "Cannot abort: campaign must be active with zero consumed "
                "rounds and no request guards"
            )
        path.unlink()
        target.unlink()
    return {"aborted": True, "removed": str(path)}


def rollover_campaign_identity(
    repository: str,
    pr_number: int,
    *,
    owner_token: str,
    proposed_campaign: dict[str, Any],
    repository_path: str | Path = ".",
) -> dict[str, Any]:
    """One fixed guarded C1-to-C2 identity transition.

    Prerequisites: the caller holds rollover ownership and has fully
    constructed and locally validated the proposed C2. Inside one guard
    section this verifies the terminal C1 identity and rollover eligibility,
    validates C2 for the same repository/PR, then applies the fixed production
    order: lock identity first (preserving the owner token), campaign record
    second.

    An interruption after the lock replacement deliberately leaves
    lock=C2 with a terminal C1 record. That mismatch fails closed: ordinary
    acquisition, mutation, and release reject it, and only explicit recovery
    (expecting C2) clears the lock. There is no rollback, journal, or repair.
    """
    target = lock_path(repository, pr_number, repository_path=repository_path)
    path = campaign_path(repository, pr_number, repository_path=repository_path)
    with _guard(target):
        metadata = _verify_owner_unlocked(
            repository, pr_number, owner_token, repository_path=repository_path
        )
        current = _load_valid_campaign_unlocked(
            repository, pr_number, repository_path=repository_path
        )
        if current["campaign_id"] != metadata["campaign_id"]:
            raise RuntimeError(
                "Ownership lock belongs to a different campaign; refusing rollover"
            )
        if not model.is_rollover_eligible(current):
            raise RuntimeError("Current campaign is not a valid rollover source")
        model.validate_campaign(
            proposed_campaign, repository=repository, pull_request_number=pr_number
        )
        if proposed_campaign["campaign_id"] == current["campaign_id"]:
            raise RuntimeError("Rollover requires a new campaign identity")
        lock_payload = load_json(target)
        lock_payload["campaign_id"] = proposed_campaign["campaign_id"]
        save_json(target, lock_payload)
        os.chmod(target, 0o600)
        save_json(path, proposed_campaign)
    return {
        "rolled_over": True,
        "previous_campaign_id": current["campaign_id"],
        "campaign_id": proposed_campaign["campaign_id"],
    }


def _require_retirement_authorization(user_authorized_retirement: bool) -> None:
    if not user_authorized_retirement:
        raise RuntimeError(
            "Campaign retirement is a destructive human decision and requires "
            "explicit user authorization confirming that every previous owner "
            "and product-mutating operation can no longer continue"
        )


def retire_campaign_retaining_lock(
    repository: str,
    pr_number: int,
    *,
    expected_campaign_id: str,
    user_authorized_retirement: bool,
    repository_path: str | Path = ".",
) -> dict[str, Any]:
    """Explicit user-authorized retirement while the matching valid lock remains.

    Requires the exact valid expected campaign, a semantically valid permanent
    lock whose campaign identity matches, and agreeing repository/PR identity.
    Removal order is fixed: campaign record first, lock last. A crash between
    the two leaves campaign-absent + lock-present, which stays fail-closed and
    requires explicit lock recovery. Malformed/unsupported campaign state and
    invalid or mismatched locks are never removed here.
    """
    _require_retirement_authorization(user_authorized_retirement)
    target = lock_path(repository, pr_number, repository_path=repository_path)
    path = campaign_path(repository, pr_number, repository_path=repository_path)
    with _guard(target):
        campaign = _load_valid_campaign_unlocked(
            repository, pr_number, repository_path=repository_path
        )
        if campaign["campaign_id"] != expected_campaign_id:
            raise RuntimeError(
                "Expected campaign id does not match the campaign record"
            )
        payload = load_json(target)
        if payload is None:
            raise RuntimeError(
                "No permanent lock exists; use lock-absent retirement"
            )
        try:
            _validate_lock_shape(payload, repository=repository, pr_number=pr_number)
        except ValueError as error:
            raise RuntimeError(
                f"Permanent lock is invalid and cannot be bypassed through "
                f"retirement: {error}"
            ) from error
        if payload["campaign_id"] != campaign["campaign_id"]:
            raise RuntimeError(
                "Permanent lock belongs to a different campaign; recover the "
                "lock explicitly before retiring the campaign"
            )
        path.unlink()
        target.unlink()
    return {"retired": True, "mode": "retained-lock", "campaign_id": expected_campaign_id}


def retire_campaign_without_lock(
    repository: str,
    pr_number: int,
    *,
    expected_campaign_id: str,
    user_authorized_retirement: bool,
    repository_path: str | Path = ".",
) -> dict[str, Any]:
    """Explicit user-authorized retirement when no permanent lock remains.

    Used after lock recovery. Refuses while any permanent lock file exists
    (even an invalid one): it may belong to another owner and is never
    deleted, recovered, reinterpreted, or stolen here.
    """
    _require_retirement_authorization(user_authorized_retirement)
    path = campaign_path(repository, pr_number, repository_path=repository_path)
    target = lock_path(repository, pr_number, repository_path=repository_path)
    with _guard(target):
        campaign = _load_valid_campaign_unlocked(
            repository, pr_number, repository_path=repository_path
        )
        if campaign["campaign_id"] != expected_campaign_id:
            raise RuntimeError(
                "Expected campaign id does not match the campaign record"
            )
        if target.exists():
            raise RuntimeError(
                "A permanent lock exists; refusing lock-absent retirement. "
                "Use explicit lock recovery first"
            )
        path.unlink()
    return {"retired": True, "mode": "lock-absent", "campaign_id": expected_campaign_id}


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
