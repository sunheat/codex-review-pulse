#!/usr/bin/env python3
"""Install and verify a commit-pinned Codex Review Pulse pilot copy."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Any
from uuid import uuid4


SKILL_NAME = "codex-review-pulse"
SKILL_REPOSITORY_PATH = f"skills/{SKILL_NAME}"
MANIFEST_NAME = ".codex-review-pulse-install.json"
MANIFEST_SCHEMA_VERSION = 1


def default_skills_root() -> Path:
    """Return OpenAI's documented user-level local skill directory."""
    return Path.home() / ".agents" / "skills"


def run_git(repository: Path, arguments: list[str], *, text: bool = True) -> Any:
    process = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        capture_output=True,
        text=text,
    )
    if process.returncode != 0:
        stderr = process.stderr.strip() if text else process.stderr.decode(errors="replace").strip()
        raise RuntimeError(stderr or f"git {' '.join(arguments)} failed")
    return process.stdout


def inspect_source(repository: str | Path, source_commit: str) -> tuple[Path, str, str]:
    source = Path(repository).resolve()
    if not source.is_dir():
        raise RuntimeError(f"Source repository does not exist: {source}")
    top_level = Path(run_git(source, ["rev-parse", "--show-toplevel"]).strip()).resolve()
    dirty = run_git(top_level, ["status", "--porcelain=v1", "--untracked-files=all"])
    if dirty.strip():
        raise RuntimeError("Source repository is dirty; commit or remove changes before installation")
    try:
        resolved_commit = run_git(
            top_level, ["rev-parse", "--verify", f"{source_commit}^{{commit}}"]
        ).strip()
    except RuntimeError as error:
        raise RuntimeError(f"Source commit does not exist: {source_commit}") from error
    version_path = f"{SKILL_REPOSITORY_PATH}/VERSION"
    try:
        version = run_git(top_level, ["show", f"{resolved_commit}:{version_path}"]).strip()
    except RuntimeError as error:
        raise RuntimeError(f"Source commit does not contain {version_path}") from error
    if not version or any(character.isspace() for character in version):
        raise RuntimeError("Skill VERSION must be one non-empty token")
    return top_level, resolved_commit, version


def _source_files(repository: Path, commit: str) -> list[str]:
    output = run_git(
        repository,
        ["ls-tree", "-r", "--name-only", "-z", commit, "--", SKILL_REPOSITORY_PATH],
    )
    prefix = f"{SKILL_REPOSITORY_PATH}/"
    files = [value for value in output.split("\0") if value]
    if not files or f"{SKILL_REPOSITORY_PATH}/SKILL.md" not in files:
        raise RuntimeError(f"Source commit does not contain {SKILL_REPOSITORY_PATH}/SKILL.md")
    if any(not value.startswith(prefix) for value in files):
        raise RuntimeError("Git returned a path outside the skill directory")
    return files


def _read_blob(repository: Path, commit: str, path: str) -> bytes:
    return run_git(repository, ["show", f"{commit}:{path}"], text=False)


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _source_inventory(repository: Path, commit: str) -> tuple[dict[str, str], str]:
    """Return the skill inventory and version from immutable Git objects."""
    prefix = f"{SKILL_REPOSITORY_PATH}/"
    inventory = {
        source_path.removeprefix(prefix).replace("\\", "/"): _sha256(
            _read_blob(repository, commit, source_path)
        )
        for source_path in _source_files(repository, commit)
    }
    version_path = f"{SKILL_REPOSITORY_PATH}/VERSION"
    version = _read_blob(repository, commit, version_path).decode("utf-8").strip()
    if not version or any(character.isspace() for character in version):
        raise RuntimeError("Pinned source VERSION must be one non-empty token")
    return dict(sorted(inventory.items())), version


def _is_runtime_artifact(path: Path) -> bool:
    return "__pycache__" in path.parts or path.suffix.casefold() in {".pyc", ".pyo"}


def build_installation(
    staging: Path,
    *,
    repository: Path,
    commit: str,
    version: str,
) -> dict[str, Any]:
    inventory: dict[str, str] = {}
    prefix = f"{SKILL_REPOSITORY_PATH}/"
    for source_path in _source_files(repository, commit):
        relative = source_path.removeprefix(prefix)
        relative_path = Path(relative)
        if (
            relative_path.is_absolute()
            or ".." in relative_path.parts
            or relative_path.name == MANIFEST_NAME
        ):
            raise RuntimeError(f"Unsafe source skill path: {relative}")
        destination = staging / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        content = _read_blob(repository, commit, source_path)
        destination.write_bytes(content)
        inventory[relative.replace("\\", "/")] = _sha256(content)
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "skill_name": SKILL_NAME,
        "skill_version": version,
        "source_commit": commit,
        "source_repository": str(repository),
        "files": dict(sorted(inventory.items())),
    }
    (staging / MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def installation_path(skills_root: str | Path | None = None) -> Path:
    root = Path(skills_root).expanduser() if skills_root is not None else default_skills_root()
    return root.resolve() / SKILL_NAME


def verify_installation(
    target: str | Path,
    *,
    expected_version: str | None = None,
    expected_source_commit: str | None = None,
) -> dict[str, Any]:
    path = Path(target).expanduser().absolute()
    errors: list[str] = []
    manifest_path = path / MANIFEST_NAME
    manifest: dict[str, Any] | None = None
    if not path.is_dir() or path.is_symlink():
        errors.append("installation_missing_or_not_independent_directory")
    elif manifest_path.is_symlink():
        errors.append("installation_manifest_is_symlink")
    elif not manifest_path.is_file():
        errors.append("installation_manifest_missing")
    else:
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("manifest root is not an object")
            manifest = payload
        except (OSError, ValueError, json.JSONDecodeError):
            errors.append("installation_manifest_invalid")

    if manifest is not None:
        if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
            errors.append("installation_manifest_schema_mismatch")
        if manifest.get("skill_name") != SKILL_NAME:
            errors.append("installation_skill_name_mismatch")
        if expected_version is not None and manifest.get("skill_version") != expected_version:
            errors.append("installed_skill_version_mismatch")
        if (
            expected_source_commit is not None
            and manifest.get("source_commit") != expected_source_commit
        ):
            errors.append("installed_source_commit_mismatch")
        recorded = manifest.get("files")
        if not isinstance(recorded, dict):
            errors.append("installation_file_inventory_invalid")
        else:
            symlink_paths = sorted(
                item.relative_to(path).as_posix()
                for item in path.rglob("*")
                if item.is_symlink()
            )
            errors.extend(
                f"installation_symlink_present:{relative}"
                for relative in symlink_paths
            )
            actual_paths = {
                item.relative_to(path).as_posix()
                for item in path.rglob("*")
                if item.is_file()
                and not item.is_symlink()
                and item.name != MANIFEST_NAME
                and not _is_runtime_artifact(item.relative_to(path))
            }
            trusted_inventory: dict[str, str] | None = None
            trusted_version: str | None = None
            source_repository = manifest.get("source_repository")
            source_commit = expected_source_commit or manifest.get("source_commit")
            if not isinstance(source_repository, str) or not source_repository:
                errors.append("installation_source_repository_invalid")
            elif not isinstance(source_commit, str) or not source_commit:
                errors.append("installation_source_commit_invalid")
            else:
                try:
                    source_path = Path(source_repository).expanduser().resolve()
                    resolved_commit = run_git(
                        source_path,
                        ["rev-parse", "--verify", f"{source_commit}^{{commit}}"],
                    ).strip()
                    if resolved_commit.casefold() != source_commit.casefold():
                        raise RuntimeError("Pinned source commit did not resolve exactly")
                    trusted_inventory, trusted_version = _source_inventory(
                        source_path, resolved_commit
                    )
                except (OSError, RuntimeError, UnicodeDecodeError):
                    errors.append("installation_source_provenance_unavailable")

            if trusted_inventory is not None and recorded != trusted_inventory:
                errors.append("installation_manifest_inventory_mismatch")
            if trusted_version is not None and manifest.get("skill_version") != trusted_version:
                errors.append("installation_manifest_version_mismatch")

            expected_inventory = trusted_inventory or recorded
            expected_paths = set(expected_inventory)
            if actual_paths != expected_paths:
                errors.append("installation_file_set_mismatch")
            for relative, expected_hash in expected_inventory.items():
                candidate = path / Path(relative)
                if not candidate.is_file() or candidate.is_symlink():
                    continue
                if _sha256(candidate.read_bytes()) != expected_hash:
                    errors.append(f"installation_file_hash_mismatch:{relative}")

    return {
        "ok": not errors,
        "installation_path": str(path),
        "expected_skill_version": expected_version,
        "expected_source_commit": expected_source_commit,
        "installed_skill_version": (manifest or {}).get("skill_version"),
        "installed_source_commit": (manifest or {}).get("source_commit"),
        "errors": errors,
    }


def _stage_installation(
    root: Path, repository: Path, commit: str, version: str
) -> tuple[Path, dict[str, Any]]:
    root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{SKILL_NAME}.staging-", dir=root))
    try:
        manifest = build_installation(
            staging, repository=repository, commit=commit, version=version
        )
        return staging, manifest
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def install(
    *, source_repository: str | Path, source_commit: str, skills_root: str | Path | None
) -> dict[str, Any]:
    repository, commit, version = inspect_source(source_repository, source_commit)
    target = installation_path(skills_root)
    if target.exists() or target.is_symlink():
        raise RuntimeError(f"Installation already exists: {target}; use update")
    staging, manifest = _stage_installation(target.parent, repository, commit, version)
    try:
        staging.replace(target)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return {"operation": "install", "installation_path": str(target), **manifest}


def update(
    *, source_repository: str | Path, source_commit: str, skills_root: str | Path | None
) -> dict[str, Any]:
    repository, commit, version = inspect_source(source_repository, source_commit)
    target = installation_path(skills_root)
    current = verify_installation(target)
    if not current["ok"]:
        raise RuntimeError("Existing installation failed verification: " + ", ".join(current["errors"]))
    staging, manifest = _stage_installation(target.parent, repository, commit, version)
    backup = target.parent / f".{SKILL_NAME}.backup-{uuid4().hex}"
    try:
        target.replace(backup)
        try:
            staging.replace(target)
        except Exception:
            backup.replace(target)
            raise
        shutil.rmtree(backup)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
    return {"operation": "update", "installation_path": str(target), **manifest}


def uninstall(*, skills_root: str | Path | None) -> dict[str, Any]:
    target = installation_path(skills_root)
    current = verify_installation(target)
    if not current["ok"]:
        raise RuntimeError("Refusing to remove an unverified installation: " + ", ".join(current["errors"]))
    tombstone = target.parent / f".{SKILL_NAME}.uninstall-{uuid4().hex}"
    target.replace(tombstone)
    try:
        shutil.rmtree(tombstone)
    except Exception:
        tombstone.replace(target)
        raise
    return {"operation": "uninstall", "installation_path": str(target), "removed": True}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Manage a commit-pinned Codex Review Pulse pilot installation"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("install", "update"):
        command = commands.add_parser(name)
        command.add_argument(
            "--skills-root", type=Path,
            help="Parent skill directory; defaults to the documented user location",
        )
        command.add_argument("--source-repository", type=Path, default=Path("."))
        command.add_argument("--source-commit", required=True)
    verify = commands.add_parser("verify")
    verify.add_argument(
        "--skills-root", type=Path,
        help="Parent skill directory; defaults to the documented user location",
    )
    verify.add_argument("--expected-version")
    verify.add_argument("--expected-source-commit")
    uninstall_command = commands.add_parser("uninstall")
    uninstall_command.add_argument(
        "--skills-root", type=Path,
        help="Parent skill directory; defaults to the documented user location",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.command == "install":
        result = install(
            source_repository=args.source_repository,
            source_commit=args.source_commit,
            skills_root=args.skills_root,
        )
    elif args.command == "update":
        result = update(
            source_repository=args.source_repository,
            source_commit=args.source_commit,
            skills_root=args.skills_root,
        )
    elif args.command == "verify":
        target = installation_path(args.skills_root)
        result = verify_installation(
            target,
            expected_version=args.expected_version,
            expected_source_commit=args.expected_source_commit,
        )
        if not result["ok"]:
            print(json.dumps(result, indent=2), file=sys.stderr)
            raise SystemExit(1)
    else:
        result = uninstall(skills_root=args.skills_root)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from error
