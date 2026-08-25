from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
VALIDATOR_PATH = ROOT / "scripts" / "validate_repository.py"
SPEC = importlib.util.spec_from_file_location("validate_repository", VALIDATOR_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("Unable to load repository validator")
VALIDATOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VALIDATOR)


class RepositoryValidationTests(unittest.TestCase):
    def test_current_repository_passes_publication_checks(self) -> None:
        self.assertEqual(VALIDATOR.validate_repository(ROOT), [])

    def test_markdown_validation_detects_missing_links_and_open_fences(self) -> None:
        with tempfile.TemporaryDirectory() as directory_name:
            root = Path(directory_name)
            path = root / "README.md"
            path.write_text(
                "[missing](docs/missing.md)\n\n```text\nnot closed\n",
                encoding="utf-8",
            )
            errors = VALIDATOR.markdown_errors(path, root=root)
        self.assertTrue(any("missing local link" in error for error in errors))
        self.assertTrue(any("unmatched fenced block" in error for error in errors))

    def test_workflow_validation_rejects_unpinned_or_non_official_actions(self) -> None:
        with tempfile.TemporaryDirectory() as directory_name:
            path = Path(directory_name) / "ci.yml"
            path.write_text(
                """name: CI
on:
  pull_request:
  push:
    branches: [main]
permissions:
  contents: read
concurrency: test
jobs:
  test:
    timeout-minutes: 5
    strategy:
      matrix:
        os: [ubuntu-latest, windows-latest]
        python-version: [\"3.10\", \"3.12\"]
    steps:
      - uses: third-party/example@v1
      - name: PowerShell AST
        run: echo test
""",
                encoding="utf-8",
            )
            errors = VALIDATOR.workflow_errors(path)
        self.assertTrue(any("full commit SHA" in error for error in errors))

    def test_workflow_validation_accepts_named_step_with_pinned_action(self) -> None:
        with tempfile.TemporaryDirectory() as directory_name:
            path = Path(directory_name) / "ci.yml"
            path.write_text(
                """name: CI
jobs:
  test:
    steps:
      - name: Check out repository
        uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1
""",
                encoding="utf-8",
            )
            errors = VALIDATOR.workflow_errors(path)
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
