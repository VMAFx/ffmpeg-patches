# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Parse upstream records, issue references and overlap evidence."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime

if not __package__:
    import series
    import upstream_drift
else:
    from scripts import series, upstream_drift


DriftError = upstream_drift.DriftError
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
MAX_NAME_CHARS = series.MAX_RELATED_AUTHOR_CHARS
# Permit substantial pull descriptions while bounding regular-expression work.
MAX_BODY_CHARS = 256 * 1024
# ISO timestamps with fractional seconds and offsets fit well below this ceiling.
MAX_TIMESTAMP_CHARS = 64
ISSUE_CLAUSE = re.compile(
    r"\b(?:fixes|closes|refs)\b[ \t]*:?[ \t]*([^\r\n;]+?)"
    r"(?=\b(?:fixes|closes|refs)\b|[\r\n;]|$)",
    re.IGNORECASE,
)
ISSUE_ITEM = re.compile(r"(#[1-9][0-9]*|https?://[^\s,;]+|[\w.-]+/[\w.-]+#[1-9][0-9]*)")
ISSUE_LIST_ITEM = re.compile(r"\s*(?:,\s*(?:(?:and|or)\s+)?|(?:and|or)\s+)([^\s,]+)", re.IGNORECASE)


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


def _run_day(today: date | None) -> date:
    """Read the local UTC clock once per run unless the fixture supplies its date."""
    return datetime.now(UTC).date() if today is None else today


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
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except (ValueError, OverflowError) as exc:
        raise DriftError(f"invalid {field}: {raw!r}") from exc


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


def _extract_author(raw: dict) -> str:
    """Read and validate the pull or issue author from author or user.login."""
    if "author" in raw:
        author = raw["author"]
    else:
        user = raw.get("user")
        author = user.get("login") if isinstance(user, dict) else None
    _bounded_text(author, "pull author (user.login)", MAX_NAME_CHARS)
    if not series.valid_related_author(author):
        raise DriftError("missing or invalid pull author")
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


def match_reason(own: dict[str, object], candidate: dict[str, object]) -> str | None:
    """Return the relationship reason between an own pull and a candidate, or None."""
    if own["number"] == candidate["number"]:
        return None
    if set(own["patch_ids"]) & set(candidate["patch_ids"]):
        return "same-patch-id"
    if set(own["files"]) & set(candidate["files"]):
        return "same-files"
    if not getattr(candidate["files"], "complete", True):
        return None
    own_subject = subject_key(str(own["title"]))
    if own_subject and own_subject == subject_key(str(candidate["title"])):
        return "same-subject"
    return None


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


def _issue_reference(token: str, local_url: re.Pattern[str]) -> tuple[int | None, bool]:
    """Accept whole standalone hashes or exact configured issue URLs only."""
    if token.startswith("#"):
        return _issue_number(token[1:]), False
    local = local_url.fullmatch(token)
    if local is not None:
        return _issue_number(local.group(1)), False
    return None, "/issues/" in token or "#" in token


def _issue_number(token: str) -> int:
    """Reject oversized decimal IDs before integer conversion."""
    if len(token) > MAX_ISSUE_NUMBER_DIGITS:
        raise DriftError(f"body issue number exceeds {MAX_ISSUE_NUMBER_DIGITS} digits")
    return int(token)


def _clause_items(clause: str, remaining: int) -> list[str]:
    """Read a closed item list from its start, stopping at the first prose item."""
    first = re.match(r"\s*([^\s,]+)", clause)
    items: list[str] = []
    match = first
    for _ in range(remaining + 1):
        if match is None:
            break
        token = match.group(1).removesuffix(".")
        if ISSUE_ITEM.fullmatch(token) is None:
            break
        items.append(token)
        match = ISSUE_LIST_ITEM.match(clause, match.end())
    return items


def _issue_matches(body: str, number: int | None) -> list[str]:
    """Bound both clause traversal and total reference traversal before recording issues."""
    clauses = ISSUE_CLAUSE.findall(body)
    if len(clauses) > MAX_ISSUE_MATCHES:
        raise DriftError(
            f"pull {number} body issue clauses: {len(clauses)} exceeds {MAX_ISSUE_MATCHES}"
        )
    matches: list[str] = []
    for clause in clauses[:MAX_ISSUE_MATCHES]:
        remaining = MAX_ISSUE_REFERENCES - len(matches)
        matches.extend(_clause_items(clause, remaining))
        if len(matches) > MAX_ISSUE_REFERENCES:
            raise DriftError(
                f"pull {number} body issue references: {len(matches)} "
                f"exceeds {MAX_ISSUE_REFERENCES}"
            )
    return matches


def body_issues(body: object, *, number: int | None = None) -> list[int]:
    """Extract whole local issue items and print one ignored-reference count per pull."""
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


def _link_relations(link: str) -> set[str]:
    """Read bounded Link entries and their parameters without splitting quoted delimiters."""
    entries = re.findall(r'<([^>]+)>((?:[^"<>,]|"(?:[^"\\]|\\.)*")*)', link)
    relations: set[str] = set()
    for _address, parameters in entries[:MAX_PATH_CHARS]:
        pairs = re.findall(r';\s*([^\s=;]+)(?:\s*=\s*("(?:[^"\\]|\\.)*"|[^;\s]+))?', parameters)
        for name, raw_value in pairs[:MAX_PATH_CHARS]:
            if name.lower() == "rel":
                value = raw_value
                if raw_value.startswith('"'):
                    value = re.sub(r"\\(.)", r"\1", raw_value[1:-1])
                relations.update(value.lower().split())
    return relations


def _listing_complete(batch: list, read_count: int, url: str) -> bool:
    """Require an empty page or server pagination evidence, never shortness alone."""
    if not batch:
        return True
    headers = getattr(batch, "headers", {})
    link = headers.get("Link")
    if link:
        _bounded_text(link, f"{url} Link header", MAX_PATH_CHARS)
        relations = _link_relations(link)
        if "next" in relations:
            return False
        if relations:
            return True
    total = headers.get("X-Total-Count")
    if total is not None:
        if not isinstance(total, str) or not re.fullmatch(r"[0-9]{1,20}", total):
            raise DriftError(f"{url}: invalid X-Total-Count header")
        return read_count >= int(total)
    return False
