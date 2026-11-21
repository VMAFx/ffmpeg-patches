#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Fill linked upstream issues and related pulls, and check open pulls for overlaps.

    upstream_links.py [--root DIR] fill [--dry-run] [--pull N]... [--max-age-days N]
        Fill open or merged manifest rows, optionally limited by --pull N.
        Share one candidate scan and changed-file memo across the selected
        pulls, extract issue numbers after Fixes, Closes or Refs in each body,
        and write fixes_issues and related into upstream-contributions.json.
    upstream_links.py [--root DIR] check [--pull N]... [--since DATE]
        For every open manifest pull and each --pull N, list new related
        pulls and issues not yet recorded in the manifest and exit 1 when at
        least one is an overlap by another author. Scan updates since the
        manifest's checked date minus CHECK_MARGIN_DAYS, or --since DATE.

Exit status: 0 on success or non-overlapping check, 1 on an overlap by
another author or undecided file comparison in check, 2 on a read, input or incomplete-scan error,
3 when fill published the manifest but cleanup left a staging file or lock.
SCAN_BUDGET_SECONDS gives network waits a 300-second monotonic budget;
requests reserve pacing and a full per-operation timeout. Waiting for a fetch
ends at the deadline even if its blocking operations keep renewing their timeout.
Fill checks the same deadline during preparation and immediately before atomic
replacement; detected expiry refuses publication with status 2 and attempts
staging-file and lock cleanup, reporting every failed path. Local CPU work and
file operations are not interrupted, so the budget is not a hard whole-run time limit.
CI gives the overlap job twice this budget.
REQUEST_TIMEOUT is the 30-second timeout of each blocking network operation.
Pagination reads each Link entry's rel parameter regardless of parameter order,
case or quoting, splitting space-separated relation values. Only an exact next
relation continues; other relations prove completion, and no rel supplies no evidence.
MAX_RELATED_AUTHOR_CHARS (series) allows at most 256 printable account characters;
forge and manifest authors share that allow-list, excluding controls and line separators.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

if not __package__:
    import series
    import upstream_drift
    from upstream_links_forge import (
        MAX_CANDIDATES,
        Fetch,
        RequestBudget,
        Sleep,
        _budgeted,
        fetch_forge,
        related_for,
    )
    from upstream_links_manifest import (
        DEFAULT_FILL_MAX_AGE_DAYS,
        MAX_FILL_MAX_AGE_DAYS,
        MIN_FILL_MAX_AGE_DAYS,
        PUBLISHED_WITH_LEFTOVERS,
        _checked_manifest,
        _fill_manifest,
        _FillOptions,
    )
    from upstream_links_parse import DriftError, _run_day, overlaps, parse_timestamp
    from upstream_links_scan import (
        _partial_match_lines,
        _undecided_for,
        _undecided_lines,
        inspect_candidates,
        read_run_pulls,
        request_cap,
    )
else:
    from scripts import series, upstream_drift
    from scripts.upstream_links_forge import (
        MAX_CANDIDATES,
        Fetch,
        RequestBudget,
        Sleep,
        _budgeted,
        fetch_forge,
        related_for,
    )
    from scripts.upstream_links_manifest import (
        DEFAULT_FILL_MAX_AGE_DAYS,
        MAX_FILL_MAX_AGE_DAYS,
        MIN_FILL_MAX_AGE_DAYS,
        PUBLISHED_WITH_LEFTOVERS,
        _checked_manifest,
        _fill_manifest,
        _FillOptions,
    )
    from scripts.upstream_links_parse import DriftError, _run_day, overlaps, parse_timestamp
    from scripts.upstream_links_scan import (
        _partial_match_lines,
        _undecided_for,
        _undecided_lines,
        inspect_candidates,
        read_run_pulls,
        request_cap,
    )


# Revisit a week around the drift snapshot to cover date granularity and late updates.
CHECK_MARGIN_DAYS = 7


def _check_targets(entries: list[dict], extra_pulls: list[int]) -> list[tuple[int, str, set[int]]]:
    """Build ordered (number, url, recorded_related_numbers) targets for check."""
    cap = upstream_drift.MAX_SERIES_ENTRIES
    if len(extra_pulls) > cap:
        raise DriftError(f"--pull arguments exceed {cap}")
    by_number = {int(entry["number"]): entry for entry in entries[:cap]}
    targets: list[tuple[int, str, set[int]]] = []
    seen: set[int] = set()
    for entry in entries[:cap]:
        if entry["state"] == "open":
            num = int(entry["number"])
            recorded = {int(r["number"]) for r in entry.get("related", [])[: series.MAX_RELATED]}
            targets.append((num, entry["url"], recorded))
            seen.add(num)
    for num in extra_pulls[:cap]:
        if not isinstance(num, int) or isinstance(num, bool) or num <= 0:
            raise DriftError(f"invalid --pull number: {num!r}")
        if num in seen:
            continue
        entry = by_number.get(num)
        url = entry["url"] if entry else ""
        recorded = (
            {int(r["number"]) for r in entry.get("related", [])[: series.MAX_RELATED]}
            if entry
            else set()
        )
        targets.append((num, url, recorded))
        seen.add(num)
    return targets


def check_window(checked: str, since: str | None) -> datetime:
    """Resolve a calendar-date override or the manifest snapshot minus the fixed margin."""
    raw = since if since is not None else checked
    if not isinstance(raw, str) or not series.DAY.fullmatch(raw):
        raise DriftError(f"invalid {'--since' if since is not None else 'checked'} date: {raw!r}")
    cutoff = parse_timestamp(raw, "check window")
    try:
        return cutoff if since is not None else cutoff - timedelta(days=CHECK_MARGIN_DAYS)
    except OverflowError as exc:
        raise DriftError(f"{checked}: check window exceeds calendar range") from exc


def check_overlaps(
    root: Path,
    extra_pulls: list[int],
    fetch: Fetch,
    *,
    today: date | None = None,
    since: str | None = None,
) -> tuple[list[str], list[dict]]:
    """Close the single scan worker after overlap checks or any read failure."""
    request = _budgeted(fetch, request_cap(upstream_drift.MAX_SERIES_ENTRIES))
    try:
        return _check_overlaps(root, extra_pulls, request, today=today, since=since)
    finally:
        request.reader.close()


def _check_overlaps(
    root: Path,
    extra_pulls: list[int],
    fetch: Fetch,
    *,
    today: date | None = None,
    since: str | None = None,
) -> tuple[list[str], list[dict]]:
    """Report new relations and undecided file comparisons using one bounded run memo."""
    run_day = _run_day(today)
    data = _checked_manifest(root)
    cutoff = check_window(data["checked"], since)
    targets = _check_targets(data["contributions"], extra_pulls)
    if len(targets) > upstream_drift.MAX_SERIES_ENTRIES:
        raise DriftError(f"check targets exceed {upstream_drift.MAX_SERIES_ENTRIES}")
    request = _budgeted(fetch, request_cap(len(targets)))
    request.window_start = cutoff.isoformat()
    request.narrow_option = "check --since YYYY-MM-DD"
    own_pulls, candidates, files = read_run_pulls(targets, request, cutoff)
    lines: list[str] = []
    loud: list[dict] = []
    for own, (_, _, recorded) in zip(
        own_pulls[: upstream_drift.MAX_SERIES_ENTRIES],
        targets[: upstream_drift.MAX_SERIES_ENTRIES],
        strict=True,
    ):
        eligible = inspect_candidates(own, candidates, recorded, files, request)
        new_rows = related_for(own, eligible, today=run_day)
        undecided = _undecided_for(own, eligible)
        lines.extend(_undecided_lines(own, undecided))
        lines.extend(_partial_match_lines(own, eligible))
        loud.extend(undecided)
        for row in new_rows[: series.MAX_RELATED]:
            lines.append(
                f"{own['number']} {row['number']} {row['author']} {row['reason']} {row['state']}"
            )
        loud.extend(overlaps(new_rows))
    request.ensure_time()
    return lines, loud


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser for the fill and check subcommands."""
    default_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=default_root)
    sub = parser.add_subparsers(dest="command", required=True)
    fill_p = sub.add_parser("fill", help="fill fixes_issues and related into the manifest")
    fill_p.add_argument("--root", type=Path, default=argparse.SUPPRESS)
    fill_p.add_argument("--dry-run", action="store_true", help="print changes without writing")
    fill_p.add_argument("--pull", type=int, action="append", default=[], metavar="N")
    fill_p.add_argument(
        "--max-age-days",
        type=int,
        default=DEFAULT_FILL_MAX_AGE_DAYS,
        metavar="N",
        help=(
            f"read at most N days of candidate updates ({MIN_FILL_MAX_AGE_DAYS}-"
            f"{MAX_FILL_MAX_AGE_DAYS}; default {DEFAULT_FILL_MAX_AGE_DAYS})"
        ),
    )
    check_p = sub.add_parser("check", help="list new related items and fail on overlaps")
    check_p.add_argument("--root", type=Path, default=argparse.SUPPRESS)
    check_p.add_argument("--pull", type=int, action="append", default=[], metavar="N")
    check_p.add_argument(
        "--since", metavar="DATE", help="override the checked-date window (YYYY-MM-DD)"
    )
    return parser


def _caught_errors() -> tuple[type[Exception], ...]:
    """Return all active DriftError and SeriesError classes across import paths."""
    classes: list[type[Exception]] = [DriftError, series.SeriesError, OSError, UnicodeError]
    for mod_name, attr in (
        ("upstream_drift", "DriftError"),
        ("scripts.upstream_drift", "DriftError"),
        ("series", "SeriesError"),
        ("scripts.series", "SeriesError"),
    ):
        mod = sys.modules.get(mod_name)
        exc = getattr(mod, attr, None) if mod is not None else None
        if isinstance(exc, type) and issubclass(exc, Exception) and exc not in classes:
            classes.append(exc)
    return tuple(classes)


def main(
    argv: list[str],
    fetch: Fetch = fetch_forge,
    today: date | None = None,
    *,
    sleep: Sleep | None = None,
) -> int:
    """Run fill/check; return 1 for overlaps, 2 for errors or 3 for published leftovers."""
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code else 0
    try:
        series.pull_url_pattern()
        request = RequestBudget(fetch, request_cap(upstream_drift.MAX_SERIES_ENTRIES), sleep)
        if args.command == "fill":
            changes = _fill_manifest(
                args.root,
                request,
                _FillOptions(
                    dry_run=args.dry_run,
                    today=today,
                    pulls=args.pull,
                    max_age_days=args.max_age_days,
                ),
            )
            # Reserve two cleanup paths and the final publication state beyond change lines.
            for line in changes[: upstream_drift.MAX_SERIES_ENTRIES * (MAX_CANDIDATES + 2) + 3]:
                print(line)
            return PUBLISHED_WITH_LEFTOVERS if changes.cleanup_incomplete else 0
        lines, loud = check_overlaps(args.root, args.pull, request, today=today, since=args.since)
    except _caught_errors() as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    for line in lines[: upstream_drift.MAX_SERIES_ENTRIES * (MAX_CANDIDATES + series.MAX_RELATED)]:
        print(line)
    return 1 if loud else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
