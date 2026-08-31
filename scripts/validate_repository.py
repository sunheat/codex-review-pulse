#!/usr/bin/env python3
"""Run deterministic, network-free repository publication checks."""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "codex-review-pulse"
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
LOCAL_LINK = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")
PINNED_OFFICIAL_ACTION = re.compile(r"actions/[a-z0-9_.-]+@[0-9a-f]{40}")


def markdown_errors(path: Path, *, root: Path) -> list[str]:
    """Return local-link and fenced-block errors for one Markdown file."""
    text = path.read_text(encoding="utf-8")
    errors: list[str] = []
    fence_count = sum(
        1 for line in text.splitlines() if line.lstrip().startswith("```")
    )
    if fence_count % 2:
        errors.append(f"{path.relative_to(root)}: unmatched fenced block")
    for raw_target in LOCAL_LINK.findall(text):
        target = raw_target.strip().strip("<>")
        if target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        target = target.split("#", 1)[0]
        if target and not (path.parent / target).resolve().exists():
            errors.append(f"{path.relative_to(root)}: missing local link {target}")
    return errors


def workflow_errors(path: Path) -> list[str]:
    """Return safety and coverage errors for the public CI workflow."""
    if not path.is_file():
        return [f"missing workflow: {path}"]
    text = path.read_text(encoding="utf-8")
    errors: list[str] = []
    action_refs = re.findall(
        r"^\s*(?:-\s*)?uses:\s*([^\s#]+)", text, re.MULTILINE
    )
    if not action_refs:
        errors.append("CI workflow does not use any actions")
    for action_ref in action_refs:
        if PINNED_OFFICIAL_ACTION.fullmatch(action_ref) is None:
            errors.append(
                "CI action must be an official actions/* dependency pinned to a "
                f"full commit SHA: {action_ref}"
            )
    return errors


def cli_help_errors(root: Path) -> list[str]:
    """Exercise every shipped Python entrypoint's help path."""
    errors: list[str] = []
    scripts = sorted((root / "scripts").glob("*.py"))
    scripts.extend(
        sorted((root / "skills" / "codex-review-pulse" / "scripts").glob("*.py"))
    )
    for script in scripts:
        process = subprocess.run(
            [sys.executable, str(script), "--help"],
            cwd=root,
            capture_output=True,
            text=True,
        )
        if process.returncode != 0:
            errors.append(
                f"CLI help failed for {script.relative_to(root)}: "
                f"{(process.stderr or process.stdout).strip()}"
            )
    return errors


def tracked_hygiene_errors(root: Path) -> list[str]:
    """Reject tracked local state, caches, bytecode, and symlinks."""
    process = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-s", "-z"],
        capture_output=True,
    )
    if process.returncode != 0:
        return ["unable to inspect tracked paths"]
    errors: list[str] = []
    for entry in process.stdout.decode("utf-8").split("\0"):
        if not entry:
            continue
        metadata, path = entry.split("\t", 1)
        mode = metadata.split(" ", 1)[0]
        normalized = path.replace("\\", "/")
        if re.search(
            r"(^|/)(__pycache__|\.pytest_cache)(/|$)|\.py[co]$|(^|/)notes/",
            normalized,
        ):
            errors.append(f"tracked local or cache path: {normalized}")
        if mode == "120000":
            errors.append(f"tracked symlink is not allowed: {normalized}")
    return errors


def validate_repository(root: Path = ROOT, *, run_cli_help: bool = True) -> list[str]:
    """Return all deterministic publication-check failures."""
    root = root.resolve()
    errors: list[str] = []
    for path in sorted(root.rglob("*.md")):
        if ".git" in path.parts or path.is_relative_to(root / "notes"):
            continue
        errors.extend(markdown_errors(path, root=root))
    errors.extend(workflow_errors(root / ".github" / "workflows" / "ci.yml"))
    errors.extend(tracked_hygiene_errors(root))
    if run_cli_help:
        errors.extend(cli_help_errors(root))
    version_path = root / "skills" / "codex-review-pulse" / "VERSION"
    version = version_path.read_text(encoding="utf-8").strip()
    if re.fullmatch(r"\d+\.\d+\.\d+", version) is None:
        errors.append("skill VERSION must be a three-part semantic version")
    return errors


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate public repository structure without network access"
    )
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument(
        "--skip-cli-help",
        action="store_true",
        help="Skip exercising shipped Python CLI help paths",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    errors = validate_repository(args.root, run_cli_help=not args.skip_cli_help)
    if errors:
        print("\n".join(errors), file=sys.stderr)
        raise SystemExit(1)
    print("Repository validation passed")


if __name__ == "__main__":
    main()
