#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Check pull-request bodies and comments for stale base-commit claims.

With --pull-json, --head HEX requires an exact match between pull head.sha and
full 40-hex HEX (exiting 3 on mismatch) before checking claims, and --label TEXT
overrides the display label for FILE arguments in stale-claim diagnostics.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

BASE_CLAIM = re.compile(
    r"\bbase\b[:\s]+(?:(?:ffmpeg|master|is|commit)\s+){0,3}([0-9a-f]{7,40})\b",
    re.IGNORECASE,
)
CLAUSE_BREAK = re.compile(r"[;,|()]|\.\s")
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
HISTORY_WORDS = re.compile(r"\b(previous|prior|earlier|former|old)\b", re.IGNORECASE)
HEX_SHA = re.compile(r"[0-9a-fA-F]{40}")


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


def find_base_claims(text: str) -> list[str]:
    """Return lowercased base-commit hex claims outside fences, quotes and history clauses."""
    claims: list[str] = []
    fence: tuple[str, int] | None = None
    for line in text.splitlines():
        fence, skipped = _step_fence(line, fence)
        if skipped or line.lstrip().startswith(">"):
            continue
        claims.extend(_line_claims(line))
    return claims


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
    """Construct the command-line argument parser."""
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
    parser.add_argument("files", nargs="*", metavar="FILE", help="body file to check")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Validate optional pull head and base claims against the live merge base."""
    args = _build_parser().parse_args(argv)
    try:
        mismatch, merge_base, targets = _load_targets(args)
    except (OSError, UnicodeError, ValueError) as error:
        print(f"base_claims: {error}", file=sys.stderr)
        return 2
    if mismatch is not None:
        print(mismatch, file=sys.stderr)
        return 3
    if not any(find_base_claims(text) for _, text in targets):
        return 0
    if not isinstance(merge_base, str) or HEX_SHA.fullmatch(merge_base) is None:
        print(f"base_claims: invalid merge base: {merge_base!r}", file=sys.stderr)
        return 2
    stale = [
        f"{label}: names base {claim}, the live merge base is {merge_base}"
        for label, text in targets
        for claim in stale_claims(text, merge_base)
    ]
    for message in stale:
        print(message, file=sys.stderr)
    return 1 if stale else 0


if __name__ == "__main__":
    sys.exit(main())
