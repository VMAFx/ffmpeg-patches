#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Compare upstream-contributions.json and patch metadata with the FFmpeg forge.

    upstream_drift.py [--root DIR]
        Read every pull request upstream-contributions.json records from the
        forge's API (Forgejo, read-only, no token) and print each field that
        no longer matches: state, title, base branch, and for a merged pull
        request its merge instant and commit. `in_release` is not compared:
        the forge does not say which release tag contains a commit.
        Check recorded master_form head and merge base against the forge.
        Report covered_by pull requests that have merged or closed.

Exit status: 0 when every record matches, 1 on drift, 2 when the tool
cannot run (unsound data file, forge unreachable, unexpected answer).
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

import series

PULL_URL = series.PULL_URL
Fetch = Callable[[str], dict]


class DriftError(Exception):
    """The comparison cannot run; main() exits 2."""


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


def drift(entries: list[dict], fetch: Fetch) -> list[str]:
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
    return lines


def master_form_drift(name: str, form: dict, pull: dict) -> list[str]:
    """One line per recorded master commit that differs from the forge."""
    try:
        actual = {"head": pull["head"]["sha"], "base": pull["merge_base"]}
    except (KeyError, TypeError) as exc:
        raise DriftError(f"unexpected pull request answer: {exc!r}") from exc
    if not all(isinstance(value, str) for value in actual.values()):
        raise DriftError("unexpected pull request answer: master_form commits must be strings")
    sidecar = Path(name).with_suffix(".toml")
    return [
        f"{name}: master_form {key} is {value!r} on the forge, {form[key]!r} in patches/{sidecar}"
        for key, value in actual.items()
        if form[key] != value
    ]


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


def patch_drift(root: Path, fetch: Fetch) -> list[str]:
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
            lines.extend(master_form_drift(name, meta["master_form"], pull))
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


def main(argv: list[str], fetch: Fetch = fetch_json) -> int:
    """Exit 0 when every record matches, 1 on drift, 2 when the tool cannot run."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    args = parser.parse_args(argv)
    cache: dict[str, dict] = {}

    def cached_fetch(url: str) -> dict:
        if url not in cache:
            cache[url] = fetch(url)
        return cache[url]

    try:
        entries = recorded_entries(args.root)
        lines = drift(entries, cached_fetch)
        lines.extend(patch_drift(args.root, cached_fetch))
    except (DriftError, series.SeriesError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    for line in lines:
        print(f"::error title=Upstream drift::{line}")
    print(f"{len(cache)} pull request(s) read from the forge, {len(lines)} field(s) drifted.")
    return 1 if lines else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
