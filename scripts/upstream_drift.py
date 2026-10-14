#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Compare upstream-contributions.json with the FFmpeg forge.

    upstream_drift.py [--root DIR]
        Read every pull request upstream-contributions.json records from the
        forge's API (Forgejo, read-only, no token) and print each field that
        no longer matches: state, title, base branch, and for a merged pull
        request its merge instant and commit. `in_release` is not compared:
        the forge does not say which release tag contains a commit.

Exit status: 0 when every record matches, 1 on drift, 2 when the tool
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
from pathlib import Path

import series

PULL_URL = re.compile(r"(https://code\.ffmpeg\.org)/([^/]+)/([^/]+)/pulls/(\d+)")
Fetch = Callable[[str], dict]


class DriftError(Exception):
    """The comparison cannot run; main() exits 2."""


def api_url(entry: dict) -> str:
    match = PULL_URL.fullmatch(entry["url"])
    if not match or int(match.group(4)) != entry["number"]:
        raise DriftError(f"{entry['url']}: not a pull request URL of code.ffmpeg.org")
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


def drift(entries: list[dict], fetch: Fetch) -> list[str]:
    """One line per field that differs between a record and the forge."""
    lines = []
    for entry in entries:
        actual = forge_record(fetch(api_url(entry)))
        for key, value in actual.items():
            if entry[key] != value:
                lines.append(
                    f"{entry['number']}: {key} is {value!r} on the forge, "
                    f"{entry[key]!r} in {series.CONTRIBUTIONS}"
                )
    return lines


def recorded_entries(root: Path) -> list[dict]:
    findings = series.Findings()
    data = series.checked_contributions(root, series.read_series(root, findings), findings)
    if data is None or findings.errors:
        raise DriftError("; ".join(findings.errors) or f"{series.CONTRIBUTIONS} is unsound")
    return data["contributions"]


def main(argv: list[str], fetch: Fetch = fetch_json) -> int:
    """Exit 0 when every record matches, 1 on drift, 2 when the tool cannot run."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    args = parser.parse_args(argv)
    try:
        entries = recorded_entries(args.root)
        lines = drift(entries, fetch)
    except (DriftError, series.SeriesError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    for line in lines:
        print(f"::error title=Upstream drift::{line}")
    print(f"{len(entries)} pull request(s) read from the forge, {len(lines)} field(s) drifted.")
    return 1 if lines else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
