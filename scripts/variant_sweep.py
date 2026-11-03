#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""List unchecked-return variants around changed C functions and checked calls."""

from __future__ import annotations

import argparse
import bisect
import json
import re
import subprocess
import sys
from pathlib import Path

IDENTIFIER = r"[A-Za-z_]\w*"
LEXEMES = re.compile(rf"{IDENTIFIER}|!=|==|<=|>=|->|[^\s]", re.ASCII)
LITERALS = re.compile(r'//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'', re.S)
MACRO_STYLE = re.compile(r"[A-Z][A-Z0-9_]*", re.ASCII)
CONTROL_HEADERS = {"if", "for", "while", "switch"}
TABLE = "| file:line | callee | kind | same file as fix | disposition |"


def git(directory: str, *args: str, missing_ok: bool = False) -> str:
    """Read Git output with a deadline; grep may report no matches."""

    result = subprocess.run(  # nosec B603 B607 - fixed git argv, no shell.
        ["git", "-C", directory, *args], capture_output=True, text=True, timeout=120, check=False
    )
    if result.returncode and not (missing_ok and result.returncode == 1):
        raise RuntimeError(f"git {args[0]} failed: {result.stderr.strip()}")
    return result.stdout


def mask_literals(source: str) -> str:
    """Blank comments and literals while preserving offsets and line numbers."""

    return LITERALS.sub(lambda match: re.sub(r"[^\n]", " ", match[0]), source)


def added_lines(diff: str) -> dict[str, dict[int, str]]:
    """Map head paths and added line numbers from a zero-context diff."""

    files: dict[str, dict[int, str]] = {}
    path = None
    line_number = 0
    for line in diff.splitlines():
        if line.startswith("+++ "):
            name = line[4:]
            name = json.loads(name) if name.startswith('"') else name
            path = None if name == "/dev/null" else name.removeprefix("b/")
            if path is not None:
                files.setdefault(path, {})
        elif line.startswith("@@ "):
            line_number = int(re.search(r"\+(\d+)", line)[1])
        elif line.startswith("+") and path is not None:
            files[path][line_number] = line[1:]
            line_number += 1
        elif line.startswith(" "):
            line_number += 1
    return files


def delimiter_pairs(tokens: list[re.Match]) -> dict[int, int]:
    """Pair balanced parentheses and braces in one bounded token pass."""

    stack = []
    pairs = {}
    for index, token in enumerate(tokens):
        value = token[0]
        if value in {"(", "{"}:
            stack.append(index)
        elif stack and value in {")", "}"}:
            opening = stack.pop()
            if (tokens[opening][0], value) in {("(", ")"), ("{", "}")}:
                pairs[opening] = index
    return pairs


def function_bodies(tokens: list[re.Match], pairs: dict[int, int]) -> list[tuple[str, int, int]]:
    """Find definition bodies from depth-zero signatures, excluding prototypes."""

    bodies = []
    depth = 0
    start = 0
    for index, token in enumerate(tokens):
        value = token[0]
        if value == "{" and depth == 0 and index in pairs:
            signature = tokens[start:index]
            names = [
                signature[i - 1][0]
                for i in range(1, len(signature))
                if signature[i][0] == "(" and re.fullmatch(IDENTIFIER, signature[i - 1][0])
            ]
            if names and not any(item[0] == "=" for item in signature):
                bodies.append((names[0], index, pairs[index]))
        depth += (value == "{") - (value == "}")
        if depth == 0 and value in {";", "}"}:
            start = index + 1
    return bodies


def parse_source(source: str) -> tuple[list[re.Match], dict[int, int], list[tuple[str, int, int]]]:
    """Tokenize masked C text and locate balanced definition bodies."""

    tokens = list(LEXEMES.finditer(mask_literals(source)))
    pairs = delimiter_pairs(tokens)
    return tokens, pairs, function_bodies(tokens, pairs)


def error_tested(
    values: list[str], variable: list[str], index: int, end: int | None = None
) -> bool:
    """Recognize zero comparisons and boolean if tests of one complete lvalue."""

    if values[index : index + len(variable)] != variable:
        return False
    if index and values[index - 1] in {".", "->"}:
        return False
    tail = index + len(variable) if end is None else end
    comparison = tail
    for comparison in range(tail, len(values)):
        if values[comparison] != ")":
            break
    if values[comparison : comparison + 2] in (["<", "0"], ["!=", "0"]):
        return True
    prefix = index - 1
    for prefix in range(index - 1, -1, -1):
        if values[prefix] not in {"(", "!"}:
            break
    return prefix >= 0 and values[prefix] == "if" and values[tail : tail + 1] == [")"]


def checked_assignment(values: list[str], index: int, closing: int, window: list[str]) -> bool:
    """Require a bare assigned call and an error test of that same lvalue."""

    if index < 2 or values[index - 1] != "=" or not re.fullmatch(IDENTIFIER, values[index - 2]):
        return False
    start = index - 2
    for member in range(start - 1, 0, -2):
        if values[member] not in {".", "->"} or not re.fullmatch(IDENTIFIER, values[member - 1]):
            break
        start = member - 1
    variable = values[start : index - 1]
    following = values[closing + 1 : closing + 2]
    if following == [")"]:
        return error_tested(values, variable, start, closing + 1)
    if following != [";"]:
        return False
    return any(error_tested(window, variable, offset) for offset in range(len(window)))


def checked_callees(
    tokens: list[re.Match], pairs: dict[int, int], newlines: list[int], changed: dict[int, str]
) -> set[str]:
    """Seed callees only from added error checks, looking ahead at most three lines."""

    values = [token[0] for token in tokens]
    lines = [bisect.bisect_left(newlines, token.start()) + 1 for token in tokens]
    targets = set()
    for index, token in enumerate(tokens[:-1]):
        line = lines[index]
        if line not in changed or values[index + 1] != "(" or index + 1 not in pairs:
            continue
        closing = pairs[index + 1]
        if values[max(0, index - 2) : index] == ["if", "("] and values[
            closing + 1 : closing + 4
        ] in (["<", "0", ")"], ["!=", "0", ")"]):
            targets.add(token[0])
            continue
        first = bisect.bisect_left(lines, line)
        last = bisect.bisect_left(lines, line + 4)
        window = [values[offset] for offset in range(first, last) if lines[offset] in changed]
        if checked_assignment(values, index, closing, window):
            targets.add(token[0])
    return targets


def touched_targets(source: str, changed: dict[int, str]) -> set[str]:
    """Collect checked callees on added lines and enclosing touched definitions."""

    tokens, pairs, bodies = parse_source(source)
    newlines = [match.start() for match in re.finditer("\n", source)]
    targets = checked_callees(tokens, pairs, newlines, changed)
    for name, opening, closing in bodies:
        first = bisect.bisect_left(newlines, tokens[opening].start()) + 1
        last = bisect.bisect_left(newlines, tokens[closing].start()) + 1
        if any(first <= line <= last for line in changed):
            targets.add(name)
    return {name for name in targets if not MACRO_STYLE.fullmatch(name)}


def call_kind(values: list[str], index: int, closing: int, headers: set[int]) -> str | None:
    """Classify whole discarded statements and immediate normalized error returns."""

    following = values[closing + 1 : closing + 10]
    previous = values[max(0, index - 3) : index]
    if following[:1] == [";"]:
        if previous == ["(", "void", ")"]:
            return None
        if index == 0 or values[index - 1] in {";", "{", "}", "else"} or index - 1 in headers:
            return "discarded"
    if previous[-2:] != ["if", "("] or following[:3] not in (["!=", "0", ")"], ["<", "0", ")"]):
        return None
    branch = following[3:]
    if branch[:1] == ["{"]:
        branch = branch[1:]
    if branch[:4] == ["return", "-", "1", ";"]:
        return "normalized"
    if len(branch) > 2 and branch[0] == "return" and branch[1].startswith("AVERROR_"):
        return "normalized"
    return None


def find_hits(source: str, targets: set[str]) -> list[tuple[int, str, str]]:
    """Report target calls inside function bodies, never declarations or prototypes."""

    tokens, pairs, bodies = parse_source(source)
    values = [token[0] for token in tokens]
    headers = {
        closing
        for opening, closing in pairs.items()
        if opening and values[opening] == "(" and values[opening - 1] in CONTROL_HEADERS
    }
    newlines = [match.start() for match in re.finditer("\n", source)]
    hits = []
    for index, token in enumerate(tokens[:-1]):
        if token[0] not in targets or index + 1 not in pairs or values[index + 1] != "(":
            continue
        if not any(opening < index < closing for _, opening, closing in bodies):
            continue
        kind = call_kind(values, index, pairs[index + 1], headers)
        if kind:
            line = bisect.bisect_left(newlines, token.start()) + 1
            hits.append((line, token[0], kind))
    return hits


def sweep(directory: str, base: str, head: str) -> str:
    """Resolve revisions, discover target callers in head and render the review table."""

    base = git(directory, "rev-parse", "--verify", "--end-of-options", f"{base}^{{commit}}").strip()
    head = git(directory, "rev-parse", "--verify", "--end-of-options", f"{head}^{{commit}}").strip()
    diff = git(
        directory, "diff", "--no-ext-diff", "--no-textconv", "-U0", base, head, "--", "*.c", "*.h"
    )
    changed = added_lines(diff)
    sources = {path: git(directory, "show", f"{head}:{path}") for path in changed}
    targets = set()
    for path, source in sources.items():
        targets.update(touched_targets(source, changed[path]))
    if targets:
        patterns = [arg for target in sorted(targets) for arg in ("-e", target)]
        names = git(
            directory, "grep", "-l", "-w", "-z", *patterns, head, "--", "*.c", missing_ok=True
        )
        for name in names.split("\0"):
            if name:
                path = name.removeprefix(f"{head}:")
                if path not in sources:
                    sources[path] = git(directory, "show", f"{head}:{path}")
    rows = []
    for path, source in sorted(sources.items()):
        same = "yes" if path in changed else "no"
        rows.extend(
            f"| {path}:{line} | {callee} | {kind} | {same} | TODO |"
            for line, callee, kind in find_hits(source, targets)
        )
    lines = [
        "class: unchecked-return",
        f"base: {base}",
        f"head: {head}",
        f"targets: {', '.join(sorted(targets))}",
        "",
        TABLE,
        "| --- | --- | --- | --- | --- |",
        *(rows or ["no hits"]),
    ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    """Parse the class allow-list and write a report, failing on Git or output errors."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-C", dest="directory", default=".")
    parser.add_argument("--class", dest="bug_class", choices=("unchecked-return",), required=True)
    parser.add_argument("base")
    parser.add_argument("head")
    parser.add_argument("-o", dest="output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        args.output.write_text(sweep(args.directory, args.base, args.head), encoding="utf-8")
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as error:
        print(f"variant_sweep: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
