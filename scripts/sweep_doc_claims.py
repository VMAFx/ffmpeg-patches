#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""List added Texi claims requiring comparison with supported library sources."""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.variant_sweep import (  # noqa: E402
    MAX_REPORT_BYTES,
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
    rf"(?:\W+\w+){{0,3}}?\W+{NUMBER}\b|\b(?:averag|approximat|round|clamp|truncat|smallest|largest)\w*",
    re.I,
)
SECTION = re.compile(r"^@section\s+(\S+)")
FLOOR = re.compile(r'require_pkg_config\s+(\w+)\s+"[^"\s]+\s+>=\s*([^"\s]+)"')
GUARD = re.compile(r"\b[A-Z][A-Z0-9_]*_CHECK_VERSION\s*\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)")
TABLE = REPORT_TABLES["doc-claims"]


def find_claims(source: str, changed: dict[int, str]) -> list[tuple[int, str, str]]:
    """Select allowed added claim lines and their nearest preceding encoder section."""
    library = "unknown"
    claims = []
    for number, line in enumerate(source.splitlines(), 1):
        section = SECTION.match(line)
        if section:
            library = section[1]
        if number in changed and CLAIM.search(line):
            claims.append((number, line.strip().replace("|", r"\|"), library))
    return claims


def library_versions(library: str, sources: dict[str, str]) -> str:
    """Combine the configured version floor with distinct encoder version guards."""
    floors = {match[1]: match[2] for match in FLOOR.finditer(sources.get("configure", ""))}
    if library == "unknown" or library not in floors:
        return "unknown"
    versions = [floors[library]]
    for match in GUARD.finditer(sources.get(f"libavcodec/{library}.c", "")):
        version = ".".join(match.groups())
        if version not in versions:
            versions.append(version)
    return ", ".join(versions)


def version_sources(
    directory: str, head: str, libraries: set[str], deadline: float
) -> dict[str, str]:
    """Read only existing configure and encoder blobs needed by selected claims."""
    wanted = {"configure"} | {f"libavcodec/{name}.c" for name in libraries if name != "unknown"}
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
    paths = {name for name in listing.split("\0") if name in wanted}
    sources: dict[str, str] = {}
    read_sources(directory, head, paths, sources, deadline)
    return sources


def sweep(directory: str, base: str, head: str, deadline: float) -> str:
    """Render bounded documentation rows with library floors and reviewer placeholders."""
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
    for path, source in sorted(sources.items()):
        check_deadline(deadline)
        claims.extend((path, *claim) for claim in find_claims(source, changed[path]))
    versions = (
        version_sources(directory, head, {row[3] for row in claims}, deadline) if claims else {}
    )
    lines = [
        "class: doc-claims",
        f"head: {head}",
        "",
        TABLE,
        "| --- | --- | --- | --- | --- | --- |",
    ]
    size = len(("\n".join(lines) + "\n").encode())
    for path, number, text, library in claims:
        check_deadline(deadline)
        oldest = library_versions(library, versions)
        row = f"| {path}:{number} | {text} | {library} | {oldest} | TODO | TODO |"
        size += len((row + "\n").encode())
        check_limit("report size", size, MAX_REPORT_BYTES)
        lines.append(row)
    if not claims:
        lines.append("no hits")
    check_deadline(deadline)
    report = "\n".join(lines) + "\n"
    check_limit("report size", len(report.encode()), MAX_REPORT_BYTES)
    return report
