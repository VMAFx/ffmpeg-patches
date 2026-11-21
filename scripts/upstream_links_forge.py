# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Fetch bounded forge pages within a shared scan budget."""

from __future__ import annotations

import contextlib
import queue
import re
import threading
import time
from collections.abc import Callable
from datetime import date

if not __package__:
    import series
    import upstream_drift
    from upstream_links_parse import (
        MAX_BODY_CHARS,
        MAX_NAME_CHARS,
        MAX_PATH_CHARS,
        MAX_TIMESTAMP_CHARS,
        MAX_TITLE_CHARS,
        DriftError,
        FileNames,
        RelatedRow,
        _bounded_text,
        _extract_author,
        _extract_state,
        _issue_matches,
        _listing_complete,
        _run_day,
        _string_tuple,
        match_reason,
        parse_timestamp,
    )
else:
    from scripts import series, upstream_drift
    from scripts.upstream_links_parse import (
        MAX_BODY_CHARS,
        MAX_NAME_CHARS,
        MAX_PATH_CHARS,
        MAX_TIMESTAMP_CHARS,
        MAX_TITLE_CHARS,
        DriftError,
        FileNames,
        RelatedRow,
        _bounded_text,
        _extract_author,
        _extract_state,
        _issue_matches,
        _listing_complete,
        _run_day,
        _string_tuple,
        match_reason,
        parse_timestamp,
    )


Fetch = Callable[[str], dict | list]
Sleep = Callable[[float], None]
REQUEST_TIMEOUT = upstream_drift.REQUEST_TIMEOUT
REQUEST_PAUSE_SECONDS = 0.5
# Reserve a complete network timeout before each request; CI allows twice this budget.
SCAN_BUDGET_SECONDS = 300
PAGE_SIZE = 50
MAX_PULL_PAGES = 20
MAX_PULL_ROWS = PAGE_SIZE * MAX_PULL_PAGES
MAX_FILE_PAGES = 20
MAX_FILE_ROWS = PAGE_SIZE * MAX_FILE_PAGES
MAX_CANDIDATES = MAX_PULL_ROWS
# Validate all inline names before retaining the documented 1000-name prefix.
MAX_INLINE_FILE_ROWS = 2 * MAX_FILE_ROWS


class _ScanReader:
    """Own one daemon fetch worker and two single-entry queues for a bounded scan."""

    def __init__(self, fetch: Fetch, cap: int) -> None:
        """Start one worker, retaining no reference from that worker to its owner."""
        self.tasks: queue.Queue = queue.Queue(maxsize=1)
        self.answers: queue.Queue = queue.Queue(maxsize=1)
        self.closed = threading.Event()
        threading.Thread(
            target=_fetch_worker,
            args=(fetch, cap, self.tasks, self.answers, self.closed),
            daemon=True,
        ).start()

    def read(
        self, url: str, remaining: float, admit: Callable[[], object] | None = None
    ) -> dict | list:
        """Wait for this response only until the scan's remaining elapsed budget."""
        self.tasks.put_nowait((url, admit))
        try:
            answer = self.answers.get(timeout=remaining)
        except queue.Empty as exc:
            raise DriftError(
                f"incomplete scan: {SCAN_BUDGET_SECONDS}-second elapsed-time budget exhausted "
                f"during {url}"
            ) from exc
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def close(self) -> None:
        """Stop an idle worker or let the sole outstanding read finish before stopping."""
        self.closed.set()
        # A full queue already wakes the worker, which discards its task when closed.
        with contextlib.suppress(queue.Full):
            self.tasks.put_nowait(None)


def _fetch_worker(
    fetch: Fetch, cap: int, tasks: queue.Queue, answers: queue.Queue, closed: threading.Event
) -> None:
    """Serve bounded requests, forwarding Exception, KeyboardInterrupt and SystemExit to readers."""
    for _ in range(cap):
        if closed.is_set():
            return
        try:
            task = tasks.get(timeout=SCAN_BUDGET_SECONDS)
        except queue.Empty:
            return
        if task is None or closed.is_set():
            return
        url, admit = task
        try:
            with upstream_drift.redirect_admit(admit):
                answer = fetch(url)
        except (Exception, KeyboardInterrupt, SystemExit) as exc:
            answer = exc
        answers.put_nowait(answer)


class RequestBudget:
    """Count and pace every injected fetch, failing before a request above the cap."""

    def __init__(self, fetch: Fetch, cap: int, sleep: Sleep | None = None) -> None:
        """Create a fresh run counter with an injectable pause function."""
        self.deadline = time.monotonic() + SCAN_BUDGET_SECONDS
        self.reader = _ScanReader(fetch, cap)
        self.cap = cap
        self.count = 0
        self.window_start = "not yet read"
        self.candidates = 0
        self.narrow_option = "--pull N or check --since YYYY-MM-DD"
        self.sleep = sleep if sleep is not None else time.sleep

    def admit(self) -> float:
        """Count and pace one network request, reserving a full operation timeout."""
        if self.count >= self.cap:
            raise DriftError(
                f"request budget exceeded: count {self.count + 1}, cap {self.cap}; "
                f"window start {self.window_start}; candidates {self.candidates}; "
                f"narrow the window with {self.narrow_option}"
            )
        self.ensure_time(REQUEST_TIMEOUT + REQUEST_PAUSE_SECONDS)
        self.count += 1
        self.sleep(REQUEST_PAUSE_SECONDS)
        return self.ensure_time(REQUEST_TIMEOUT)

    def __call__(self, url: str) -> dict | list:
        """Pause once per admitted request and preserve the underlying per-operation timeout."""
        remaining = self.admit()
        result = self.reader.read(url, remaining, self.admit)
        self.ensure_time()
        return result

    def ensure_time(self, reserve: float = 0) -> float:
        """Fail an incomplete scan before its monotonic budget or reserved operation expires."""
        remaining = self.deadline - time.monotonic()
        if remaining <= reserve:
            raise DriftError(
                f"incomplete scan: {SCAN_BUDGET_SECONDS}-second elapsed-time budget exhausted; "
                f"requests {self.count}; narrow with {self.narrow_option}"
            )
        return remaining


def _budgeted(fetch: Fetch, cap: int) -> RequestBudget:
    """Reuse the CLI run counter or create one for a direct fill or check call."""
    if isinstance(fetch, RequestBudget):
        fetch.cap = min(fetch.cap, cap)
        return fetch
    return RequestBudget(fetch, cap)


class _ForgePage(list):
    """Keep pagination headers with list rows; headers take part in equality between pages."""

    __hash__ = list.__hash__

    def __init__(self, rows: list, headers: dict[str, str]) -> None:
        """Retain server evidence for the listing readers only."""
        super().__init__(rows)
        self.headers = headers

    def __eq__(self, other: object) -> bool:
        """Compare page rows and headers, or just rows for another list."""
        if isinstance(other, _ForgePage):
            return super().__eq__(other) and self.headers == other.headers
        if isinstance(other, list):
            return super().__eq__(other)
        return NotImplemented

    def __ne__(self, other: object) -> bool:
        """Invert supported equality comparisons and defer other types."""
        equal = self.__eq__(other)
        if equal is NotImplemented:
            return NotImplemented
        return not equal


def fetch_forge(url: str) -> dict | list:
    """Fetch a JSON object or list from the forge with the shared per-operation timeout."""
    headers: dict[str, str] = {}
    data = upstream_drift.fetch_json(url, allow_list=True, response_headers=headers)
    return _ForgePage(data, headers) if isinstance(data, list) else data


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


def normalize_item(
    raw: object, *, default_state: str = "open", partial_files: bool = False
) -> dict[str, object]:
    """Normalize trusted matcher inputs; raw forge rows must use parse_forge_pull."""
    if not isinstance(raw, dict):
        raise DriftError("pull or candidate is not an object")
    number = raw.get("number")
    if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
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


def pull_api_url(number: int, url: str = "") -> str:
    """Return the Forgejo API URL for pull request number, validating url when given."""
    if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
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
        if _listing_complete(batch, files.file_count, url):
            return _file_result(files, True, partial, pull_url, "")
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
    if "changed_files" in raw and (
        not isinstance(count, int) or isinstance(count, bool) or count < 0
    ):
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
    item["revision"] = (checked["head"]["sha"], checked["base"]["ref"])
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
            if isinstance(number, int) and not isinstance(number, bool) and number > 0:
                address = pull_api_url(number)
                where = f"{where}: {address}" if where and where != address else address
        raise DriftError(f"{where or 'pull request'}: {exc}") from exc


def read_candidate_pulls(since: str, fetch: Fetch) -> list[dict[str, object]]:
    """Read candidate metadata once, stopping at updates older than the run window."""
    since_dt = parse_timestamp(since, "created_at")
    repo_url = upstream_drift.repo_api_url()
    candidates: list[dict[str, object]] = []
    seen_numbers: set[int] = set()
    read_count = 0
    for page in range(1, MAX_PULL_PAGES + 1):
        url = f"{repo_url}/pulls?state=all&sort=recentupdate&limit={PAGE_SIZE}&page={page}"
        batch = fetch(url)
        if not isinstance(batch, list) or len(batch) > PAGE_SIZE:
            raise DriftError(f"{url}: expected a JSON list of at most {PAGE_SIZE} pull requests")
        read_count += len(batch)
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
        if reached_older or _listing_complete(batch, read_count, url):
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
        return _item_files(item["files"], number, partial)
    revision = item.get("revision")
    cached = files.get(number)
    if (
        cached is not None
        and getattr(cached, "revision", None) is not None
        and revision != cached.revision
    ):
        raise DriftError(
            f"{pull_api_url(number)}/files: inconsistent pull revision for memoized file list"
        )
    resolved = memoized_files(number, fetch, files, partial=partial)
    if getattr(files[number], "revision", None) is None:
        files[number].revision = revision
    return resolved
