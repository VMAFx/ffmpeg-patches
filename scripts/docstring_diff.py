#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Require docstrings on new or changed public Python functions.

    docstring_diff.py --cached
        Compare the index with HEAD and read new text from the index.
    docstring_diff.py --base REF
        Compare the merge base of REF and HEAD with the working tree.

Only module functions and methods directly inside module classes are checked.
Exit status: 0 when clean, 1 for missing docstrings, 2 for usage, Git, file,
or Python syntax errors.
"""

from __future__ import annotations

import argparse
import ast
import os
import re
import subprocess  # nosec B404 - fixed Git argv, no shell, bounded timeout.
import sys
from pathlib import Path

HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)


def git(*args: str) -> bytes:
    """Return output of a bounded Git command or name its failure."""
    try:
        result = subprocess.run(  # nosec B603 B607 - fixed Git executable and explicit argv.
            ["git", *args], capture_output=True, check=False, timeout=30
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError(f"git {' '.join(args)}: {error}") from error
    if result.returncode:
        cause = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"git {' '.join(args)} failed ({result.returncode}): {cause}")
    return result.stdout


def diff_options(cached: bool, ref: str | None) -> list[str]:
    """Build one diff selector for both path discovery and patch parsing."""
    if cached:
        return ["--cached", "HEAD"]
    if ref is None:
        raise ValueError("--base requires a ref")
    base = git("merge-base", ref, "HEAD").decode("ascii").strip()
    if not base:
        raise RuntimeError(f"git merge-base {ref} HEAD returned no revision")
    return [base]


def changed_files(options: list[str]) -> list[tuple[str, set[int], set[int]]]:
    """Pair each new Python path with added lines and deletion points."""
    common = ["diff", "--no-ext-diff", "--no-color", "--find-renames", "--diff-filter=AMR"]
    paths = [
        os.fsdecode(path)
        for path in git(*common, "--name-only", "-z", *options, "--", "*.py").split(b"\0")
        if path
    ]
    patch = git(*common, "-U0", *options, "--", "*.py").decode("utf-8")
    sections: list[tuple[set[int], set[int]]] = []
    added: set[int] | None = None
    deleted: set[int] | None = None
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            added = set()
            deleted = set()
            sections.append((added, deleted))
            continue
        match = HUNK.match(line)
        if match and added is not None and deleted is not None:
            start = int(match.group(1))
            count = int(match.group(2) or "1")
            if count == 0:
                deleted.add(start)
            else:
                added.update(range(start, start + count))
    if len(paths) != len(sections):
        raise RuntimeError(
            f"git diff path/patch mismatch: {len(paths)} paths, {len(sections)} patches"
        )
    return [(path, added, deleted) for path, (added, deleted) in zip(paths, sections, strict=True)]


def public_functions(tree: ast.Module) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """Select public module functions and direct public-class methods."""
    found: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
    for node in tree.body:
        if isinstance(node, FUNCTIONS) and not node.name.startswith("_"):
            found.append(node)
        elif isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
            for member in node.body:
                if isinstance(member, FUNCTIONS) and not member.name.startswith("_"):
                    found.append(member)
    return found


def findings(path: str, added: set[int], deleted: set[int], cached: bool) -> list[str]:
    """Report undocumented selected functions in one new file version."""
    source = (
        git("show", f":{path}").decode("utf-8")
        if cached
        else Path(path).read_text(encoding="utf-8")
    )
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as error:
        raise ValueError(f"{path}:{error.lineno}: syntax error: {error.msg}") from error
    result = []
    for node in public_functions(tree):
        start = min([node.lineno, *(item.lineno for item in node.decorator_list)])
        selected = not added.isdisjoint(range(start, node.end_lineno + 1)) or any(
            start <= point and point + 1 <= node.end_lineno for point in deleted
        )
        if selected and ast.get_docstring(node) is None:
            result.append(
                f"{path}:{node.lineno}: {node.name} has no docstring "
                "(new or changed public function)"
            )
    return result


def main(argv: list[str] | None = None) -> int:
    """Check the requested diff and print all findings or a named error."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--cached", action="store_true", help="compare the index with HEAD")
    mode.add_argument("--base", metavar="REF", help="compare merge-base(REF, HEAD) with worktree")
    args = parser.parse_args(argv)
    try:
        options = diff_options(args.cached, args.base)
        reports = [
            report
            for path, added, deleted in changed_files(options)
            for report in findings(path, added, deleted, args.cached)
        ]
    except (RuntimeError, OSError, UnicodeError, ValueError) as error:
        print(f"docstring_diff: {error}", file=sys.stderr)
        return 2
    for report in reports:
        print(report, file=sys.stderr)
    return 1 if reports else 0


if __name__ == "__main__":
    sys.exit(main())
