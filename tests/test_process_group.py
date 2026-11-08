# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Tests for process group kill helper and isolated script execution."""

from __future__ import annotations

import contextlib
import io
import os
import signal
import subprocess  # nosec B404 - test subprocesses run fixed commands with deadlines.
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from scripts.process_group import (
    TASKKILL_TIMEOUT_S,
    kill_process_group,
    kill_process_tree,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


class ProcessGroupTest(unittest.TestCase):
    """Direct verification of process-group kill guards."""

    def test_pid_matrix_guards(self) -> None:
        """Verify kill_process_group guards against invalid and current-group PIDs."""

        def run_with(pid: object) -> tuple[Mock, Mock]:
            mock_killpg = Mock()
            fake_process = SimpleNamespace(pid=pid, kill=Mock())
            with patch.object(os, "killpg", mock_killpg):
                kill_process_group(fake_process)
            return mock_killpg, fake_process.kill

        for bad_pid in (0, 1, True, False, "invalid", None, 3.14):
            with self.subTest(bad_pid=bad_pid):
                mock_killpg, kill = run_with(bad_pid)
                mock_killpg.assert_not_called()
                kill.assert_not_called()

        with self.subTest(bad_pid=os.getpgrp()):
            mock_killpg, kill = run_with(os.getpgrp())
            mock_killpg.assert_not_called()
            kill.assert_called_once_with()

        good_pid = 4242 if os.getpgrp() != 4242 else 4243
        mock_killpg, kill = run_with(good_pid)
        mock_killpg.assert_called_once_with(good_pid, signal.SIGKILL)
        kill.assert_not_called()

        boundary_pid = 2
        mock_killpg, kill = run_with(boundary_pid)
        mock_killpg.assert_called_once_with(boundary_pid, signal.SIGKILL)
        kill.assert_not_called()

    def test_process_lookup_error_suppressed(self) -> None:
        """Verify kill_process_group suppresses ProcessLookupError on kill and killpg."""
        good_pid = 4242 if os.getpgrp() != 4242 else 4243
        fake_good = SimpleNamespace(pid=good_pid, kill=Mock())
        with patch.object(os, "killpg", side_effect=ProcessLookupError):
            kill_process_group(fake_good)

        fake_own = SimpleNamespace(pid=os.getpgrp(), kill=Mock(side_effect=ProcessLookupError))
        with patch.object(os, "killpg") as mock_killpg:
            kill_process_group(fake_own)
        mock_killpg.assert_not_called()
        fake_own.kill.assert_called_once_with()


class ProcessTreeTest(unittest.TestCase):
    """Direct verification of platform-specific process-tree termination."""

    def test_kill_process_tree_windows_taskkill_argv(self) -> None:
        """Windows invokes taskkill with correct arguments and timeout before process kill."""
        fake_process = SimpleNamespace(pid=4242, kill=Mock())
        with (
            patch.object(os, "name", "nt"),
            patch("subprocess.run") as mock_run,
        ):
            kill_process_tree(fake_process)  # type: ignore[arg-type]
        mock_run.assert_called_once_with(
            ["taskkill", "/T", "/F", "/PID", "4242"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=TASKKILL_TIMEOUT_S,
        )
        fake_process.kill.assert_called_once_with()

    def test_kill_process_tree_windows_fallback_filenotfound(self) -> None:
        """Missing taskkill falls back to process.kill and emits one stderr warning line."""
        fake_process = SimpleNamespace(pid=4242, kill=Mock())
        stderr_buf = io.StringIO()
        with (
            patch.object(os, "name", "nt"),
            patch("subprocess.run", side_effect=FileNotFoundError("taskkill missing")),
            contextlib.redirect_stderr(stderr_buf),
        ):
            kill_process_tree(fake_process)  # type: ignore[arg-type]
        fake_process.kill.assert_called_once_with()
        err_output = stderr_buf.getvalue()
        self.assertIn("falling back to process.kill()", err_output)
        self.assertEqual(len(err_output.splitlines()), 1)

    def test_kill_process_tree_windows_fallback_timeout(self) -> None:
        """Taskkill timeout falls back to process.kill and emits one stderr warning line."""
        fake_process = SimpleNamespace(pid=4242, kill=Mock())
        stderr_buf = io.StringIO()
        with (
            patch.object(os, "name", "nt"),
            patch(
                "subprocess.run",
                side_effect=subprocess.TimeoutExpired(cmd=["taskkill"], timeout=TASKKILL_TIMEOUT_S),
            ),
            contextlib.redirect_stderr(stderr_buf),
        ):
            kill_process_tree(fake_process)  # type: ignore[arg-type]
        fake_process.kill.assert_called_once_with()
        err_output = stderr_buf.getvalue()
        self.assertIn("falling back to process.kill()", err_output)
        self.assertEqual(len(err_output.splitlines()), 1)

    def test_kill_process_tree_windows_pid_matrix_guards(self) -> None:
        """Invalid PIDs, bools, and root IDs skip taskkill and process kill on Windows."""
        for bad_pid in (0, 1, True, False, "invalid", None, 3.14):
            with self.subTest(bad_pid=bad_pid):
                fake_process = SimpleNamespace(pid=bad_pid, kill=Mock())
                with (
                    patch.object(os, "name", "nt"),
                    patch("subprocess.run") as mock_run,
                ):
                    kill_process_tree(fake_process)  # type: ignore[arg-type]
                mock_run.assert_not_called()
                fake_process.kill.assert_not_called()

    def test_kill_process_tree_posix_delegates_to_kill_process_group(self) -> None:
        """POSIX platforms delegate process tree termination to kill_process_group."""
        fake_process = SimpleNamespace(pid=4242, kill=Mock())
        with (
            patch.object(os, "name", "posix"),
            patch("scripts.process_group.kill_process_group") as mock_killpg,
        ):
            kill_process_tree(fake_process)  # type: ignore[arg-type]
        mock_killpg.assert_called_once_with(fake_process)


class IsolatedScriptExecutionTest(unittest.TestCase):
    """Verify scripts execute cleanly under python -I."""

    def test_isolated_script_execution(self) -> None:
        """Prove scripts execute under python -I matching expected return code and output."""
        expectations: tuple[tuple[str, int, str | None], ...] = (
            ("ffmpeg_configure.py", 2, "missing -- before configure arguments"),
            ("lane_brief.py", 0, None),
            ("upstream_lint.py", 0, None),
        )
        for script_name, expected_rc, expected_stderr in expectations:
            with self.subTest(script=script_name):
                script = REPO_ROOT / "scripts" / script_name
                result = subprocess.run(  # nosec B603 - fixed python executable and script path.
                    [sys.executable, "-I", str(script), "--help"],
                    capture_output=True,
                    check=False,
                    timeout=30,
                )
                stderr = result.stderr.decode("utf-8", errors="replace")
                self.assertNotIn("Traceback", stderr, f"{script_name} emitted traceback: {stderr}")
                self.assertEqual(
                    result.returncode,
                    expected_rc,
                    (
                        f"{script_name} returned rc {result.returncode}, "
                        f"expected {expected_rc}: {stderr}"
                    ),
                )
                if expected_stderr is not None:
                    self.assertIn(expected_stderr, stderr)


if __name__ == "__main__":
    unittest.main()
