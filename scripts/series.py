#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Check, apply and export the FFmpeg patch series.

    series.py check
        Lint base.env, series.txt, patches/*.patch and their metadata.
    series.py apply TREE [--expect-base] [--keep-going] [--report FILE] [--master-forms URL]
        Apply the series with `git am --3way` onto the clean FFmpeg
        checkout TREE and print one status per patch. With --master-forms,
        fetch recorded upstream PR forms from URL, recognise recorded
        upstream commits already in the tested master's history and skip
        patches marked release_only. A pull request that carries several
        patches records its master_form on the last of them; the earlier
        ones report in-master-form and are included only if that form
        applies.
    series.py export TREE
        Regenerate patches/ and series.txt from the commits on TREE after
        the pinned base commit.
    series.py warnings TREE LOG [--report FILE] [--base REF]
        Fail on every compiler warning in LOG that names a file the
        commits on TREE after the pinned base change; count the others.
        On a tree that applies the series to another upstream commit,
        --base names that commit.
    series.py coverage TREE LOG [--report FILE] [--base REF]
        Fail unless LOG shows a CC line for every C file those commits
        change (included *_template.c files excepted), so a file in a
        component the build left out cannot pass the warning gate unseen.
    series.py lengths TREE [--base REF] [--report FILE] [--today YYYY-MM-DD]
        Fail when a patch makes a C function longer than 60 lines without
        a live exception in that patch's metadata TOML.
    series.py notes VERSION REPOSITORY
        Print release notes: the pinned base, every patch with its status
        and upstream link, and how to verify and apply the release.
    series.py contributions [--write]
        Print the table of upstream pull requests rendered from
        upstream-contributions.json; --write puts it into README.md
        between its two marker comments. `check` compares the two.
        One record per pull request; its `patch` string lists patch names
        separated by ", ", or is empty when the series carries none.
    series.py scope [--base REF] [--head REF] [--full]
        Select FFmpeg, GPU parity and macOS CI work from the changed files.
        Append outputs with --github-output FILE and a summary with --report FILE.

Exit status: 0 on success, 1 on findings or a failed apply, 2 when the
tool cannot run (unreadable series files, unusable checkout).

Every git call runs without global or system configuration, so user
settings (apply.whitespace, am.keepcr, format.*, hooks) cannot change what
is applied or exported.
"""

from __future__ import annotations

import argparse
import bisect
import contextlib
import functools
import json
import os
import re
import shlex
import subprocess  # nosec B404 - fixed git argv, no shell, explicit timeouts.
import sys
import tempfile
import tomllib
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tree_sitter import Node, Parser

try:
    import forge_settings
except ModuleNotFoundError:
    from scripts import forge_settings

ROOT = Path(__file__).resolve().parent.parent
BASE_KEYS = ("FFMPEG_REMOTE", "FFMPEG_TAG", "FFMPEG_COMMIT")
STATUSES = ("unsubmitted", "submitted", "merged", "rejected")
METADATA_KEYS = {
    "status",
    "upstream",
    "consumers",
    "origin",
    "notes",
    "master_form",
    "upstream_commit",
    "release_only",
    "covered_by",
    "exceptions",
}
REQUIRED_EXCEPTION_KEYS = {
    "rule",
    "path",
    "function",
    "lines",
    "category",
    "entered",
    "expires",
    "reason",
}
VALID_CATEGORIES = {"A", "B", "C", "D"}
CATEGORY_INTERVALS = {"B": 3, "C": 10, "D": 120}
DIFF_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
PATCH_NAME = re.compile(r"^(\d{4})-([A-Za-z0-9._-]+)\.patch$")
FROM_LINE = re.compile(r"^From [0-9a-f]{40} Mon Sep 17 00:00:00 2001$")
COMMIT = re.compile(r"^[0-9a-f]{40}$")
FUNCTION_NAME = re.compile(
    r"[A-Za-z_][A-Za-z0-9_]*"
    r"(?: ?\( ?(?:[A-Za-z_][A-Za-z0-9_]*|[0-9]+)"
    r"(?: ?, ?(?:[A-Za-z_][A-Za-z0-9_]*|[0-9]+))* ?\))?",
    re.ASCII,
)
CONTRIBUTIONS = "upstream-contributions.json"
CONTRIBUTION_KEYS = (
    "number",
    "url",
    "title",
    "base",
    "state",
    "merged_at",
    "merged_commit",
    "in_release",
    "patch",
    "project",
    "note",
)
# Patch status in the metadata -> state of its pull request on the forge.
FORGE_STATES = {"submitted": "open", "merged": "merged", "rejected": "closed"}
DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
INSTANT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
TABLE_START = "<!-- upstream-contributions:start -->"
TABLE_END = "<!-- upstream-contributions:end -->"
WARNING = re.compile(r"(^|\s)warning(\s|#|:)", re.IGNORECASE)
LOCATION = re.compile(r"^(?P<path>[^:\s]+):\d+(:\d+)?: ")
COMPILED = re.compile(r"^CC\t(?P<object>\S+)\.o$")
GIT_IDENTITY = ("-c", "user.name=ffmpeg-patches", "-c", "user.email=ffmpeg-patches@invalid")
# Deadline for one git call (HISS-02); a 3-way am of the largest patch takes seconds.
GIT_TIMEOUT = 600
FETCH_TIMEOUT = 300
GIT_POLICY = ("-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false")
FORMAT_PATCH = (
    "format-patch",
    "--zero-commit",
    "--full-index",
    "--no-signature",
    "--no-numbered",
)


Results = list[tuple[str, str]]


class SeriesError(Exception):
    """The series or the FFmpeg checkout is unusable; main() exits 2."""


def pull_url_pattern() -> re.Pattern[str]:
    """Resolve forge settings lazily and report invalid settings as tool errors."""
    try:
        return forge_settings.pull_url_pattern()
    except ValueError as exc:
        raise SeriesError(str(exc)) from exc


@dataclass
class Findings:
    """Errors fail the check; warnings are printed and pass."""

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def error(self, where: str, message: str) -> None:
        self.errors.append(f"{where}: {message}")

    def warn(self, where: str, message: str) -> None:
        self.warnings.append(f"{where}: {message}")


def git_env() -> dict[str, str]:
    env = dict(os.environ)
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["LC_ALL"] = "C"
    for key in ("GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "GIT_COMMITTER_DATE"):
        env.pop(key, None)
    return env


def git(
    tree: Path, *args: str, check: bool = True, timeout: int = GIT_TIMEOUT
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # nosec B603 B607 - fixed git argv plus caller-supplied Git arguments.
        ["git", "-C", str(tree), *GIT_IDENTITY, *GIT_POLICY, *args],
        env=git_env(),
        capture_output=True,
        text=True,
        check=check,
        timeout=timeout,
    )


def read_base(root: Path, findings: Findings) -> dict[str, str]:
    """Read and validate the pinned upstream settings in base.env."""
    path = root / "base.env"
    if not path.is_file():
        findings.error("base.env", "missing")
        return {}
    values = {}
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep or key not in BASE_KEYS or key in values:
            findings.error(f"base.env:{number}", f"unexpected line {line!r}")
            continue
        values[key] = value
    for key in BASE_KEYS:
        if not values.get(key):
            findings.error("base.env", f"{key} is not set")
    if values.get("FFMPEG_COMMIT") and not COMMIT.match(values["FFMPEG_COMMIT"]):
        findings.error("base.env", "FFMPEG_COMMIT is not a full 40-hex commit")
    return values


def read_series(root: Path, findings: Findings) -> list[str]:
    """Read ordered patch names and report duplicate series entries."""
    path = root / "series.txt"
    if not path.is_file():
        findings.error("series.txt", "missing")
        return []
    names: list[str] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        name = line.strip()
        if not name or name.startswith("#"):
            continue
        if name in names:
            findings.error(f"series.txt:{number}", f"{name} is listed twice")
            continue
        names.append(name)
    return names


def check_patch_name(name: str, position: int, findings: Findings) -> None:
    match = PATCH_NAME.match(name)
    if not match:
        findings.error(name, "name is not NNNN-slug.patch")
    elif int(match.group(1)) != position:
        findings.error(name, f"number differs from its series position {position:04d}")


def check_patch_form(path: Path, findings: Findings) -> None:
    """Require `git format-patch` form: zero From line, headers, sign-off, diff."""
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    name = path.name
    if not lines or not FROM_LINE.match(lines[0]):
        findings.error(name, "first line is not a format-patch From line (--zero-commit)")
    header, _, rest = text.partition("\n\n")
    for prefix in ("From: ", "Date: ", "Subject: [PATCH] "):
        if f"\n{prefix}" not in header:
            findings.error(name, f"header lacks '{prefix.strip()}'")
    message = rest.split("\n---\n", 1)[0]
    if "\nSigned-off-by: " not in f"\n{message}":
        findings.error(name, "commit message lacks a Signed-off-by trailer")
    if "\ndiff --git " not in text:
        findings.error(name, "no diff --git section")


def _skip_literal(line: str, index: int, quote: str) -> int:
    """Return the index after a quoted C literal, honoring backslash escapes."""
    index += 1
    while index < len(line):
        if line[index] == "\\":
            index += 2
        elif line[index] == quote:
            return index + 1
        else:
            index += 1
    return index


def _code_line(line: str, in_block_comment: bool) -> tuple[str, bool]:
    """Strip comments and C literals from one line and return block state."""
    code: list[str] = []
    index = 0
    while index < len(line):
        char = line[index]
        next_char = line[index + 1] if index + 1 < len(line) else ""
        if in_block_comment:
            if char == "*" and next_char == "/":
                in_block_comment = False
                index += 2
            else:
                index += 1
        elif char == "/" and next_char == "*":
            in_block_comment = True
            index += 2
        elif char == "/" and next_char == "/":
            break
        elif char in "\"'":
            index = _skip_literal(line, index, char)
        else:
            code.append(char)
            index += 1
    return "".join(code), in_block_comment


def _spliced_diff_lines(text: str) -> Iterator[tuple[str, str, str | None]]:
    """Join new-file continuations, retaining the first and first added lines."""
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        first_line = line = lines[index]
        added_line = first_line if first_line.startswith("+") else None
        index += 1
        while line.startswith((" ", "+")) and line.endswith("\\") and index < len(lines):
            if lines[index].startswith(("-", "\\ No newline at end of file")):
                index += 1
                continue
            if not lines[index].startswith((" ", "+")):
                break
            if added_line is None and lines[index].startswith("+"):
                added_line = lines[index]
            line = line[:-1] + lines[index][1:]
            index += 1
        yield first_line, line, added_line


def check_added_posix_types(path: Path, findings: Findings) -> None:
    """Reject added `ssize_t` in C sources because MSVC lacks the POSIX type."""
    file_name: str | None = None
    in_hunk = False
    in_block_comment = False
    text = path.read_text(encoding="utf-8", errors="replace")
    for diff_line, spliced_line, added_line in _spliced_diff_lines(text):
        if diff_line.startswith("diff --git "):
            file_name = None
            in_hunk = False
            in_block_comment = False
            continue
        if diff_line.startswith("+++ "):
            file_name = patch_path(diff_line)
            in_hunk = False
            in_block_comment = False
            continue
        if diff_line.startswith("@@"):
            in_hunk = True
            in_block_comment = False
            continue
        if not in_hunk or not file_name or not file_name.endswith((".c", ".h")):
            continue
        if diff_line.startswith(("-", "\\ No newline")):
            continue
        if not diff_line.startswith((" ", "+")):
            continue
        code, in_block_comment = _code_line(spliced_line[1:], in_block_comment)
        if added_line is not None and re.search(r"\bssize_t\b", code):
            line = added_line[1:].rstrip()
            findings.error(
                path.name,
                f"{file_name}: added line {line!r}: POSIX type; MSVC lacks it, "
                "use ptrdiff_t or size_t",
            )


def check_exception_dates(entry: dict, name: str, findings: Findings, today: date) -> None:
    cat = entry.get("category")
    ent_raw = entry.get("entered")
    exp_raw = entry.get("expires")
    ent_date: date | None = None
    exp_date: date | None = None
    if isinstance(ent_raw, str) and DAY.fullmatch(ent_raw):
        try:
            ent_date = date.fromisoformat(ent_raw)
        except ValueError:
            findings.error(name, f"'entered' date {ent_raw!r} is invalid")
    else:
        findings.error(name, "'entered' must be a YYYY-MM-DD date")
    if isinstance(exp_raw, str) and DAY.fullmatch(exp_raw):
        try:
            exp_date = date.fromisoformat(exp_raw)
        except ValueError:
            findings.error(name, f"'expires' date {exp_raw!r} is invalid")
    else:
        findings.error(name, "'expires' must be a YYYY-MM-DD date")

    if ent_date is not None and exp_date is not None and isinstance(cat, str):
        delta = (exp_date - ent_date).days
        if cat in CATEGORY_INTERVALS:
            exp_delta = CATEGORY_INTERVALS[cat]
            if delta != exp_delta:
                findings.error(
                    name,
                    f"interval mismatch: category {cat} expires must be entered + {exp_delta} days",
                )
        elif cat == "A" and not (1 <= delta <= 90):
            findings.error(
                name,
                "interval mismatch: category A expires must be between entered + 1 and + 90 days",
            )
    if exp_date is not None and exp_date < today:
        findings.error(name, f"exception for {entry.get('function')} expired on {exp_date}")


def check_exception_fields(entry: dict, name: str, findings: Findings) -> tuple[str, str] | None:
    """Validate one HISS-04 exception entry and return its (path, function) key."""
    rule = entry.get("rule")
    if not isinstance(rule, str) or rule != "HISS-04":
        findings.error(name, "rule must be 'HISS-04'")

    path = entry.get("path")
    if not isinstance(path, str) or not path or path.startswith("/") or "\n" in path:
        findings.error(name, "path must be a repo-relative file path")

    func = entry.get("function")
    if not isinstance(func, str) or not FUNCTION_NAME.fullmatch(func):
        findings.error(name, "function must be a C identifier or a macro call such as FUNC(name)")

    lines = entry.get("lines")
    if type(lines) is not int:
        findings.error(name, "'lines' must be an integer > 60")
    elif lines <= 60:
        findings.error(name, "'lines' must be > 60")

    cat = entry.get("category")
    if not isinstance(cat, str) or cat not in VALID_CATEGORIES:
        findings.error(name, "category must be one of 'A', 'B', 'C', 'D'")

    reason = entry.get("reason")
    if (
        not isinstance(reason, str)
        or not reason
        or "\n" in reason
        or "\r" in reason
        or len(reason.encode("utf-8")) > 1024
    ):
        findings.error(name, "'reason' must be a single non-empty line of <= 1024 bytes")

    if isinstance(path, str) and isinstance(func, str):
        return (path, func)
    return None


def check_exception_entry(
    entry: object, name: str, findings: Findings, today: date
) -> tuple[str, str] | None:
    if not isinstance(entry, dict):
        findings.error(name, "each exception must be a table")
        return None
    for k in sorted(set(entry) - REQUIRED_EXCEPTION_KEYS):
        findings.error(name, f"unknown key {k!r} in exception")
    for k in sorted(REQUIRED_EXCEPTION_KEYS - set(entry)):
        findings.error(name, f"missing key {k!r} in exception")

    pair = check_exception_fields(entry, name, findings)
    check_exception_dates(entry, name, findings, today)
    return pair


def check_exceptions_metadata(data: dict, name: str, findings: Findings, today: date) -> None:
    if "exceptions" not in data:
        return
    exceptions = data["exceptions"]
    if not isinstance(exceptions, list):
        findings.error(name, "'exceptions' must be an array of tables")
        return
    seen_pairs: set[tuple[str, str]] = set()
    for entry in exceptions:
        pair = check_exception_entry(entry, name, findings, today)
        if pair is not None:
            if pair in seen_pairs:
                findings.error(name, f"duplicate exception for ({pair[0]}, {pair[1]})")
            seen_pairs.add(pair)


def check_metadata(path: Path, findings: Findings, today: date | None = None) -> None:
    """Validate one patch sidecar and record every metadata finding."""
    name = path.name
    if today is None:
        today = date.today()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        findings.error(name, f"invalid TOML: {exc}")
        return
    for key in sorted(set(data) - METADATA_KEYS):
        findings.error(name, f"unknown key {key!r}")
    status = data.get("status")
    if status not in STATUSES:
        findings.error(name, f"status must be one of {', '.join(STATUSES)}")
    upstream = data.get("upstream", "")
    if status != "unsubmitted" and not str(upstream).startswith("https://"):
        findings.error(name, f"status {status!r} needs an https upstream link")
    consumers = data.get("consumers")
    if (
        not isinstance(consumers, list)
        or not consumers
        or not all(isinstance(c, str) and c for c in consumers)
    ):
        findings.error(name, "consumers must be a non-empty list of names")
    if status == "merged":
        findings.warn(name, "merged upstream: drop it once the pinned base contains it")
    check_master_metadata(data, name, findings)
    check_covered_by(data, name, findings)
    check_exceptions_metadata(data, name, findings, today)


def check_covered_by(data: dict, name: str, findings: Findings) -> None:
    """Only an unsubmitted patch may be covered by another FFmpeg pull request."""
    if "covered_by" not in data:
        return
    if data.get("status") != "unsubmitted":
        findings.error(name, "covered_by is allowed only with status unsubmitted")
    url = data["covered_by"]
    if not isinstance(url, str) or not pull_url_pattern().fullmatch(url):
        findings.error(
            name, f"covered_by must be a pull request URL of {forge_settings.keyring_service()}"
        )


def check_master_form(form: object, name: str, findings: Findings) -> None:
    if not isinstance(form, dict) or set(form) != {"base", "head"}:
        findings.error(name, "master_form must be a table with exactly base and head")
    elif not all(isinstance(v, str) and COMMIT.fullmatch(v) for v in form.values()):
        findings.error(name, "master_form base and head must each be a full 40-hex commit")


def check_master_metadata(data: dict, name: str, findings: Findings) -> None:
    if "master_form" in data:
        if data.get("status") != "submitted":
            findings.error(name, "master_form is allowed only with status submitted")
        check_master_form(data["master_form"], name, findings)
    if "upstream_commit" in data:
        commit = data["upstream_commit"]
        if data.get("status") != "merged":
            findings.error(name, "upstream_commit is allowed only with status merged")
        if not isinstance(commit, str) or not COMMIT.fullmatch(commit):
            findings.error(name, "upstream_commit must be a full 40-hex commit")
    if "release_only" in data:
        reason = data["release_only"]
        if not isinstance(reason, str) or not reason.strip():
            findings.error(name, "release_only must be a non-empty reason")
        if "master_form" in data or "upstream_commit" in data:
            findings.error(name, "release_only excludes master_form and upstream_commit")


def check_tree(root: Path, names: list[str], findings: Findings, today: date | None = None) -> None:
    """Every listed patch exists with metadata, and nothing unlisted lies around."""
    patches = root / "patches"
    listed = set(names)
    for position, name in enumerate(names, 1):
        check_patch_name(name, position, findings)
        path = patches / name
        if not path.is_file():
            findings.error(name, "listed in series.txt but missing from patches/")
            continue
        check_patch_form(path, findings)
        check_added_posix_types(path, findings)
        sidecar = path.with_suffix(".toml")
        if sidecar.is_file():
            check_metadata(sidecar, findings, today=today)
        else:
            findings.error(name, f"no metadata file patches/{sidecar.name}")
    for path in sorted(patches.glob("*.patch")) if patches.is_dir() else []:
        if path.name not in listed:
            findings.error(path.name, "in patches/ but not listed in series.txt")
    for path in sorted(patches.glob("*.toml")) if patches.is_dir() else []:
        if path.with_suffix(".patch").name not in listed:
            findings.error(path.name, "metadata without a listed patch")


# Upstream code a patch ports carries that project's notice on an added line;
# REUSE.toml then names the holder in an override entry for exactly that patch.
UPSTREAM_NOTICES = {
    "Niklas Haas and the libplacebo contributors": re.compile(
        r"^\+.*Copyright \(C\) Niklas Haas\b"
    ),
}


def reuse_holders(root: Path, findings: Findings) -> dict[str, list[str]]:
    """Copyright holders of REUSE.toml override entries that name single files, by path."""
    path = root / "REUSE.toml"
    if not path.is_file():
        return {}
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        findings.error("REUSE.toml", f"unreadable: {exc}")
        return {}
    holders: dict[str, list[str]] = {}
    for entry in data.get("annotations", []):
        if entry.get("precedence") != "override":
            continue
        paths = entry.get("path", [])
        texts = entry.get("SPDX-FileCopyrightText", [])
        texts = [texts] if isinstance(texts, str) else texts
        for name in [paths] if isinstance(paths, str) else paths:
            if "*" not in name:
                holders.setdefault(name, []).extend(texts)
    return holders


def check_upstream_credits(root: Path, names: list[str], findings: Findings) -> None:
    """A patch adds an upstream notice exactly when REUSE.toml credits that holder for it."""
    holders = reuse_holders(root, findings)
    for name in names:
        path = root / "patches" / name
        if not path.is_file():
            continue
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        credited = holders.get(f"patches/{name}", [])
        for holder, notice in UPSTREAM_NOTICES.items():
            carries = any(notice.match(line) for line in lines)
            named = any(holder in text for text in credited)
            if carries and not named:
                findings.error(
                    name, f"adds the notice of {holder}, but no REUSE.toml override names it"
                )
            elif named and not carries:
                findings.error(
                    name, f"REUSE.toml credits {holder}, but the patch adds no such notice"
                )


def refuse_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """JSON object hook: a key written twice is an error, not the last value."""
    keys = [key for key, _ in pairs]
    for key in keys:
        if keys.count(key) > 1:
            raise ValueError(f"key {key!r} is written twice")
    return dict(pairs)


def read_contributions(root: Path, findings: Findings) -> dict | None:
    """Read contribution JSON and report malformed data or duplicate keys."""
    path = root / CONTRIBUTIONS
    if not path.is_file():
        findings.error(CONTRIBUTIONS, "missing")
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=refuse_duplicates)
    except ValueError as exc:
        findings.error(CONTRIBUTIONS, f"invalid JSON: {exc}")
        return None
    if not isinstance(data, dict) or set(data) != {"checked", "contributions"}:
        findings.error(CONTRIBUTIONS, "top level must hold 'checked' and 'contributions' only")
        return None
    if not isinstance(data["checked"], str) or not DAY.match(data["checked"]):
        findings.error(CONTRIBUTIONS, "'checked' must be a YYYY-MM-DD date")
        return None
    if not isinstance(data["contributions"], list):
        findings.error(CONTRIBUTIONS, "'contributions' must be a list")
        return None
    return data


def entry_shape_ok(entry: object, position: int, findings: Findings) -> bool:
    """Exactly the known keys, `number` an integer, every other value a string."""
    where = f"{CONTRIBUTIONS}: entry {position}"
    if not isinstance(entry, dict):
        findings.error(where, "is not an object")
        return False
    before = len(findings.errors)
    for key in sorted(set(entry) - set(CONTRIBUTION_KEYS)):
        findings.error(where, f"unknown key {key!r}")
    for key in CONTRIBUTION_KEYS:
        if key not in entry:
            findings.error(where, f"lacks key {key!r}")
        elif key == "number" and type(entry[key]) is not int:
            findings.error(where, "'number' must be an integer")
        elif key != "number" and not isinstance(entry[key], str):
            findings.error(where, f"{key!r} must be a string")
    return len(findings.errors) == before


def merge_fields_fit(entry: dict) -> bool:
    """A merged pull request names its commit and instant; any other names neither."""
    if entry["state"] == "merged":
        return bool(COMMIT.match(entry["merged_commit"]) and INSTANT.match(entry["merged_at"]))
    return not (entry["merged_commit"] or entry["merged_at"] or entry["in_release"])


def check_entry(entry: dict, findings: Findings) -> None:
    where = f"{CONTRIBUTIONS}: {entry['number']}"
    url = entry["url"]
    if not url.startswith("https://") or not url.endswith(f"/pulls/{entry['number']}"):
        findings.error(where, "url must be an https link ending in /pulls/<number>")
    for key in ("title", "base", "project"):
        if not entry[key].strip():
            findings.error(where, f"{key!r} is empty")
    for key in CONTRIBUTION_KEYS[1:]:
        if "|" in entry[key] or "\n" in entry[key]:
            findings.error(where, f"{key!r} holds a '|' or a line break")
    if entry["state"] not in FORGE_STATES.values():
        findings.error(where, f"state must be one of {', '.join(FORGE_STATES.values())}")
    elif not merge_fields_fit(entry):
        findings.error(
            where,
            "state 'merged', and only it, carries a 40-hex merged_commit and a "
            "merged_at instant (YYYY-MM-DDTHH:MM:SSZ); in_release needs both",
        )


def _sent_status(data: dict, name: str, findings: Findings) -> str | None:
    """Return the status if sent upstream, reporting non-string values as findings."""
    status = data.get("status")
    if isinstance(status, str):
        return status if status in FORGE_STATES else None
    if "status" in data:
        findings.error(name, "status must be a string")
    return None


def patch_links(root: Path, names: list[str], findings: Findings) -> dict[str, tuple[str, str]]:
    """Map each listed patch that was sent upstream to (status, upstream link)."""
    links: dict[str, tuple[str, str]] = {}
    for name in names:
        sidecar = (root / "patches" / name).with_suffix(".toml")
        try:
            data = tomllib.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            findings.error(str(sidecar), f"invalid metadata: {exc}")
            continue
        status, link = _sent_status(data, str(sidecar), findings), str(data.get("upstream", ""))
        if status and link.startswith("https://"):
            links[name] = (status, link)
    return links


def _check_group_master_form(
    url: str, items: list[tuple[str, bool]], findings: Findings, parts: dict[str, str]
) -> None:
    """Validate one multi-patch pull request group and record earlier parts."""
    with_form = [name for name, has_form in items if has_form]
    if not with_form:
        return
    if len(with_form) > 1:
        findings.error(url, f"master_form on more than one patch: {', '.join(with_form)}")
    elif with_form[0] != items[-1][0]:
        findings.error(
            with_form[0], f"master_form must be on the last patch of {url}, {items[-1][0]}"
        )
    else:
        for name, _ in items[:-1]:
            parts[name] = with_form[0]


def master_form_parts(root: Path, names: list[str], findings: Findings) -> dict[str, str]:
    """Map earlier patches of a pull request to the patch carrying its master form."""
    patches = root / "patches"
    groups: dict[str, list[tuple[str, bool]]] = {}
    for name in names:
        sidecar = (patches / name).with_suffix(".toml")
        try:
            data = tomllib.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
            continue
        link = str(data.get("upstream", ""))
        if _sent_status(data, name, findings) and link.startswith("https://"):
            groups.setdefault(link, []).append((name, "master_form" in data))
    parts: dict[str, str] = {}
    for url, items in groups.items():
        if len(items) >= 2:
            _check_group_master_form(url, items, findings, parts)
    return parts


def contribution_patches(entry: dict) -> list[str]:
    """Patch names separated by comma and space; an empty string names none."""
    return entry["patch"].split(", ") if entry["patch"] else []


def check_records(
    entries: list[dict], links: dict[str, tuple[str, str]], findings: Findings
) -> None:
    """Check every patch in each record's comma-and-space-separated patch string."""
    recorded: set[str] = set()
    for entry in entries:
        where = f"{CONTRIBUTIONS}: {entry['number']}"
        for patch in contribution_patches(entry):
            if patch in recorded:
                findings.error(where, f"{patch} is recorded twice")
            recorded.add(patch)
            status, link = links.get(patch, ("", ""))
            if link != entry["url"]:
                findings.error(where, f"the metadata of {patch} does not link {entry['url']}")
            elif FORGE_STATES[status] != entry["state"]:
                findings.error(where, f"state {entry['state']!r} disagrees with status {status!r}")
    for name, (_, link) in links.items():
        if name not in recorded:
            findings.error(name, f"upstream link {link} has no record in {CONTRIBUTIONS}")


def checked_contributions(root: Path, names: list[str], findings: Findings) -> dict | None:
    """Return the data file's content when every record is sound, else None."""
    data = read_contributions(root, findings)
    if data is None:
        return None
    before = len(findings.errors)
    entries = [
        entry
        for position, entry in enumerate(data["contributions"], 1)
        if entry_shape_ok(entry, position, findings)
    ]
    for entry in entries:
        check_entry(entry, findings)
    numbers = [entry["number"] for entry in entries]
    if numbers != sorted(set(numbers)):
        findings.error(CONTRIBUTIONS, "numbers must be unique and ascending")
    check_records(entries, patch_links(root, names, findings), findings)
    return data if len(findings.errors) == before else None


def contribution_state(entry: dict) -> str:
    if entry["state"] != "merged":
        return entry["state"]
    release = f"in {entry['in_release']}" if entry["in_release"] else "in no release yet"
    return f"merged {entry['merged_at'][:10]} (`{entry['merged_commit'][:12]}`), {release}"


def contributions_table(data: dict) -> str:
    """One README row per pull request, listing all patch numbers and the read day."""
    lines = [
        "| Pull request | Target | State | Change | Series patch |",
        "| --- | --- | --- | --- | --- |",
    ]
    for entry in data["contributions"]:
        patches = ", ".join(patch[:4] for patch in contribution_patches(entry)) or "none"
        lines.append(
            f"| [{entry['number']}]({entry['url']}) | `{entry['base']}` "
            f"| {contribution_state(entry)} | {entry['title']} | {patches} |"
        )
    lines += ["", f"State read from the FFmpeg forge on {data['checked']}."]
    return "\n".join(lines)


def readme_block(text: str) -> str | None:
    """The text between the two markers, or None unless each occurs once, in order."""
    if text.count(TABLE_START) != 1 or text.count(TABLE_END) != 1:
        return None
    start, end = text.index(TABLE_START), text.index(TABLE_END)
    if end < start:
        return None
    return text[start + len(TABLE_START) : end].strip("\n")


def check_contributions(root: Path, names: list[str], findings: Findings) -> None:
    """Validate contribution records and their rendered README table."""
    data = checked_contributions(root, names, findings)
    if data is None:
        return
    readme = root / "README.md"
    block = readme_block(readme.read_text(encoding="utf-8")) if readme.is_file() else None
    if block is None:
        findings.error("README.md", f"lacks one {TABLE_START} ... {TABLE_END} block")
    elif block != contributions_table(data):
        findings.error(
            "README.md",
            f"contribution table differs from {CONTRIBUTIONS}: "
            "run `scripts/series.py contributions --write`",
        )


def contributions(root: Path, write: bool) -> int:
    """Print the table, or write it into the README block; 1 when the data is unsound."""
    findings = Findings()
    data = checked_contributions(root, read_series(root, findings), findings)
    if data is None or findings.errors:
        for line in findings.errors:
            print(f"error: {line}", file=sys.stderr)
        return 1
    table = contributions_table(data)
    if not write:
        print(table)
        return 0
    readme = root / "README.md"
    text = readme.read_text(encoding="utf-8") if readme.is_file() else ""
    if readme_block(text) is None:
        raise SeriesError(f"README.md lacks one {TABLE_START} ... {TABLE_END} block")
    head, _, rest = text.partition(TABLE_START)
    tail = rest.partition(TABLE_END)[2]
    readme.write_text(f"{head}{TABLE_START}\n{table}\n{TABLE_END}{tail}", encoding="utf-8")
    return 0


def check(root: Path, today: date | None = None) -> Findings:
    """Lint base.env, series.txt, patches/*.patch and their metadata."""
    findings = Findings()
    read_base(root, findings)
    names = read_series(root, findings)
    if not names and not findings.errors:
        findings.error("series.txt", "lists no patch")
    check_tree(root, names, findings, today=today)
    check_upstream_credits(root, names, findings)
    check_contributions(root, names, findings)
    master_form_parts(root, names, findings)
    return findings


def am_in_progress(tree: Path) -> bool:
    result = git(tree, "rev-parse", "--path-format=absolute", "--git-path", "rebase-apply")
    return Path(result.stdout.strip()).exists()


def require_clean(tree: Path, base: dict[str, str], expect_base: bool) -> None:
    if git(tree, "status", "--porcelain", "--untracked-files=no").stdout.strip():
        raise SeriesError(f"{tree}: working tree has local changes")
    if am_in_progress(tree):
        raise SeriesError(f"{tree}: a git am session is in progress")
    head = git(tree, "rev-parse", "HEAD").stdout.strip()
    if expect_base and head != base["FFMPEG_COMMIT"]:
        raise SeriesError(f"{tree}: HEAD is {head}, base.env pins {base['FFMPEG_COMMIT']}")


def patch_path(line: str) -> str | None:
    """Read a file header from a patch, including a removed file's old path."""
    for prefix in ("+++ b/", "--- a/"):
        if line.startswith(prefix):
            return line[len(prefix) :]
    return None


def added_runs(text: str) -> dict[str, list[list[str]]]:
    """Map each file the patch writes to its runs of consecutive added lines."""
    runs: dict[str, list[list[str]]] = {}
    path: str | None = None
    run: list[str] = []
    for line in text.splitlines():
        if path and line.startswith("+") and not line.startswith("+++ "):
            run.append(line[1:].rstrip())
            continue
        if run and path:
            runs.setdefault(path, []).append(run)
        run = []
        if line.startswith("+++ "):
            path = patch_path(line)
    if run and path:
        runs.setdefault(path, []).append(run)
    return runs


def contains_run(lines: list[str], run: list[str]) -> bool:
    width = len(run)
    return any(lines[i : i + width] == run for i in range(len(lines) - width + 1))


def content_present(tree: Path, patch: Path) -> bool:
    """True when every run of added lines already stands in the tree's files.

    Upstream often lands a fix with different surrounding context, where
    neither a reverse apply nor a 3-way merge recognises it; the added
    lines themselves still match.
    """
    runs = added_runs(patch.read_text(encoding="utf-8", errors="replace"))
    for path, file_runs in runs.items():
        target = tree / path
        if not target.is_file():
            return False
        lines = [
            line.rstrip()
            for line in target.read_text(errors="replace", encoding="utf-8").splitlines()
        ]
        if not all(contains_run(lines, run) for run in file_runs):
            return False
    return bool(runs)


def apply_one(tree: Path, patch: Path) -> str:
    """Apply one patch: 'applied', 'upstream' (already in the tree) or 'conflict'."""
    forward = git(tree, "apply", "--check", str(patch), check=False).returncode == 0
    if not forward and content_present(tree, patch):
        return "upstream"
    if git(tree, "am", "--3way", str(patch), check=False).returncode == 0:
        return "applied"
    if am_in_progress(tree):
        git(tree, "am", "--abort", check=False)
    return "conflict"


def recorded_upstream(tree: Path, patch: Path, commit: str) -> bool:
    """Check recorded upstream ancestry, rejecting inconclusive shallow history."""
    if git(tree, "cat-file", "-e", f"{commit}^{{commit}}", check=False).returncode:
        raise SeriesError(f"{patch.name}: upstream commit {commit} not in the tested history")
    result = git(tree, "merge-base", "--is-ancestor", commit, "HEAD", check=False)
    if result.returncode not in (0, 1):
        raise SeriesError(f"{patch.name}: checking upstream commit {commit}: {result.stderr}")
    if result.returncode == 0:
        return True
    shallow = git(tree, "rev-parse", "--is-shallow-repository", check=False)
    if shallow.returncode or shallow.stdout.strip() not in ("true", "false"):
        raise SeriesError(
            f"{patch.name}: checking shallow history for upstream commit {commit}: "
            f"stderr: {shallow.stderr}; stdout: {shallow.stdout}"
        )
    if shallow.stdout.strip() == "true":
        raise SeriesError(
            f"{patch.name}: upstream commit {commit} is not an ancestor of HEAD in a shallow "
            "tree, where the shallow boundary can hide it; fetch the full history "
            "(git fetch --unshallow) and rerun"
        )
    return False


def fetch_master_form(tree: Path, patch: Path, url: str, form: dict[str, str]) -> None:
    # No --depth: in a shallow tree it would make the PR base, a commit of the
    # tested history, a shallow boundary and hide older upstream commits.
    try:
        result = git(
            tree,
            "fetch",
            "--no-tags",
            url,
            form["base"],
            form["head"],
            check=False,
            timeout=FETCH_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise SeriesError(f"{patch.name}: fetch from {url} timed out: {exc.stderr or exc}") from exc
    if result.returncode:
        raise SeriesError(f"{patch.name}: fetch from {url} failed: {result.stderr}")
    for commit in (form["base"], form["head"]):
        resolved = git(tree, "rev-parse", "--verify", f"{commit}^{{commit}}", check=False)
        if resolved.returncode:
            raise SeriesError(
                f"{patch.name}: fetch from {url} did not resolve {commit}: {resolved.stderr}"
            )


def apply_master_form(tree: Path, patch: Path, url: str, form: dict[str, str]) -> str:
    """Fetch and apply the recorded master form, returning its result."""
    fetch_master_form(tree, patch, url, form)
    base, head = form["base"], form["head"]
    diff = git(tree, "diff", "--binary", "--full-index", base, head).stdout
    if not diff:
        raise SeriesError(f"{patch.name}: empty master form {base}..{head} from {url}")
    with tempfile.TemporaryDirectory() as scratch:
        path = Path(scratch) / "master.patch"
        path.write_text(diff, encoding="utf-8")
        applied = git(tree, "apply", "--3way", "--index", str(path), check=False)
    if applied.returncode:
        git(tree, "reset", "-q", "--hard", "HEAD")
        return "conflict"
    git(tree, "commit", "-q", "-m", f"{patch.name}: master form {base[:12]}..{head[:12]}")
    return "master-form"


def apply_for_master(tree: Path, patch: Path, url: str) -> str:
    meta = note_metadata(patch)
    findings = Findings()
    check_master_metadata(meta, patch.name, findings)
    if findings.errors:
        raise SeriesError("\n".join(findings.errors))
    if "upstream_commit" in meta and recorded_upstream(tree, patch, meta["upstream_commit"]):
        return "upstream"
    if "master_form" in meta:
        return apply_master_form(tree, patch, url, meta["master_form"])
    if "release_only" in meta:
        return "release-only"
    return apply_one(tree, patch)


def apply_master_part(patch: Path) -> str:
    """Validate metadata of an earlier patch covered by a later master form."""
    meta = note_metadata(patch)
    findings = Findings()
    check_master_metadata(meta, patch.name, findings)
    if findings.errors:
        raise SeriesError("\n".join(findings.errors))
    return "in-master-form"


def apply_series(
    root: Path, tree: Path, expect_base: bool, keep_going: bool, master_forms: str | None = None
) -> Results:
    """Apply the patch series onto TREE, optionally using master forms."""
    findings = Findings()
    base = read_base(root, findings)
    names = read_series(root, findings)
    parts = master_form_parts(root, names, findings) if master_forms is not None else {}
    if findings.errors:
        raise SeriesError("\n".join(findings.errors))
    require_clean(tree, base, expect_base)
    results: Results = []
    stopped = False
    for name in names:
        if stopped:
            results.append((name, "not-tried"))
            continue
        patch = root / "patches" / name
        if name in parts:
            status = apply_master_part(patch)
        elif master_forms is not None:
            status = apply_for_master(tree, patch, master_forms)
        else:
            status = apply_one(tree, patch)
        results.append((name, status))
        stopped = status == "conflict" and not keep_going
    return results


def apply_failed(results: Results, expect_base: bool) -> bool:
    """A conflict always fails; a patch already in the pinned base is a broken series."""
    statuses = {status for _, status in results}
    return "conflict" in statuses or (expect_base and "upstream" in statuses)


def write_report(results: Results, tree: Path, report: Path | None) -> None:
    head = git(tree, "rev-parse", "--short=12", "HEAD").stdout.strip()
    rows = [f"| `{name}` | {status} |" for name, status in results]
    for name, status in results:
        print(f"{status:10} {name}")
    if report is None:
        return
    lines = [f"Series applied onto FFmpeg `{head}` (HEAD after the run).", ""]
    lines += ["| Patch | Result |", "| :-- | :-- |", *rows, ""]
    with report.open("a") as out:
        out.write("\n".join(lines) + "\n")


def slug(name: str) -> str | None:
    match = PATCH_NAME.match(Path(name).with_suffix(".patch").name)
    return match.group(2) if match else None


def renamed_sidecars(patches: Path, new_names: list[str]) -> None:
    """Carry metadata across renumbering: NNNN-slug.toml follows its slug."""
    by_slug = {slug(path.name): path for path in patches.glob("*.toml")}
    for name in new_names:
        target = (patches / name).with_suffix(".toml")
        source = by_slug.get(slug(name))
        if source and source != target and not target.exists():
            source.rename(target)
        if not target.exists():
            print(f"note: write metadata patches/{target.name}", file=sys.stderr)


def write_series(root: Path, names: list[str]) -> None:
    """Write ordered patch names while preserving series header comments."""
    path = root / "series.txt"
    old = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    header = [line for line in old if line.startswith("#")]
    path.write_text("\n".join([*header, *names]) + "\n", encoding="utf-8")


def pinned_commit(root: Path) -> str:
    """FFMPEG_COMMIT from base.env; an unusable base.env is a tool error."""
    findings = Findings()
    base = read_base(root, findings)
    if findings.errors:
        raise SeriesError("\n".join(findings.errors))
    return base["FFMPEG_COMMIT"]


def require_descendant(tree: Path, commit: str, message: str) -> None:
    """Raise SeriesError with MESSAGE unless HEAD of TREE descends from COMMIT."""
    if git(tree, "merge-base", "--is-ancestor", commit, "HEAD", check=False).returncode:
        raise SeriesError(f"{tree}: {message}")


def export_series(root: Path, tree: Path) -> list[str]:
    commit = pinned_commit(root)
    require_descendant(tree, commit, f"HEAD does not descend from the pinned base {commit}")
    patches = root / "patches"
    patches.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as scratch:
        git(tree, *FORMAT_PATCH, "-o", scratch, f"{commit}..HEAD")
        new = sorted(Path(scratch).glob("*.patch"))
        if not new:
            # A tree at the base exports nothing; writing that would delete every patch.
            raise SeriesError(
                f"{tree}: no commit after the pinned base {commit}; patches/ left as it is"
            )
        names = [p.name for p in new]
        for old in patches.glob("*.patch"):
            if old.name not in names:
                old.unlink()
        for path in new:
            (patches / path.name).write_bytes(path.read_bytes())
    renamed_sidecars(patches, names)
    write_series(root, names)
    return names


def resolve_series_base(root: Path, tree: Path, base: str | None) -> str:
    """Resolve the base commit and require it to precede HEAD."""
    if base is None:
        base = pinned_commit(root)
    resolved = git(tree, "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}", check=False)
    if resolved.returncode:
        raise SeriesError(f"{tree}: base {base!r} is not a commit")
    commit = resolved.stdout.strip()
    require_descendant(
        tree,
        commit,
        f"base {base!r} is not an ancestor of HEAD; "
        "pass --base with the upstream commit the series was applied to",
    )
    return commit


def series_files(root: Path, tree: Path, base: str | None = None) -> set[str]:
    """Paths the commits on TREE after BASE (default: the pinned base) change.

    BASE must be an ancestor of HEAD: on a tree that applies the series to a
    newer upstream, the pinned base would count upstream changes as the
    series' own.
    """
    commit = resolve_series_base(root, tree, base)
    names = git(tree, "diff", "--name-only", commit, "HEAD").stdout
    return {name for name in names.splitlines() if name}


def triage_warnings(lines: list[str], files: set[str]) -> tuple[list[str], int]:
    """Split compiler warning lines into those in FILES and a count of the rest.

    A build prints a source as given to the compiler: relative to the build
    directory, through the source path, or absolute. A path matches a file
    when it is that file or ends in "/" plus it.
    """
    own: list[str] = []
    other = 0
    for line in lines:
        if not WARNING.search(line):
            continue
        match = LOCATION.match(line)
        path = match.group("path") if match else ""
        if path and any(path == f or path.endswith(f"/{f}") for f in files):
            own.append(line)
        else:
            other += 1
    return own, other


def warnings_gate(
    root: Path, tree: Path, log: Path, report: Path | None, base: str | None = None
) -> int:
    """Report build warnings in series files; return 1 when any is found."""
    lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
    own, other = triage_warnings(lines, series_files(root, tree, base))
    for line in own:
        print(line)
    summary = (
        f"{len(own)} compiler warning(s) in files the series changes (gated); "
        f"{other} elsewhere in FFmpeg (counted, not gated)."
    )
    print(summary)
    if report is not None:
        with report.open("a") as out:
            out.write(summary + "\n")
    return 1 if own else 0


def compiled_sources(lines: list[str]) -> set[str]:
    """C sources of the objects a build log reports as compiled ("CC\tpath.o")."""
    return {f"{m.group('object')}.c" for line in lines if (m := COMPILED.match(line))}


def coverage_report(files: set[str], lines: list[str]) -> tuple[int, list[str]]:
    """Check that the build compiled every C file in FILES.

    Included *_template.c files have no object of their own and are not
    checked. Nothing to check is a finding too: a gate over zero files proves
    nothing. Return the exit status and the lines to print.
    """
    sources = sorted(name for name in files if name.endswith(".c"))
    checked = [name for name in sources if not name.endswith("_template.c")]
    compiled = compiled_sources(lines)
    missing = [name for name in checked if name not in compiled]
    out = [f"not compiled: {name}" for name in missing]
    out.append(
        f"{len(checked) - len(missing)} of {len(checked)} C file(s) the series changes "
        f"were compiled; {len(sources) - len(checked)} included template(s) not checked."
    )
    if not checked:
        out.append("error: no C file to check; the coverage gate proves nothing")
    return (1 if missing or not checked else 0), out


def coverage_gate(
    root: Path, tree: Path, log: Path, report: Path | None, base: str | None = None
) -> int:
    """Report coverage of the compiled series files and return its status."""
    lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
    status, out = coverage_report(series_files(root, tree, base), lines)
    print("\n".join(out))
    if report is not None:
        with report.open("a") as stream:
            stream.write("\n".join(out) + "\n")
    return status


def patch_subject(path: Path) -> str:
    """The Subject header without its [PATCH] prefix, unfolded."""
    header = path.read_text(encoding="utf-8", errors="replace").split("\n\n", 1)[0]
    match = re.search(r"^Subject: \[PATCH\] (.*(?:\n .*)*)", header, re.MULTILINE)
    return " ".join(match.group(1).split()) if match else path.name


def note_metadata(path: Path) -> dict:
    """The metadata of the listed patch at PATH; a missing or unreadable file is a tool error."""
    sidecar = path.with_suffix(".toml")
    if not path.is_file():
        raise SeriesError(f"{path.name}: listed in series.txt but missing from patches/")
    try:
        meta = tomllib.loads(sidecar.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SeriesError(f"{path.name}: no metadata file patches/{sidecar.name}") from None
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise SeriesError(f"patches/{sidecar.name}: {exc}") from exc
    if "status" not in meta:
        raise SeriesError(f"patches/{sidecar.name}: no status")
    return meta


def note_row(root: Path, number: int, name: str) -> str:
    path = root / "patches" / name
    meta = note_metadata(path)
    upstream = meta.get("upstream", "")
    link = f"[link]({upstream})" if upstream else "none"
    consumers = ", ".join(meta.get("consumers", []))
    return f"| {number:04d} | {patch_subject(path)} | {meta['status']} | {link} | {consumers} |"


def release_notes(root: Path, version: str, repository: str) -> str:
    findings = Findings()
    base = read_base(root, findings)
    names = read_series(root, findings)
    if findings.errors:
        raise SeriesError("\n".join(findings.errors))
    signer = (
        f"https://github.com/{repository}/.github/workflows/release-build.yml@refs/tags/{version}"
    )
    lines = [
        f"FFmpeg base: `{base['FFMPEG_TAG']}` (`{base['FFMPEG_COMMIT']}`).",
        "",
        "| # | Patch | Status | Upstream | Needed by |",
        "| :-- | :-- | :-- | :-- | :-- |",
        *(note_row(root, number, name) for number, name in enumerate(names, 1)),
        "",
        "Verify the release files:",
        "",
        "```bash",
        "cosign verify-blob --bundle SHA256SUMS.sigstore.json \\",
        f"  --certificate-identity '{signer}' \\",
        "  --certificate-oidc-issuer https://token.actions.githubusercontent.com SHA256SUMS",
        "sha256sum --check SHA256SUMS",
        f"gh attestation verify ffmpeg-patches-{version}.tar.gz --repo {repository}",
        "```",
        "",
        (
            "Apply the patches in `series.txt` order with `git am --3way` onto the base, "
            "or run `scripts/series.py apply --expect-base <ffmpeg-tree>` from the tarball."
        ),
    ]
    return "\n".join(lines) + "\n"


@dataclass
class FunctionSpan:
    name: str
    start: int
    end: int
    length: int


@dataclass
class LengthFinding:
    patch: str
    commit: str
    path: str
    function: str
    old_length: int
    new_length: int


@functools.cache
def _get_c_parser() -> Parser:
    """Return the cached tree-sitter C parser singleton."""
    try:
        import tree_sitter_c  # noqa: PLC0415 - optional parser dependency.
        from tree_sitter import Language, Parser  # noqa: PLC0415 - optional parser dependency.
    except ModuleNotFoundError as error:
        package = (error.name or "tree_sitter").replace("_", "-")
        raise SeriesError(
            f"Missing package {package}; run pip install --require-hashes -r requirements-dev.txt"
        ) from error
    return Parser(Language(tree_sitter_c.language()))


def _collect_function_definitions(root: Node, max_steps: int = 1_000_000) -> list[Node]:
    """Collect function_definition nodes at any depth without entering their bodies."""
    nodes: list[Node] = []
    stack: list[Node] = [root]
    for _ in range(max_steps):
        if not stack:
            return nodes
        node = stack.pop()
        if node.type == "function_definition":
            nodes.append(node)
            continue
        for child in reversed(node.children):
            stack.append(child)
    raise SeriesError(f"tree traversal exceeded limit of {max_steps} nodes")


def _find_declarator_child(node: Node) -> Node | None:
    """Return the first declarator-typed child of a node."""
    for child in node.children:
        if "declarator" in child.type:
            return child
    return None


def _find_function_declarator(node: Node) -> Node | None:
    """Find the function_declarator child in a declarator chain."""
    curr = node.child_by_field_name("declarator")
    for _ in range(32):
        if curr is None or curr.type == "function_declarator":
            return curr
        next_decl = curr.child_by_field_name("declarator")
        if next_decl is None and curr.type == "parenthesized_declarator":
            next_decl = _find_declarator_child(curr)
        curr = next_decl
    return None


def _unwrap_parentheses(node: Node | None) -> Node | None:
    """Unwrap parenthesized_declarator wrappers down to the inner declarator."""
    curr = node
    for _ in range(8):
        if curr is None or curr.type != "parenthesized_declarator":
            return curr
        curr = next((c for c in curr.children if c.type not in ("(", ")")), None)
    return curr


def _extract_function_name(node: Node) -> str | None:
    """Extract the function name from a function_definition node."""
    fn_decl = _find_function_declarator(node)
    if fn_decl is None:
        return None
    inner = _unwrap_parentheses(fn_decl.child_by_field_name("declarator"))
    if inner is not None and inner.type == "pointer_declarator":
        for _ in range(32):
            if inner is None:
                break
            if inner.type == "pointer_declarator":
                inner = _unwrap_parentheses(inner.child_by_field_name("declarator"))
            elif inner.type == "function_declarator":
                nxt = _unwrap_parentheses(inner.child_by_field_name("declarator"))
                if nxt is not None and nxt.type == "function_declarator":
                    inner = nxt
                    break
                inner = nxt
            else:
                break
    if inner is None or inner.text is None:
        return None
    text = inner.text.decode("utf-8", errors="replace")
    name = " ".join(text.split())
    return name if name else None


def _newline_byte_offsets(source: bytes) -> list[int]:
    """Index newline byte offsets in one bounded pass over the parser input."""
    return [offset for offset in range(len(source)) if source[offset] == 10]


def _byte_row(newlines: list[int], offset: int) -> int:
    """Return the zero-based row by counting newlines strictly before offset."""
    # Avoid Point's borrowed-reference getters: https://github.com/tree-sitter/py-tree-sitter/issues/505
    return bisect.bisect_right(newlines, offset - 1)


def parse_function_spans(
    text: str,
) -> tuple[dict[str, FunctionSpan], dict[str, list[FunctionSpan]]]:
    """Parse C function spans using tree-sitter."""
    longest: dict[str, FunctionSpan] = {}
    all_spans: dict[str, list[FunctionSpan]] = {}
    if not text:
        return longest, all_spans

    parser = _get_c_parser()
    source = text.encode("utf-8")
    newlines = _newline_byte_offsets(source)
    tree = parser.parse(source)
    function_nodes = _collect_function_definitions(tree.root_node)

    for node in function_nodes:
        name = _extract_function_name(node)
        if name is None:
            continue
        start = _byte_row(newlines, node.start_byte) + 1
        end_offset = node.end_byte
        # A missing closing brace can leave the exclusive end just after a newline.
        if end_offset > node.start_byte and source[end_offset - 1] != 10:
            end_offset -= 1
        end = _byte_row(newlines, end_offset) + 1
        length = end - start + 1
        span = FunctionSpan(name, start, end, length)
        all_spans.setdefault(name, []).append(span)
        if name not in longest or length > longest[name].length:
            longest[name] = span

    return longest, all_spans


def commit_added_lines(tree: Path, commit: str) -> dict[str, set[int]]:
    result = git(
        tree,
        "diff",
        "-U0",
        f"{commit}^",
        commit,
        "--",
        "*.c",
        "*.h",
        "*.m",
    )
    added: dict[str, set[int]] = {}
    current_path: str | None = None

    for line in result.stdout.splitlines():
        if line.startswith("+++ "):
            if line == "+++ /dev/null" or line.startswith("+++ /dev/null"):
                current_path = None
            elif line.startswith("+++ b/"):
                current_path = line[6:].strip()
                added.setdefault(current_path, set())
            else:
                current_path = None
        elif line.startswith("@@ ") and current_path is not None:
            m = DIFF_HUNK.match(line)
            if m:
                start = int(m.group(1))
                count = int(m.group(2)) if m.group(2) is not None else 1
                if count > 0:
                    added[current_path].update(range(start, start + count))

    return {p: lines for p, lines in added.items() if lines}


def commit_length_findings(tree: Path, commit: str, patch_name: str) -> list[LengthFinding]:
    added_lines = commit_added_lines(tree, commit)
    findings: list[LengthFinding] = []

    for path, lines in added_lines.items():
        post_res = git(tree, "show", f"{commit}:{path}", check=False)
        if post_res.returncode:
            continue
        post_funcs, post_all = parse_function_spans(post_res.stdout)

        parent_res = git(tree, "show", f"{commit}^:{path}", check=False)
        parent_funcs, _ = (
            parse_function_spans(parent_res.stdout) if parent_res.returncode == 0 else ({}, {})
        )

        for func_name, span in post_funcs.items():
            spans = post_all.get(func_name, [span])
            if not any(s.start <= line_no <= s.end for s in spans for line_no in lines):
                continue
            if span.length <= 60:
                continue
            parent_len = parent_funcs[func_name].length if func_name in parent_funcs else 0
            if span.length <= parent_len:
                continue
            findings.append(
                LengthFinding(patch_name, commit, path, func_name, parent_len, span.length)
            )

    return findings


def triage_single_finding(
    f: LengthFinding,
    patch_name: str,
    exceptions: list[dict],
    today: date,
) -> tuple[bool, bool, int | None]:
    exc_match = None
    exc_idx = None
    for idx, exc in enumerate(exceptions):
        if exc.get("path") == f.path and exc.get("function") == f.function:
            exc_match = exc
            exc_idx = idx
            break

    if exc_match is None:
        print(
            f"{patch_name}: {f.path} {f.function} {f.old_length} -> {f.new_length} lines, "
            "over the 60-line cap (HISS-04) without a live exception"
        )
        return False, False, None

    exc_lines = exc_match.get("lines")
    exp_raw = exc_match.get("expires")
    exp_date: date | None = None
    if isinstance(exp_raw, str) and DAY.fullmatch(exp_raw):
        with contextlib.suppress(ValueError):
            exp_date = date.fromisoformat(exp_raw)

    if exp_date is None or exp_date < today:
        print(
            f"{patch_name}: {f.path} {f.function} {f.old_length} -> {f.new_length} lines, "
            "over the 60-line cap (HISS-04) without a live exception"
        )
        return False, False, exc_idx

    if isinstance(exc_lines, int) and exc_lines > f.new_length:
        print(
            f"error: {patch_name}: exception for {f.path} {f.function} "
            f"lines={exc_lines} > measured {f.new_length}",
            file=sys.stderr,
        )
        print(
            f"{patch_name}: {f.path} {f.function} {f.old_length} -> {f.new_length} lines, "
            "over the 60-line cap (HISS-04) without a live exception"
        )
        return False, True, exc_idx

    if not isinstance(exc_lines, int) or exc_lines < f.new_length:
        print(
            f"{patch_name}: {f.path} {f.function} {f.old_length} -> {f.new_length} lines, "
            "over the 60-line cap (HISS-04) without a live exception"
        )
        return False, False, exc_idx

    print(f"{patch_name}: {f.path} {f.function} {f.old_length} -> {f.new_length} lines (excused)")
    return True, False, exc_idx


def load_patch_exceptions(sidecar: Path, today: date) -> tuple[list[dict], bool]:
    """Return a sidecar's length exceptions and whether its metadata had errors."""
    if not sidecar.is_file():
        return [], False
    meta_findings = Findings()
    check_metadata(sidecar, meta_findings, today=today)
    had_errors = False
    if meta_findings.errors:
        had_errors = True
        for err in meta_findings.errors:
            print(f"error: {err}", file=sys.stderr)
    try:
        data = tomllib.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        # An unreadable sidecar excuses nothing and fails the gate.
        print(f"error: {sidecar.name}: {exc}", file=sys.stderr)
        return [], True
    raw_exc = data.get("exceptions", [])
    if isinstance(raw_exc, list):
        return [e for e in raw_exc if isinstance(e, dict)], had_errors
    return [], had_errors


def check_patch_findings(
    root: Path,
    patch_name: str,
    findings: list[LengthFinding],
    today: date,
) -> tuple[int, int, bool]:
    sidecar = (root / "patches" / patch_name).with_suffix(".toml")
    exceptions, meta_errors = load_patch_exceptions(sidecar, today)

    excused = 0
    failing = 0
    matched_indices: set[int] = set()

    for f in findings:
        is_excused, is_err, exc_idx = triage_single_finding(f, patch_name, exceptions, today)
        if exc_idx is not None:
            matched_indices.add(exc_idx)
        if is_excused:
            excused += 1
        else:
            failing += 1
        if is_err:
            meta_errors = True

    for idx, exc in enumerate(exceptions):
        if idx not in matched_indices:
            meta_errors = True
            p = exc.get("path", "")
            fn = exc.get("function", "")
            print(f"error: {patch_name}: stale exception for {p} {fn}", file=sys.stderr)

    return excused, failing, meta_errors


def parse_today(today_str: str | None) -> date:
    if today_str is None:
        return date.today()
    if not DAY.fullmatch(today_str):
        raise SeriesError(f"--today {today_str!r} is not a YYYY-MM-DD date")
    try:
        return date.fromisoformat(today_str)
    except ValueError as exc:
        raise SeriesError(f"--today date {today_str!r} is invalid: {exc}") from exc


def resolve_lengths_commits(
    root: Path, tree: Path, base: str | None
) -> tuple[list[str], list[str]]:
    commit = resolve_series_base(root, tree, base)
    findings = Findings()
    names = read_series(root, findings)
    if findings.errors:
        raise SeriesError("\n".join(findings.errors))
    rev_list = git(tree, "rev-list", "--reverse", f"{commit}..HEAD").stdout.splitlines()
    commits = [c.strip() for c in rev_list if c.strip()]
    if len(commits) != len(names):
        raise SeriesError(
            f"{tree}: commit count {len(commits)} differs from series length {len(names)}"
        )
    return commits, names


def lengths_gate(
    root: Path,
    tree: Path,
    report: Path | None,
    base: str | None = None,
    today_str: str | None = None,
) -> int:
    today = parse_today(today_str)
    commits, names = resolve_lengths_commits(root, tree, base)

    total_findings = 0
    total_excused = 0
    total_failing = 0
    had_errors = False

    for c, patch_name in zip(commits, names, strict=True):
        patch_findings = commit_length_findings(tree, c, patch_name)
        total_findings += len(patch_findings)
        excused, failing, meta_err = check_patch_findings(root, patch_name, patch_findings, today)
        total_excused += excused
        total_failing += failing
        if meta_err:
            had_errors = True

    summary = (
        f"{total_findings} function(s) over 60 lines, "
        f"{total_excused} excused, {total_failing} failing"
    )
    print(summary)
    if report is not None:
        with report.open("a") as out:
            out.write(summary + "\n")

    return 1 if (total_failing > 0 or had_errors) else 0


def scope_patch_paths(root: Path, changed: list[str], refs: tuple[str, str]) -> dict[str, set[str]]:
    """Inspect both versions, so deleted patches and renamed targets remain covered."""
    patches = {path for path in changed if path.startswith("patches/") and path.endswith(".patch")}
    targets: dict[str, set[str]] = {}
    for ref in refs:
        files = git(root, "ls-tree", "-r", "--name-only", "-z", ref, "--", "patches/")
        for path in sorted(patches.intersection(files.stdout.split("\0"))):
            text = git(root, "show", f"{ref}:{path}").stdout
            paths = targets.setdefault(path, set())
            for line in text.splitlines():
                target = patch_path(line)
                if target:
                    paths.add(target)
                elif line.startswith("diff --git "):
                    # Also cover rename-only and binary diffs without +/- headers.
                    paths.update(name[2:] for name in shlex.split(line)[2:])
    return targets


def scope_reasons(
    root: Path, changed: list[str], targets: dict[str, set[str]]
) -> dict[str, list[str]]:
    """Map changed paths and graph targets to the checks they require."""
    infra = {
        ".github/workflows/series.yml",
        ".github/actions/prepare-series/action.yml",
        ".github/actions/setup-python/action.yml",
        "base.env",
        "scripts/series.py",
    }
    # The macOS job applies the whole series and refuses clang warnings in
    # every file it changes, so any change to the patched tree runs it.
    tree = infra | {"series.txt", ".python-version"}
    ffmpeg = tree | {"scripts/tonemap_parity.py"}
    sources = root / ".config/clang-tidy/sources.txt"
    if sources.is_file():
        ffmpeg.update(
            line.strip()
            for line in sources.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    reasons: dict[str, list[str]] = {"ffmpeg": [], "parity": [], "macos": []}
    for path in changed:
        gpu_test = path == "tests/helpers.py" or bool(
            re.fullmatch(r"tests/test_(?:hqdn3d|tonemap)_.*\.py", path)
        )
        if path in ffmpeg or path.startswith(("patches/", ".config/clang-tidy/")) or gpu_test:
            reasons["ffmpeg"].append(path)
        if path in infra or path == "scripts/tonemap_parity.py" or gpu_test:
            reasons["parity"].append(path)
        if path in tree or path.startswith("patches/"):
            reasons["macos"].append(path)
        for target in sorted(targets.get(path, set())):
            if re.search(r"hqdn3d|tonemap", target, re.IGNORECASE):
                reasons["parity"].append(f"{path} -> {target}")
    return reasons


def scope(
    root: Path, refs: tuple[str, str], full: bool, output: Path | None, report: Path | None
) -> int:
    base, head = refs
    before = git(root, "merge-base", base, head).stdout.strip()
    diff = git(root, "diff", "--name-only", "--no-renames", "-z", f"{base}...{head}", "--")
    changed = [path for path in diff.stdout.split("\0") if path]
    targets = {} if full else scope_patch_paths(root, changed, (before, head))
    reasons = scope_reasons(root, changed, targets)
    print(f"changed files: {len(changed)}")
    rows = ["## Series CI scope", "", f"changed files: {len(changed)}", ""]
    for key, paths in reasons.items():
        value = str(full or bool(paths)).lower()
        cause = "; ".join(["--full", *paths] if full else paths) or "no matching paths"
        print(f"{key}={value}: {cause}")
        rows.append(f"- `{key}={value}`: {cause}")
        if output is not None:
            with output.open("a", encoding="utf-8") as out:
                out.write(f"{key}={value}\n")
    if report is not None:
        with report.open("a", encoding="utf-8") as out:
            out.write("\n".join(rows) + "\n")
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--root", type=Path, default=ROOT, help="series repository root")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="lint the series and its metadata")
    scope_parser = sub.add_parser("scope", help="select CI work from changed files")
    scope_parser.add_argument("--base", default="HEAD", help="diff merge base (default: HEAD)")
    scope_parser.add_argument("--head", default="HEAD", help="diff head (default: HEAD)")
    scope_parser.add_argument("--full", action="store_true", help="force every scope key true")
    scope_parser.add_argument("--github-output", type=Path, help="append key=value outputs")
    scope_parser.add_argument("--report", type=Path, help="append a Markdown summary")
    apply = sub.add_parser(
        "apply", help="apply pinned-release or recorded master forms onto FFmpeg"
    )
    apply.add_argument("tree", type=Path)
    apply.add_argument("--expect-base", action="store_true", help="require HEAD == FFMPEG_COMMIT")
    apply.add_argument("--keep-going", action="store_true", help="try every patch after a conflict")
    apply.add_argument("--report", type=Path, help="append a Markdown table to this file")
    apply.add_argument(
        "--master-forms", metavar="URL", help="fetch recorded upstream PR forms from URL"
    )
    export = sub.add_parser("export", help="regenerate patches/ from an FFmpeg checkout")
    export.add_argument("tree", type=Path)
    warn = sub.add_parser("warnings", help="gate compiler warnings in the series' files")
    warn.add_argument("tree", type=Path)
    warn.add_argument("log", type=Path)
    warn.add_argument("--report", type=Path, help="append the summary to this file")
    warn.add_argument("--base", help="upstream commit under the series (default: FFMPEG_COMMIT)")
    cover = sub.add_parser("coverage", help="fail when the build skipped a C file of the series")
    cover.add_argument("tree", type=Path)
    cover.add_argument("log", type=Path)
    cover.add_argument("--report", type=Path, help="append the summary to this file")
    cover.add_argument("--base", help="upstream commit under the series (default: FFMPEG_COMMIT)")
    lengths = sub.add_parser("lengths", help="gate function lengths grown over 60 lines")
    lengths.add_argument("tree", type=Path)
    lengths.add_argument("--base", help="upstream commit under the series (default: FFMPEG_COMMIT)")
    lengths.add_argument("--report", type=Path, help="append the summary to this file")
    lengths.add_argument("--today", help="check date as YYYY-MM-DD (default: current day)")
    notes = sub.add_parser("notes", help="print release notes for a tag")
    notes.add_argument("version")
    notes.add_argument("repository", help="owner/name on GitHub")
    table = sub.add_parser("contributions", help="print the table of upstream pull requests")
    table.add_argument("--write", action="store_true", help="write it into README.md")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    """Exit 0 on success, 1 on findings or a failed apply, 2 when the tool cannot run."""
    args = parse_args(argv)
    try:
        pull_url_pattern()
        return run(args)
    except (SeriesError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        detail = getattr(exc, "stderr", None) or ""
        print(f"error: {exc}\n{detail}".rstrip(), file=sys.stderr)
        return 2


def run_text(args: argparse.Namespace) -> int:
    """The commands that print generated text."""
    if args.command == "contributions":
        return contributions(args.root, args.write)
    print(release_notes(args.root, args.version, args.repository), end="")
    return 0


def run(args: argparse.Namespace) -> int:
    if args.command == "check":
        findings = check(args.root)
        for line in findings.warnings:
            print(f"warning: {line}")
        for line in findings.errors:
            print(f"error: {line}", file=sys.stderr)
        return 1 if findings.errors else 0
    if args.command == "apply":
        results = apply_series(
            args.root, args.tree, args.expect_base, args.keep_going, args.master_forms
        )
        write_report(results, args.tree, args.report)
        return 1 if apply_failed(results, args.expect_base) else 0
    if args.command in ("notes", "contributions"):
        return run_text(args)
    gates = {
        "scope": lambda: scope(
            args.root, (args.base, args.head), args.full, args.github_output, args.report
        ),
        "warnings": lambda: warnings_gate(args.root, args.tree, args.log, args.report, args.base),
        "coverage": lambda: coverage_gate(args.root, args.tree, args.log, args.report, args.base),
        "lengths": lambda: lengths_gate(args.root, args.tree, args.report, args.base, args.today),
    }
    if args.command in gates:
        return gates[args.command]()
    for name in export_series(args.root, args.tree):
        print(name)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
