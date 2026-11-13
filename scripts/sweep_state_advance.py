#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""List member state advances after earlier error returns in touched C bodies."""

from __future__ import annotations

import bisect
import re
import sys
from pathlib import Path
from typing import NamedTuple

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


class BodyScan(NamedTuple):
    """Hold shared token values, operator offsets, and line mapping for body scans."""

    values: list[str]
    operators: set[int]
    newlines: list[int]
    line_offset: int = 0


def chain_end(values: list[str], start: int, end: int) -> int:
    """Consume one member link or balanced subscript, stopping on incomplete input."""
    if (
        values[start] in {".", "->"}
        and start + 1 < end
        and re.fullmatch(IDENTIFIER, values[start + 1])
    ):
        return start + 2
    if values[start] != "[":
        return start
    depth = 0
    for index in range(start, end):
        depth += (values[index] == "[") - (values[index] == "]")
        if depth == 0:
            return index + 1
    return start


def member_end(values: list[str], start: int, end: int) -> int | None:
    """Find a subscripted chain with a member link, excluding partial suffix matches."""
    if not re.fullmatch(IDENTIFIER, values[start]):
        return None
    if start and values[start - 1] in {".", "->"}:
        return None
    tail = start + 1
    linked = False
    for index in range(tail, end):
        if index < tail:
            continue
        following = chain_end(values, index, end)
        if following == index:
            break
        linked |= values[index] in {".", "->"}
        tail = following
    return tail if linked else None


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


def scan_body(source: str, tokens: list[re.Match], opening: int, closing: int) -> BodyScan:
    """Build body-local scan data for callers without a shared file scan."""
    start, end = tokens[opening].start(), tokens[closing].end()
    body = source[start:end]
    return BodyScan(
        [""] * opening + [token[0] for token in tokens[opening : closing + 1]],
        {start + match.start() for match in UPDATE_OPERATORS.finditer(mask_literals(body))},
        [start + match.start() for match in re.finditer("\n", body)],
        source.count("\n", 0, start),
    )


def first_error_return(values: list[str], start: int, end: int) -> tuple[int, int] | None:
    """Locate the first completed nonempty, nonzero return within a body."""
    returning = None
    for index in range(start, end):
        if values[index] == "return":
            returning = index
        elif values[index] == ";" and returning is not None:
            if values[returning + 1 : index] not in ([], ["0"]):
                return returning, index
            returning = None
    return None


def body_hits(
    source: str,
    tokens: list[re.Match],
    opening: int,
    closing: int,
    scan: BodyScan | None = None,
) -> list[tuple[int, str, int]]:
    """Use shared or body-local scan data to list updates after the first error return."""
    values, operators, newlines, line_offset = scan or scan_body(source, tokens, opening, closing)
    error = first_error_return(values, opening + 1, closing)
    if error is None:
        return []
    returning, completed = error
    first_error = bisect.bisect_left(newlines, tokens[returning].start()) + 1 + line_offset
    hits = []
    for index in range(completed + 1, closing):
        span = update_span(tokens, values, operators, index, closing)
        if span is not None:
            start, end = span
            line = bisect.bisect_left(newlines, tokens[start].start()) + 1 + line_offset
            expression = source[tokens[start].start() : tokens[end - 1].end()]
            hits.append((line, " ".join(expression.split()).replace("|", r"\|"), first_error))
    return hits


def find_hits(
    source: str,
    changed: dict[int, str],
    deleted: set[int] | None = None,
    deadline: float | None = None,
) -> list[tuple[int, str, int]]:
    """Inspect touched bodies within an optional deadline using one shared file scan."""
    tokens, _, bodies = parse_source(source)
    newlines = [match.start() for match in re.finditer("\n", source)]
    scan = BodyScan(
        [token[0] for token in tokens],
        {match.start() for match in UPDATE_OPERATORS.finditer(mask_literals(source))},
        newlines,
    )
    hits = []
    for _, opening, closing in bodies:
        first = bisect.bisect_left(newlines, tokens[opening].start()) + 1
        last = bisect.bisect_left(newlines, tokens[closing].start()) + 1
        touched = any(first <= line <= last for line in changed)
        touched |= any(first <= anchor < last for anchor in deleted or ())
        if touched:
            if deadline is not None:
                check_deadline(deadline)
            hits.extend(body_hits(source, tokens, opening, closing, scan))
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
        for line, expression, earlier in find_hits(
            source, changed[path], deleted.get(path), deadline
        ):
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
