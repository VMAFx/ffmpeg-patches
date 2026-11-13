#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""List unchecked-return variants around changed C functions and checked calls."""

from __future__ import annotations

import argparse
import bisect
import importlib.util
import json
import os
import re
import subprocess  # nosec B404 - fixed git argv, no shell, bounded timeout.
import sys
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import BinaryIO

IDENTIFIER = r"[A-Za-z_]\w*"
LEXEMES = re.compile(rf"{IDENTIFIER}|!=|==|<=|>=|->|[^\s]", re.ASCII)
LITERALS = re.compile(
    r'(?m:^[ \t]*\#(?:\\\r?\n|[^\n])*)|//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'',
    re.S,
)
MACRO_STYLE = re.compile(r"[A-Z][A-Z0-9_]*", re.ASCII)
CONTROL_HEADERS = {"if", "for", "while", "switch"}
TABLE = "| file:line | callee | kind | same file as fix | disposition |"
DISPOSITION = re.compile(r"fold|(?:follow-up|no-variant): \S(?:.*\S)?")
REPORT_TABLES = {
    "unchecked-return": TABLE,
    "state-advance": "| file:line | expression | first earlier error return line | disposition |",
    "doc-claims": "| file:line | claim text | library | oldest | newest | disposition |",
}
VERSION = r"(?:[A-Z][A-Z0-9_]* (?:>=|<) \d+|(?!TODO\b)[^\s,;|]+)"
DOC_DISPOSITION = re.compile(
    rf"no-claim: \S(?:.*\S)?|checked: [\w.-]+ {VERSION}(?:, {VERSION})* {VERSION} "
    r"[^\s;|]+:[1-9]\d*(?:; [^\s;|]+:[1-9]\d*)*"
)
ATTRIBUTES = {"__attribute__", "__attribute", "__declspec", "av_printf_format", "av_alloc_size"}
MAX_REPORT_BYTES = 4 * 1024 * 1024
MAX_SOURCE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_SOURCE_BYTES = 128 * 1024 * 1024
MAX_SOURCE_COUNT = 4096
MAX_TARGET_COUNT = 512
MAX_SWEEP_SECONDS = 120
MAX_GIT_BYTES = 32 * 1024 * 1024
GIT_LOCATION_NAMES = frozenset(
    {
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_CEILING_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_DIR",
        "GIT_DISCOVERY_ACROSS_FILESYSTEM",
        "GIT_GRAFT_FILE",
        "GIT_IMPLICIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_NAMESPACE",
        "GIT_NO_REPLACE_OBJECTS",
        "GIT_OBJECT_DIRECTORY",
        "GIT_PREFIX",
        "GIT_REPLACE_REF_BASE",
        "GIT_SHALLOW_FILE",
        "GIT_WORK_TREE",
    }
)


def check_limit(name: str, size: int, maximum: int) -> None:
    """Reject oversized input before tokenization or report validation."""

    if size > maximum:
        raise RuntimeError(f"{name} limit exceeded: {size} > {maximum}")


def check_deadline(deadline: float) -> float:
    """Return the remaining shared budget, failing before more work starts."""

    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RuntimeError("sweep deadline exceeded")
    return remaining


def git_environment(environment: Mapping[str, str] | None = None) -> dict[str, str]:
    """Drop repository-location overrides while preserving credential configuration."""

    source = os.environ if environment is None else environment
    return {name: value for name, value in source.items() if name not in GIT_LOCATION_NAMES}


def bounded_output(stream: BinaryIO, maximum: int, name: str) -> str:
    """Read bounded raw Git bytes with an explicit source-text decoding policy."""

    size = stream.seek(0, os.SEEK_END)
    check_limit(name, size, maximum)
    stream.seek(0)
    return stream.read(maximum + 1).decode("utf-8", errors="replace")


def git(
    directory: str,
    *args: str,
    missing_ok: bool = False,
    deadline: float | None = None,
    maximum: int = MAX_GIT_BYTES,
    limit_name: str = "git output size",
) -> str:
    """Read Git output with a deadline; grep may report no matches."""

    timeout = min(120, check_deadline(deadline)) if deadline is not None else 120
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        try:
            result = subprocess.run(  # nosec B603 B607 - fixed git argv, no shell.
                ["git", "-C", directory, *args],
                stdout=stdout,
                stderr=stderr,
                env=git_environment(),
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            if deadline is not None:
                raise RuntimeError("sweep deadline exceeded during git") from error
            raise
        if deadline is not None:
            check_deadline(deadline)
        if result.returncode and not (missing_ok and result.returncode == 1):
            diagnostic = bounded_output(stderr, 65536, "git diagnostic size").strip()
            raise RuntimeError(f"git {args[0]} failed: {diagnostic}")
        return bounded_output(stdout, maximum, limit_name)


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
        if value in {"(", "{", "["}:
            stack.append(index)
        elif stack and value in {")", "}", "]"}:
            opening = stack.pop()
            if (tokens[opening][0], value) in {("(", ")"), ("{", "}"), ("[", "]")}:
                pairs[opening] = index
    return pairs


def declarator(tokens: list[re.Match], pairs: dict[int, int], start: int, end: int) -> int | None:
    """Find a top-level declarator, skipping attributes and their nested arguments."""

    skip = start
    macro_candidate = None
    for index in range(start, end):
        if index < skip:
            continue
        value = tokens[index][0]
        if value == "=":
            return None
        if value not in {"(", "["} or index not in pairs:
            continue
        skip = pairs[index] + 1
        if value == "(" and index > start and re.fullmatch(IDENTIFIER, tokens[index - 1][0]):
            name = tokens[index - 1][0]
            if name in ATTRIBUTES:
                continue
            if not MACRO_STYLE.fullmatch(name):
                return index - 1
            # A leading attribute macro yields to a later function declarator.
            if macro_candidate is None:
                macro_candidate = index - 1
    return macro_candidate


def function_signatures(
    tokens: list[re.Match], pairs: dict[int, int]
) -> list[tuple[str, bool, int, int]]:
    """Collect depth-zero declarations and definitions with their local linkage."""

    signatures = []
    start = 0
    skip = 0
    for index, token in enumerate(tokens):
        if index < skip:
            continue
        value = token[0]
        if value in {"(", "["} and index in pairs:
            skip = pairs[index] + 1
        elif value in {";", "{"}:
            name = declarator(tokens, pairs, start, index)
            closing = pairs.get(index, index)
            if name is not None:
                private = any(item[0] == "static" for item in tokens[start:name])
                signatures.append((tokens[name][0], private, index, closing))
            skip = closing + 1
            start = skip
    return signatures


def function_bodies(tokens: list[re.Match], pairs: dict[int, int]) -> list[tuple[str, int, int]]:
    """Find definition bodies using the same declarators as linkage discovery."""

    return [
        (name, opening, closing)
        for name, _, opening, closing in function_signatures(tokens, pairs)
        if tokens[opening][0] == "{" and opening != closing
    ]


def parse_source(source: str) -> tuple[list[re.Match], dict[int, int], list[tuple[str, int, int]]]:
    """Tokenize masked C text and locate balanced definition bodies."""

    tokens = list(LEXEMES.finditer(mask_literals(source)))
    pairs = delimiter_pairs(tokens)
    return tokens, pairs, function_bodies(tokens, pairs)


def parenthesis_end(values: list[str], opening: int) -> int | None:
    """Locate a matching parenthesis without accepting a partial expression."""

    depth = 0
    for index in range(opening, len(values)):
        depth += (values[index] == "(") - (values[index] == ")")
        if depth == 0:
            return index
    return None


def strip_parentheses(values: list[str]) -> list[str]:
    """Remove only balanced parentheses around an entire expression."""

    for _ in range(len(values)):
        if not values or values[0] != "(" or parenthesis_end(values, 0) != len(values) - 1:
            break
        values = values[1:-1]
    return values


def error_condition(values: list[str], variable: list[str]) -> bool:
    """Allow complete truthiness tests or the supported zero comparisons."""

    values = strip_parentheses(values)
    if values[:1] == ["!"]:
        return strip_parentheses(values[1:]) == variable
    if values[-2:] in (["<", "0"], ["!=", "0"]):
        return strip_parentheses(values[:-2]) == variable
    return values == variable


def error_branch(
    values: list[str], variable: list[str], index: int, end: int | None = None
) -> int | None:
    """Return the branch start only when the whole enclosing if tests this value."""

    if values[index : index + len(variable)] != variable:
        return None
    if index and values[index - 1] in {".", "->"}:
        return None
    tail = index + len(variable) if end is None else end
    for header in range(index - 1, -1, -1):
        if values[header] in {";", "{", "}"}:
            break
        if values[header : header + 2] != ["if", "("]:
            continue
        closing = parenthesis_end(values, header + 1)
        if closing is not None and closing >= tail:
            condition = values[header + 2 : index] + variable + values[tail:closing]
            if error_condition(condition, variable):
                return closing + 1
        break
    return None


def error_tested(
    values: list[str], variable: list[str], index: int, end: int | None = None
) -> bool:
    """Recognize complete error tests through the shared branch classifier."""

    return error_branch(values, variable, index, end) is not None


def assignment_lvalue(values: list[str], index: int) -> tuple[int, list[str]] | None:
    """Locate one simple or member lvalue immediately assigned a call result."""

    if index < 2 or values[index - 1] != "=" or not re.fullmatch(IDENTIFIER, values[index - 2]):
        return None
    start = index - 2
    for member in range(start - 1, 0, -2):
        if values[member] not in {".", "->"} or not re.fullmatch(IDENTIFIER, values[member - 1]):
            break
        start = member - 1
    return start, values[start : index - 1]


def checked_assignment(values: list[str], index: int, closing: int, window: list[str]) -> bool:
    """Require a bare assigned call and an error test of that same lvalue."""

    assignment = assignment_lvalue(values, index)
    if assignment is None:
        return False
    start, variable = assignment
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
        if (
            line not in changed
            or not re.fullmatch(IDENTIFIER, token[0])
            or values[index + 1] != "("
            or index + 1 not in pairs
        ):
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


def statement_boundaries(values: list[str], headers: set[int]) -> set[int]:
    """Include label colons and do bodies without treating ternary colons as labels."""

    boundaries = set(headers)
    start = 0
    for index, value in enumerate(values):
        if value in {";", "{", "}", "else", "do"} or index in headers:
            boundaries.add(index)
            start = index + 1
        elif value == ":":
            prefix = values[start:index]
            if prefix and (
                prefix[0] in {"case", "default"}
                or (len(prefix) == 1 and re.fullmatch(IDENTIFIER, prefix[0]))
            ):
                boundaries.add(index)
                start = index + 1
    return boundaries


def normalized_return(values: list[str], branch: int | None) -> bool:
    """Require the first branch statement to return a fixed error constructor."""

    if branch is None:
        return False
    if values[branch : branch + 1] == ["{"]:
        branch += 1
    if values[branch : branch + 4] == ["return", "-", "1", ";"]:
        return True
    if values[branch : branch + 1] != ["return"] or branch + 2 >= len(values):
        return False
    if values[branch + 1].startswith("AVERROR_") and values[branch + 2] == ";":
        return True
    if values[branch + 1 : branch + 3] != ["AVERROR", "("]:
        return False
    closing = parenthesis_end(values, branch + 2)
    return closing is not None and values[closing + 1 : closing + 2] == [";"]


def assigned_error_branch(values: list[str], index: int, closing: int) -> int | None:
    """Track a bare assignment into its inline or immediately following error check."""

    assignment = assignment_lvalue(values, index)
    if assignment is None:
        return None
    start, variable = assignment
    if values[closing + 1 : closing + 2] == [")"]:
        return error_branch(values, variable, start, closing + 1)
    if values[closing + 1 : closing + 4] != [";", "if", "("]:
        return None
    end = parenthesis_end(values, closing + 3)
    if end is not None:
        for offset in range(closing + 4, end):
            branch = error_branch(values, variable, offset)
            if branch is not None:
                return branch
    return None


def call_kind(values: list[str], index: int, closing: int, boundaries: set[int]) -> str | None:
    """Classify discarded statements and checked direct or assigned error returns."""

    if values[closing + 1 : closing + 2] == [";"] and (index == 0 or index - 1 in boundaries):
        return "discarded"
    branch = error_branch(values, [values[index]], index, closing + 1)
    if normalized_return(values, branch):
        return "normalized"
    if normalized_return(values, assigned_error_branch(values, index, closing)):
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
    boundaries = statement_boundaries(values, headers)
    newlines = [match.start() for match in re.finditer("\n", source)]
    hits = []
    for index, token in enumerate(tokens[:-1]):
        if token[0] not in targets or index + 1 not in pairs or values[index + 1] != "(":
            continue
        if not any(opening < index < closing for _, opening, closing in bodies):
            continue
        kind = call_kind(values, index, pairs[index + 1], boundaries)
        if kind:
            line = bisect.bisect_left(newlines, token.start()) + 1
            hits.append((line, token[0], kind))
    return hits


def read_sources(
    directory: str,
    head: str,
    paths: set[str],
    sources: dict[str, str],
    deadline: float,
) -> None:
    """Preflight the full count and raw blob sizes before reading any new sources."""

    check_limit("source count", len(paths | sources.keys()), MAX_SOURCE_COUNT)
    sizes = {}
    for path in sorted(paths - sources.keys()):
        size = int(git(directory, "cat-file", "-s", f"{head}:{path}", deadline=deadline))
        check_limit("source size", size, MAX_SOURCE_BYTES)
        sizes[path] = size
    total = sum(len(source.encode("utf-8")) for source in sources.values()) + sum(sizes.values())
    check_limit("total source size", total, MAX_TOTAL_SOURCE_BYTES)
    for path in sizes:
        sources[path] = git(
            directory,
            "show",
            f"{head}:{path}",
            deadline=deadline,
            maximum=MAX_SOURCE_BYTES,
            limit_name="source size",
        )


def private_functions(source: str) -> set[str]:
    """Use the shared declarators to find static definitions and prototypes."""

    tokens, pairs, _ = parse_source(source)
    return {name for name, private, _, _ in function_signatures(tokens, pairs) if private}


def scoped_targets(
    sources: dict[str, str],
    changed: dict[str, dict[int, str]],
    deadline: float,
) -> dict[str, set[str | None]]:
    """Qualify private targets by source path; global names remain externally visible."""

    targets: dict[str, set[str | None]] = {}
    for path, source in sources.items():
        check_deadline(deadline)
        private = private_functions(source)
        for name in touched_targets(source, changed[path]):
            targets.setdefault(name, set()).add(path if name in private else None)
        check_limit("target count", sum(map(len, targets.values())), MAX_TARGET_COUNT)
    check_deadline(deadline)
    return targets


def render_report(
    revs: tuple[str, str],
    sources: dict[str, str],
    changed: dict[str, dict[int, str]],
    targets: dict[str, set[str | None]],
    deadline: float,
) -> str:
    """Render bounded findings, respecting static target and caller linkage."""

    base, head = revs
    lines = [
        "class: unchecked-return",
        f"base: {base}",
        f"head: {head}",
        f"targets: {', '.join(sorted(targets))}",
        "",
        TABLE,
        "| --- | --- | --- | --- | --- |",
    ]
    size = len(("\n".join(lines) + "\n").encode("utf-8"))
    check_limit("report size", size, MAX_REPORT_BYTES)
    found = False
    for path, source in sorted(sources.items()):
        check_deadline(deadline)
        private = private_functions(source)
        eligible = {
            name
            for name, scopes in targets.items()
            if path in scopes or (None in scopes and name not in private)
        }
        same = "yes" if path in changed else "no"
        for line, callee, kind in find_hits(source, eligible):
            row = f"| {path}:{line} | {callee} | {kind} | {same} | TODO |"
            size += len((row + "\n").encode("utf-8"))
            check_limit("report size", size, MAX_REPORT_BYTES)
            lines.append(row)
            found = True
        check_deadline(deadline)
    if not found:
        lines.append("no hits")
    report = "\n".join(lines) + "\n"
    check_limit("report size", len(report.encode("utf-8")), MAX_REPORT_BYTES)
    check_deadline(deadline)
    return report


def sweep(directory: str, base: str, head: str, deadline: float | None = None) -> str:
    """Resolve revisions and sweep within the supplied or a new wall-clock budget."""

    if deadline is None:
        deadline = time.monotonic() + MAX_SWEEP_SECONDS
    check_deadline(deadline)
    base = git(
        directory,
        "rev-parse",
        "--verify",
        "--end-of-options",
        f"{base}^{{commit}}",
        deadline=deadline,
    ).strip()
    head = git(
        directory,
        "rev-parse",
        "--verify",
        "--end-of-options",
        f"{head}^{{commit}}",
        deadline=deadline,
    ).strip()
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
    sources: dict[str, str] = {}
    read_sources(directory, head, set(changed), sources, deadline)
    targets = scoped_targets(sources, changed, deadline)
    if targets:
        patterns = [arg for target in sorted(targets) for arg in ("-e", target)]
        names = git(
            directory,
            "grep",
            "--no-textconv",
            "--no-color",
            "-l",
            "-w",
            "-z",
            *patterns,
            head,
            "--",
            "*.c",
            missing_ok=True,
            deadline=deadline,
        )
        paths = {name.removeprefix(f"{head}:") for name in names.split("\0") if name}
        read_sources(directory, head, paths, sources, deadline)
    return render_report((base, head), sources, changed, targets, deadline)


def validate_report(path: Path, sha: str) -> bool:
    """Validate only bounded metadata, the no-hits marker and reviewed table rows."""

    with path.open("rb") as stream:
        data = stream.read(MAX_REPORT_BYTES + 1)
    check_limit("report size", len(data), MAX_REPORT_BYTES)
    lines = data.decode("utf-8").splitlines()
    heads = [line.removeprefix("head: ") for line in lines if line.startswith("head:")]
    classes = [line.removeprefix("class: ") for line in lines if line.startswith("class:")]
    if heads != [sha] or classes != ["unchecked-return"]:
        return False
    body = False
    for line in lines:
        if not line.strip() or line.startswith(("class: ", "head: ", "base: ", "targets: ")):
            continue
        if line == "no hits":
            body = True
            continue
        if not line.startswith("|") or not line.endswith("|"):
            return False
        cells = [cell.strip() for cell in line.split("|")[1:-1]]
        if cells == ["file:line", "callee", "kind", "same file as fix", "disposition"]:
            continue
        if len(cells) == 5 and all(re.fullmatch(r":?-+:?", cell) for cell in cells):
            continue
        if len(cells) != 5:
            return False
        if not DISPOSITION.fullmatch(cells[-1]):
            return False
        body = True
    return body


def reviewed_row(line: str, bug_class: str) -> bool:
    """Require reviewed dispositions and completed version cells for checked doc rows."""
    if not line.startswith("|") or not line.endswith("|"):
        return False
    cells = [cell.strip() for cell in re.split(r"(?<!\\)\|", line)[1:-1]]
    if len(cells) != REPORT_TABLES[bug_class].count("|") - 1:
        return False
    pattern = DOC_DISPOSITION if bug_class == "doc-claims" else DISPOSITION
    if (
        bug_class == "doc-claims"
        and cells[-1].startswith("checked:")
        and any(not cell or re.search(r"\bTODO\b", cell) for cell in cells[3:5])
    ):
        return False
    return pattern.fullmatch(cells[-1]) is not None


def validate_section(lines: list[str], bug_class: str, sha: str) -> bool:
    """Validate one exact-head section with its own header and exclusive body form."""
    heads = [line.removeprefix("head: ") for line in lines if line.startswith("head:")]
    table = REPORT_TABLES[bug_class]
    if heads != [sha] or lines.count(table) != 1:
        return False
    rows = 0
    empty = 0
    width = table.count("|") - 1
    separator = "| " + " | ".join(["---"] * width) + " |"
    for line in lines:
        if not line.strip() or line in {table, separator}:
            continue
        if line.startswith(("head: ", "base: ", "targets: ")):
            continue
        if line == "no hits":
            empty += 1
        elif reviewed_row(line, bug_class):
            rows += 1
        else:
            return False
    return (rows > 0 and empty == 0) or (rows == 0 and empty == 1)


def validate_full_report(path: Path, sha: str) -> bool:
    """Require exactly three distinct reviewed sections, each for the requested head."""
    with path.open("rb") as stream:
        data = stream.read(MAX_REPORT_BYTES + 1)
    check_limit("report size", len(data), MAX_REPORT_BYTES)
    sections: dict[str, list[str]] = {}
    current = None
    for line in data.decode("utf-8").splitlines():
        if line.startswith("class:"):
            current = line.removeprefix("class: ")
            if current not in REPORT_TABLES or current in sections:
                return False
            sections[current] = []
        elif current is None:
            if line.strip():
                return False
        else:
            sections[current].append(line)
    return sections.keys() == REPORT_TABLES.keys() and all(
        validate_section(lines, name, sha) for name, lines in sections.items()
    )


def sweep_class(name: str, directory: str, base: str, head: str, deadline: float) -> str:
    """Load an additional sweep by sibling file path only when all classes are run."""
    path = Path(__file__).with_name(f"sweep_{name}.py")
    spec = importlib.util.spec_from_file_location(f"_ffmpeg_sweep_{name}", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load sweep: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.sweep(directory, base, head, deadline)


def sweep_all(directory: str, base: str, head: str) -> str:
    """Run all three class sweeps within one aggregate wall-clock budget."""
    deadline = time.monotonic() + MAX_SWEEP_SECONDS
    report = sweep(directory, base, head, deadline=deadline)
    metadata = report.splitlines()
    base = metadata[1].removeprefix("base: ")
    head = metadata[2].removeprefix("head: ")
    for name in ("state_advance", "doc_claims"):
        report += "\n" + sweep_class(name, directory, base, head, deadline)
        check_limit("report size", len(report.encode("utf-8")), MAX_REPORT_BYTES)
    return report


def write_report(output: Path, report: str) -> None:
    """Stage the complete report beside its destination and atomically publish it."""

    check_limit("report size", len(report.encode("utf-8")), MAX_REPORT_BYTES)
    descriptor, name = tempfile.mkstemp(prefix=f".{output.name}.", suffix=".tmp", dir=output.parent)
    staging = Path(name)
    try:
        os.close(descriptor)
        with staging.open("w", encoding="utf-8") as stream:
            stream.write(report)
        os.replace(staging, output)
    finally:
        staging.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    """Parse the class allow-list and write a report, failing on Git or output errors."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-C", dest="directory", default=".")
    parser.add_argument(
        "--class", dest="bug_class", choices=("unchecked-return", "all"), required=True
    )
    parser.add_argument("base")
    parser.add_argument("head")
    parser.add_argument("-o", dest="output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        runner = sweep_all if args.bug_class == "all" else sweep
        write_report(args.output, runner(args.directory, args.base, args.head))
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as error:
        print(f"variant_sweep: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
