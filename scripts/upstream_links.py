#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Fill linked upstream issues and related pulls, and check open pulls for overlaps.

    upstream_links.py [--root DIR] fill [--dry-run]
        For every manifest row in state open or merged, read the pull, its
        changed files and candidate pulls updated since the pull was opened,
        extract issue numbers after Fixes, Closes or Refs in the pull body,
        and write fixes_issues and related into upstream-contributions.json.
    upstream_links.py [--root DIR] check [--pull N]...
        For every open manifest pull and each --pull N, list new related
        pulls and issues not yet recorded in the manifest and exit 1 when at
        least one is an overlap by another author.

Exit status: 0 on success or non-overlapping check, 1 on an overlap by
another author in check, 2 on a read or input error.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path

try:
    import series
    import upstream_drift
except ModuleNotFoundError:
    from scripts import series, upstream_drift

Fetch = Callable[[str], dict | list]
DriftError = upstream_drift.DriftError
REQUEST_TIMEOUT = upstream_drift.REQUEST_TIMEOUT
PAGE_SIZE = 50
MAX_PULL_PAGES = 20
MAX_PULL_ROWS = PAGE_SIZE * MAX_PULL_PAGES
MAX_FILE_PAGES = 20
MAX_FILE_ROWS = PAGE_SIZE * MAX_FILE_PAGES
MAX_CANDIDATES = MAX_PULL_ROWS
MAX_ISSUE_MATCHES = series.MAX_FIXES_ISSUES
ISSUE_CLAUSE = re.compile(
    r"\b(?:fixes|closes|refs)\b\s*:?\s*([^\r\n;]+)",
    re.IGNORECASE,
)
ISSUE_REF = re.compile(
    r"(?:https?://[^\s,;]+/issues/|#|\bissues?\s+#?)([1-9][0-9]*)\b"
    r"|(?<!/)\b([1-9][0-9]*)\b",
    re.IGNORECASE,
)


class RelatedRow(dict):
    """A related manifest row carrying own-pull and merge context for overlap checks."""

    own_author: str = ""
    own_opened: str = ""
    merged_at: str = ""


def fetch_forge(url: str) -> dict | list:
    """Fetch a JSON object or list from the forge with the shared request deadline."""
    return upstream_drift.fetch_json(url, allow_list=True)


def parse_timestamp(raw: object, field: str = "timestamp") -> datetime:
    """Parse an ISO-8601 instant or YYYY-MM-DD date string into a UTC datetime."""
    if not isinstance(raw, str) or not raw.strip():
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
        items.append(item)
    return tuple(items)


def _extract_author(raw: dict) -> str:
    """Read and validate the pull or issue author from author or user.login."""
    if "author" in raw:
        author = raw["author"]
    else:
        user = raw.get("user")
        author = user.get("login") if isinstance(user, dict) else None
    if not isinstance(author, str) or not author.strip() or "\n" in author:
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


def normalize_item(raw: object, *, default_state: str = "open") -> dict[str, object]:
    """Validate and normalize an own pull or candidate pull or issue mapping."""
    if not isinstance(raw, dict):
        raise DriftError("pull or candidate is not an object")
    number = raw.get("number")
    if type(number) is not int or number <= 0:
        raise DriftError(f"invalid number: {number!r}")
    title = raw.get("title")
    if not isinstance(title, str) or not title.strip():
        raise DriftError(f"pull {number}: missing or invalid title")
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
        "files": _string_tuple(raw.get("files", raw.get("changed_files")), "files", MAX_FILE_ROWS),
        "patch_ids": _string_tuple(
            raw.get("patch_ids", raw.get("stable_patch_ids")), "patch_ids", MAX_FILE_ROWS
        ),
        "created_at": created_at,
        "merged_at": merged_at,
        "seen": raw.get("seen") or "",
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
    seen_day = (today or datetime.now(UTC).date()).isoformat()
    by_number: dict[int, RelatedRow] = {}
    for raw in candidates[:MAX_CANDIDATES]:
        cand = normalize_item(raw)
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
            seen=cand["seen"] or seen_day,
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


def body_issues(body: object) -> list[int]:
    """Extract sorted unique upstream issue numbers after Fixes, Closes or Refs in a pull body."""
    if body is None:
        return []
    if not isinstance(body, str):
        raise DriftError("pull request body must be a string")
    numbers: set[int] = set()
    clauses = ISSUE_CLAUSE.findall(body)
    for clause in clauses[:MAX_ISSUE_MATCHES]:
        for match in ISSUE_REF.finditer(clause):
            token = match.group(1) or match.group(2)
            if token:
                numbers.add(int(token))
            if len(numbers) > series.MAX_FIXES_ISSUES:
                raise DriftError(f"fixes_issues entries exceed {series.MAX_FIXES_ISSUES}")
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
    names = [filename]
    previous = row.get("previous_filename")
    if previous is not None:
        if not isinstance(previous, str):
            raise DriftError(f"{url}: invalid previous_filename")
        if previous.strip():
            names.append(previous)
    return names


def read_pull_files(pull_url: str, fetch: Fetch) -> list[str]:
    """Read and validate the paged changed file names of a pull request from the forge."""
    files: list[str] = []
    for page in range(1, MAX_FILE_PAGES + 1):
        url = f"{pull_url}/files?limit={PAGE_SIZE}&page={page}"
        batch = fetch(url)
        if not isinstance(batch, list) or len(batch) > PAGE_SIZE:
            raise DriftError(f"{url}: expected a JSON list of at most {PAGE_SIZE} file entries")
        for row in batch[:PAGE_SIZE]:
            files.extend(_file_names_from_row(row, url))
            if len(files) > MAX_FILE_ROWS:
                raise DriftError(f"{pull_url}/files: file entries exceed {MAX_FILE_ROWS}")
        if len(batch) < PAGE_SIZE:
            return sorted(set(files))
    raise DriftError(f"{pull_url}/files: exceeds {MAX_FILE_PAGES} pages")


def _require_forge_envelope(raw: object) -> dict:
    """Validate top-level Forgejo pull fields before normalizing the record."""
    if not isinstance(raw, dict):
        raise DriftError("pull request answer is not a JSON object")
    user = raw.get("user")
    if not isinstance(user, dict) or not isinstance(user.get("login"), str):
        raise DriftError("pull request answer has invalid user")
    if not isinstance(raw.get("merged"), bool):
        raise DriftError("pull request answer lacks boolean merged field")
    if raw.get("state") not in ("open", "closed"):
        raise DriftError(f"pull request answer has invalid state {raw.get('state')!r}")
    return raw


def parse_forge_pull(raw: object) -> dict[str, object]:
    """Validate a Forgejo pull request JSON object and return its normalized fields."""
    checked = _require_forge_envelope(raw)
    item = normalize_item(checked)
    if not item["created_at"]:
        raise DriftError(f"pull request {item['number']}: missing created_at")
    if item["state"] == "merged" and not item["merged_at"]:
        raise DriftError(f"pull request {item['number']}: merged pull lacks merged_at")
    updated_at = checked.get("updated_at")
    if updated_at is None:
        updated_at = item["merged_at"] or item["created_at"]
    parse_timestamp(updated_at, "updated_at")
    body = checked.get("body", "")
    if body is not None and not isinstance(body, str):
        raise DriftError(f"pull request {item['number']}: invalid body")
    item["updated_at"] = updated_at
    item["body"] = body or ""
    return item


def read_candidate_pulls(since: str, own_number: int, fetch: Fetch) -> list[dict[str, object]]:
    """Read paged candidate pulls updated since the own pull was opened, with changed files."""
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
            item = parse_forge_pull(raw)
            if parse_timestamp(item["updated_at"], "updated_at") < since_dt:
                reached_older = True
                continue
            num = int(item["number"])
            if num == own_number or num in seen_numbers:
                continue
            if len(candidates) >= MAX_PULL_ROWS:
                raise DriftError(f"{repo_url}/pulls: candidate pulls exceed {MAX_PULL_ROWS}")
            item["files"] = tuple(read_pull_files(f"{repo_url}/pulls/{num}", fetch))
            seen_numbers.add(num)
            candidates.append(item)
        if len(batch) < PAGE_SIZE or reached_older:
            return candidates
    raise DriftError(f"{repo_url}/pulls: exceeds {MAX_PULL_PAGES} pages")


def inspect_own_pull(
    number: int, url: str, fetch: Fetch, today: date | None
) -> tuple[dict[str, object], list[int], list[RelatedRow]]:
    """Read one own pull, its files and candidate pulls; return pull, issues and related."""
    p_url = pull_api_url(number, url)
    own = parse_forge_pull(fetch(p_url))
    if own["number"] != number:
        raise DriftError(f"{p_url}: pull number {own['number']} disagrees with {number}")
    own["files"] = tuple(read_pull_files(p_url, fetch))
    issues = body_issues(own["body"])
    candidates = read_candidate_pulls(str(own["created_at"]), number, fetch)
    related = related_for(own, candidates, today=today)
    return own, issues, related


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
    return data


def merge_related(existing: list[dict], fresh: list[RelatedRow]) -> list[dict]:
    """Merge freshly discovered related rows with existing manifest rows, keeping seen dates."""
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


def fill_manifest(
    root: Path, fetch: Fetch, *, dry_run: bool = False, today: date | None = None
) -> list[str]:
    """Fill fixes_issues and related for open and merged manifest rows; write unless dry_run."""
    path = root / series.CONTRIBUTIONS
    original = path.read_bytes() if path.is_file() else b""
    data = _checked_manifest(root)
    changes: list[str] = []
    for entry in data["contributions"][: upstream_drift.MAX_SERIES_ENTRIES]:
        if entry["state"] not in ("open", "merged"):
            continue
        _, issues, related = inspect_own_pull(entry["number"], entry["url"], fetch, today)
        merged_issues = sorted(set(entry.get("fixes_issues", [])) | set(issues))
        if len(merged_issues) > series.MAX_FIXES_ISSUES:
            raise DriftError(f"fixes_issues entries exceed {series.MAX_FIXES_ISSUES}")
        merged_rel = merge_related(entry.get("related", []), related)
        if entry.get("fixes_issues") != merged_issues or entry.get("related") != merged_rel:
            entry["fixes_issues"] = merged_issues
            entry["related"] = merged_rel
            changes.append(
                f"{entry['number']}: fixes_issues={json.dumps(merged_issues)} "
                f"related={json.dumps(merged_rel)}"
            )
    if changes and not dry_run:
        if path.read_bytes() != original:
            raise DriftError(f"{path}: changed since read; no changes written")
        rendered = (json.dumps(data, indent=2, ensure_ascii=original.isascii()) + "\n").encode(
            "utf-8"
        )
        path.write_bytes(rendered)
    return changes


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


def check_overlaps(
    root: Path, extra_pulls: list[int], fetch: Fetch, *, today: date | None = None
) -> tuple[list[str], list[dict]]:
    """Check open manifest pulls and --pull numbers; return new related lines and loud overlaps."""
    data = _checked_manifest(root)
    targets = _check_targets(data["contributions"], extra_pulls)
    lines: list[str] = []
    loud: list[dict] = []
    for num, url, recorded in targets[: upstream_drift.MAX_SERIES_ENTRIES]:
        _, _, related = inspect_own_pull(num, url, fetch, today)
        new_rows = [row for row in related if row["number"] not in recorded]
        for row in new_rows[: series.MAX_RELATED]:
            lines.append(f"{num} {row['number']} {row['author']} {row['reason']} {row['state']}")
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
    check_p = sub.add_parser("check", help="list new related items and fail on overlaps")
    check_p.add_argument("--root", type=Path, default=argparse.SUPPRESS)
    check_p.add_argument("--pull", type=int, action="append", default=[], metavar="N")
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


def main(argv: list[str], fetch: Fetch = fetch_forge, today: date | None = None) -> int:
    """Run fill or check; exit 0 when clean, 1 on overlaps, 2 on read or input error."""
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code else 0
    cache: dict[str, dict | list] = {}

    def cached_fetch(url: str) -> dict | list:
        if url not in cache:
            cache[url] = fetch(url)
        return cache[url]

    try:
        series.pull_url_pattern()
        if args.command == "fill":
            for line in fill_manifest(args.root, cached_fetch, dry_run=args.dry_run, today=today):
                print(line)
            return 0
        lines, loud = check_overlaps(args.root, args.pull, cached_fetch, today=today)
    except _caught_errors() as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    for line in lines:
        print(line)
    return 1 if loud else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
