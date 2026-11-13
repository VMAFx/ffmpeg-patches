#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""List added Texi claims requiring comparison with supported library sources."""

from __future__ import annotations

import re
import sys
from collections.abc import Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.variant_sweep import (  # noqa: E402
    MAX_REPORT_BYTES,
    MAX_SOURCE_BYTES,
    MAX_SOURCE_COUNT,
    REPORT_TABLES,
    added_lines,
    check_deadline,
    check_limit,
    git,
    read_sources,
)

NUMBER = (
    r"(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    r"thirteen|fourteen|fifteen|sixteen)"
)
CLAIM = re.compile(
    rf"\b\d+x\d+\b|\b(?:at most|up to|at least|no more than|maximum|minimum)"
    rf"(?:\W+\w+){{0,3}}?\W+{NUMBER}\b|\b(?:averag|approximat|round|clamp|truncat|smallest|largest)\w*"
    rf"|\b(?:above|below|over|under|exceeds?|greater than|less than|more than|fewer than|"
    rf"higher than|lower than)(?:\W+\w+){{0,3}}?\W+{NUMBER}\b"
    r"|\b\d+(?:\.\d+)+\s+or\s+(?:newer|older|later|earlier|above|below)\b"
    r"|\b(?:newer|older) than\s+\d+(?:\.\d+)*\b"
    r"|-?\d+\s*\.\.\s*-?\d+|-?\d+\s+to\s+-?\d+"
    r"|\b\d+\s*(?:KiB|MiB|GiB|KB|MB|GB|bytes?|bits?)\b",
    re.I,
)
SECTION = re.compile(r"^@section[ \t]+([^\r\n]+)")
FLOOR = re.compile(r'require_pkg_config\s+(\w+)\s+"[^"\s]+\s+>=\s*([^"\s]+)"')
CPP_FLOOR = re.compile(
    r'require_cpp_condition[ \t]+(\w+)[ \t]+\S+[ \t]+"'
    r'([A-Z][A-Z0-9_]*)[ \t]*>=[ \t]*(\d+)"'
)
GUARD = re.compile(r"\b[A-Z][A-Z0-9_]*_CHECK_VERSION\s*\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)")
CONDITION = re.compile(r"^[ \t]*#[ \t]*(?:if|elif)\b([^\n]*)", re.M)
BUILD_GUARD = re.compile(
    r"\b([A-Z][A-Z0-9_]*_(?:BUILD|VERSION|VERSION_MAJOR|API_VERSION))"
    r"[ \t]*(>=|<)[ \t]*(\d+)\b"
)
NONCODE = re.compile(r'//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'', re.S)
TABLE = REPORT_TABLES["doc-claims"]


def claim_paragraphs(lines: list[str]) -> Iterator[list[int]]:
    """Group consecutive nonblank lines, starting fresh at each Texi directive."""
    paragraph = []
    for number, line in enumerate(lines, 1):
        if paragraph and (not line.strip() or line.lstrip().startswith("@")):
            yield paragraph
            paragraph = []
        if line.strip():
            paragraph.append(number)
    if paragraph:
        yield paragraph


def paragraph_claims(
    lines: list[str], paragraph: list[int], changed: dict[int, str]
) -> dict[int, tuple[int, int]]:
    """Map changed claim row lines to the union of their matched line spans."""
    text = " ".join(lines[number - 1].strip() for number in paragraph)
    numbers = [number for number in paragraph for _ in range(len(lines[number - 1].strip()) + 1)]
    spans = {}
    for match in CLAIM.finditer(text):
        first, last = numbers[match.start()], numbers[match.end() - 1]
        touched = [number for number in range(first, last + 1) if number in changed]
        if touched:
            row = touched[0]
            low, high = spans.get(row, (first, last))
            spans[row] = (min(low, first), max(high, last))
    return spans


def find_claims(source: str, changed: dict[int, str]) -> list[tuple[int, str, str]]:
    """Select changed claim spans across paragraphs with their row's encoder section."""
    library = "unknown"
    libraries = []
    claims = []
    check_limit("source size", len(source.encode()), MAX_SOURCE_BYTES)
    lines = source.splitlines()
    for line in lines:
        section = SECTION.match(line)
        if section:
            library = section[1].split(",", 1)[0].strip()
        libraries.append(library)
    for paragraph in claim_paragraphs(lines):
        for number, (first, last) in sorted(paragraph_claims(lines, paragraph, changed).items()):
            text = " ".join(line.strip() for line in lines[first - 1 : last])
            claims.append((number, text.replace("|", r"\|"), libraries[number - 1]))
    return claims


def resolve_library(name: str, configure: str) -> str:
    """Resolve the first section name through encoder dependencies or library checks."""
    if name == "unknown":
        return "unknown"
    encoder = re.escape(name.replace("-", "_"))
    dependency = re.search(rf'^{encoder}_encoder_deps="([A-Za-z0-9_]+)(?=[ "])', configure, re.M)
    if dependency:
        return dependency[1]
    requirement = re.search(
        rf"\brequire_(?:pkg_config|cpp_condition)[ \t]+{re.escape(name)}[ \t]+", configure
    )
    return name if requirement else "unknown"


def encoder_sources(name: str, makefile: str) -> list[str]:
    """Map an encoder to allow-listed Makefile object paths or its legacy source path."""
    encoder = re.escape(name.replace("-", "_").upper())
    objects = re.search(
        rf"^[ \t]*OBJS-\$\(CONFIG_{encoder}_ENCODER\)[ \t]*\+=[ \t]*"
        r"((?:[A-Za-z0-9_]+\.o[ \t]*)+)$",
        makefile,
        re.M,
    )
    if objects is None:
        return [f"libavcodec/{name}.c"]
    names = objects[1].split()
    return [f"libavcodec/{names[index][:-2]}.c" for index in range(len(names))]


def mask_noncode(source: str) -> str:
    """Blank C comments and literals, preserving offsets, newlines, and directives."""
    return NONCODE.sub(lambda match: re.sub(r"[^\n]", " ", match[0]), source)


def source_versions(source: str) -> list[str]:
    """Collect active version-call guards before comparisons on conditional lines."""
    source = mask_noncode(source)
    calls = GUARD.findall(source)
    versions = [".".join(calls[index]) for index in range(len(calls))]
    comparisons = BUILD_GUARD.findall("\n".join(CONDITION.findall(source)))
    versions.extend(" ".join(comparisons[index]) for index in range(len(comparisons)))
    return versions


def library_versions(name: str, sources: dict[str, str]) -> str:
    """Combine distinct pkg-config floors, CPP floors, version calls, and build guards."""
    configure = sources.get("configure", "")
    library = resolve_library(name, configure)
    if library == "unknown":
        return "unknown"
    floors = FLOOR.findall(configure)
    versions = [floors[index][1] for index in range(len(floors)) if floors[index][0] == library]
    cpp = CPP_FLOOR.findall(configure)
    versions.extend(
        f"{cpp[index][1]} >= {cpp[index][2]}"
        for index in range(len(cpp))
        if cpp[index][0] == library
    )
    paths = encoder_sources(name, sources.get("libavcodec/Makefile", ""))
    source = "\n".join(sources.get(paths[index], "") for index in range(len(paths)))
    versions.extend(source_versions(source))
    return ", ".join(dict.fromkeys(versions)) or "unknown"


def read_existing_sources(
    directory: str, head: str, wanted: set[str], sources: dict[str, str], deadline: float
) -> None:
    """Read requested blobs that exist at head without consulting working-tree sources."""
    check_limit("source count", len(wanted | sources.keys()), MAX_SOURCE_COUNT)
    listing = git(
        directory,
        "ls-tree",
        "-r",
        "--name-only",
        "-z",
        head,
        "--",
        *sorted(wanted),
        deadline=deadline,
    )
    read_sources(directory, head, set(listing.split("\0")) & wanted, sources, deadline)


def version_sources(
    directory: str, head: str, libraries: set[str], deadline: float
) -> dict[str, str]:
    """Read head configure, Makefile, and existing sources for the selected encoder names."""
    sources: dict[str, str] = {}
    read_existing_sources(directory, head, {"configure", "libavcodec/Makefile"}, sources, deadline)
    names = sorted(libraries - {"unknown"})
    check_limit("source count", len(names), MAX_SOURCE_COUNT)
    wanted: set[str] = set()
    for index in range(len(names)):
        check_deadline(deadline)
        if resolve_library(names[index], sources.get("configure", "")) != "unknown":
            wanted.update(encoder_sources(names[index], sources.get("libavcodec/Makefile", "")))
    if wanted:
        read_existing_sources(directory, head, wanted, sources, deadline)
    return sources


def claim_row(claim: tuple[str, int, str, str], sources: dict[str, str]) -> str:
    """Render an escaped row naming the resolved library and its encoder when different."""
    path, number, text, name = claim
    library = resolve_library(name, sources.get("configure", ""))
    if library not in ("unknown", name):
        library = f"{library} ({name})"
    library = library.replace("|", r"\|")
    oldest = library_versions(name, sources)
    return f"| {path}:{number} | {text} | {library} | {oldest} | TODO | TODO |"


def sweep(directory: str, base: str, head: str, deadline: float) -> str:
    """Render bounded documentation rows with resolved libraries and ordered version evidence."""
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
        "doc/*.texi",
        deadline=deadline,
    )
    changed = added_lines(diff)
    sources: dict[str, str] = {}
    read_sources(directory, head, set(changed), sources, deadline)
    claims = []
    paths = sorted(sources)
    for index in range(len(paths)):
        path = paths[index]
        check_deadline(deadline)
        selected = find_claims(sources[path], changed[path])
        claims.extend((path, *selected[index]) for index in range(len(selected)))
    versions = (
        version_sources(
            directory, head, {claims[index][3] for index in range(len(claims))}, deadline
        )
        if claims
        else {}
    )
    lines = [
        "class: doc-claims",
        f"head: {head}",
        "",
        TABLE,
        "| --- | --- | --- | --- | --- | --- |",
    ]
    size = len(("\n".join(lines) + "\n").encode())
    for index in range(len(claims)):
        check_deadline(deadline)
        row = claim_row(claims[index], versions)
        size += len((row + "\n").encode())
        check_limit("report size", size, MAX_REPORT_BYTES)
        lines.append(row)
    if not claims:
        lines.append("no hits")
    check_deadline(deadline)
    report = "\n".join(lines) + "\n"
    check_limit("report size", len(report.encode()), MAX_REPORT_BYTES)
    return report
