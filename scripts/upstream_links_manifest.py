# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Fill and atomically publish upstream contribution manifests."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

if not __package__:
    import series
    import upstream_drift
    from upstream_links_forge import Fetch, RequestBudget, _budgeted, related_for
    from upstream_links_parse import (
        MAX_NAME_CHARS,
        MAX_PATH_CHARS,
        MAX_TITLE_CHARS,
        DriftError,
        RelatedRow,
        _bounded_text,
        _run_day,
        body_issues,
        parse_timestamp,
    )
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
    from scripts.upstream_links_forge import Fetch, RequestBudget, _budgeted, related_for
    from scripts.upstream_links_parse import (
        MAX_NAME_CHARS,
        MAX_PATH_CHARS,
        MAX_TITLE_CHARS,
        DriftError,
        RelatedRow,
        _bounded_text,
        _run_day,
        body_issues,
        parse_timestamp,
    )
    from scripts.upstream_links_scan import (
        _partial_match_lines,
        _undecided_for,
        _undecided_lines,
        inspect_candidates,
        read_run_pulls,
        request_cap,
    )


PUBLISHED_WITH_LEFTOVERS = upstream_drift.PUBLISHED_WITH_LEFTOVERS
# Bound first-fill history even when the manifest contains months-old own pulls.
DEFAULT_FILL_MAX_AGE_DAYS = 14
MIN_FILL_MAX_AGE_DAYS = 1
MAX_FILL_MAX_AGE_DAYS = 365


class _FillChanges(list[str]):
    """Carry publication cleanup state without changing the public list interface."""

    cleanup_incomplete: bool = False


@dataclass(frozen=True)
class _FillOptions:
    """Keep fill-only options together without changing the existing public entry point."""

    dry_run: bool = False
    today: date | None = None
    pulls: list[int] | None = None
    max_age_days: int = DEFAULT_FILL_MAX_AGE_DAYS


def _manifest_text_limits(data: dict) -> None:
    """Apply stored text ceilings after schema and retained discovery-date validation."""
    for entry in data["contributions"][: upstream_drift.MAX_SERIES_ENTRIES]:
        number = entry["number"]
        for field, cap in (
            ("title", MAX_TITLE_CHARS),
            ("base", MAX_NAME_CHARS),
            ("patch", MAX_PATH_CHARS),
        ):
            _bounded_text(entry[field], f"pull {number} {field}", cap)
        for row in entry.get("related", [])[: series.MAX_RELATED]:
            _bounded_text(
                row["author"], f"pull {number} related {row['number']} author", MAX_NAME_CHARS
            )


def _checked_manifest(root: Path) -> dict:
    """Validate and return the contribution manifest data in root."""
    findings = series.Findings()
    names = series.read_series(root, findings) if (root / "series.txt").is_file() else []
    if len(names) > upstream_drift.MAX_SERIES_ENTRIES:
        raise DriftError(f"series entries exceed {upstream_drift.MAX_SERIES_ENTRIES}")
    raw = series.read_contributions(root, findings)
    if raw is not None and len(raw["contributions"]) > upstream_drift.MAX_SERIES_ENTRIES:
        raise DriftError(f"contribution entries exceed {upstream_drift.MAX_SERIES_ENTRIES}")
    data = series.checked_contributions(root, names, findings)
    if data is None or findings.errors:
        raise DriftError("; ".join(findings.errors) or f"{series.CONTRIBUTIONS} is unsound")
    _manifest_text_limits(data)
    return data


def merge_related(existing: list[dict], fresh: list[RelatedRow]) -> list[dict]:
    """Merge freshly discovered related rows with existing manifest rows, keeping seen dates."""
    if len(existing) > series.MAX_RELATED or len(fresh) > series.MAX_RELATED:
        raise DriftError(f"related entries exceed {series.MAX_RELATED}")
    by_number: dict[int, dict] = {
        int(row["number"]): dict(row) for row in existing[: series.MAX_RELATED]
    }
    for row in fresh[: series.MAX_RELATED]:
        num = int(row["number"])
        plain = {key: row[key] for key in series.RELATED_KEYS}
        if num in by_number:
            plain["seen"] = by_number[num]["seen"]
        by_number[num] = plain
    if len(by_number) > series.MAX_RELATED:
        raise DriftError(f"related entries exceed {series.MAX_RELATED}")
    return [by_number[num] for num in sorted(by_number)]


def _fill_targets(entries: list[dict], pulls: list[int]) -> list[tuple[int, str, set[int]]]:
    """Limit fill to eligible manifest rows and reject invalid selections before fetching."""
    cap = upstream_drift.MAX_SERIES_ENTRIES
    if len(pulls) > cap or any(
        not isinstance(num, int) or isinstance(num, bool) or num <= 0 for num in pulls[:cap]
    ):
        raise DriftError(f"--pull requires at most {cap} positive numbers")
    eligible = {int(e["number"]): e for e in entries[:cap] if e["state"] in ("open", "merged")}
    requested = set(pulls) or set(eligible)
    missing = requested - set(eligible)
    if missing:
        raise DriftError(f"--pull not an open or merged manifest row: {sorted(missing)}")
    return [(num, eligible[num]["url"], set()) for num in sorted(requested)[:cap]]


def _fill_entry(entry: dict, own: dict, related: list[RelatedRow]) -> str:
    """Merge discovered links into an entry and return a diagnostic only when changed."""
    merged_issues = sorted(
        set(entry.get("fixes_issues", []))
        | set(body_issues(own["body"], number=int(own["number"])))
    )
    if len(merged_issues) > series.MAX_FIXES_ISSUES:
        raise DriftError(f"fixes_issues entries exceed {series.MAX_FIXES_ISSUES}")
    merged_rel = merge_related(entry.get("related", []), related)
    if entry.get("fixes_issues") == merged_issues and entry.get("related") == merged_rel:
        return ""
    entry["fixes_issues"] = merged_issues
    entry["related"] = merged_rel
    return (
        f"{entry['number']}: fixes_issues={json.dumps(merged_issues)} "
        f"related={json.dumps(merged_rel)}"
    )


def fill_window(max_age_days: int, today: date | None = None) -> datetime:
    """Resolve the bounded fill age to UTC midnight, rejecting invalid ages and underflow."""
    if (
        not isinstance(max_age_days, int)
        or isinstance(max_age_days, bool)
        or not (MIN_FILL_MAX_AGE_DAYS <= max_age_days <= MAX_FILL_MAX_AGE_DAYS)
    ):
        raise DriftError(
            f"--max-age-days must be an integer from {MIN_FILL_MAX_AGE_DAYS} "
            f"through {MAX_FILL_MAX_AGE_DAYS}: {max_age_days!r}"
        )
    day = _run_day(today)
    try:
        return parse_timestamp(day.isoformat(), "fill date") - timedelta(days=max_age_days)
    except OverflowError as exc:
        raise DriftError(f"{day}: fill window exceeds calendar range") from exc


def _fill_window_lines(own_pulls: list[dict], cutoff: datetime) -> list[str]:
    """Name each old own pull whose potentially eligible history was not fully read."""
    return [
        f"{own['number']}: candidates updated before {cutoff.isoformat()} not read (fill age limit)"
        for own in own_pulls[: upstream_drift.MAX_SERIES_ENTRIES]
        if parse_timestamp(own["created_at"], "created_at") < cutoff
    ]


def _write_manifest_temp(
    path: Path, original: bytes, rendered: bytes, request: RequestBudget
) -> list[str]:
    """Sync a sibling; defer Exception, KeyboardInterrupt and SystemExit until cleanup."""
    staging: Path | None = None
    leftovers: list[str] = []
    failure: BaseException | None = None
    try:
        request.ensure_time()
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, delete=False
        ) as stream:
            staging = Path(stream.name)
            stream.write(rendered)
            stream.flush()
            os.fsync(stream.fileno())
            staging.chmod(path.stat().st_mode & 0o777)
        if path.read_bytes() != original:
            raise DriftError(f"{path}: changed since read; no changes written")
        request.ensure_time()
        os.replace(staging, path)
    except (Exception, KeyboardInterrupt, SystemExit) as exc:
        failure = exc
    finally:
        if staging is not None:
            upstream_drift.cleanup_manifest_path(staging, leftovers)
    if failure is not None:
        upstream_drift.raise_write_failure(failure, leftovers)
    return leftovers


def _replace_manifest(
    path: Path, original: bytes, rendered: bytes, request: RequestBudget
) -> list[str]:
    """Serialize fills; defer Exception, KeyboardInterrupt and SystemExit until lock cleanup."""
    request.ensure_time()
    lock = path.with_name(f".{path.name}.lock")
    try:
        stream = lock.open("xb")
    except FileExistsError as exc:
        raise DriftError(
            f"{path}: another fill holds {lock}; no changes written; "
            f"stale only after you confirm no fill and no --fix repair is running "
            f"for this manifest; then review and remove {lock} before retrying"
        ) from exc
    published = False
    leftovers: list[str] = []
    failure: BaseException | None = None
    try:
        with stream:
            if path.read_bytes() != original:
                raise DriftError(f"{path}: changed since read; no changes written")
        leftovers.extend(_write_manifest_temp(path, original, rendered, request))
        published = True
    except (Exception, KeyboardInterrupt, SystemExit) as exc:
        failure = exc
    finally:
        upstream_drift.cleanup_manifest_path(lock, leftovers)
    if failure is not None:
        upstream_drift.raise_write_failure(failure, leftovers)
    if published and leftovers:
        leftovers.append(f"published {path}; cleanup incomplete; exit {PUBLISHED_WITH_LEFTOVERS}")
    return leftovers


def _fill_manifest(root: Path, fetch: Fetch, options: _FillOptions) -> _FillChanges:
    """Close the single scan worker after a successful fill or any failure."""
    request = _budgeted(fetch, request_cap(upstream_drift.MAX_SERIES_ENTRIES))
    try:
        return _fill_records(root, request, options)
    finally:
        request.reader.close()


def _fill_records(root: Path, fetch: Fetch, options: _FillOptions) -> _FillChanges:
    """Fill selected rows within an age and request budget, naming unread older history."""
    run_day = _run_day(options.today)
    cutoff = fill_window(options.max_age_days, run_day)
    path = root / series.CONTRIBUTIONS
    original = path.read_bytes() if path.is_file() else b""
    data = _checked_manifest(root)
    targets = _fill_targets(data["contributions"], options.pulls or [])
    request = _budgeted(fetch, request_cap(len(targets)))
    request.narrow_option = "fill --max-age-days N (shorten the window) or --pull N"
    own_pulls, candidates, files = read_run_pulls(targets, request, cutoff)
    by_number = {e["number"]: e for e in data["contributions"][: upstream_drift.MAX_SERIES_ENTRIES]}
    changes = _FillChanges(_fill_window_lines(own_pulls, cutoff))
    records_changed = False
    for own in own_pulls[: upstream_drift.MAX_SERIES_ENTRIES]:
        eligible = inspect_candidates(own, candidates, set(), files, request)
        related = related_for(own, eligible, today=run_day)
        changes.extend(_undecided_lines(own, _undecided_for(own, eligible)))
        changes.extend(_partial_match_lines(own, eligible))
        line = _fill_entry(by_number[own["number"]], own, related)
        if line:
            changes.append(line)
            records_changed = True
    request.ensure_time()
    if records_changed and not options.dry_run:
        if path.read_bytes() != original:
            raise DriftError(f"{path}: changed since read; no changes written")
        rendered = (json.dumps(data, indent=2, ensure_ascii=original.isascii()) + "\n").encode(
            "utf-8"
        )
        leftovers = _replace_manifest(path, original, rendered, request)
        changes.cleanup_incomplete = bool(leftovers)
        changes.extend(leftovers)
    return changes


def fill_manifest(
    root: Path,
    fetch: Fetch,
    *,
    dry_run: bool = False,
    today: date | None = None,
    pulls: list[int] | None = None,
) -> list[str]:
    """Fill selected rows using the default age limit, preserving the public call signature."""
    return _fill_manifest(root, fetch, _FillOptions(dry_run=dry_run, today=today, pulls=pulls))
