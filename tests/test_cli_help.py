from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "codex-review-pulse" / "scripts"


class CliHelpTests(unittest.TestCase):
    def test_every_shipped_script_has_working_help(self) -> None:
        failures: list[str] = []
        for script in sorted(SCRIPTS.glob("*.py")):
            process = subprocess.run(
                [sys.executable, str(script), "--help"],
                capture_output=True,
                text=True,
            )
            if process.returncode != 0:
                failures.append(f"{script.name}: {process.stderr.strip()}")
        self.assertEqual(failures, [])


if __name__ == "__main__":
    unittest.main()
