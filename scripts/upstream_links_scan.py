# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Plan upstream requests and inspect pull overlap candidates."""

from __future__ import annotations

from datetime import datetime

if not __package__:
    import upstream_drift
    from upstream_links_forge import (
        MAX_CANDIDATES,
        MAX_FILE_PAGES,
        MAX_PULL_PAGES,
        Fetch,
        RequestBudget,
        _resolved_files,
        parse_forge_pull,
        pull_api_url,
        read_candidate_pulls,
    )
    from upstream_links_parse import DriftError, FileNames, overlaps, parse_timestamp
else:
    from scripts import upstream_drift
    from scripts.upstream_links_forge import (
        MAX_CANDIDATES,
        MAX_FILE_PAGES,
        MAX_PULL_PAGES,
        Fetch,
        RequestBudget,
        _resolved_files,
        parse_forge_pull,
        pull_api_url,
        read_candidate_pulls,
    )
    from scripts.upstream_links_parse import DriftError, FileNames, overlaps, parse_timestamp


def request_cap(own_pulls: int) -> int:
    """Bound metadata, list pages and all accepted own and candidate file pages."""
    return own_pulls + MAX_PULL_PAGES + (own_pulls + MAX_CANDIDATES) * MAX_FILE_PAGES


def _candidate_eligible(own: dict, candidate: dict, recorded: set[int]) -> bool:
    """Apply the existing overlap predicate and per-target exclusions before file I/O."""
    number = int(candidate["number"])
    if number == own["number"] or number in recorded:
        return False
    opened = parse_timestamp(own["created_at"], "created_at")
    if parse_timestamp(candidate["updated_at"], "updated_at") < opened:
        return False
    row = candidate | {"own_author": own["author"], "own_opened": own["created_at"]}
    return bool(overlaps([row]))


def _print_request_plan(
    own: list[dict],
    candidates: list[dict],
    targets: list[tuple[int, str, set[int]]],
    fetch: Fetch,
    cutoff: datetime,
) -> None:
    """Print one-page cost and a file-page upper bound before the first files request."""
    needed = {int(pull["number"]): pull for pull in own[: upstream_drift.MAX_SERIES_ENTRIES]}
    eligible: set[int] = set()
    for pull, (_, _, recorded) in zip(
        own[: upstream_drift.MAX_SERIES_ENTRIES],
        targets[: upstream_drift.MAX_SERIES_ENTRIES],
        strict=True,
    ):
        for candidate in candidates[:MAX_CANDIDATES]:
            if _candidate_eligible(pull, candidate, recorded):
                number = int(candidate["number"])
                needed.setdefault(number, candidate)
                eligible.add(number)
    listings = sum(
        not item["inline_files"]
        for item in list(needed.values())[: MAX_CANDIDATES + upstream_drift.MAX_SERIES_ENTRIES]
    )
    if isinstance(fetch, RequestBudget):
        fetch.candidates = len(candidates)
        print(
            f"window {cutoff.isoformat()}: candidates {len(candidates)}, "
            f"file candidates {len(eligible)}; expected requests {fetch.count + listings} "
            f"(one page per file listing), at most {fetch.count + listings * MAX_FILE_PAGES}; "
            f"cap {fetch.cap}; same-patch-id not checked live",
            flush=True,
        )


def inspect_own_pull(
    number: int,
    url: str,
    fetch: Fetch,
    files: dict[int, FileNames],
    *,
    metadata_only: bool = False,
) -> dict[str, object]:
    """Read an own pull, optionally deferring file resolution until after run planning."""
    p_url = pull_api_url(number, url)
    own = parse_forge_pull(fetch(p_url), source=p_url)
    if own["number"] != number:
        raise DriftError(f"{p_url}: pull number {own['number']} disagrees with {number}")
    if not metadata_only:
        own["files"] = _resolved_files(own, fetch, files)
    return own


def read_run_pulls(
    targets: list[tuple[int, str, set[int]]], fetch: Fetch, since: datetime | None = None
) -> tuple[list[dict], list[dict], dict[int, FileNames]]:
    """Plan one bounded metadata window before reading and memoizing own file names."""
    if len(targets) > upstream_drift.MAX_SERIES_ENTRIES:
        raise DriftError(f"targets exceed {upstream_drift.MAX_SERIES_ENTRIES}")
    files: dict[int, FileNames] = {}
    own = [
        inspect_own_pull(number, url, fetch, files, metadata_only=True)
        for number, url, _ in targets[: upstream_drift.MAX_SERIES_ENTRIES]
    ]
    if not own:
        return [], [], files
    earliest = min(
        parse_timestamp(pull["created_at"], "created_at")
        for pull in own[: upstream_drift.MAX_SERIES_ENTRIES]
    )
    cutoff = max(earliest, since) if since is not None else earliest
    if isinstance(fetch, RequestBudget):
        fetch.window_start = cutoff.isoformat()
    candidates = read_candidate_pulls(cutoff.isoformat(), fetch)
    _print_request_plan(own, candidates, targets, fetch, cutoff)
    for pull in own[: upstream_drift.MAX_SERIES_ENTRIES]:
        pull["files"] = _resolved_files(pull, fetch, files)
    return own, candidates, files


def inspect_candidates(
    own: dict,
    candidates: list[dict],
    recorded: set[int],
    files: dict[int, FileNames],
    fetch: Fetch,
) -> list[dict]:
    """Read files only for unrecorded candidates that can overlap this own pull."""
    eligible: list[dict] = []
    for candidate in candidates[:MAX_CANDIDATES]:
        if not _candidate_eligible(own, candidate, recorded):
            continue
        candidate["files"] = _resolved_files(candidate, fetch, files, partial=True)
        eligible.append(candidate)
    return eligible


def _undecided_for(own: dict, candidates: list[dict]) -> list[dict]:
    """Keep partial file nonmatches visible without asserting a manifest relationship."""
    undecided: list[dict] = []
    for candidate in candidates[:MAX_CANDIDATES]:
        names = candidate["files"]
        if names.complete or set(own["files"]) & set(names):
            continue
        undecided.append(
            {
                "number": candidate["number"],
                "author": candidate["author"],
                "files_read": names.read_count,
            }
        )
    return undecided


def _undecided_lines(own: dict, undecided: list[dict]) -> list[str]:
    """Format one diagnostic per partial nonmatch, identifying both pulls and the read count."""
    return [
        f"{own['number']} {row['number']} {row['author']} same-files undecided: "
        f"{row['files_read']} files read"
        for row in undecided[:MAX_CANDIDATES]
    ]


def _partial_match_lines(own: dict, candidates: list[dict]) -> list[str]:
    """Disclose retained file prefixes even when they already prove a shared path."""
    return [
        f"{own['number']} {row['number']}: files incomplete: {row['files'].read_count} "
        "names read; same-files proven from prefix"
        for row in candidates[:MAX_CANDIDATES]
        if not row["files"].complete and set(own["files"]) & set(row["files"])
    ]
