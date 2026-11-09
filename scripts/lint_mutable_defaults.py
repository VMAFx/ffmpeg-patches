#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Check Python mutable defaults with the pinned Pylint version."""

from __future__ import annotations

import os
import shutil
import subprocess  # nosec B404 - bounded local tool invocations, without a shell.
import sys
from pathlib import Path

PYLINT_VERSION = "4.1.3"
PYLINT_FLAGS = (
    "--disable=all",
    "--enable=dangerous-default-value",
    "--score=n",
    "--persistent=n",
)


def resolve_pylint() -> list[str] | None:
    """Use pinned Pylint under the active interpreter, on PATH, or via uvx."""

    def _interpreter_candidate() -> list[str]:
        """Return the active Python interpreter running Pylint as a module."""
        return [sys.executable, "-m", "pylint"]

    def _path_candidate() -> list[str] | None:
        """Return the Pylint executable located on PATH when present."""
        path = shutil.which("pylint")
        return [path] if path else None

    for resolve_candidate in (_interpreter_candidate, _path_candidate):
        candidate = resolve_candidate()
        if candidate is None:
            continue
        try:
            version = subprocess.run(  # nosec B603 - resolved tool, fixed arguments.
                [*candidate, "--version"], capture_output=True, text=True, check=True, timeout=10
            )
            if version.stdout.splitlines()[:1] == [f"pylint {PYLINT_VERSION}"]:
                return candidate
        except (OSError, subprocess.SubprocessError):
            pass
    uvx = shutil.which("uvx")
    if uvx:
        return [uvx, "--from", f"pylint=={PYLINT_VERSION}", "pylint"]
    return None


def main(argv: list[str] | None = None) -> int:
    """Lint explicit paths or existing tracked and unignored Python files."""
    paths = sys.argv[1:] if argv is None else argv
    try:
        if not paths:
            listed = subprocess.run(  # nosec B603 B607 - fixed local Git query, no shell.
                ["git", "ls-files", "-c", "-o", "--exclude-standard", "-z", "--", "*.py"],
                capture_output=True,
                check=True,
                timeout=30,
            )
            paths = [os.fsdecode(path) for path in listed.stdout.split(b"\0") if path]
        files = [path for path in paths if Path(path).is_file()]
        if not files:
            print("lint-mutable-defaults: no Python files")
            return 0
        command = resolve_pylint()
        if command is None:
            print(
                (
                    f"lint-mutable-defaults: neither pylint=={PYLINT_VERSION} under"
                    f" {sys.executable} or on PATH nor uvx found"
                ),
                file=sys.stderr,
            )
            return 1
        # Pylint preprocesses options even after '--'; prefix every relative path too.
        operands = [path if Path(path).is_absolute() else f"./{path}" for path in files]
        result = subprocess.run(  # nosec B603 - fixed flags and protected path operands.
            [*command, *PYLINT_FLAGS, "--", *operands], check=False, timeout=120
        )
        return result.returncode
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"lint-mutable-defaults: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
