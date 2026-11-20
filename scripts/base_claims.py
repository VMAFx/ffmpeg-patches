#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Check pull-request bodies and comments for stale base and dependency-head claims.

With --pull-json, --head HEX requires an exact match between pull head.sha and
full 40-hex HEX (exiting 3 on mismatch) before checking claims, and --label TEXT
overrides the display label for FILE arguments in stale-claim diagnostics.
Repeat --dependency-json N=PATH to supply each dependency PR's live head (no stdin).
With --list-dependencies, print sorted unique dependency PR numbers instead of
checking claims, after the same input and optional pull-head validation.
Dependency lists pair each reference with its own head; an ambiguous lone
trailing head is rejected. Headless list members still count toward the limit.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Iterator
from pathlib import Path

BASE_CLAIM = re.compile(
    r"\bbase\b[:\s]+(?:(?:ffmpeg|master|is|commit)\s+){0,3}([0-9a-f]{7,40})\b",
    re.IGNORECASE,
)
DEPENDENCY_MEMBER = re.compile(r"(?:#|PR\s+#?|pull\s+request\s+#?)([0-9]{1,7})\b", re.IGNORECASE)
DEPENDENCY_REFERENCE = re.compile(
    r"\b(?:depends\s+on|stacked\s+on|on\s+top\s+of|builds\s+on|based\s+on)"
    r"\s+" + DEPENDENCY_MEMBER.pattern,
    re.IGNORECASE,
)
DEPENDENCY_SEPARATOR = re.compile(r"\s*(?:,\s*(?:and\s+)?|and\s+|&\s*)", re.IGNORECASE)
DEPENDENCY_HEAD = re.compile(
    r"\b(?:head|ending\s+at|ends\s+at|tip)\b[\s:(]*([0-9a-f]{7,40})\b", re.IGNORECASE
)
DEPENDENCY_ANNOTATION = re.compile(
    r"\s*(?:[,|]\s*)?(?:[a-z]+\s+)*(?:\([^()#&;]*"
    + DEPENDENCY_HEAD.pattern
    + r"[^()#&;]*\)+|"
    + DEPENDENCY_HEAD.pattern
    + r"\)*)",
    re.IGNORECASE,
)
SENTENCE_BREAK = re.compile(r"[.!?]\s|;")
CLAUSE_BREAK = re.compile(r"[;,|()]|\.\s")
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
HISTORY_WORDS = re.compile(r"\b(previous|prior|earlier|former|old)\b", re.IGNORECASE)
HEX_SHA = re.compile(r"[0-9a-fA-F]{40}")
MAX_DEPENDENCIES = 8


def _step_fence(line: str, fence: tuple[str, int] | None) -> tuple[tuple[str, int] | None, bool]:
    """Update CommonMark fence state for one line and report whether to skip it."""
    match = FENCE.match(line)
    if fence is None:
        if match is None:
            return None, False
        run, rest = match.group(1), match.group(2)
        if run[0] == "~" or "`" not in rest:
            return (run[0], len(run)), True
        return None, False
    open_char, open_len = fence
    if (
        match is not None
        and match.group(1)[0] == open_char
        and len(match.group(1)) >= open_len
        and not match.group(2).strip()
    ):
        return None, True
    return fence, True


def _line_claims(line: str) -> list[str]:
    """Extract base-commit hex claims from one non-fenced, non-quoted line."""
    claims: list[str] = []
    prev_end = 0
    for match in BASE_CLAIM.finditer(line):
        clause_start = prev_end
        for sep in CLAUSE_BREAK.finditer(line, prev_end, match.start()):
            clause_start = sep.end()
        if HISTORY_WORDS.search(line, clause_start, match.start()) is None:
            claims.append(match.group(1).lower())
        prev_end = match.end()
    return claims


def _claim_lines(text: str) -> Iterator[str]:
    """Yield lines outside CommonMark fences and block quotes for both claim kinds."""
    fence: tuple[str, int] | None = None
    for line in text.splitlines():
        fence, skipped = _step_fence(line, fence)
        if not skipped and not line.lstrip().startswith(">"):
            yield line


def find_base_claims(text: str) -> list[str]:
    """Return lowercased base-commit hex claims outside fences, quotes and history clauses."""
    return [claim for line in _claim_lines(text) for claim in _line_claims(line)]


def _dependency_clause_start(sentence: str, start: int, end: int, fallback: int = 0) -> int:
    """Return the last clause boundary in a range or the supplied fallback."""
    clause_start = fallback
    for sep in CLAUSE_BREAK.finditer(sentence, start, end):
        clause_start = sep.end()
    return clause_start


def _dependency_head_is_historical(
    sentence: str, reference: re.Match[str], head: re.Match[str], reference_clause_start: int
) -> bool:
    """Check history in the reference clause and the head's own clause before its hex."""
    boundary = next(CLAUSE_BREAK.finditer(sentence, reference.end(), head.start(1)), None)
    reference_clause_end = boundary.start() if boundary is not None else head.start(1)
    head_clause_start = _dependency_clause_start(
        sentence, reference.end(), head.start(), reference_clause_start
    )
    clauses = (
        sentence[reference_clause_start:reference_clause_end],
        sentence[head_clause_start : head.start(1)],
    )
    return any(
        HISTORY_WORDS.search(clause) or re.search(r"\bpreviously\b", clause, re.IGNORECASE)
        for clause in clauses
    )


def _dependency_member_end(sentence: str, start: int, end: int) -> int:
    """Consume only adjacent head annotations, leaving list separators and unrelated prose."""
    cursor = start
    for _ in range(end - start + 1):
        annotation = DEPENDENCY_ANNOTATION.match(sentence, cursor, end)
        if annotation is None:
            break
        cursor = annotation.end()
    return cursor


def _dependency_lists(sentence: str) -> Iterator[list[tuple[re.Match[str], int]]]:
    """Yield led reference lists with each member's own head-span endpoint."""
    references = list(DEPENDENCY_REFERENCE.finditer(sentence))
    for index, first in enumerate(references):
        end = references[index + 1].start() if index + 1 < len(references) else len(sentence)
        reference = first
        members = []
        for _ in range(end - reference.end() + 1):
            member_end = _dependency_member_end(sentence, reference.end(), end)
            members.append((reference, member_end))
            separator = DEPENDENCY_SEPARATOR.match(sentence, member_end, end)
            following = (
                DEPENDENCY_MEMBER.match(sentence, separator.end(), end) if separator else None
            )
            if following is None:
                break
            reference = following
        if len(members) == 1:
            members[0] = (members[0][0], end)
        yield members


def _reject_ambiguous_dependency_list(
    members: list[tuple[re.Match[str], int]], heads: list[list[re.Match[str]]]
) -> None:
    """Reject a lone trailing head that could belong to one member or the whole list."""
    if len(members) > 1 and heads[-1] and not any(heads[:-1]):
        numbers = ", ".join(f"#{int(reference.group(1))}" for reference, _ in members)
        prefix = heads[-1][0].group(1).lower()
        raise ValueError(
            f"ambiguous dependency heads for {numbers}: {prefix}; "
            "write the head right after each reference or one dependency per sentence"
        )


def _dependency_list_claims(
    sentence: str, members: list[tuple[re.Match[str], int]]
) -> list[tuple[int, str | None]]:
    """Pair member heads with local and lead history, retaining headless list numbers."""
    heads = [
        list(DEPENDENCY_HEAD.finditer(sentence, reference.end(), end)) for reference, end in members
    ]
    _reject_ambiguous_dependency_list(members, heads)
    first = members[0][0]
    lead_start = _dependency_clause_start(sentence, 0, first.start())
    lead_end = heads[0][0].start(1) if heads[0] else first.end()
    boundary = next(CLAUSE_BREAK.finditer(sentence, first.end(), lead_end), None)
    lead = sentence[lead_start : boundary.start() if boundary else lead_end]
    historical = HISTORY_WORDS.search(lead) or re.search(r"\bpreviously\b", lead, re.IGNORECASE)
    claims: list[tuple[int, str | None]] = []
    for (reference, _), member_heads in zip(members, heads, strict=True):
        number = int(reference.group(1))
        if number == 0:
            continue
        if len(members) > 1:
            claims.append((number, None))
        clause_start = lead_start if reference is first else reference.start()
        for head in member_heads:
            if not historical and not _dependency_head_is_historical(
                sentence, reference, head, clause_start
            ):
                claims.append((number, head.group(1).lower()))
    return claims


def _sentence_dependencies(sentence: str) -> list[tuple[int, str | None]]:
    """Extract list members and their own heads, raising ValueError for ambiguous lists."""
    return [
        claim
        for members in _dependency_lists(sentence)
        for claim in _dependency_list_claims(sentence, members)
    ]


def _check_dependency_count(numbers: set[int]) -> None:
    """Reject distinct dependency member counts above the shared limit."""
    if len(numbers) > MAX_DEPENDENCIES:
        raise ValueError(f"more than {MAX_DEPENDENCIES} distinct dependency PR numbers")


def _dependency_records(text: str) -> list[tuple[int, str | None]]:
    """Collect bounded member records outside fences and quotes, rejecting ambiguous lists."""
    records = [
        claim
        for line in _claim_lines(text)
        for sentence in SENTENCE_BREAK.split(line)
        for claim in _sentence_dependencies(sentence)
    ]
    _check_dependency_count({number for number, _ in records})
    return records


def find_dependency_claims(text: str) -> list[tuple[int, str]]:
    """Return members' own nonhistorical heads; raise ValueError on ambiguity or excess."""
    return [(number, head) for number, head in _dependency_records(text) if head is not None]


def stale_claims(text: str, merge_base: str) -> list[str]:
    """Return base claims in text that are not a prefix of merge_base."""
    normalized = merge_base.lower()
    return [claim for claim in find_base_claims(text) if not normalized.startswith(claim)]


def _validate_options(args: argparse.Namespace) -> None:
    """Reject option combinations and --head values outside the CLI contract."""
    if args.pull_json is None:
        if args.live_body:
            raise ValueError("--live-body requires --pull-json")
        if args.head is not None:
            raise ValueError("--head requires --pull-json")
    if args.head is not None and HEX_SHA.fullmatch(args.head) is None:
        raise ValueError("--head must be a full 40-hex commit id")


def _read_pull_payload(path_or_dash: str) -> dict[str, object]:
    """Load and type-check the pull-request JSON object from a path or stdin."""
    raw = (
        sys.stdin.read() if path_or_dash == "-" else Path(path_or_dash).read_text(encoding="utf-8")
    )
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("pull JSON must be an object")
    return payload


def _check_pull_head(payload: dict[str, object], expected_head: str | None) -> str | None:
    """Verify pull JSON head.sha against expected_head, returning a mismatch message."""
    if expected_head is None:
        return None
    head = payload.get("head")
    sha = head.get("sha") if isinstance(head, dict) else None
    if not isinstance(sha, str) or HEX_SHA.fullmatch(sha) is None:
        raise ValueError(f"invalid pull head: {sha!r}")
    actual, wanted = sha.lower(), expected_head.lower()
    if actual != wanted:
        return f"pull head {actual} is not {wanted}"
    return None


def _load_targets(
    args: argparse.Namespace,
) -> tuple[str | None, object, list[tuple[str, str]]]:
    """Read head mismatch, merge base and labelled texts from parsed CLI arguments."""
    _validate_options(args)
    targets: list[tuple[str, str]] = []
    merge_base: object = args.merge_base
    if args.pull_json is not None:
        payload = _read_pull_payload(args.pull_json)
        mismatch = _check_pull_head(payload, args.head)
        if mismatch is not None:
            return mismatch, None, []
        merge_base = payload.get("merge_base")
        if args.live_body:
            body = payload.get("body")
            if not isinstance(body, str):
                raise ValueError("pull JSON missing string body")
            targets.append(("live PR body", body))
    targets.extend(
        (args.label if args.label is not None else path, Path(path).read_text(encoding="utf-8"))
        for path in args.files
    )
    return None, merge_base, targets


def _build_parser() -> argparse.ArgumentParser:
    """Construct the parser for base checks, dependency heads and dependency listing."""
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--merge-base", metavar="HEX", help="live 40-hex merge base")
    source.add_argument("--pull-json", metavar="PATH", help="pull JSON path or '-' for stdin")
    parser.add_argument(
        "--head",
        metavar="HEX",
        help="require pull JSON head.sha to match full 40-hex HEX exactly (exit 3 on mismatch)",
    )
    parser.add_argument(
        "--live-body",
        action="store_true",
        help="check the pull JSON body field as 'live PR body'",
    )
    parser.add_argument(
        "--label",
        metavar="TEXT",
        help="label every FILE argument as TEXT instead of its path",
    )
    parser.add_argument(
        "--dependency-json",
        action="append",
        default=[],
        metavar="N=PATH",
        help="dependency PR number and pull JSON path (repeatable, no stdin)",
    )
    parser.add_argument(
        "--list-dependencies",
        action="store_true",
        help="list sorted unique dependency PR numbers without checking claims",
    )
    parser.add_argument("files", nargs="*", metavar="FILE", help="body file to check")
    return parser


def _dependency_numbers(targets: list[tuple[str, str]]) -> list[int]:
    """List all member numbers, including headless members; reject ambiguity and overflow."""
    numbers = {number for _, text in targets for number, _ in _dependency_records(text)}
    _check_dependency_count(numbers)
    return sorted(numbers)


def _dependency_option(option: str) -> tuple[int, str]:
    """Parse one positive bounded PR number and non-stdin dependency JSON path."""
    number_text, separator, path = option.partition("=")
    if not separator or re.fullmatch(r"[0-9]{1,7}", number_text) is None:
        raise ValueError(f"invalid --dependency-json N=PATH: {option!r}")
    number = int(number_text)
    if number == 0 or not path or path == "-":
        raise ValueError(f"invalid --dependency-json N=PATH: {option!r}")
    return number, path


def _load_dependencies(options: list[str]) -> dict[int, str]:
    """Validate bounded N=PATH options and load full live dependency heads."""
    if len(options) > MAX_DEPENDENCIES:
        raise ValueError(f"more than {MAX_DEPENDENCIES} --dependency-json options")
    heads: dict[int, str] = {}
    for option in options:
        number, path = _dependency_option(option)
        if number in heads:
            raise ValueError(f"repeated --dependency-json for #{number}")
        payload = _read_pull_payload(path)
        head = payload.get("head")
        sha = head.get("sha") if isinstance(head, dict) else None
        if not isinstance(sha, str) or HEX_SHA.fullmatch(sha) is None:
            raise ValueError(f"invalid dependency pull #{number} head: {sha!r}")
        heads[number] = sha.lower()
    return heads


def _base_diagnostics(targets: list[tuple[str, str]], merge_base: object) -> list[str]:
    """Validate the merge base when needed and format stale base diagnostics."""
    if not any(find_base_claims(text) for _, text in targets):
        return []
    if not isinstance(merge_base, str) or HEX_SHA.fullmatch(merge_base) is None:
        raise ValueError(f"invalid merge base: {merge_base!r}")
    return [
        f"{label}: names base {claim}, the live merge base is {merge_base}"
        for label, text in targets
        for claim in stale_claims(text, merge_base)
    ]


def _dependency_diagnostics(targets: list[tuple[str, str]], heads: dict[int, str]) -> list[str]:
    """Format stale dependency-head diagnostics from validated live heads."""
    return [
        f"{label}: names head {claim} of #{number}, the live head of #{number} is {heads[number]}"
        for label, text in targets
        for number, claim in find_dependency_claims(text)
        if not heads[number].startswith(claim)
    ]


def main(argv: list[str] | None = None) -> int:
    """Check claims or list bounded dependency members, refusing ambiguous heads with rc 2."""
    args = _build_parser().parse_args(argv)
    try:
        mismatch, merge_base, targets = _load_targets(args)
        if mismatch is not None:
            print(mismatch, file=sys.stderr)
            return 3
        numbers = _dependency_numbers(targets)
        heads = _load_dependencies(args.dependency_json)
        if args.list_dependencies:
            for number in numbers:
                print(number)
            return 0
        for number in numbers:
            if number not in heads:
                raise ValueError(f"missing --dependency-json for #{number}")
        stale = _base_diagnostics(targets, merge_base)
        stale.extend(_dependency_diagnostics(targets, heads))
    except (OSError, UnicodeError, ValueError) as error:
        print(f"base_claims: {error}", file=sys.stderr)
        return 2
    for message in stale:
        print(message, file=sys.stderr)
    return 1 if stale else 0


if __name__ == "__main__":
    sys.exit(main())
