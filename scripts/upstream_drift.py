#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Compare upstream-contributions.json and patch metadata with the FFmpeg forge.

    upstream_drift.py [--root DIR] [--fix]
        Read every pull request upstream-contributions.json records from the
        forge's API (Forgejo, read-only, no token) and print each field that
        no longer matches: state, title, base branch, and for a merged pull
        request its merge instant and commit. `in_release` is not compared:
        the forge does not say which release tag contains a commit.
        Check recorded master_form head and merge base against the forge.
        Report covered_by pull requests that have merged or closed.
        With --fix, update drifted contribution fields and inline master_form
        commits, date the changes in UTC and regenerate the README table.
        Print each repaired field with `fixed:`. Covering pull requests still
        require a human to drop the patch and remain drift findings.

Exit status: 0 when no unfixed drift remains, 1 on drift, 2 when the tool
cannot run (unsound data file, forge unreachable, unexpected answer).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

import series

PULL_URL = series.PULL_URL
Fetch = Callable[[str], dict]


class DriftError(Exception):
    """The comparison cannot run; main() exits 2."""


@dataclass
class Repairs:
    """Pending writes and their diagnostics, collected before changing files."""

    files: dict[Path, bytes] = field(default_factory=dict)
    lines: list[str] = field(default_factory=list)


def api_url(url: str, number: int | None = None) -> str:
    match = PULL_URL.fullmatch(url) if isinstance(url, str) else None
    if not match or (number is not None and int(match.group(4)) != number):
        raise DriftError(f"{url}: not a pull request URL of code.ffmpeg.org")
    host, owner, repository, number = match.groups()
    return f"{host}/api/v1/repos/{owner}/{repository}/pulls/{number}"


def fetch_json(url: str) -> dict:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=30) as answer:
            data = json.load(answer)
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise DriftError(f"{url}: {exc}") from exc
    if not isinstance(data, dict):
        raise DriftError(f"{url}: the answer is not a JSON object")
    return data


def forge_record(pull: dict) -> dict[str, str]:
    """The fields of a forge pull request that upstream-contributions.json records."""
    try:
        merged = pull["merged"] is True
        record = {"title": pull["title"], "base": pull["base"]["ref"]}
        if merged:
            state = "merged"
        elif pull["state"] in ("open", "closed"):
            state = pull["state"]
        else:
            raise DriftError(f"pull request {pull['number']}: unknown state {pull['state']!r}")
        record["state"] = state
        record["merged_at"] = (pull["merged_at"] or "") if merged else ""
        record["merged_commit"] = (pull["merge_commit_sha"] or "") if merged else ""
    except (KeyError, TypeError) as exc:
        raise DriftError(f"unexpected pull request answer: {exc!r}") from exc
    return record


def drift(entries: list[dict], fetch: Fetch, *, fix: bool = False) -> list[str]:
    """One line per field that differs between a record and the forge."""
    lines = []
    for entry in entries:
        actual = forge_record(fetch(api_url(entry["url"], entry["number"])))
        for key, value in actual.items():
            if entry[key] != value:
                lines.append(
                    f"{entry['number']}: {key} is {value!r} on the forge, "
                    f"{entry[key]!r} in {series.CONTRIBUTIONS}"
                )
                if fix:
                    entry[key] = value
    return lines


def master_form_record(pull: dict) -> dict[str, str]:
    """Read the forge's master commits, rejecting malformed answers."""
    try:
        actual = {"head": pull["head"]["sha"], "base": pull["merge_base"]}
    except (KeyError, TypeError) as exc:
        raise DriftError(f"unexpected pull request answer: {exc!r}") from exc
    if not all(isinstance(value, str) for value in actual.values()):
        raise DriftError("unexpected pull request answer: master_form commits must be strings")
    return actual


def master_form_drift(name: str, form: dict, pull: dict) -> list[str]:
    """One line per recorded master commit that differs from the forge."""
    actual = master_form_record(pull)
    sidecar = Path(name).with_suffix(".toml")
    return [
        f"{name}: master_form {key} is {value!r} on the forge, {form[key]!r} in patches/{sidecar}"
        for key, value in actual.items()
        if form[key] != value
    ]


def master_form_fix(path: Path, form: dict, pull: dict) -> bytes:
    """Replace each old SHA exactly once on the single inline master_form line."""
    actual = master_form_record(pull)
    if not all(series.COMMIT.fullmatch(value) for value in actual.values()):
        raise DriftError(f"{path}: forge master_form commits must be full 40-hex commits")
    lines = path.read_bytes().decode("utf-8").splitlines(keepends=True)
    matches = [i for i, line in enumerate(lines) if re.match(r"^\s*master_form\s*=", line)]
    if len(matches) != 1 or not re.match(r"^\s*master_form\s*=\s*\{", lines[matches[0]]):
        raise DriftError(f"{path}: expected exactly one inline master_form line")
    index = matches[0]
    line = lines[index]
    edits = []
    for key, value in actual.items():
        old = form[key]
        if old != value:
            literal = rf"(?:\b{key}|['\"]{key}['\"])\s*=\s*(['\"]){re.escape(old)}\1"
            if line.count(old) != 1 or not re.search(literal, line.partition("#")[0]):
                raise DriftError(
                    f"{path}: old {key} SHA must occur exactly once on master_form line"
                )
            edits.append((line.index(old), old, value))
    for start, old, value in sorted(edits, reverse=True):
        line = line[:start] + value + line[start + len(old) :]
    lines[index] = line
    return "".join(lines).encode("utf-8")


def covered_by_drift(name: str, url: str, pull: dict) -> list[str]:
    """A covering pull request must stay open until the patch can be dropped."""
    actual = forge_record(pull)
    if actual["state"] == "open":
        return []
    number = url.rsplit("/", 1)[1]
    line = f"{name}: covered_by pull request {number} is {actual['state']} on the forge"
    if actual["state"] == "merged":
        line += (
            f" (merge commit {actual['merged_commit']}); "
            "drop the patch once the pinned base contains it"
        )
    return [line]


def patch_drift(root: Path, fetch: Fetch, repairs: Repairs | None = None) -> list[str]:
    """Compare the series' master forms and covering pull requests with the forge."""
    findings = series.Findings()
    names = series.read_series(root, findings)
    lines = []
    for name in names:
        meta = series.note_metadata(root / "patches" / name)
        series.check_master_metadata(meta, name, findings)
        series.check_covered_by(meta, name, findings)
        if findings.errors:
            raise DriftError("; ".join(findings.errors))
        if "master_form" in meta:
            pull = fetch(api_url(meta.get("upstream", "")))
            moved = master_form_drift(name, meta["master_form"], pull)
            if repairs is not None and moved:
                path = (root / "patches" / name).with_suffix(".toml")
                repairs.files[path] = master_form_fix(path, meta["master_form"], pull)
                repairs.lines.extend(moved)
            else:
                lines.extend(moved)
        if "covered_by" in meta:
            pull = fetch(api_url(meta["covered_by"]))
            lines.extend(covered_by_drift(name, meta["covered_by"], pull))
    if findings.errors:
        raise DriftError("; ".join(findings.errors))
    return lines


def recorded_entries(root: Path) -> list[dict]:
    findings = series.Findings()
    data = series.checked_contributions(root, series.read_series(root, findings), findings)
    if data is None or findings.errors:
        raise DriftError("; ".join(findings.errors) or f"{series.CONTRIBUTIONS} is unsound")
    return data["contributions"]


def write_repairs(root: Path, entries: list[dict], repairs: Repairs, today: date | None) -> None:
    """Write repairs and the table together; restore original bytes on failure."""
    if not repairs.lines:
        return
    path = root / series.CONTRIBUTIONS
    original = path.read_bytes()
    data = json.loads(original)
    data["contributions"] = entries
    data["checked"] = (today or datetime.now(UTC).date()).isoformat()
    repairs.files[path] = (
        json.dumps(data, indent=2, ensure_ascii=original.isascii()) + "\n"
    ).encode("utf-8")
    originals = {path: path.read_bytes() for path in [*repairs.files, root / "README.md"]}
    try:
        for path, content in repairs.files.items():
            path.write_bytes(content)
        if series.contributions(root, write=True) != 0:
            raise DriftError("cannot regenerate the contribution table")
    except (OSError, DriftError, series.SeriesError):
        for path, content in originals.items():
            path.write_bytes(content)
        raise


def fix_drift(root: Path, fetch: Fetch, today: date | None) -> tuple[list[str], list[str]]:
    """Repair supported drift; return unfixed and fixed field diagnostics."""
    repairs = Repairs()
    try:
        entries = recorded_entries(root)
        repairs.lines.extend(drift(entries, fetch, fix=True))
        lines = patch_drift(root, fetch, repairs)
        write_repairs(root, entries, repairs, today)
    except (OSError, UnicodeError) as exc:
        raise DriftError(str(exc)) from exc
    return lines, repairs.lines


def main(argv: list[str], fetch: Fetch = fetch_json, today: date | None = None) -> int:
    """Exit 0 with no unfixed drift, 1 on drift, 2 when the tool cannot run."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument(
        "--fix", action="store_true", help="repair fields and regenerate the README"
    )
    args = parser.parse_args(argv)
    cache: dict[str, dict] = {}

    def cached_fetch(url: str) -> dict:
        if url not in cache:
            cache[url] = fetch(url)
        return cache[url]

    try:
        fixed = []
        if args.fix:
            lines, fixed = fix_drift(args.root, cached_fetch, today)
        else:
            entries = recorded_entries(args.root)
            lines = drift(entries, cached_fetch)
            lines.extend(patch_drift(args.root, cached_fetch))
    except (DriftError, series.SeriesError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    for line in fixed:
        print(f"fixed: {line}")
    for line in lines:
        print(f"::error title=Upstream drift::{line}")
    print(f"{len(cache)} pull request(s) read from the forge, {len(lines)} field(s) drifted.")
    return 1 if lines else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
