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
    """Use exactly the pinned Pylint on PATH, or fetch that version with uvx."""
    pylint = shutil.which("pylint")
    if pylint:
        try:
            version = subprocess.run(  # nosec B603 - resolved tool, fixed arguments.
                [pylint, "--version"], capture_output=True, text=True, check=True, timeout=10
            )
            if version.stdout.splitlines()[:1] == [f"pylint {PYLINT_VERSION}"]:
                return [pylint]
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
                f"lint-mutable-defaults: neither pylint=={PYLINT_VERSION} nor uvx found on PATH",
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
