#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Bound unit-test wall time while retaining temporary-file leak detection."""

from __future__ import annotations

import argparse
import sys
import time
import unittest
from collections.abc import Sequence
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import tmp_leak_guard  # noqa: E402

WORKFLOW = ROOT / ".github" / "workflows" / "series.yml"
UNIT_BUDGET_SHARE = 0.60


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
    if type(minutes) is not int or minutes <= 0:
        raise ValueError(f"{workflow}: {key} must be a positive integer")
    return minutes * 60


def run_unit_tests(cmd: str, args: Sequence[str], job_timeout: int) -> int:
    """Run the entire suite under the leak guard and fail above its wall-time budget."""
    budget = job_timeout * UNIT_BUDGET_SHARE
    outcome = tmp_leak_guard.GuardOutcome()
    started = time.perf_counter()
    rc = tmp_leak_guard.run_guard(cmd, args, budget, outcome=outcome)
    elapsed = time.perf_counter() - started
    exceeded = elapsed > budget or outcome.timed_out
    print(
        f"unit tests: wall time {elapsed:.3f} s; budget {budget:g} s "
        f"({UNIT_BUDGET_SHARE:.0%} of series.yml check job's {job_timeout:g} s limit)",
        file=sys.stderr,
        flush=True,
    )
    if exceeded:
        print(
            "unit tests: FAIL wall-time budget exceeded; investigate task balance "
            "before the check job is cancelled",
            file=sys.stderr,
            flush=True,
        )
    return rc or int(exceeded)


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
