#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Refuse a lane worktree whose git common dir is not a marked lane clone or names a private ref.

Usage: lane_isolation.py check WORKTREE
       lane_isolation.py mark CLONE
Exit status: 0 on pass, 1 when refused, 2 for usage or git errors.
"""

from __future__ import annotations

import argparse
import os
import subprocess  # nosec B404 - fixed Git argv, no shell, bounded timeout.
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.variant_sweep import git_environment  # noqa: E402

MARKER = "lane.clone"
PRIVATE_WORDS = ("embargo", "nvenc", "qsv")
GIT_TIMEOUT = 30
MAX_REFS = 100000
MAX_LISTED = 10


class IsolationError(ValueError):
    """Report a failed Git query or an unusable answer."""


def run_git(args: list[str]) -> subprocess.CompletedProcess[str]:
    """Run Git with isolated repository paths and a bounded deadline."""
    try:
        return subprocess.run(  # nosec B603 B607 - fixed Git executable, explicit argv.
            ["git", *args],
            env=git_environment(),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            stdin=subprocess.DEVNULL,
            timeout=GIT_TIMEOUT,
        )
    except subprocess.TimeoutExpired as error:
        raise IsolationError(
            f"git {' '.join(args)} timed out after {GIT_TIMEOUT} seconds"
        ) from error


def common_dir(worktree: Path) -> Path:
    """Resolve the absolute Git common directory for the requested worktree."""
    result = run_git(["-C", str(worktree), "rev-parse", "--git-common-dir"])
    directory = result.stdout.removesuffix("\n")
    if result.returncode != 0 or not directory:
        raise IsolationError(f"{worktree}: not a git worktree: {result.stderr.strip()}")
    return Path(os.path.realpath(Path(worktree) / directory))


def inside_work_tree(worktree: Path) -> bool:
    """Check whether the requested path belongs to a checked-out worktree."""
    result = run_git(["-C", str(worktree), "rev-parse", "--is-inside-work-tree"])
    if result.returncode != 0:
        raise IsolationError(f"{worktree}: cannot query work tree: {result.stderr.strip()}")
    return result.stdout.removesuffix("\n") == "true"


def is_lane_clone(common: Path) -> bool:
    """Read the lane marker only from the clone's own common configuration."""
    config = common / "config"
    result = run_git(
        ["config", "--file", str(config), "--no-includes", "--type=bool", "--get", MARKER]
    )
    if result.returncode == 1:
        return False
    if result.returncode != 0:
        raise IsolationError(f"{config}: cannot read {MARKER}: {result.stderr.strip()}")
    return result.stdout.strip() == "true"


def private_refs(worktree: Path) -> list[str]:
    """Collect private-topic references in Git output order within the count cap."""
    result = run_git(
        ["-C", str(worktree), "for-each-ref", f"--count={MAX_REFS + 1}", "--format=%(refname)"]
    )
    if result.returncode != 0:
        raise IsolationError(f"{worktree}: cannot list refs: {result.stderr.strip()}")
    refs = result.stdout.splitlines()
    if len(refs) > MAX_REFS:
        raise IsolationError(f"{worktree}: ref count exceeds cap {MAX_REFS}")
    return [ref for ref in refs if any(word in ref.lower() for word in PRIVATE_WORDS)]


def private_refusals(worktree: Path, common: Path, refs: list[str]) -> list[str]:
    """Format a bounded private-reference listing and summarize any overflow."""
    lines = [
        f"{worktree}: {ref} in {common} names a private topic word ({', '.join(PRIVATE_WORDS)}); "
        "hosted lanes need a clone without such refs"
        for ref in refs[:MAX_LISTED]
    ]
    if len(refs) > MAX_LISTED:
        lines.append(f"{worktree}: {len(refs) - MAX_LISTED} more private refs in {common}")
    return lines


def refusals(worktree: Path) -> list[str]:
    """Return independent worktree, marker, and private-reference refusals."""
    common = common_dir(worktree)
    lines = []
    if not inside_work_tree(worktree):
        lines.append(
            f"{worktree}: not inside a work tree; hosted lanes need a checked-out worktree"
        )
    if not is_lane_clone(common):
        lines.append(
            f"{worktree}: git common dir {common} is not a lane clone "
            f"({MARKER} is not true in {common / 'config'}); "
            "use a fresh public clone marked with lane_isolation.py mark"
        )
    lines.extend(private_refusals(worktree, common, private_refs(worktree)))
    return lines


def mark(clone: Path) -> list[str]:
    """Mark a clone only after rejecting linked worktrees and private references."""
    common = common_dir(clone)
    result = run_git(["-C", str(clone), "rev-parse", "--absolute-git-dir"])
    directory = result.stdout.removesuffix("\n")
    if result.returncode != 0 or not directory:
        raise IsolationError(f"{clone}: cannot read git directory: {result.stderr.strip()}")
    if Path(directory) != common:
        return [f"{clone}: is a linked worktree; mark the clone itself"]
    lines = private_refusals(clone, common, private_refs(clone))
    if lines:
        return lines
    config = common / "config"
    result = run_git(["config", "--file", str(config), MARKER, "true"])
    if result.returncode != 0:
        if is_lane_clone(common):
            return []
        raise IsolationError(f"{config}: cannot set {MARKER}: {result.stderr.strip()}")
    return []


def main(argv: list[str] | None = None) -> int:
    """Run the requested isolation operation and report refusals or Git errors."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check").add_argument("worktree", type=Path)
    commands.add_parser("mark").add_argument("clone", type=Path)
    args = parser.parse_args(argv)
    try:
        path = args.worktree if args.command == "check" else args.clone
        lines = refusals(path) if args.command == "check" else mark(path)
        for line in lines[: MAX_LISTED + 3]:
            print(line, file=sys.stderr)
        if lines:
            return 1
        common = common_dir(path)
        if args.command == "check":
            print(f"lane_isolation: pass {path} (common dir {common})")
        else:
            print(f"lane_isolation: marked {common / 'config'}")
        return 0
    except (IsolationError, OSError) as error:
        print(f"lane_isolation: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
