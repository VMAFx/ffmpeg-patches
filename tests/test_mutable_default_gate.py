# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Tests for the mutable default argument lint gate."""

from __future__ import annotations

import shutil
import subprocess  # nosec B404 - fixture runner drives make lint-mutable-defaults on temporary fixtures.
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def check_pylint_toolchain() -> str | None:
    """Return skip reason if neither pylint nor uvx is available, else None."""
    has_pylint = shutil.which("pylint") is not None
    has_uvx = shutil.which("uvx") is not None
    if not (has_pylint or has_uvx):
        return "neither pylint nor uvx exists on PATH"
    return None


def run_gate(fixture_path: Path) -> subprocess.CompletedProcess[str]:
    """Execute make -s lint-mutable-defaults on a fixture file."""
    return subprocess.run(  # nosec B603 B607 - fixture runner drives make on local test files.
        ["make", "-s", "lint-mutable-defaults", f"FILES={fixture_path}"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


class MutableDefaultGateTest(unittest.TestCase):
    """Verify that Makefile lint-mutable-defaults catches dangerous default values."""

    def setUp(self) -> None:
        """Skip when neither pylint nor uvx is available."""
        reason = check_pylint_toolchain()
        if reason:
            print(reason)
            self.skipTest(reason)

    def test_planted_name_bound_default_fails(self) -> None:
        """Planted name-bound mutable default must fail with W0102 in output."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            fixture = Path(tmp_dir) / "fixture_name_bound.py"
            fixture.write_text(
                "EMPTY_LIST = []\n\n\n"
                "def dummy_func(path, items=EMPTY_LIST):\n"
                "    return (path, items)\n",
                encoding="utf-8",
            )
            result = run_gate(fixture)
            combined = result.stdout + result.stderr
            self.assertNotEqual(result.returncode, 0, f"Expected non-zero exit code: {combined}")
            self.assertIn("W0102", combined, f"Expected W0102 in output: {combined}")

    def test_planted_literal_list_default_fails(self) -> None:
        """Planted literal [] default must fail with non-zero exit code."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            fixture = Path(tmp_dir) / "fixture_literal.py"
            fixture.write_text(
                "def dummy_func(path, items=[]):\n    return (path, items)\n",
                encoding="utf-8",
            )
            result = run_gate(fixture)
            combined = result.stdout + result.stderr
            self.assertNotEqual(result.returncode, 0, f"Expected non-zero exit code: {combined}")
            self.assertIn("W0102", combined, f"Expected W0102 in output: {combined}")

    def test_clean_none_default_passes(self) -> None:
        """Clean None default parameter must pass with exit code 0."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            fixture = Path(tmp_dir) / "fixture_clean.py"
            fixture.write_text(
                "def dummy_func(path, items=None):\n"
                "    if items is None:\n"
                "        items = []\n"
                "    return (path, items)\n",
                encoding="utf-8",
            )
            result = run_gate(fixture)
            combined = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, f"Expected zero exit code: {combined}")
            self.assertNotIn("W0102", combined)


if __name__ == "__main__":
    unittest.main()
