# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Exercise scheduling and wall-time guards without running repository test fixtures."""

from __future__ import annotations

import ast
import contextlib
import io
import subprocess  # nosec B404 - synthetic tests only, bounded local runner.
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

import unittest_parallel.main
import yaml

from scripts import unit_test_budget


class UnitTestBudgetTest(unittest.TestCase):
    """Keep overruns visible and preserve failures, cleanup and test identifiers."""

    def run_timed(self, elapsed: float, rc: int = 0) -> tuple[int, str]:
        """Simulate elapsed wall time while capturing the actual budget diagnostic."""
        output = io.StringIO()
        with (
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
        guard.assert_called_once_with("runner", ["arg"], 300)

    def test_deadline_failure_names_budget(self) -> None:
        """A killed runner preserves its timeout status and reports the budget failure."""
        rc, output = self.run_timed(360, 124)
        self.assertEqual(rc, 124)
        self.assertIn("FAIL wall-time budget exceeded", output)

    def test_nonzero_status_is_preserved(self) -> None:
        """Child failures remain nonzero both within and beyond the time budget."""
        self.assertEqual(self.run_timed(1, 7)[0], 7)
        self.assertEqual(self.run_timed(361, 7)[0], 7)

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
        """A real sleeping child is terminated at the budget before the job limit."""
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as scratch:
            with (
                unittest.mock.patch.object(tempfile, "tempdir", scratch),
                contextlib.redirect_stderr(output),
            ):
                rc = unit_test_budget.run_unit_tests(
                    sys.executable, ["-B", "-c", "import time; time.sleep(30)"], 1
                )
            self.assertEqual(list(Path(scratch).iterdir()), [])
        self.assertEqual(rc, 124)
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

    def test_singletons_run_in_real_process_pool(self) -> None:
        """A synthetic adapted module produces two tasks and runs both tests in workers."""
        source = (
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
        with tempfile.TemporaryDirectory() as scratch:
            fixture = Path(scratch) / "test_synthetic.py"
            fixture.write_text(source, encoding="utf-8")
            result = subprocess.run(  # nosec B603 - fixed runner and fixture-only tests.
                [
                    sys.executable,
                    "-B",
                    str(unit_test_budget.ROOT / "scripts/unit_test_budget.py"),
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
                    scratch,
                ],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Running 2 test suites (2 total tests) across 2 workers", result.stderr)
        self.assertIn("unit tests: wall time", result.stderr)

    def test_forgejo_adapter_has_no_shared_fixtures(self) -> None:
        """Method tasks remain valid only while the adapted module has no shared fixtures."""
        source = (unit_test_budget.ROOT / "tests/test_ffmpeg_forgejo.py").read_text(
            encoding="utf-8"
        )
        forbidden = {"setUpModule", "tearDownModule", "setUpClass", "tearDownClass"}
        tree = ast.parse(source)
        names = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
        self.assertEqual(names & forbidden, set(), "shared fixtures require coarser tasks")
