# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Tests for the temporary directory leak guard."""

from __future__ import annotations

import os
import subprocess  # nosec B404 - test runner subprocess invocation with timeout.
import sys
import tempfile
import unittest
from pathlib import Path

GUARD = Path(__file__).resolve().parent.parent / "scripts" / "tmp_leak_guard.py"


class TmpLeakGuardTest(unittest.TestCase):
    """Test suite for the tmp_leak_guard CLI runner."""

    def setUp(self) -> None:
        """Set up an isolated outer temporary directory for guard execution."""
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.outer_dir = Path(scratch.name)

    def run_guard(
        self,
        guard_args: list[str],
        cmd_args: list[str],
        timeout: float = 15.0,
    ) -> subprocess.CompletedProcess[str]:
        """Execute the leak guard subprocess in the test temporary directory."""
        env = os.environ.copy()
        env["TMPDIR"] = str(self.outer_dir)
        return subprocess.run(  # nosec B603 - fixed guard script invocation with explicit timeout.
            [sys.executable, "-I", str(GUARD), *guard_args, "--", *cmd_args],
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout,
            check=False,
        )

    def test_clean_command_passes(self) -> None:
        """A clean command exits zero with empty stderr and no leftover files."""
        proc = self.run_guard([], [sys.executable, "-I", "-c", "pass"])
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stderr, "")
        self.assertEqual(list(self.outer_dir.iterdir()), [])

    def test_leaked_entries_reported_and_cleaned(self) -> None:
        """Leaked files and directories trigger exit code one and are cleaned up."""
        code = (
            "import pathlib, tempfile; "
            "root = pathlib.Path(tempfile.gettempdir()); "
            "(root / 'left.txt').write_text('leak', encoding='utf-8'); "
            "(root / 'leftdir').mkdir()"
        )
        proc = self.run_guard([], [sys.executable, "-I", "-c", code])
        self.assertEqual(proc.returncode, 1)
        self.assertIn("left.txt", proc.stderr)
        self.assertIn("leftdir", proc.stderr)
        self.assertEqual(list(self.outer_dir.iterdir()), [])

    def test_nonzero_exit_code_preserved(self) -> None:
        """A clean command exiting with a non-zero code preserves its return code."""
        code = "import sys; sys.exit(3)"
        proc = self.run_guard([], [sys.executable, "-I", "-c", code])
        self.assertEqual(proc.returncode, 3)
        self.assertEqual(proc.stderr, "")
        self.assertEqual(list(self.outer_dir.iterdir()), [])

    def test_nonzero_exit_with_leak_preserves_exit_code_and_prints_leak(self) -> None:
        """A failing command that also leaks preserves its exit code and prints the leak line."""
        code = (
            "import pathlib, sys, tempfile; "
            "root = pathlib.Path(tempfile.gettempdir()); "
            "(root / 'left.txt').write_text('leak', encoding='utf-8'); "
            "sys.exit(3)"
        )
        proc = self.run_guard([], [sys.executable, "-I", "-c", code])
        self.assertEqual(proc.returncode, 3)
        self.assertIn("tmp leak guard:", proc.stderr)
        self.assertIn("left.txt", proc.stderr)
        self.assertEqual(list(self.outer_dir.iterdir()), [])

    def test_timeout_returns_124_and_cleans_root(self) -> None:
        """A timed-out command exits with code 124 and cleans its temporary directory."""
        code = "import time; time.sleep(30)"
        proc = self.run_guard(
            ["--timeout", "1"],
            [sys.executable, "-I", "-c", code],
            timeout=15.0,
        )
        self.assertEqual(proc.returncode, 124)
        self.assertEqual(list(self.outer_dir.iterdir()), [])

    def test_missing_command_after_separator_fails_with_rc_2(self) -> None:
        """Invoking the guard without a command after the separator exits with code two."""
        proc = self.run_guard([], [])
        self.assertEqual(proc.returncode, 2)

    def test_tmpdir_is_redirected_to_isolated_directory(self) -> None:
        """The child process executes with TMPDIR pointed inside the outer test directory."""
        code = "import tempfile; print(tempfile.gettempdir())"
        proc = self.run_guard([], [sys.executable, "-I", "-c", code])
        self.assertEqual(proc.returncode, 0)
        child_tmp = Path(proc.stdout.strip()).resolve()
        outer = self.outer_dir.resolve()
        self.assertNotEqual(child_tmp, outer)
        self.assertTrue(child_tmp.is_relative_to(outer))
