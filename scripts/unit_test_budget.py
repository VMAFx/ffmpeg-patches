#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Report suite timings and bound job elapsed and unit wall time under the leak guard."""

from __future__ import annotations

import argparse
import io
import json
import math
import os
import sys
import tempfile
import time
import unittest
from collections.abc import Sequence
from pathlib import Path
from typing import NamedTuple

import yaml

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import tmp_leak_guard  # noqa: E402

WORKFLOW = ROOT / ".github" / "workflows" / "series.yml"
UNIT_BUDGET_SHARE = 0.60
JOB_START_VARIABLE = "SERIES_JOB_STARTED_AT"
TIMING_DIRECTORY_VARIABLE = "UNIT_SUITE_TIMINGS"
SLOWEST_SUITE_COUNT = 10


class SuiteRecord(NamedTuple):
    """Carry one scheduled task's timing and the parent runner's result counts."""

    name: str
    duration: float
    tests_run: int
    failures: int
    errors: int
    skipped: int
    expected_failures: int
    unexpected_successes: int


def individual_test_suites(tests: unittest.TestSuite) -> unittest.TestSuite:
    """Expose independent methods as singleton tasks under class-level scheduling."""
    classes = list(tests)
    tasks = unittest.TestSuite()
    for class_index in range(len(classes)):
        methods = list(classes[class_index])
        for method_index in range(len(methods)):
            tasks.addTest(unittest.TestSuite([methods[method_index]]))
    return tasks


def job_timeout_seconds(workflow: Path = WORKFLOW) -> int:
    """Read the check job's single authoritative timeout from the workflow."""
    key = "jobs.check.timeout-minutes"
    try:
        document = yaml.safe_load(workflow.read_text(encoding="utf-8"))
        minutes = document["jobs"]["check"]["timeout-minutes"]
    except (OSError, UnicodeError, yaml.YAMLError, KeyError, TypeError) as exc:
        detail = " ".join(str(exc).split())
        raise ValueError(f"{workflow}: cannot read {key}: {detail}") from exc
    if not isinstance(minutes, int) or isinstance(minutes, bool) or minutes <= 0:
        raise ValueError(f"{workflow}: {key} must be a positive integer")
    return minutes * 60


class TimedSuiteRunner(unittest.TextTestRunner):
    """Record duration and result counts per task through unittest_parallel's runner hook."""

    def __init__(self, **kwargs: object) -> None:
        """Keep the parallel result's progress while suppressing per-task summaries."""
        kwargs["stream"] = io.StringIO()
        super().__init__(**kwargs)

    def run(self, test: unittest.TestSuite) -> unittest.TestResult:
        """Time the complete task and record its result including class and module fixtures."""
        first = next(iter(test), None)
        name = first.id() if first is not None else "empty suite"
        if test.countTestCases() > 1 and first is not None:
            name = name.rsplit(".", 1)[0]
        started = time.perf_counter()
        result = unittest.TestResult()
        try:
            result = super().run(test)
            return result
        finally:
            elapsed = time.perf_counter() - started
            directory = os.environ[TIMING_DIRECTORY_VARIABLE]
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", suffix=".tmp", dir=directory, delete=False
            ) as record:
                json.dump(
                    SuiteRecord(
                        name,
                        elapsed,
                        result.testsRun,
                        len(result.failures),
                        len(result.errors),
                        len(result.skipped),
                        len(result.expectedFailures),
                        len(result.unexpectedSuccesses),
                    ),
                    record,
                )
            path = Path(record.name)
            path.replace(path.with_suffix(".json"))


def print_suite_timings(records: Sequence[tuple[str, float]]) -> None:
    """Print the slowest tasks and their shares of all recorded suite time."""
    total = sum(records[index][1] for index in range(len(records)))
    print(f"unit suites: {len(records)} suites; summed time {total:.3f} s", file=sys.stderr)
    slowest = sorted(records, key=lambda row: (-row[1], row[0]))[:SLOWEST_SUITE_COUNT]
    for index in range(len(slowest)):
        name, duration = slowest[index]
        share = duration / total if total else 0.0
        print(f"  {duration:.3f} s ({share:.1%}) {name}", file=sys.stderr)


def print_run_summary(records: Sequence[SuiteRecord], elapsed: float, rc: int) -> None:
    """Restore unittest_parallel's aggregate wording and result-info order after timings."""
    tests_run = sum(records[index].tests_run for index in range(len(records)))
    fields = ("failures", "errors", "skipped", "expected_failures", "unexpected_successes")
    infos = []
    for index in range(len(fields)):
        field = fields[index]
        count = sum(getattr(records[row], field) for row in range(len(records)))
        if count:
            infos.append(f"{field.replace('_', ' ')}={count}")
    # Only the child knows the scheduled task count; records cover completed tasks.
    # A nonzero child status must forbid OK even when missing records hide failures.
    success = rc == 0 and not any(
        records[index].failures or records[index].errors or records[index].unexpected_successes
        for index in range(len(records))
    )
    print(unittest.TextTestResult.separator2, file=sys.stderr)
    print(
        f"Ran {tests_run} {'tests' if tests_run > 1 else 'test'} in {elapsed:.3f}s", file=sys.stderr
    )
    print(file=sys.stderr)
    info = f" ({', '.join(infos)})" if infos else ""
    print(f"{'OK' if success else 'FAILED'}{info}", file=sys.stderr)


def run_with_timings(
    cmd: str, args: Sequence[str], budget: float, outcome: tmp_leak_guard.GuardOutcome
) -> tuple[int, list[SuiteRecord]]:
    """Collect timings and counts outside the guarded temporary directory and clean both."""
    previous = os.environ.get(TIMING_DIRECTORY_VARIABLE)
    with tempfile.TemporaryDirectory(prefix="unit-suite-timings-") as directory:
        os.environ[TIMING_DIRECTORY_VARIABLE] = directory
        try:
            rc = tmp_leak_guard.run_guard(cmd, args, budget, outcome=outcome)
            paths = sorted(Path(directory).glob("*.json"))
            records = [
                SuiteRecord(*json.loads(paths[index].read_text(encoding="utf-8")))
                for index in range(len(paths))
            ]
            print_suite_timings(
                [(records[index].name, records[index].duration) for index in range(len(records))]
            )
            return rc, records
        finally:
            if previous is None:
                os.environ.pop(TIMING_DIRECTORY_VARIABLE, None)
            else:
                os.environ[TIMING_DIRECTORY_VARIABLE] = previous


def job_started_at(now: float) -> float:
    """Require a finite positive job epoch timestamp that is no later than now."""
    try:
        started = float(os.environ[JOB_START_VARIABLE])
    except (KeyError, ValueError) as exc:
        raise ValueError(f"{JOB_START_VARIABLE} must contain a job start epoch timestamp") from exc
    if not math.isfinite(started) or started <= 0 or started > now:
        raise ValueError(
            f"{JOB_START_VARIABLE} must be a finite positive timestamp at or before now"
        )
    return started


def report_budget(label: str, elapsed: float, budget: float, job_timeout: int) -> None:
    """Print either measured clock against the one shared budget and workflow limit."""
    print(
        f"unit tests: {label} {elapsed:.3f} s; budget {budget:g} s "
        f"({UNIT_BUDGET_SHARE:.0%} of series.yml check job's {job_timeout:g} s limit)",
        file=sys.stderr,
        flush=True,
    )


def run_unit_tests(cmd: str, args: Sequence[str], job_timeout: int) -> int:
    """Preserve unit deadlines and statuses while also failing elapsed job overruns."""
    try:
        job_start = job_started_at(time.time())
    except ValueError as exc:
        print(f"unit tests: {exc}", file=sys.stderr, flush=True)
        return 1
    budget = job_timeout * UNIT_BUDGET_SHARE
    outcome = tmp_leak_guard.GuardOutcome()
    started = time.perf_counter()
    rc, records = run_with_timings(cmd, args, budget, outcome)
    elapsed = time.perf_counter() - started
    job_elapsed = time.time() - job_start
    exceeded = elapsed > budget or outcome.timed_out
    job_exceeded = job_elapsed > budget
    print_run_summary(records, elapsed, rc)
    print(f"unit tests: exit status {rc}", file=sys.stderr, flush=True)
    report_budget("wall time", elapsed, budget, job_timeout)
    report_budget("job elapsed", job_elapsed, budget, job_timeout)
    if exceeded:
        print(
            "unit tests: FAIL wall-time budget exceeded; investigate task balance "
            "before the check job is cancelled",
            file=sys.stderr,
            flush=True,
        )
    if job_exceeded:
        print(
            "unit tests: FAIL job elapsed budget exceeded; include setup and prerequisites",
            file=sys.stderr,
            flush=True,
        )
    return rc or int(exceeded or job_exceeded)


def main(argv: Sequence[str] | None = None) -> int:
    """Execute a unit-test command with the workflow-derived time and leak guards."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cmd", metavar="CMD", help="unit-test executable to run")
    parser.add_argument("args", nargs="*", metavar="ARG", help="arguments passed to CMD")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    try:
        job_timeout = job_timeout_seconds()
    except ValueError as exc:
        print(f"unit tests: {exc}", file=sys.stderr)
        return 1
    return run_unit_tests(args.cmd, args.args, job_timeout)


if __name__ == "__main__":
    sys.exit(main())
