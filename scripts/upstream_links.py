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
another author or undecided file comparison in check, 2 on a read or input error.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from itertools import islice
from pathlib import Path

try:
    import series
    import upstream_drift
except ModuleNotFoundError:
    from scripts import series, upstream_drift

Fetch = Callable[[str], dict | list]
Sleep = Callable[[float], None]
DriftError = upstream_drift.DriftError
REQUEST_TIMEOUT = upstream_drift.REQUEST_TIMEOUT
REQUEST_PAUSE_SECONDS = 0.5
# Revisit a week around the drift snapshot to cover date granularity and late updates.
CHECK_MARGIN_DAYS = 7
# Bound first-fill history even when the manifest contains months-old own pulls.
DEFAULT_FILL_MAX_AGE_DAYS = 14
MIN_FILL_MAX_AGE_DAYS = 1
MAX_FILL_MAX_AGE_DAYS = 365
PAGE_SIZE = 50
MAX_PULL_PAGES = 20
MAX_PULL_ROWS = PAGE_SIZE * MAX_PULL_PAGES
MAX_FILE_PAGES = 20
MAX_FILE_ROWS = PAGE_SIZE * MAX_FILE_PAGES
MAX_CANDIDATES = MAX_PULL_ROWS
# Every clause can contribute an issue; reject overflow rather than lose links.
MAX_ISSUE_MATCHES = series.MAX_FIXES_ISSUES
# Bound repeated references as well as unique IDs to cap matching work.
MAX_ISSUE_REFERENCES = 1024
# Twenty digits accommodate long issue IDs while bounding integer conversion.
MAX_ISSUE_NUMBER_DIGITS = 20
# Permit descriptive forge subjects while bounding stored title and subject text.
MAX_TITLE_CHARS = 4096
# Cover practical source paths, including rename aliases, without unbounded strings.
MAX_PATH_CHARS = 4096
# Account and branch names need much less space than source paths.
MAX_NAME_CHARS = 256
# Permit substantial pull descriptions while bounding regular-expression work.
MAX_BODY_CHARS = 256 * 1024
# ISO timestamps with fractional seconds and offsets fit well below this ceiling.
MAX_TIMESTAMP_CHARS = 64
# Validate all inline names before retaining the documented 1000-name prefix.
MAX_INLINE_FILE_ROWS = 2 * MAX_FILE_ROWS
ISSUE_CLAUSE = re.compile(
    r"\b(?:fixes|closes|refs)\b\s*:?\s*([^\r\n;]+?)"
    r"(?=\b(?:fixes|closes|refs)\b|[\r\n;]|$)",
    re.IGNORECASE,
)
ISSUE_REF = re.compile(
    r"https?://[^\s,;]+|(?:#|\bissues?\s+#?)([1-9][0-9]*)\b"
    r"|(?:^|,\s*|\band\s+|\bor\s+)([1-9][0-9]*)\b",
    re.IGNORECASE,
)


class RequestBudget:
    """Count and pace every injected fetch, failing before a request above the cap."""

    def __init__(self, fetch: Fetch, cap: int, sleep: Sleep | None = None) -> None:
        """Create a fresh run counter with an injectable pause function."""
        self.fetch = fetch
        self.cap = cap
        self.count = 0
        self.window_start = "not yet read"
        self.candidates = 0
        self.narrow_option = "--pull N or check --since YYYY-MM-DD"
        self.sleep = sleep if sleep is not None else time.sleep

    def __call__(self, url: str) -> dict | list:
        """Pause once per admitted request and preserve the underlying fetch deadline."""
        if self.count >= self.cap:
            raise DriftError(
                f"request budget exceeded: count {self.count + 1}, cap {self.cap}; "
                f"window start {self.window_start}; candidates {self.candidates}; "
                f"narrow the window with {self.narrow_option}"
            )
        self.count += 1
        self.sleep(REQUEST_PAUSE_SECONDS)
        return self.fetch(url)


def _budgeted(fetch: Fetch, cap: int) -> RequestBudget:
    """Reuse the CLI run counter or create one for a direct fill or check call."""
    if isinstance(fetch, RequestBudget):
        fetch.cap = min(fetch.cap, cap)
        return fetch
    return RequestBudget(fetch, cap)


class RelatedRow(dict):
    """A related manifest row carrying own-pull and merge context for overlap checks."""

    own_author: str = ""
    own_opened: str = ""
    merged_at: str = ""


class FileNames(list[str]):
    """Bounded file names retaining completeness and the count before deduplication."""

    def __init__(
        self,
        names: list[str] | tuple[str, ...],
        *,
        complete: bool = True,
        read_count: int | None = None,
        file_count: int | None = None,
    ) -> None:
        """Keep file evidence and its read limit together across the run memo."""
        super().__init__(names)
        self.complete = complete
        self.read_count = len(names) if read_count is None else read_count
        self.file_count = len(names) if file_count is None else file_count


@dataclass(frozen=True)
class _FillOptions:
    """Keep fill-only options together without changing the existing public entry point."""

    dry_run: bool = False
    today: date | None = None
    pulls: list[int] | None = None
    max_age_days: int = DEFAULT_FILL_MAX_AGE_DAYS


def _run_day(today: date | None) -> date:
    """Read the local UTC clock once per run unless the fixture supplies its date."""
    return datetime.now(UTC).date() if today is None else today


def fetch_forge(url: str) -> dict | list:
    """Fetch a JSON object or list from the forge with the shared request deadline."""
    return upstream_drift.fetch_json(url, allow_list=True)


def _bounded_text(raw: object, field: str, limit: int) -> str:
    """Require string data within its named character ceiling before storing or parsing."""
    if not isinstance(raw, str):
        raise DriftError(f"invalid {field}: expected string")
    if len(raw) > limit:
        raise DriftError(f"{field} exceeds {limit} characters")
    return raw


def parse_timestamp(raw: object, field: str = "timestamp") -> datetime:
    """Parse an ISO-8601 instant or YYYY-MM-DD date string into a UTC datetime."""
    _bounded_text(raw, field, MAX_TIMESTAMP_CHARS)
    if not raw.strip():
        raise DriftError(f"invalid {field}: {raw!r}")
    text = f"{raw}T00:00:00+00:00" if series.DAY.fullmatch(raw) else raw
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise DriftError(f"invalid {field}: {raw!r}") from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def subject_key(title: str) -> str:
    """Return a title normalized by dropping its component prefix, case and outer space."""
    _bounded_text(title, "subject", MAX_TITLE_CHARS)
    body = title.partition(":")[2] if ":" in title else title
    return body.strip().lower()


def _string_tuple(raw: object, label: str, cap: int) -> tuple[str, ...]:
    """Validate and convert an optional bounded collection of non-empty strings."""
    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple, set)) or len(raw) > cap:
        raise DriftError(f"invalid {label}: expected at most {cap} strings")
    items: list[str] = []
    for item in list(raw)[:cap]:
        if not isinstance(item, str) or not item.strip():
            raise DriftError(f"invalid {label} entry: {item!r}")
        _bounded_text(item, label, MAX_PATH_CHARS)
        items.append(item)
    return tuple(items)


def _item_files(raw: object, number: int, partial: bool) -> FileNames:
    """Validate inline file names, allowing only external candidates a bounded prefix."""
    label = f"pull {number} files"
    if isinstance(raw, FileNames):
        _string_tuple(raw, label, MAX_FILE_ROWS)
        if not partial and not raw.complete:
            raise DriftError(f"{label}: incomplete own pull listing")
        return raw
    if partial and isinstance(raw, (list, tuple, set)):
        names = _string_tuple(raw, label, MAX_INLINE_FILE_ROWS)
        return FileNames(names[:MAX_FILE_ROWS], complete=len(raw) <= MAX_FILE_ROWS)
    return FileNames(_string_tuple(raw, label, MAX_FILE_ROWS))


def _extract_author(raw: dict) -> str:
    """Read and validate the pull or issue author from author or user.login."""
    if "author" in raw:
        author = raw["author"]
    else:
        user = raw.get("user")
        author = user.get("login") if isinstance(user, dict) else None
    if not isinstance(author, str) or not author.strip() or "\n" in author:
        raise DriftError("missing or invalid pull author")
    _bounded_text(author, "user.login", MAX_NAME_CHARS)
    return author


def _extract_state(raw: dict, default_state: str) -> str:
    """Read and validate a pull or issue state from normalized or forge fields."""
    if "merged" in raw:
        if not isinstance(raw["merged"], bool):
            raise DriftError(f"invalid merged flag: {raw['merged']!r}")
        if raw["merged"]:
            return "merged"
    state = raw.get("state", default_state)
    if not isinstance(state, str) or state not in series.FORGE_STATES.values():
        raise DriftError(f"unknown state: {state!r}")
    return state


def normalize_item(
    raw: object, *, default_state: str = "open", partial_files: bool = False
) -> dict[str, object]:
    """Normalize trusted matcher inputs; raw forge rows must use parse_forge_pull."""
    if not isinstance(raw, dict):
        raise DriftError("pull or candidate is not an object")
    number = raw.get("number")
    if type(number) is not int or number <= 0:
        raise DriftError(f"invalid number: {number!r}")
    title = raw.get("title")
    if not isinstance(title, str) or not title.strip():
        raise DriftError(f"pull {number}: missing or invalid title")
    _bounded_text(title, f"pull {number} title", MAX_TITLE_CHARS)
    kind = raw.get("kind", "pull")
    if not isinstance(kind, str) or kind not in series.RELATED_KINDS:
        raise DriftError(f"pull {number}: invalid kind {kind!r}")
    state = _extract_state(raw, default_state)
    created_at = raw.get("created_at", raw.get("opened_at", "")) or ""
    if created_at:
        parse_timestamp(created_at, "created_at")
    merged_at = raw.get("merged_at") or ""
    if merged_at:
        parse_timestamp(merged_at, "merged_at")
    return {
        "number": number,
        "kind": kind,
        "author": _extract_author(raw),
        "title": title,
        "state": state,
        "files": _item_files(raw.get("files"), number, partial_files),
        "patch_ids": _string_tuple(
            raw.get("patch_ids", raw.get("stable_patch_ids")),
            f"pull {number} patch_ids",
            MAX_FILE_ROWS,
        ),
        "created_at": created_at,
        "merged_at": merged_at,
        "seen": "",
    }


def match_reason(own: dict[str, object], candidate: dict[str, object]) -> str | None:
    """Return the relationship reason between an own pull and a candidate, or None."""
    if own["number"] == candidate["number"]:
        return None
    if set(own["patch_ids"]) & set(candidate["patch_ids"]):
        return "same-patch-id"
    if set(own["files"]) & set(candidate["files"]):
        return "same-files"
    own_subject = subject_key(str(own["title"]))
    if own_subject and own_subject == subject_key(str(candidate["title"])):
        return "same-subject"
    return None


def related_for(
    pull: dict, candidates: list[dict], *, today: date | None = None
) -> list[RelatedRow]:
    """Return sorted related rows for one own pull against a bounded candidate list."""
    if not isinstance(candidates, list) or len(candidates) > MAX_CANDIDATES:
        raise DriftError(f"candidates must be a list of at most {MAX_CANDIDATES} items")
    own = normalize_item(pull)
    seen_day = _run_day(today).isoformat()
    by_number: dict[int, RelatedRow] = {}
    for raw in candidates[:MAX_CANDIDATES]:
        cand = normalize_item(raw, partial_files=True)
        reason = match_reason(own, cand)
        if reason is None:
            continue
        num = int(cand["number"])
        row = RelatedRow(
            number=num,
            kind=cand["kind"],
            author=cand["author"],
            reason=reason,
            state=cand["state"],
            seen=seen_day,
        )
        row.own_author = str(own["author"])
        row.own_opened = str(own["created_at"])
        row.merged_at = str(cand["merged_at"])
        by_number[num] = row
    if len(by_number) > series.MAX_RELATED:
        raise DriftError(f"related entries exceed {series.MAX_RELATED}")
    return [by_number[num] for num in sorted(by_number)]


def _merged_after_opened(row: dict) -> bool:
    """Return True when a merged related pull landed after the own pull was opened."""
    own_opened = getattr(row, "own_opened", "") or row.get("own_opened", "")
    merged_at = getattr(row, "merged_at", "") or row.get("merged_at", "")
    if own_opened and merged_at:
        return parse_timestamp(merged_at, "merged_at") > parse_timestamp(own_opened, "created_at")
    return not own_opened and not merged_at


def overlaps(related: list[dict]) -> list[dict]:
    """Return related pulls by another author that are open or merged after the own pull opened."""
    if not isinstance(related, list) or len(related) > series.MAX_RELATED:
        raise DriftError(f"related must be a list of at most {series.MAX_RELATED} items")
    loud: list[dict] = []
    for row in related[: series.MAX_RELATED]:
        if not isinstance(row, dict):
            raise DriftError("related row is not an object")
        if row.get("kind", "pull") != "pull":
            continue
        own_author = getattr(row, "own_author", "") or row.get("own_author", "")
        if own_author and row.get("author") == own_author:
            continue
        state = row.get("state")
        if state == "open" or (state == "merged" and _merged_after_opened(row)):
            loud.append(row)
    return loud


def _issue_reference(match: re.Match[str], local_url: re.Pattern[str]) -> tuple[int | None, bool]:
    """Accept bare issue numbers or exact configured issue URLs without URL digit leakage."""
    token = match.group(1) or match.group(2)
    if token is not None:
        return _issue_number(token), False
    address = match.group(0).rstrip(".")
    local = local_url.fullmatch(address)
    if local is not None:
        return _issue_number(local.group(1)), False
    return None, "/issues/" in address


def _issue_number(token: str) -> int:
    """Reject oversized decimal IDs before integer conversion."""
    if len(token) > MAX_ISSUE_NUMBER_DIGITS:
        raise DriftError(f"body issue number exceeds {MAX_ISSUE_NUMBER_DIGITS} digits")
    return int(token)


def _issue_matches(body: str, number: int | None) -> list[re.Match[str]]:
    """Bound both clause traversal and total reference traversal before recording issues."""
    clauses = ISSUE_CLAUSE.findall(body)
    if len(clauses) > MAX_ISSUE_MATCHES:
        raise DriftError(
            f"pull {number} body issue clauses: {len(clauses)} exceeds {MAX_ISSUE_MATCHES}"
        )
    matches: list[re.Match[str]] = []
    for clause in clauses[:MAX_ISSUE_MATCHES]:
        remaining = MAX_ISSUE_REFERENCES - len(matches)
        matches.extend(islice(ISSUE_REF.finditer(clause.strip()), remaining + 1))
        if len(matches) > MAX_ISSUE_REFERENCES:
            raise DriftError(
                f"pull {number} body issue references: {len(matches)} "
                f"exceeds {MAX_ISSUE_REFERENCES}"
            )
    return matches


def body_issues(body: object, *, number: int | None = None) -> list[int]:
    """Extract local issues and print one foreign-address count without storing those URLs."""
    if body is None:
        return []
    _bounded_text(body, f"pull {number} body", MAX_BODY_CHARS)
    local = upstream_drift.repo_api_url().replace("/api/v1/repos/", "/")
    local_url = re.compile(re.escape(local) + r"/issues/([1-9][0-9]*)")
    numbers: set[int] = set()
    ignored = 0
    try:
        for match in _issue_matches(body, number)[:MAX_ISSUE_REFERENCES]:
            issue, foreign = _issue_reference(match, local_url)
            ignored += int(foreign)
            if issue is not None:
                numbers.add(issue)
            if len(numbers) > series.MAX_FIXES_ISSUES:
                raise DriftError(f"fixes_issues entries exceed {series.MAX_FIXES_ISSUES}")
    except DriftError as exc:
        raise DriftError(f"pull {number} body: {exc}") from exc
    if ignored:
        print(f"{number}: ignored {ignored} foreign issue addresses")
    return sorted(numbers)


def pull_api_url(number: int, url: str = "") -> str:
    """Return the Forgejo API URL for pull request number, validating url when given."""
    if type(number) is not int or number <= 0:
        raise DriftError(f"pull request number must be a positive integer: {number!r}")
    if url:
        return upstream_drift.api_url(url, number)
    return f"{upstream_drift.repo_api_url()}/pulls/{number}"


def _file_names_from_row(row: object, url: str) -> list[str]:
    """Extract filename and optional previous_filename from one Forgejo pull-file object."""
    if not isinstance(row, dict):
        raise DriftError(f"{url}: file entry is not an object")
    filename = row.get("filename")
    if not isinstance(filename, str) or not filename.strip():
        raise DriftError(f"{url}: file entry lacks a valid filename")
    _bounded_text(filename, f"{url} filename", MAX_PATH_CHARS)
    names = [filename]
    previous = row.get("previous_filename")
    if previous is not None:
        if not isinstance(previous, str):
            raise DriftError(f"{url}: invalid previous_filename")
        _bounded_text(previous, f"{url} previous_filename", MAX_PATH_CHARS)
        if previous.strip():
            names.append(previous)
    return names


def _file_result(
    files: FileNames, complete: bool, partial: bool, pull_url: str, error: str
) -> FileNames:
    """Preserve partial external evidence while rejecting incomplete own listings."""
    if not complete and not partial:
        raise DriftError(f"{pull_url}/files: {error}")
    return FileNames(
        sorted(set(files)), complete=complete, read_count=len(files), file_count=files.file_count
    )


def read_pull_files(pull_url: str, fetch: Fetch, *, partial: bool = False) -> FileNames:
    """Read bounded file pages, retaining a partial prefix only for external candidates."""
    files = FileNames([])
    for page in range(1, MAX_FILE_PAGES + 1):
        url = f"{pull_url}/files?limit={PAGE_SIZE}&page={page}"
        batch = fetch(url)
        if not isinstance(batch, list) or len(batch) > PAGE_SIZE:
            raise DriftError(f"{url}: expected a JSON list of at most {PAGE_SIZE} file entries")
        names = [name for row in batch[:PAGE_SIZE] for name in _file_names_from_row(row, url)[:2]]
        files.file_count += len(batch)
        remaining = MAX_FILE_ROWS - len(files)
        files.extend(names[:remaining])
        if len(names) > remaining:
            return _file_result(
                files, False, partial, pull_url, f"file entries exceed {MAX_FILE_ROWS}"
            )
        if len(batch) < PAGE_SIZE:
            return _file_result(files, True, partial, pull_url, "")
        if len(files) == MAX_FILE_ROWS:
            break
    return _file_result(files, False, partial, pull_url, f"exceeds {MAX_FILE_PAGES} pages")


def _forge_time(raw: object, field: str) -> None:
    """Require a complete forge timestamp with an explicit UTC offset."""
    _bounded_text(raw, field, MAX_TIMESTAMP_CHARS)
    if not re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
        r"(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})",
        raw,
    ):
        raise DriftError(f"invalid {field}: expected timestamp with timezone")
    parse_timestamp(raw, field)


def _forge_branch_fields(raw: dict) -> None:
    """Require the head commit and base branch instead of trusting malformed objects."""
    head, base = raw["head"], raw["base"]
    if not isinstance(head, dict) or not isinstance(head.get("sha"), str):
        raise DriftError("invalid head.sha")
    _bounded_text(head["sha"], "head.sha", MAX_NAME_CHARS)
    if not series.COMMIT.fullmatch(head["sha"]):
        raise DriftError("invalid head.sha: expected full commit")
    if not isinstance(base, dict) or not isinstance(base.get("ref"), str):
        raise DriftError("invalid base.ref")
    _bounded_text(base["ref"], "base.ref", MAX_NAME_CHARS)
    if not base["ref"].strip():
        raise DriftError("invalid base.ref: expected nonblank branch")


def _require_forge_envelope(raw: object) -> dict:
    """Require forge fields before any chronological stop or normalized matching."""
    if not isinstance(raw, dict):
        raise DriftError("pull request answer is not a JSON object")
    for key in (
        "number",
        "title",
        "user",
        "merged",
        "state",
        "created_at",
        "updated_at",
        "merged_at",
        "head",
        "base",
        "body",
    ):
        if key not in raw:
            raise DriftError(f"missing {key}")
    user = raw["user"]
    if not isinstance(user, dict) or not isinstance(user.get("login"), str):
        raise DriftError("invalid user.login")
    if not isinstance(raw["merged"], bool):
        raise DriftError("invalid merged: expected boolean")
    if raw["state"] not in ("open", "closed"):
        raise DriftError("invalid state: expected open or closed")
    _forge_time(raw["created_at"], "created_at")
    _forge_time(raw["updated_at"], "updated_at")
    if raw["merged"] or raw["merged_at"] is not None:
        _forge_time(raw["merged_at"], "merged_at")
    _forge_branch_fields(raw)
    return raw


def _forge_files(raw: dict, partial: bool) -> FileNames:
    """Read forge names only from a list and validate the separate changed-file count."""
    count = raw.get("changed_files")
    if "changed_files" in raw and (type(count) is not int or count < 0):
        raise DriftError(
            f"invalid changed_files: found {type(count).__name__}; expected nonnegative integer"
        )
    names = raw.get("files")
    if "files" in raw and not isinstance(names, list):
        raise DriftError(f"invalid files: found {type(names).__name__}; expected list")
    return _item_files(names, raw["number"], partial)


def _parse_forge_pull(raw: object, partial_files: bool) -> dict[str, object]:
    """Normalize only allow-listed forge fields after validating required metadata."""
    checked = _require_forge_envelope(raw)
    names = _forge_files(checked, partial_files)
    trusted = {
        key: checked[key]
        for key in ("number", "title", "user", "merged", "state", "created_at", "merged_at")
    }
    item = normalize_item(trusted | {"files": names}, partial_files=partial_files)
    item["inline_files"] = "files" in checked
    item["changed_files"] = checked.get("changed_files")
    body = checked["body"]
    if body is not None and not isinstance(body, str):
        raise DriftError(f"pull {item['number']}: invalid body")
    if body is not None:
        _bounded_text(body, "body", MAX_BODY_CHARS)
        _issue_matches(body, int(item["number"]))
    item["updated_at"] = checked["updated_at"]
    item["body"] = "" if body is None else body
    return item


def parse_forge_pull(
    raw: object, *, partial_files: bool = False, source: str = ""
) -> dict[str, object]:
    """Validate forge metadata and identify its pull or source address on any error."""
    try:
        return _parse_forge_pull(raw, partial_files)
    except DriftError as exc:
        where = source
        if isinstance(raw, dict):
            number = raw.get("number")
            if type(number) is int and number > 0:
                address = pull_api_url(number)
                where = f"{where}: {address}" if where and where != address else address
        raise DriftError(f"{where or 'pull request'}: {exc}") from exc


def read_candidate_pulls(since: str, fetch: Fetch) -> list[dict[str, object]]:
    """Read candidate metadata once, stopping at updates older than the run window."""
    since_dt = parse_timestamp(since, "created_at")
    repo_url = upstream_drift.repo_api_url()
    candidates: list[dict[str, object]] = []
    seen_numbers: set[int] = set()
    for page in range(1, MAX_PULL_PAGES + 1):
        url = f"{repo_url}/pulls?state=all&sort=recentupdate&limit={PAGE_SIZE}&page={page}"
        batch = fetch(url)
        if not isinstance(batch, list) or len(batch) > PAGE_SIZE:
            raise DriftError(f"{url}: expected a JSON list of at most {PAGE_SIZE} pull requests")
        reached_older = False
        for raw in batch[:PAGE_SIZE]:
            item = parse_forge_pull(raw, partial_files=True, source=url)
            if parse_timestamp(item["updated_at"], "updated_at") < since_dt:
                reached_older = True
                continue
            num = int(item["number"])
            if num in seen_numbers:
                continue
            if len(candidates) >= MAX_PULL_ROWS:
                raise DriftError(f"{repo_url}/pulls: candidate pulls exceed {MAX_PULL_ROWS}")
            seen_numbers.add(num)
            candidates.append(item)
            if isinstance(fetch, RequestBudget):
                fetch.candidates = len(candidates)
        if len(batch) < PAGE_SIZE or reached_older:
            return candidates
    raise DriftError(f"{repo_url}/pulls: exceeds {MAX_PULL_PAGES} pages")


def memoized_files(
    number: int, fetch: Fetch, files: dict[int, FileNames], *, partial: bool = False
) -> FileNames:
    """Memoize bounded names together with completeness without repeating file-page reads."""
    if number not in files:
        files[number] = read_pull_files(pull_api_url(number), fetch, partial=partial)
    return _item_files(files[number], number, partial)


def _resolved_files(
    item: dict, fetch: Fetch, files: dict[int, FileNames], *, partial: bool = False
) -> FileNames:
    """Resolve file names once without trusting the forge's separate count."""
    number = int(item["number"])
    if item["inline_files"]:
        files.setdefault(number, item["files"])
    return memoized_files(number, fetch, files, partial=partial)


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
            f"cap {fetch.cap}; same-patch-id not checked live"
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
    if len(pulls) > cap or any(type(num) is not int or num <= 0 for num in pulls[:cap]):
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
    if type(max_age_days) is not int or not (
        MIN_FILL_MAX_AGE_DAYS <= max_age_days <= MAX_FILL_MAX_AGE_DAYS
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


def _write_manifest_temp(path: Path, original: bytes, rendered: bytes) -> None:
    """Sync a sibling temporary file and compare again immediately before replacement."""
    staging: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, delete=False
        ) as stream:
            staging = Path(stream.name)
            stream.write(rendered)
            stream.flush()
            os.fsync(stream.fileno())
        if path.read_bytes() != original:
            raise DriftError(f"{path}: changed since read; no changes written")
        os.replace(staging, path)
    finally:
        if staging is not None:
            staging.unlink(missing_ok=True)


def _replace_manifest(path: Path, original: bytes, rendered: bytes) -> None:
    """Serialize cooperating fills and reject changed content before atomic publication."""
    lock = path.with_name(f".{path.name}.lock")
    try:
        stream = lock.open("xb")
    except FileExistsError as exc:
        raise DriftError(f"{path}: another fill holds {lock.name}; no changes written") from exc
    try:
        with stream:
            if path.read_bytes() != original:
                raise DriftError(f"{path}: changed since read; no changes written")
            _write_manifest_temp(path, original, rendered)
    finally:
        lock.unlink()


def _fill_manifest(root: Path, fetch: Fetch, options: _FillOptions) -> list[str]:
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
    changes = _fill_window_lines(own_pulls, cutoff)
    for own in own_pulls[: upstream_drift.MAX_SERIES_ENTRIES]:
        eligible = inspect_candidates(own, candidates, set(), files, request)
        related = related_for(own, eligible, today=run_day)
        changes.extend(_undecided_lines(own, _undecided_for(own, eligible)))
        changes.extend(_partial_match_lines(own, eligible))
        line = _fill_entry(by_number[own["number"]], own, related)
        if line:
            changes.append(line)
    if changes and not options.dry_run:
        if path.read_bytes() != original:
            raise DriftError(f"{path}: changed since read; no changes written")
        rendered = (json.dumps(data, indent=2, ensure_ascii=original.isascii()) + "\n").encode(
            "utf-8"
        )
        _replace_manifest(path, original, rendered)
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
        if type(num) is not int or num <= 0:
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
    """Run fill or check; exit 1 on overlaps or undecided checks, 2 on read or input errors."""
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
            for line in changes[: upstream_drift.MAX_SERIES_ENTRIES * (MAX_CANDIDATES + 2)]:
                print(line)
            return 0
        lines, loud = check_overlaps(args.root, args.pull, request, today=today, since=args.since)
    except _caught_errors() as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    for line in lines[: upstream_drift.MAX_SERIES_ENTRIES * (MAX_CANDIDATES + series.MAX_RELATED)]:
        print(line)
    return 1 if loud else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
