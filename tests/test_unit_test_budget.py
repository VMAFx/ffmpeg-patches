# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Exercise scheduling and wall-time guards without running repository test fixtures."""

from __future__ import annotations

import ast
import contextlib
import io
import os
import signal
import socket
import subprocess  # nosec B404 - synthetic tests only, bounded local runner.
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

import unittest_parallel.main
import yaml

from scripts import unit_test_budget

SINGLETON_POOL_SOURCE = (
    "import unittest\n"
    "from scripts import unit_test_budget\n"
    "class IndependentCase(unittest.TestCase):\n"
    "    def test_one(self):\n"
    '        """Pass the first independent case."""\n'
    "        self.assertTrue(True)\n"
    "    def test_two(self):\n"
    '        """Pass the second independent case."""\n'
    "        self.assertTrue(True)\n"
    "def load_tests(loader, tests, pattern):\n"
    '    """Expose each synthetic case as a task."""\n'
    "    return unit_test_budget.individual_test_suites(tests)\n"
)
POOL_WRAPPER_CODE = (
    "import sys, unittest.mock\n"
    "from scripts import unit_test_budget\n"
    "with unittest.mock.patch.object(\n"
    "    unit_test_budget, 'job_timeout_seconds', return_value=float(sys.argv[1])\n"
    "):\n"
    "    sys.exit(unit_test_budget.main(sys.argv[2:]))\n"
)


class UnitTestBudgetTest(unittest.TestCase):
    """Keep overruns visible and preserve failures, cleanup and test identifiers."""

    def run_timed(self, elapsed: float, rc: int = 0, *, timed_out: bool = False) -> tuple[int, str]:
        """Simulate elapsed wall time while capturing the actual budget diagnostic."""
        output = io.StringIO()
        outcome = unit_test_budget.tmp_leak_guard.GuardOutcome(timed_out=timed_out)
        with (
            unittest.mock.patch.object(
                unit_test_budget.tmp_leak_guard, "GuardOutcome", return_value=outcome
            ),
            unittest.mock.patch.object(
                unit_test_budget.time, "perf_counter", side_effect=[0, elapsed]
            ),
            unittest.mock.patch.object(
                unit_test_budget.tmp_leak_guard, "run_guard", return_value=rc
            ),
            contextlib.redirect_stderr(output),
        ):
            result = unit_test_budget.run_unit_tests("runner", ["arg"], 600)
        return result, output.getvalue()

    def test_within_budget_reports_elapsed_wall_time(self) -> None:
        """A successful run below the budget reports its elapsed time and share."""
        rc, output = self.run_timed(359.5)
        self.assertEqual(rc, 0)
        self.assertIn("wall time 359.500 s", output)
        self.assertIn("budget 360 s (60%", output)
        self.assertNotIn("FAIL", output)

    def test_exact_budget_boundary_passes(self) -> None:
        """A completed run at exactly the wall-time budget remains successful."""
        self.assertEqual(self.run_timed(360)[0], 0)

    def test_planted_overrun_fails_after_successful_command(self) -> None:
        """A planted overrun including cleanup cannot return a successful status."""
        rc, output = self.run_timed(360.001)
        self.assertEqual(rc, 1)
        self.assertIn("FAIL wall-time budget exceeded", output)
        self.assertIn("before the check job is cancelled", output)

    def test_guard_deadline_is_workflow_share(self) -> None:
        """The whole command receives the budget as its leak-guard deadline."""
        with (
            unittest.mock.patch.object(unit_test_budget.time, "perf_counter", side_effect=[0, 1]),
            unittest.mock.patch.object(
                unit_test_budget.tmp_leak_guard, "run_guard", return_value=0
            ) as guard,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(unit_test_budget.run_unit_tests("runner", ["arg"], 500), 0)
        guard.assert_called_once_with(
            "runner", ["arg"], 300, outcome=unit_test_budget.tmp_leak_guard.GuardOutcome()
        )

    def test_deadline_failure_names_budget(self) -> None:
        """A killed runner preserves its timeout status and reports the budget failure."""
        rc, output = self.run_timed(360, 124, timed_out=True)
        self.assertEqual(rc, 124)
        self.assertIn("FAIL wall-time budget exceeded", output)

    def test_nonzero_status_is_preserved(self) -> None:
        """Reserved-looking child statuses remain statuses rather than inferred causes."""
        statuses = (1, 2, 7, 124, 127)
        for index in range(len(statuses)):
            rc = statuses[index]
            with self.subTest(status=rc):
                result, output = self.run_timed(1, rc)
                self.assertEqual(result, rc)
                self.assertNotIn("FAIL wall-time budget exceeded", output)
                result, output = self.run_timed(361, rc)
                self.assertEqual(result, rc)
                self.assertIn("FAIL wall-time budget exceeded", output)

    def test_fast_child_status_124_is_not_a_deadline(self) -> None:
        """A fast child exit 124 keeps its status without a false overrun diagnostic."""
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            rc = unit_test_budget.run_unit_tests(
                sys.executable, ["-B", "-c", "import sys; sys.exit(124)"], 600
            )
        self.assertEqual(rc, 124)
        self.assertIn("unit tests: wall time", output.getvalue())
        self.assertNotIn("FAIL wall-time budget exceeded", output.getvalue())
        self.assertNotIn("exceeded the", output.getvalue())

    def assert_workflow_refused(self, workflow: Path) -> None:
        """Refuse a bad workflow with one contextual line before starting the test run."""
        reader = unit_test_budget.job_timeout_seconds
        output = io.StringIO()
        with (
            unittest.mock.patch.object(
                unit_test_budget, "job_timeout_seconds", side_effect=lambda: reader(workflow)
            ),
            unittest.mock.patch.object(unit_test_budget, "run_unit_tests") as run,
            contextlib.redirect_stderr(output),
        ):
            self.assertEqual(unit_test_budget.main(["runner"]), 1)
        run.assert_not_called()
        self.assertIn(str(workflow), output.getvalue())
        self.assertIn("jobs.check.timeout-minutes", output.getvalue())
        self.assertNotIn("Traceback", output.getvalue())
        self.assertEqual(len(output.getvalue().splitlines()), 1)

    def test_unreadable_workflow_is_refused(self) -> None:
        """An unreadable workflow names its file and key path without starting tests."""
        with tempfile.TemporaryDirectory() as scratch:
            workflow = Path(scratch) / "unreadable.yml"
            with unittest.mock.patch.object(
                Path, "read_text", side_effect=PermissionError("permission denied")
            ):
                self.assert_workflow_refused(workflow)

    def test_missing_workflow_job_is_refused(self) -> None:
        """A missing check job names its file and key path without starting tests."""
        with tempfile.TemporaryDirectory() as scratch:
            workflow = Path(scratch) / "missing-job.yml"
            workflow.write_text("jobs:\n  other: {}\n", encoding="utf-8")
            self.assert_workflow_refused(workflow)

    def test_missing_workflow_timeout_is_refused(self) -> None:
        """A missing timeout key names its file and key path without starting tests."""
        with tempfile.TemporaryDirectory() as scratch:
            workflow = Path(scratch) / "missing-timeout.yml"
            workflow.write_text("jobs:\n  check: {}\n", encoding="utf-8")
            self.assert_workflow_refused(workflow)

    def test_positive_integer_yaml_anchor_is_accepted(self) -> None:
        """A resolved positive integer YAML anchor supplies the workflow timeout."""
        with tempfile.TemporaryDirectory() as scratch:
            workflow = Path(scratch) / "anchored.yml"
            workflow.write_text(
                "limit: &limit 7\njobs:\n  check:\n    timeout-minutes: *limit\n",
                encoding="utf-8",
            )
            self.assertEqual(unit_test_budget.job_timeout_seconds(workflow), 420)

    def test_workflow_is_single_timeout_source(self) -> None:
        """The reader follows the live check job rather than a second timeout constant."""
        document = yaml.safe_load(unit_test_budget.WORKFLOW.read_text(encoding="utf-8"))
        seconds = document["jobs"]["check"]["timeout-minutes"] * 60
        self.assertEqual(unit_test_budget.job_timeout_seconds(), seconds)
        with tempfile.TemporaryDirectory() as scratch:
            workflow = Path(scratch) / "series.yml"
            workflow.write_text("jobs:\n  check:\n    timeout-minutes: 7\n", encoding="utf-8")
            self.assertEqual(unit_test_budget.job_timeout_seconds(workflow), 420)

    def test_invalid_workflow_timeout_is_rejected(self) -> None:
        """Malformed timeout values cannot silently disable the unit deadline."""
        with tempfile.TemporaryDirectory() as scratch:
            workflow = Path(scratch) / "series.yml"
            values = ("0", "-1", "true", "'10'", "null", "1.5")
            for index in range(len(values)):
                value = values[index]
                with self.subTest(value=value):
                    workflow.write_text(
                        f"jobs:\n  check:\n    timeout-minutes: {value}\n", encoding="utf-8"
                    )
                    with self.assertRaises(ValueError):
                        unit_test_budget.job_timeout_seconds(workflow)

    def test_cli_preserves_command_arguments(self) -> None:
        """The CLI passes arguments after the separator unchanged to the guarded command."""
        with (
            unittest.mock.patch.object(unit_test_budget, "job_timeout_seconds", return_value=600),
            unittest.mock.patch.object(unit_test_budget, "run_unit_tests", return_value=9) as run,
        ):
            self.assertEqual(unit_test_budget.main(["--", "runner", "-j", "4"]), 9)
        run.assert_called_once_with("runner", ["-j", "4"], 600)

    def test_cli_requires_a_command(self) -> None:
        """Missing commands fail argument parsing before starting a process."""
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            unit_test_budget.main([])
        self.assertEqual(caught.exception.code, 2)

    def test_real_guard_cleans_planted_leak(self) -> None:
        """The budget wrapper retains whole-command leak detection and directory cleanup."""
        code = (
            "import pathlib, tempfile; pathlib.Path(tempfile.gettempdir(), 'planted-leak').touch()"
        )
        with tempfile.TemporaryDirectory() as scratch:
            output = io.StringIO()
            with (
                unittest.mock.patch.object(tempfile, "tempdir", scratch),
                contextlib.redirect_stderr(output),
            ):
                rc = unit_test_budget.run_unit_tests(sys.executable, ["-B", "-c", code], 10)
            self.assertEqual(list(Path(scratch).iterdir()), [])
        self.assertEqual(rc, 1)
        self.assertIn("planted-leak", output.getvalue())
        self.assertIn("unit tests: wall time", output.getvalue())

    def test_real_guard_timeout_cleans_and_reports(self) -> None:
        """A real timeout reports failure even when measured elapsed time is below budget."""
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as scratch:
            with (
                unittest.mock.patch.object(tempfile, "tempdir", scratch),
                unittest.mock.patch.object(
                    unit_test_budget.time, "perf_counter", side_effect=[0, 0.1]
                ),
                contextlib.redirect_stderr(output),
            ):
                rc = unit_test_budget.run_unit_tests(
                    sys.executable, ["-B", "-c", "import time; time.sleep(30)"], 1
                )
            self.assertEqual(list(Path(scratch).iterdir()), [])
        self.assertEqual(rc, 124)
        self.assertIn("exceeded the 0.6 s deadline", output.getvalue())
        self.assertIn("wall time 0.100 s", output.getvalue())
        self.assertIn("FAIL wall-time budget exceeded", output.getvalue())

    def test_singletons_preserve_ids_and_class_scheduling(self) -> None:
        """Independent cases retain every ID while each becomes a distinct class-level task."""
        cases = [unittest.FunctionTestCase(bool), unittest.FunctionTestCase(int)]
        original = unittest.TestSuite([unittest.TestSuite(cases)])
        adapted = unit_test_budget.individual_test_suites(original)
        tasks = list(unittest_parallel.main._iter_class_suites(adapted))
        self.assertEqual(original.countTestCases(), adapted.countTestCases())
        self.assertEqual([task.countTestCases() for task in tasks], [1, 1])
        self.assertEqual([list(task)[0].id() for task in tasks], [case.id() for case in cases])

    def test_ordinary_class_stays_one_task(self) -> None:
        """Unadapted modules retain their full class suites and class-fixture boundaries."""
        cases = [unittest.FunctionTestCase(bool), unittest.FunctionTestCase(int)]
        original = unittest.TestSuite([unittest.TestSuite(cases)])
        tasks = list(unittest_parallel.main._iter_class_suites(original))
        self.assertEqual([task.countTestCases() for task in tasks], [2])

    def test_empty_singleton_suite(self) -> None:
        """Empty suites add no synthetic cases or tasks."""
        suite = unit_test_budget.individual_test_suites(unittest.TestSuite())
        self.assertEqual(suite.countTestCases(), 0)
        self.assertEqual(list(unittest_parallel.main._iter_class_suites(suite)), [])

    def test_make_target_uses_budget_and_fine_tasks(self) -> None:
        """The actual target retains Git isolation and selects guarded class scheduling."""
        makefile = (unit_test_budget.ROOT / "Makefile").read_text(encoding="utf-8")
        recipe = makefile.split("test: dev-deps build lint\n", 1)[1].split("\n\n", 1)[0]
        self.assertIn("GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1", recipe)
        self.assertIn("scripts/unit_test_budget.py --", recipe)
        self.assertIn("-m unittest_parallel -j $(UNIT_JOBS) --level class -s tests", recipe)
        self.assertNotIn("--level module", recipe)

    def run_pool_fixture(
        self,
        source: str = SINGLETON_POOL_SOURCE,
        timeout: float = 15.0,
        *,
        fixture_paths: list[Path] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run a synthetic fixture under the budget wrapper with an inner deadline below timeout."""
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            fixture_dir = root / "fixture"
            guard_dir = root / "tmp"
            fixture_dir.mkdir()
            guard_dir.mkdir()
            if fixture_paths is not None:
                fixture_paths.append(fixture_dir)
            (fixture_dir / "test_synthetic.py").write_text(source, encoding="utf-8")
            result = subprocess.run(  # nosec B603 - fixed runner and fixture-only tests.
                [
                    sys.executable,
                    "-B",
                    "-c",
                    POOL_WRAPPER_CODE,
                    f"{timeout:g}",
                    "--",
                    sys.executable,
                    "-B",
                    "-m",
                    "unittest_parallel",
                    "-j",
                    "2",
                    "--level",
                    "class",
                    "-s",
                    str(fixture_dir),
                ],
                capture_output=True,
                text=True,
                env={**os.environ, "TMPDIR": str(guard_dir)},
                timeout=timeout,
                check=False,
            )
            self.assertEqual(list(guard_dir.iterdir()), [])
        self.assertFalse(root.exists())
        return result

    def _pool_listener(self) -> tuple[socket.socket, int]:
        """Bind a localhost TCP listener with a bounded timeout and registered cleanup."""
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(5.0)
        self.addCleanup(listener.close)
        return listener, int(listener.getsockname()[1])

    @staticmethod
    def _kill_worker_group(pid: int) -> None:
        """Terminate a surviving worker process and its process group if still running."""
        if os.name == "posix":
            with contextlib.suppress(ProcessLookupError, PermissionError):
                pgid = os.getpgid(pid)
                if pgid > 1 and pgid != os.getpgrp():
                    os.killpg(pgid, signal.SIGKILL)
        with contextlib.suppress(ProcessLookupError, OSError):
            os.kill(pid, signal.SIGKILL)

    @staticmethod
    def _worker_probe_source(port: int, body: str, *, second_case: bool = True) -> str:
        """Build a synthetic adapted module whose first worker reports its PID over TCP."""
        second = (
            "    def test_two(self):\n"
            '        """Pass the second independent case."""\n'
            "        self.assertTrue(True)\n"
            if second_case
            else ""
        )
        return (
            "import os, socket, time, unittest\n"
            "from scripts import unit_test_budget\n"
            "class IndependentCase(unittest.TestCase):\n"
            "    def test_one(self):\n"
            '        """Report the worker PID and execute the requested probe body."""\n'
            f"        sock = socket.create_connection(('127.0.0.1', {port}), timeout=5.0)\n"
            "        sock.sendall(f'{os.getpid()}\\n'.encode('ascii'))\n"
            f"        {body}\n"
            "        sock.close()\n"
            f"{second}"
            "def load_tests(loader, tests, pattern):\n"
            '    """Expose each synthetic case as a task."""\n'
            "    return unit_test_budget.individual_test_suites(tests)\n"
        )

    def _accept_worker_pid(self, listener: socket.socket) -> tuple[int, socket.socket]:
        """Accept one worker probe connection, register process cleanup and return its PID."""
        conn, _ = listener.accept()
        self.addCleanup(conn.close)
        conn.settimeout(1.0)
        data = bytearray()
        for _ in range(64):
            chunk = conn.recv(64)
            if not chunk:
                break
            data.extend(chunk)
            if b"\n" in chunk:
                break
        pid = int(data.decode("ascii").strip())
        self.addCleanup(self._kill_worker_group, pid)
        return pid, conn

    def test_singletons_run_in_real_process_pool(self) -> None:
        """A synthetic adapted module produces two tasks and runs both tests in workers."""
        result = self.run_pool_fixture()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Running 2 test suites (2 total tests) across 2 workers", result.stderr)
        self.assertIn("unit tests: wall time", result.stderr)

    def test_singleton_pool_timeout_kills_descendants_and_cleans_fixture(self) -> None:
        """A timed-out pool run kills worker descendants and removes fixture directories."""
        listener, port = self._pool_listener()
        source = self._worker_probe_source(port, "time.sleep(30)")
        fixture_paths: list[Path] = []
        exc: subprocess.TimeoutExpired | None = None
        result: subprocess.CompletedProcess[str] | None = None
        try:
            result = self.run_pool_fixture(source, timeout=2.5, fixture_paths=fixture_paths)
        except subprocess.TimeoutExpired as caught:
            exc = caught
        worker_pid, conn = self._accept_worker_pid(listener)
        self.assertEqual(len(fixture_paths), 1)
        self.assertFalse(fixture_paths[0].exists())
        with self.assertRaises(ProcessLookupError):
            os.kill(worker_pid, 0)
        self.assertEqual(conn.recv(64), b"")
        self.assertIsNone(exc)
        if result is not None:
            self.assertEqual(result.returncode, 124, result.stderr)
            self.assertIn("exceeded the 1.5 s deadline", result.stderr)
            self.assertIn("FAIL wall-time budget exceeded", result.stderr)

    def test_singleton_pool_failing_worker_cleans_descendants_and_fixture(self) -> None:
        """A failing worker case preserves failure status without a false timeout or leak."""
        invalid_timeouts = (0, -1)
        for index in range(len(invalid_timeouts)):
            invalid_timeout = invalid_timeouts[index]
            with self.subTest(timeout=invalid_timeout), self.assertRaises(ValueError):
                self.run_pool_fixture(timeout=invalid_timeout)
        listener, port = self._pool_listener()
        source = self._worker_probe_source(port, "sock.close(); self.fail('planted failure')")
        fixture_paths: list[Path] = []
        result = self.run_pool_fixture(source, timeout=10.0, fixture_paths=fixture_paths)
        worker_pid, conn = self._accept_worker_pid(listener)
        self.assertEqual(len(fixture_paths), 1)
        self.assertFalse(fixture_paths[0].exists())
        with self.assertRaises(ProcessLookupError):
            os.kill(worker_pid, 0)
        self.assertEqual(conn.recv(64), b"")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertNotIn("FAIL wall-time budget exceeded", result.stderr)

    def test_singleton_pool_single_case_boundary_within_deadline_passes(self) -> None:
        """A single-case fixture completing within a short deadline cleans up and passes."""
        listener, port = self._pool_listener()
        source = self._worker_probe_source(port, "time.sleep(0.1)", second_case=False)
        fixture_paths: list[Path] = []
        result = self.run_pool_fixture(source, timeout=5.0, fixture_paths=fixture_paths)
        worker_pid, conn = self._accept_worker_pid(listener)
        self.assertEqual(len(fixture_paths), 1)
        self.assertFalse(fixture_paths[0].exists())
        with self.assertRaises(ProcessLookupError):
            os.kill(worker_pid, 0)
        self.assertEqual(conn.recv(64), b"")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("budget 3 s (60% of series.yml check job's 5 s limit)", result.stderr)
        self.assertNotIn("FAIL", result.stderr)

    def test_forgejo_adapter_has_no_shared_fixtures(self) -> None:
        """Method tasks remain valid only while the adapted module has no shared fixtures."""
        source = (unit_test_budget.ROOT / "tests/test_ffmpeg_forgejo.py").read_text(
            encoding="utf-8"
        )
        forbidden = {"setUpModule", "tearDownModule", "setUpClass", "tearDownClass"}
        tree = ast.parse(source)
        names = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
        self.assertEqual(names & forbidden, set(), "shared fixtures require coarser tasks")
