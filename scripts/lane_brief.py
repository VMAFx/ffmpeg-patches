#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Render or check a lane brief against repository standards.

Usage: lane_brief.py render --branch B --task T --finding F --fix X --tests S --paths P --modules M
       lane_brief.py check BRIEF [--config FILE]
Exit status: 0 when valid, 1 for invalid briefs, 2 for usage or input errors.
"""

from __future__ import annotations

import argparse
import os
import re
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "scripts" / "lane_brief_template.md"
CONFIG = ROOT / ".config" / "lane-brief.yaml"
NAMES = ("task", "branch", "n", "r", "finding", "fix", "tests", "paths", "modules")
PLACEHOLDERS = re.compile(r"<(task|branch|n|r|finding|fix|tests|paths|modules)>")


def bounded_lines(path: Path) -> list[str]:
    """Read bounded lines from a regular file of at most 1 MiB."""
    info = os.stat(path)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"{path}: not a regular file")
    if info.st_size > 1024 * 1024:
        raise ValueError(f"{path}: size exceeds 1 MiB")
    lines = []
    with path.open("rb") as source:
        for _ in range(10000):
            line = source.readline(65537)
            if not line:
                return lines
            if len(line) > 65536:
                raise ValueError(f"{path}: line exceeds 65536 bytes")
            lines.append(line.decode("utf-8"))
    raise ValueError(f"{path}: exceeds 10000 lines")


def required_item(path: Path, text: str) -> str:
    """Decode a nonempty bare or double-quoted required item."""
    value = text[2:].strip()
    if value.startswith('"') and value.endswith('"'):
        value = value[1:-1]
    if not value:
        raise ValueError(f"{path}: empty required item")
    if len(value) > 200:
        raise ValueError(f"{path}: required item exceeds 200 characters")
    return value


def read_required(path: Path) -> list[str]:
    """Read the bounded required list from a lane brief config file."""
    required = False
    items: list[str] = []
    for line in bounded_lines(path):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped == "---":
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent == 0:
            if required:
                break
            required = stripped == "required:"
        elif required and indent == 2 and stripped.startswith("- "):
            items.append(required_item(path, stripped))
            if len(items) > 32:
                raise ValueError(f"{path}: required list exceeds 32 items")
    if not items:
        raise ValueError(f"{path}: missing or empty required list")
    return items


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
    verify.add_argument("--config", type=Path, default=CONFIG)
    return parser


def render_brief(args: argparse.Namespace) -> int:
    """Render and validate before writing any output."""
    values = {
        name: getattr(args, name)
        for name in ("task", "branch", "finding", "fix", "tests", "paths", "modules")
    }
    values.update(n=args.issue, r=args.round)
    content = PLACEHOLDERS.sub(
        lambda match: values[match.group(1)], TEMPLATE.read_text(encoding="utf-8")
    )
    result = check(content, str(args.o) if args.o else "stdout", read_required(CONFIG))
    if result:
        return result
    if args.o:
        args.o.write_text(content, encoding="utf-8")
    else:
        sys.stdout.write(content)
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run the selected command and report input errors."""
    args = parser_for_cli().parse_args(argv)
    try:
        if args.command == "render":
            return render_brief(args)
        content = "".join(bounded_lines(args.brief))
        return check(content, str(args.brief), read_required(args.config))
    except (OSError, UnicodeError, ValueError) as error:
        print(f"lane_brief: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
