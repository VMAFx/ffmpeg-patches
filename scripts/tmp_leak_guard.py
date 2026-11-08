#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Run a command in an isolated temporary directory and fail on file leaks."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess  # nosec B404 - bounded local tool execution without a shell.
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

# Ensure repository root is on sys.path for test/tool imports
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.process_group import kill_process_group  # noqa: E402

MAX_SCANNED_ENTRIES = 1000
MAX_REPORTED_ENTRIES = 20
DEFAULT_TIMEOUT = 3600.0
REAP_TIMEOUT_S = 10


def scan_entries(root: Path, limit: int = MAX_SCANNED_ENTRIES) -> list[str]:
    """List up to limit entry names found directly inside root."""
    entries: list[str] = []
    with os.scandir(root) as it:
        for _ in range(limit):
            try:
                entry = next(it)
            except StopIteration:
                break
            entries.append(entry.name)
    entries.sort()
    return entries


def format_leak_line(cmd: str, entries: list[str]) -> str:
    """Format the diagnostic stderr line describing leaked entries."""
    shown = ", ".join(entries[:MAX_REPORTED_ENTRIES])
    suffix = " (first 20 names)" if len(entries) > MAX_REPORTED_ENTRIES else ""
    return (
        f"tmp leak guard: {cmd} left {len(entries)} entries in its temporary directory: "
        f"{shown}{suffix}; register cleanup, e.g. scratch = tempfile.TemporaryDirectory(); "
        f"self.addCleanup(scratch.cleanup)"
    )


def parse_arguments(argv: Sequence[str]) -> argparse.Namespace:
    """Parse command line arguments for the temporary directory leak guard."""
    parser = argparse.ArgumentParser(
        prog="tmp_leak_guard.py",
        description="Run a command in an isolated TMPDIR and report leaked files.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help="deadline in seconds for the command (default: 3600)",
    )
    parser.add_argument("cmd", metavar="CMD", help="executable to run")
    parser.add_argument("args", nargs="*", metavar="ARG", help="arguments passed to CMD")
    return parser.parse_args(argv)


def _kill_command(process: subprocess.Popen[bytes]) -> None:
    """Terminate command and its descendants where process groups are available."""
    if os.name == "posix":
        kill_process_group(process)
    else:
        # HISS-21: non-POSIX platforms can terminate only the direct child here.
        process.kill()


def _reap_command(cmd: str, process: subprocess.Popen[bytes]) -> None:
    """Reap terminated command without leaking a second timeout."""
    try:
        process.wait(timeout=REAP_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        print(
            f"tmp leak guard: {cmd} did not exit within {REAP_TIMEOUT_S} s after termination",
            file=sys.stderr,
        )


def _wait_command(cmd: str, process: subprocess.Popen[bytes], timeout: float) -> int:
    """Wait with a deadline and kill and reap the command group on timeout or interruption."""
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_command(process)
        _reap_command(cmd, process)
        limit_s = int(timeout) if int(timeout) == timeout else timeout
        print(f"tmp leak guard: {cmd} exceeded the {limit_s} s deadline", file=sys.stderr)
        return 124
    except BaseException:
        _kill_command(process)
        _reap_command(cmd, process)
        raise
    return process.returncode


def run_guard(cmd: str, args: Sequence[str], timeout: float) -> int:
    """Execute command in an isolated temporary directory and report leaked entries."""
    root = Path(tempfile.mkdtemp(prefix="tmp-leak-guard-"))
    cmd_rc = 0
    clean_rc = 0
    entries: list[str] = []
    scan_error = False
    try:
        env = os.environ.copy()
        env["TMPDIR"] = str(root)
        try:
            process = subprocess.Popen(  # nosec B603 - explicit argv list without shell interpolation.
                [cmd, *args],
                env=env,
                # POSIX limit: a descendant that calls setsid() leaves the process
                # group, survives kill_process_group, and can still write into the
                # root; rmtree then fails, the guard reports it and returns 1.
                start_new_session=os.name == "posix",
            )
        except FileNotFoundError as exc:
            print(f"tmp leak guard: {exc}", file=sys.stderr)
            cmd_rc = 127
        else:
            cmd_rc = _wait_command(cmd, process, timeout)
        try:
            entries = scan_entries(root)
        except OSError as exc:
            print(f"tmp leak guard: cannot scan {root}: {exc}", file=sys.stderr)
            scan_error = True
        if entries:
            print(format_leak_line(cmd, entries), file=sys.stderr)
    finally:
        try:
            shutil.rmtree(root, ignore_errors=False)
        except OSError as exc:
            print(f"tmp leak guard: cannot remove {root}: {exc}", file=sys.stderr)
            clean_rc = 1
    if cmd_rc != 0:
        return cmd_rc
    if entries or scan_error:
        return 1
    return clean_rc


def main(argv: Sequence[str] | None = None) -> int:
    """Execute the temporary leak guard CLI entry point."""
    args = parse_arguments(sys.argv[1:] if argv is None else argv)
    return run_guard(args.cmd, args.args, args.timeout)


if __name__ == "__main__":
    sys.exit(main())
