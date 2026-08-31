from __future__ import annotations

import json
import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "codex-review-pulse" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from manage_pilot_install import (  # noqa: E402
    install,
    installation_path,
    inspect_source,
    update,
    verify_installation,
    parse_args,
)


def git(repository: Path, *arguments: str) -> str:
    process = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        capture_output=True,
        text=True,
        check=True,
    )
    return process.stdout.strip()


def create_source(repository: Path, version: str = "0.3.1") -> str:
    git(repository, "init")
    git(repository, "config", "user.email", "tests@example.test")
    git(repository, "config", "user.name", "Test Runner")
    skill = repository / "skills" / "codex-review-pulse"
    (skill / "scripts").mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: codex-review-pulse\ndescription: Test skill.\n---\n\nTest.\n",
        encoding="utf-8",
    )
    (skill / "VERSION").write_text(version + "\n", encoding="utf-8")
    (skill / "scripts" / "probe.py").write_text("print('ok')\n", encoding="utf-8")
    (skill / "scripts" / "manage_pilot_install.py").write_bytes(
        (SCRIPTS / "manage_pilot_install.py").read_bytes()
    )
    git(repository, "add", "skills/codex-review-pulse")
    git(repository, "commit", "-m", "test: add skill")
    return git(repository, "rev-parse", "HEAD")


class PilotInstallTests(unittest.TestCase):
    def test_install_verify_update_and_uninstall_in_temporary_directory(self) -> None:
        with tempfile.TemporaryDirectory() as source_directory, tempfile.TemporaryDirectory() as install_directory:
            source = Path(source_directory)
            install_root = Path(install_directory)
            first_commit = create_source(source)

            installed = install(
                source_repository=source,
                source_commit=first_commit,
                skills_root=install_root,
            )
            self.assertEqual(installed["source_commit"], first_commit)
            target = installation_path(install_root)
            self.assertTrue(target.is_dir())
            verified = verify_installation(
                target,
                expected_version="0.3.1",
                expected_source_commit=first_commit,
            )
            self.assertTrue(verified["ok"], verified["errors"])
            runtime_cache = target / "scripts" / "__pycache__"
            runtime_cache.mkdir()
            (runtime_cache / "probe.cpython-312.pyc").write_bytes(b"runtime")
            self.assertTrue(verify_installation(target)["ok"])

            (source / "skills" / "codex-review-pulse" / "VERSION").write_text(
                "0.3.2\n", encoding="utf-8"
            )
            git(source, "add", "skills/codex-review-pulse/VERSION")
            git(source, "commit", "-m", "test: update skill")
            second_commit = git(source, "rev-parse", "HEAD")
            updated = update(
                source_repository=source,
                source_commit=second_commit,
                skills_root=install_root,
            )
            self.assertEqual(updated["skill_version"], "0.3.2")
            self.assertTrue(
                verify_installation(
                    target,
                    expected_version="0.3.2",
                    expected_source_commit=second_commit,
                )["ok"]
            )

            process = subprocess.run(
                [
                    sys.executable,
                    str(target / "scripts" / "manage_pilot_install.py"),
                    "uninstall",
                    "--skills-root",
                    str(install_root),
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(process.returncode, 0, process.stderr)
            removed = json.loads(process.stdout)
            self.assertTrue(removed["removed"])
            self.assertFalse(target.exists())
            self.assertEqual(list(install_root.iterdir()), [])

    def test_expected_version_and_source_commit_mismatch_are_reported(self) -> None:
        with tempfile.TemporaryDirectory() as source_directory, tempfile.TemporaryDirectory() as install_directory:
            source = Path(source_directory)
            commit = create_source(source)
            install(
                source_repository=source,
                source_commit=commit,
                skills_root=install_directory,
            )
            result = verify_installation(
                installation_path(install_directory),
                expected_version="9.9.9",
                expected_source_commit="0" * 40,
            )
            self.assertFalse(result["ok"])
            self.assertIn("installed_skill_version_mismatch", result["errors"])
            self.assertIn("installed_source_commit_mismatch", result["errors"])

    def test_manifest_cannot_reauthorize_modified_installed_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as source_directory, tempfile.TemporaryDirectory() as install_directory:
            source = Path(source_directory)
            commit = create_source(source)
            install(
                source_repository=source,
                source_commit=commit,
                skills_root=install_directory,
            )
            target = installation_path(install_directory)
            probe = target / "scripts" / "probe.py"
            replacement = b"print('modified')\n"
            probe.write_bytes(replacement)
            manifest_path = target / ".codex-review-pulse-install.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["files"]["scripts/probe.py"] = hashlib.sha256(replacement).hexdigest()
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            result = verify_installation(
                target,
                expected_version="0.3.1",
                expected_source_commit=commit,
            )
            self.assertFalse(result["ok"])
            self.assertIn("installation_manifest_inventory_mismatch", result["errors"])
            self.assertIn(
                "installation_file_hash_mismatch:scripts/probe.py", result["errors"]
            )

    def test_replacement_refs_cannot_reauthorize_modified_source_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as source_directory, tempfile.TemporaryDirectory() as install_directory:
            source = Path(source_directory)
            base_commit = create_source(source)
            install(
                source_repository=source,
                source_commit=base_commit,
                skills_root=install_directory,
            )
            probe = source / "skills" / "codex-review-pulse" / "scripts" / "probe.py"
            probe.write_text("print('replacement')\n", encoding="utf-8")
            git(source, "add", "skills/codex-review-pulse/scripts/probe.py")
            replacement_commit = git(
                source, "commit", "-m", "test: add replacement object"
            )
            replacement_commit = git(source, "rev-parse", "HEAD")
            subprocess.run(
                ["git", "-C", str(source), "replace", base_commit, replacement_commit],
                check=True,
                capture_output=True,
                text=True,
            )
            try:
                target = installation_path(install_directory)
                installed_probe = target / "scripts" / "probe.py"
                installed_probe.write_text("print('replacement')\n", encoding="utf-8")
                manifest_path = target / ".codex-review-pulse-install.json"
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest["files"]["scripts/probe.py"] = hashlib.sha256(
                    installed_probe.read_bytes()
                ).hexdigest()
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

                result = verify_installation(
                    target,
                    expected_version="0.4.0",
                    expected_source_commit=base_commit,
                    expected_source_repository=source,
                )
            finally:
                subprocess.run(
                    ["git", "-C", str(source), "replace", "-d", base_commit],
                    check=True,
                    capture_output=True,
                    text=True,
                )

            self.assertFalse(result["ok"])
            self.assertIn("installation_manifest_inventory_mismatch", result["errors"])
            self.assertIn(
                "installation_file_hash_mismatch:scripts/probe.py", result["errors"]
            )

    def test_contract_source_repository_is_independently_bound(self) -> None:
        with tempfile.TemporaryDirectory() as source_directory, tempfile.TemporaryDirectory() as install_directory, tempfile.TemporaryDirectory() as other_directory:
            source = Path(source_directory)
            commit = create_source(source)
            install(
                source_repository=source,
                source_commit=commit,
                skills_root=install_directory,
            )
            target = installation_path(install_directory)
            manifest_path = target / ".codex-review-pulse-install.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["source_repository"] = str(Path(other_directory).resolve())
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            result = verify_installation(
                target,
                expected_version="0.4.0",
                expected_source_commit=commit,
                expected_source_repository=source,
            )

            self.assertFalse(result["ok"])
            self.assertIn("installation_source_repository_mismatch", result["errors"])

    def test_symlinked_inventory_file_is_rejected_without_following_it(self) -> None:
        with tempfile.TemporaryDirectory() as source_directory, tempfile.TemporaryDirectory() as install_directory, tempfile.TemporaryDirectory() as external_directory:
            source = Path(source_directory)
            commit = create_source(source)
            install(
                source_repository=source,
                source_commit=commit,
                skills_root=install_directory,
            )
            target = installation_path(install_directory)
            probe = target / "scripts" / "probe.py"
            external = Path(external_directory) / "probe.py"
            external.write_bytes(probe.read_bytes())
            probe.unlink()
            try:
                probe.symlink_to(external)
            except OSError as error:
                self.skipTest(f"File symlinks are unavailable: {error}")

            result = verify_installation(
                target,
                expected_version="0.3.1",
                expected_source_commit=commit,
            )
            self.assertFalse(result["ok"])
            self.assertIn(
                "installation_symlink_present:scripts/probe.py", result["errors"]
            )

    def test_dirty_source_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as source_directory, tempfile.TemporaryDirectory() as install_directory:
            source = Path(source_directory)
            commit = create_source(source)
            (source / "untracked.txt").write_text("dirty\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "Source repository is dirty"):
                install(
                    source_repository=source,
                    source_commit=commit,
                    skills_root=install_directory,
                )
            self.assertFalse(installation_path(install_directory).exists())

    def test_missing_source_commit_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as source_directory:
            source = Path(source_directory)
            create_source(source)
            with self.assertRaisesRegex(RuntimeError, "Source commit does not exist"):
                inspect_source(source, "0" * 40)

    def test_skills_root_is_accepted_after_install_subcommand(self) -> None:
        with patch.object(
            sys,
            "argv",
            [
                "manage_pilot_install.py",
                "install",
                "--source-commit",
                "abc123",
                "--skills-root",
                "C:/temporary/skills",
            ],
        ):
            args = parse_args()
        self.assertEqual(args.command, "install")
        self.assertEqual(args.skills_root, Path("C:/temporary/skills"))


if __name__ == "__main__":
    unittest.main()
