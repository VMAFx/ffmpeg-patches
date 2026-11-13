#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""List member state advances after earlier error returns in touched C bodies."""

from __future__ import annotations

import bisect
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.variant_sweep import (  # noqa: E402
    IDENTIFIER,
    MAX_REPORT_BYTES,
    REPORT_TABLES,
    added_lines,
    check_deadline,
    check_limit,
    git,
    mask_literals,
    parse_source,
    read_sources,
)

TABLE = REPORT_TABLES["state-advance"]
UPDATE_OPERATORS = re.compile(r"\+\+|--|\+=|-=")


def member_end(values: list[str], start: int, end: int) -> int | None:
    """Find a complete identifier/member chain, excluding partial suffix matches."""
    if not re.fullmatch(IDENTIFIER, values[start]):
        return None
    if start and values[start - 1] in {".", "->"}:
        return None
    tail = start + 1
    for index in range(tail, end - 1, 2):
        if values[index] not in {".", "->"} or not re.fullmatch(IDENTIFIER, values[index + 1]):
            break
        tail = index + 2
    return tail if tail > start + 1 else None


def operand_end(values: list[str], start: int, end: int) -> int:
    """Stop a compound update at its expression boundary, preserving nested calls."""
    depth = 0
    for index in range(start, end):
        value = values[index]
        if depth == 0 and value in {";", ",", ")", "]", "}"}:
            return index
        depth += value in {"(", "[", "{"}
        depth -= value in {")", "]", "}"}
    return end


def joined_operator(tokens: list[re.Match], operators: set[int], start: int) -> bool:
    """Require adjacent operator characters rather than separate unary signs."""
    return start >= 0 and tokens[start].start() in operators


def update_span(
    tokens: list[re.Match], values: list[str], operators: set[int], start: int, end: int
) -> tuple[int, int] | None:
    """Recognize member pre/post updates or compound additions and subtractions."""
    tail = member_end(values, start, end)
    if tail is None:
        return None
    before, after = values[max(0, start - 2) : start], values[tail : tail + 2]
    if before in (["+", "+"], ["-", "-"]) and joined_operator(tokens, operators, start - 2):
        return start - 2, tail
    if after in (["+", "+"], ["-", "-"]) and joined_operator(tokens, operators, tail):
        return start, tail + 2
    if after in (["+", "="], ["-", "="]) and joined_operator(tokens, operators, tail):
        return start, operand_end(values, tail + 2, end)
    return None


def body_hits(
    source: str, tokens: list[re.Match], opening: int, closing: int
) -> list[tuple[int, str, int]]:
    """List updates only after the first completed nonempty, nonzero return."""
    values = [token[0] for token in tokens]
    operators = {match.start() for match in UPDATE_OPERATORS.finditer(mask_literals(source))}
    newlines = [match.start() for match in re.finditer("\n", source)]
    first_error = None
    returning = None
    hits = []
    for index in range(opening + 1, closing):
        if values[index] == "return":
            returning = index
        elif values[index] == ";" and returning is not None:
            expression = values[returning + 1 : index]
            if first_error is None and expression not in ([], ["0"]):
                first_error = bisect.bisect_left(newlines, tokens[returning].start()) + 1
            returning = None
        if first_error is None:
            continue
        span = update_span(tokens, values, operators, index, closing)
        if span is not None:
            start, end = span
            line = bisect.bisect_left(newlines, tokens[start].start()) + 1
            expression = source[tokens[start].start() : tokens[end - 1].end()]
            hits.append((line, " ".join(expression.split()).replace("|", r"\|"), first_error))
    return hits


def find_hits(
    source: str, changed: dict[int, str], deleted: set[int] | None = None
) -> list[tuple[int, str, int]]:
    """Inspect whole touched bodies using shared masked C tokenization and parsing."""
    tokens, _, bodies = parse_source(source)
    newlines = [match.start() for match in re.finditer("\n", source)]
    hits = []
    for _, opening, closing in bodies:
        first = bisect.bisect_left(newlines, tokens[opening].start()) + 1
        last = bisect.bisect_left(newlines, tokens[closing].start()) + 1
        touched = any(first <= line <= last for line in changed)
        touched |= any(first <= anchor < last for anchor in deleted or ())
        if touched:
            hits.extend(body_hits(source, tokens, opening, closing))
    return hits


def deletion_anchors(diff: str) -> dict[str, set[int]]:
    """Locate deletion-only head gaps without selecting an adjacent function body."""
    anchors = {}
    for patch in diff.split("diff --git "):
        numbers = {int(match[1]) for match in re.finditer(r"(?m)^@@ -[^ ]+ \+(\d+),0 @@", patch)}
        for path in added_lines(patch):
            anchors[path] = numbers
    return anchors


def sweep(directory: str, base: str, head: str, deadline: float) -> str:
    """Render bounded state-advance rows for C bodies touched by the revision diff."""
    diff = git(
        directory,
        "diff",
        "--no-ext-diff",
        "--no-textconv",
        "--no-color",
        "-U0",
        base,
        head,
        "--",
        "*.c",
        "*.h",
        deadline=deadline,
    )
    changed = added_lines(diff)
    deleted = deletion_anchors(diff)
    sources: dict[str, str] = {}
    read_sources(directory, head, set(changed), sources, deadline)
    lines = ["class: state-advance", f"head: {head}", "", TABLE, "| --- | --- | --- | --- |"]
    count = 0
    size = len(("\n".join(lines) + "\n").encode())
    for path, source in sorted(sources.items()):
        check_deadline(deadline)
        for line, expression, earlier in find_hits(source, changed[path], deleted.get(path)):
            row = f"| {path}:{line} | {expression} | {earlier} | TODO |"
            size += len((row + "\n").encode())
            check_limit("report size", size, MAX_REPORT_BYTES)
            lines.append(row)
            count += 1
    if not count:
        lines.append("no hits")
    check_deadline(deadline)
    report = "\n".join(lines) + "\n"
    check_limit("report size", len(report.encode()), MAX_REPORT_BYTES)
    return report
