#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Check that the active Python interpreter satisfies development dependencies."""

from __future__ import annotations

import argparse
import importlib.metadata
import re
import sys
from collections.abc import Callable, Iterable
from pathlib import Path

try:
    import series
except ModuleNotFoundError:
    from scripts import series

ROOT = Path(__file__).resolve().parents[1]


def normalize_package_name(name: str) -> str:
    """Normalize package names according to PEP 503."""
    return re.sub(r"[-_.]+", "-", name).lower()


def development_pins(content: str) -> set[tuple[str, str]]:
    """Parse exact development pins, refusing every other requirement shape."""
    pins: set[tuple[str, str]] = set()
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9._-]*)==([0-9][A-Za-z0-9.!+_-]*)", line)
        if match is None:
            raise ValueError(f"expected name==version: {line!r}")
        name, version = match.groups()
        pins.add((normalize_package_name(name), version))
    return pins


def pin_findings(
    pins: Iterable[tuple[str, str]],
    version_of: Callable[[str], str] = importlib.metadata.version,
) -> list[str]:
    """Return findings for development pins that are missing or mismatched."""
    findings: list[str] = []
    for name, expected in sorted(pins):
        try:
            installed = version_of(name)
        except importlib.metadata.PackageNotFoundError:
            findings.append(f"{name}=={expected}: not installed")
            continue
        if installed != expected:
            findings.append(f"{name}=={expected}: installed {installed}")
    return findings


def parser_findings() -> list[str]:
    """Return findings if the tree-sitter C parser cannot be loaded."""
    try:
        series._get_c_parser()
    except series.SeriesError as error:
        return [str(error)]
    return []


def main(argv: list[str] | None = None) -> int:
    """Check development dependencies and report findings to stderr or success to stdout."""
    parser = argparse.ArgumentParser(
        description="Verify that the active Python interpreter satisfies development dependencies."
    )
    parser.add_argument(
        "--requirements",
        type=Path,
        default=ROOT / "requirements-dev.in",
        help="Path to requirements-dev.in file.",
    )
    args = parser.parse_args(argv)

    findings: list[str] = []
    pin_count = 0
    try:
        content = args.requirements.read_text(encoding="utf-8")
        pins = development_pins(content)
        pin_count = len(pins)
        findings.extend(pin_findings(pins))
    except OSError as error:
        findings.append(f"cannot read {args.requirements}: {error.strerror or error}")
    except ValueError as error:
        findings.append(str(error))

    findings.extend(parser_findings())

    if findings:
        print(
            f"dev-deps: {sys.executable} lacks the pinned development packages:",
            file=sys.stderr,
        )
        for finding in findings:
            print(f"  - {finding}", file=sys.stderr)
        print(
            "fix: create the checkout virtual environment and install the pins as "
            "CONTRIBUTING.md shows (python3 -m venv .venv; .venv/bin/python3 -m pip install "
            "--require-hashes -r requirements-dev.txt); make verify-all then prefers "
            ".venv/bin/python3. A run in another worktree, such as the pre-push gate, "
            "needs such an interpreter first on PATH.",
            file=sys.stderr,
        )
        return 1

    print(f"dev-deps: {pin_count} pins and the tree-sitter C parser load under {sys.executable}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
