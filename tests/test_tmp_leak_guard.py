# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Tests for the temporary directory leak guard."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import socket
import subprocess  # nosec B404 - test runner subprocess invocation with timeout.
import sys
import tempfile
import types
import unittest
import unittest.mock
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

    def _ready_listener(self) -> tuple[socket.socket, int]:
        """Bind a listening localhost TCP socket with timeout and cleanup."""
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(10.0)
        self.addCleanup(listener.close)
        return listener, int(listener.getsockname()[1])

    @staticmethod
    def _descendant_code(port: int, stderr_path: Path) -> str:
        """Return child process code that spawns a grandchild and sleeps."""
        grand = (
            "import pathlib, socket, tempfile\n"
            f"s = socket.create_connection(('127.0.0.1', {port}), timeout=10.0)\n"
            "s.sendall(b'ready\\n')\n"
            "s.settimeout(30.0)\n"
            "for _ in range(100):\n"
            "    if not s.recv(1024):\n"
            "        break\n"
            "target = pathlib.Path(tempfile.gettempdir()) / 'late.txt'\n"
            "target.write_text('late', encoding='utf-8')\n"
        )
        return (
            "import subprocess, sys, time\n"
            f"err = open({str(stderr_path)!r}, 'w', encoding='utf-8')\n"
            f"subprocess.Popen([sys.executable, '-c', {grand!r}], "
            "stderr=err, start_new_session=False)\n"
            "time.sleep(30)\n"
        )

    def _start_guard(self, code: str) -> subprocess.Popen[bytes]:
        """Start the leak guard subprocess redirecting TMPDIR to the outer directory."""
        env = {**os.environ, "TMPDIR": str(self.outer_dir)}
        guard_cmd = [
            sys.executable,
            "-I",
            str(GUARD),
            "--timeout",
            "1",
            "--",
            sys.executable,
            "-I",
            "-c",
            code,
        ]
        guard_proc = subprocess.Popen(  # nosec B603 - fixed guard argv with explicit timeout.
            guard_cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
        )
        self.addCleanup(guard_proc.kill)
        return guard_proc

    def test_timeout_kills_descendants(self) -> None:
        """Descendant processes in the child process tree are terminated on timeout."""
        listener, port = self._ready_listener()
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        grandchild_stderr = Path(scratch.name) / "grandchild_stderr.log"

        code = self._descendant_code(port, grandchild_stderr)
        guard_proc = self._start_guard(code)

        conn, _ = listener.accept()
        self.addCleanup(conn.close)
        conn.settimeout(10.0)
        self.assertEqual(conn.recv(1024), b"ready\n")

        self.assertEqual(guard_proc.wait(timeout=15.0), 124)

        conn.settimeout(5.0)
        self.assertEqual(conn.recv(1024), b"")
        err_text = grandchild_stderr.read_text(encoding="utf-8", errors="replace")
        self.assertNotIn("Traceback", err_text)
        self.assertEqual(list(self.outer_dir.iterdir()), [])

    def test_nonexistent_executable_fails_with_rc_127(self) -> None:
        """A nonexistent command executable triggers exit code 127 and cleans up."""
        proc = self.run_guard([], ["/nonexistent/tmp-leak-guard-cmd"])
        self.assertEqual(proc.returncode, 127)
        self.assertIn("tmp leak guard:", proc.stderr)
        self.assertEqual(list(self.outer_dir.iterdir()), [])

    def test_more_than_twenty_leaked_entries_reported(self) -> None:
        """Leaking more than twenty entries truncates the reported list in stderr."""
        code = (
            "import pathlib, tempfile\n"
            "root = pathlib.Path(tempfile.gettempdir())\n"
            "for i in range(25):\n"
            "    (root / f'leak_{i:02d}.txt').write_text('x', encoding='utf-8')\n"
        )
        proc = self.run_guard([], [sys.executable, "-I", "-c", code])
        self.assertEqual(proc.returncode, 1)
        self.assertIn("25 entries", proc.stderr)
        self.assertIn("(first 20 names)", proc.stderr)
        self.assertEqual(list(self.outer_dir.iterdir()), [])

    def test_scan_error_reported_and_root_cleaned(self) -> None:
        """An unreadable temporary root reports a scan error and removes the root."""
        spec = importlib.util.spec_from_file_location("tmp_leak_guard", GUARD)
        self.assertIsNotNone(spec)
        if spec is None or spec.loader is None:
            self.fail("failed to load tmp_leak_guard spec")
        guard_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(guard_mod)

        prev_tempdir = tempfile.tempdir
        self.addCleanup(setattr, tempfile, "tempdir", prev_tempdir)
        tempfile.tempdir = str(self.outer_dir)

        stderr_buf = io.StringIO()
        with (
            unittest.mock.patch.object(
                guard_mod, "scan_entries", side_effect=PermissionError("denied")
            ),
            contextlib.redirect_stderr(stderr_buf),
        ):
            rc = guard_mod.run_guard(sys.executable, ["-I", "-c", "pass"], 15.0)

        self.assertEqual(rc, 1)
        self.assertIn("cannot scan", stderr_buf.getvalue())
        self.assertEqual(list(self.outer_dir.iterdir()), [])

    def test_kill_command_uses_process_tree(self) -> None:
        """The command termination helper delegates to kill_process_tree."""
        spec = importlib.util.spec_from_file_location("tmp_leak_guard", GUARD)
        self.assertIsNotNone(spec)
        if spec is None or spec.loader is None:
            self.fail("failed to load tmp_leak_guard spec")
        guard_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(guard_mod)

        fake_process = types.SimpleNamespace(pid=4242, kill=unittest.mock.Mock())
        with unittest.mock.patch.object(guard_mod, "kill_process_tree") as mock_kill:
            guard_mod._kill_command(fake_process)
            mock_kill.assert_called_once_with(fake_process)
            fake_process.kill.assert_not_called()
