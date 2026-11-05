#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Render or check a lane brief against repository standards.

Usage: lane_brief.py render --branch B --task T --finding F --fix X --tests S --paths P --modules M
       lane_brief.py check BRIEF [--config FILE] [--runner PATH] [--worktree DIR]
Exit status: 0 when valid, 1 for invalid briefs, 2 for usage or input errors.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import os
import re
import selectors
import signal
import stat
import subprocess  # nosec B404 - runner precheck with explicit argv, no shell, bounded timeout.
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "scripts" / "lane_brief_template.md"
CONFIG = ROOT / ".config" / "lane-brief.yaml"
NAMES = ("task", "branch", "n", "r", "finding", "fix", "tests", "paths", "modules")
PLACEHOLDERS = re.compile(r"<(task|branch|n|r|finding|fix|tests|paths|modules)>")
MAX_SIZE = 1024 * 1024
MAX_LINES = 10000
MAX_LINE_SIZE = 65536
RUNNER_TIMEOUT = 120
RUNNER_OUTPUT_LIMIT = 65536


def validate_lines(lines: list[bytes], label: str) -> None:
    """Reject content exceeding the byte size, line count, or line length limits."""
    if sum(map(len, lines)) > MAX_SIZE:
        raise ValueError(f"{label}: size exceeds 1 MiB")
    if len(lines) > MAX_LINES:
        raise ValueError(f"{label}: exceeds 10000 lines")
    if any(len(line) > MAX_LINE_SIZE for line in lines):
        raise ValueError(f"{label}: line exceeds 65536 bytes")


def bounded_lines(path: Path) -> list[str]:
    """Read bounded lines from a regular file of at most 1 MiB."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError(f"{path}: not a regular file")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            lines = io.BytesIO(source.read(MAX_SIZE + 1)).readlines()
    finally:
        os.close(descriptor)
    validate_lines(lines, str(path))
    return [line.decode("utf-8") for line in lines]


def read_required(path: Path) -> list[str]:
    """Read and validate the required substrings from a bounded YAML document."""
    try:
        import yaml  # noqa: PLC0415 - optional parser dependency.
    except ImportError as error:
        raise ValueError(f"{path}: missing pyyaml; install requirements-dev.txt") from error

    class UniqueLoader(yaml.SafeLoader):
        """Reject duplicate mapping keys while loading safe YAML."""

        def construct_mapping(self, node: object, deep: bool = False) -> dict:
            """Construct a mapping only when each key is unique."""
            self.flatten_mapping(node)
            seen: set = set()
            for key_node, _ in node.value:
                key = self.construct_object(key_node, deep=deep)
                try:
                    duplicate = key in seen
                    seen.add(key)
                except TypeError as error:
                    raise ValueError(f"{path}: invalid YAML: unhashable key") from error
                if duplicate:
                    message = (
                        "duplicate required key" if key == "required" else f"duplicate key {key}"
                    )
                    raise ValueError(f"{path}: {message}")
            return super().construct_mapping(node, deep=deep)

    try:
        loader = UniqueLoader("".join(bounded_lines(path)))
        try:
            document = loader.get_single_data()
        finally:
            loader.dispose()
    except yaml.YAMLError as error:
        raise ValueError(f"{path}: invalid YAML: {error}") from error
    return validate_required(path, document)


def validate_required(path: Path, document: object) -> list[str]:
    """Validate and normalize the required list in a YAML document."""
    if not isinstance(document, dict) or not isinstance(document.get("required"), list):
        raise ValueError(f"{path}: missing or invalid required list")
    items = document["required"]
    if not items:
        raise ValueError(f"{path}: missing or empty required list")
    if len(items) > 32:
        raise ValueError(f"{path}: required list exceeds 32 items")
    result: list[str] = []
    for item in items:
        if not isinstance(item, str) or not item.strip() or "\n" in item or "\r" in item:
            raise ValueError(f"{path}: invalid required item")
        value = item.strip()
        if len(value) > 200:
            raise ValueError(f"{path}: required item exceeds 200 characters")
        result.append(value)
    return result


def check(content: str, label: str, required: list[str]) -> int:
    """Print each missing requirement and unfilled placeholder."""
    errors = [f"{label}: missing required line: {item}" for item in required if item not in content]
    errors.extend(
        f"{label}: unfilled placeholder <{name}>" for name in NAMES if f"<{name}>" in content
    )
    for error in errors:
        print(error, file=sys.stderr)
    return int(bool(errors))


def parser_for_cli() -> argparse.ArgumentParser:
    """Define the render and check command arguments."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    render = commands.add_parser("render")
    for name in ("branch", "task", "finding", "fix", "tests", "paths", "modules"):
        render.add_argument(f"--{name}", required=True)
    render.add_argument("--issue", default="none")
    render.add_argument("--round", default="1")
    render.add_argument("-o", type=Path)
    verify = commands.add_parser("check")
    verify.add_argument("brief", type=Path)
    verify.add_argument("--config", type=Path, default=None)
    verify.add_argument("--runner", type=Path, default=os.environ.get("LANE_BRIEF_RUNNER") or None)
    verify.add_argument("--worktree", type=Path, default=ROOT)
    return parser


def render_brief(args: argparse.Namespace) -> int:
    """Render and validate before writing any output."""
    values = {
        name: getattr(args, name)
        for name in ("task", "branch", "finding", "fix", "tests", "paths", "modules")
    }
    values.update(n=args.issue, r=args.round)
    for name, value in values.items():
        if len(value) > 8192:
            argument = {"n": "issue", "r": "round"}.get(name, name)
            raise ValueError(f"--{argument}: exceeds 8192 characters")
    content = PLACEHOLDERS.sub(
        lambda match: values[match.group(1)], TEMPLATE.read_text(encoding="utf-8")
    )
    validate_lines(io.BytesIO(content.encode("utf-8")).readlines(), str(args.o or "stdout"))
    result = check(content, str(args.o) if args.o else "stdout", read_required(CONFIG))
    if result:
        return result
    if args.o:
        args.o.write_text(content, encoding="utf-8")
    else:
        sys.stdout.write(content)
    return 0


def read_runner_output(process: subprocess.Popen[bytes], deadline: float) -> bytes:
    """Read runner output within the byte limit and monotonic deadline."""
    if process.stdout is None:
        raise OSError("runner stdout pipe unavailable")
    output = bytearray()
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        for _ in range(RUNNER_OUTPUT_LIMIT + 2):
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                raise subprocess.TimeoutExpired(process.args, RUNNER_TIMEOUT)
            chunk = os.read(process.stdout.fileno(), 4096)
            if not chunk:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(process.args, RUNNER_TIMEOUT)
                process.wait(timeout=remaining)
                return bytes(output)
            output.extend(chunk)
            if len(output) > RUNNER_OUTPUT_LIMIT:
                raise ValueError(f"output exceeds {RUNNER_OUTPUT_LIMIT} bytes")
    raise ValueError(f"output exceeds {RUNNER_OUTPUT_LIMIT} bytes")


def run_runner(argv: list[str]) -> tuple[bytes, int]:
    """Run and reap a runner while collecting bounded combined output."""
    process = subprocess.Popen(  # nosec B603 - configured runner path, explicit argv, no shell.
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    completed = False
    try:
        output = read_runner_output(process, time.monotonic() + RUNNER_TIMEOUT)
        completed = True
        return output, process.returncode
    finally:
        if not completed or process.poll() != 0:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
        try:
            process.wait(timeout=5)
        finally:
            if process.stdout is not None:
                process.stdout.close()


def check_runner(runner: Path, brief: Path, worktree: Path) -> int:
    """Run the lane runner precheck and report its output and result."""
    try:
        output, returncode = run_runner([str(runner), "--check-brief", str(brief), str(worktree)])
    except subprocess.TimeoutExpired:
        print(
            f"lane_brief: runner failed: timed out after {RUNNER_TIMEOUT} seconds", file=sys.stderr
        )
        return 2
    except (OSError, ValueError) as error:
        print(f"lane_brief: runner failed: {error}", file=sys.stderr)
        return 2
    sys.stderr.write(output.decode("utf-8", errors="replace"))
    if returncode:
        print(f"lane_brief: runner refused brief (rc {returncode})", file=sys.stderr)
        return 1
    return 0


def check_brief(args: argparse.Namespace) -> int:
    """Validate the brief locally and run the configured runner precheck."""
    content = "".join(bounded_lines(args.brief))
    config = (
        args.config if args.config is not None else args.worktree / ".config" / "lane-brief.yaml"
    )
    result = check(content, str(args.brief), read_required(config))
    if result:
        return result
    if not args.runner:
        print("lane_brief: runner check skipped: LANE_BRIEF_RUNNER unset", file=sys.stderr)
        return result
    return check_runner(args.runner, args.brief, args.worktree)


def main(argv: list[str] | None = None) -> int:
    """Run the selected command and report input errors."""
    args = parser_for_cli().parse_args(argv)
    try:
        if args.command == "render":
            return render_brief(args)
        return check_brief(args)
    except (OSError, UnicodeError, ValueError) as error:
        print(f"lane_brief: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
