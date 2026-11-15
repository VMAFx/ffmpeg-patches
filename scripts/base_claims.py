#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Check pull-request bodies and comments for stale base-commit claims."""

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
HISTORY_WORDS = re.compile(r"\b(previous|prior|earlier|former|old)\b", re.IGNORECASE)
HEX_SHA = re.compile(r"[0-9a-fA-F]{40}")


def find_base_claims(text: str) -> list[str]:
    """Return lowercased base-commit hex claims outside fences, quotes and history lines."""
    claims: list[str] = []
    in_fence = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or stripped.startswith(">"):
            continue
        for match in BASE_CLAIM.finditer(line):
            if HISTORY_WORDS.search(line, 0, match.start()) is None:
                claims.append(match.group(1).lower())
    return claims


def stale_claims(text: str, merge_base: str) -> list[str]:
    """Return base claims in text that are not a prefix of merge_base."""
    normalized = merge_base.lower()
    return [claim for claim in find_base_claims(text) if not normalized.startswith(claim)]


def _load_targets(args: argparse.Namespace) -> tuple[object, list[tuple[str, str]]]:
    """Read the merge base and labelled texts from the parsed CLI arguments."""
    targets: list[tuple[str, str]] = []
    merge_base: object = args.merge_base
    if args.pull_json is not None:
        raw = (
            sys.stdin.read()
            if args.pull_json == "-"
            else Path(args.pull_json).read_text(encoding="utf-8")
        )
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("pull JSON must be an object")
        merge_base = payload.get("merge_base")
        if args.live_body:
            body = payload.get("body")
            if not isinstance(body, str):
                raise ValueError("pull JSON missing string body")
            targets.append(("live PR body", body))
    elif args.live_body:
        raise ValueError("--live-body requires --pull-json")
    targets.extend((path, Path(path).read_text(encoding="utf-8")) for path in args.files)
    return merge_base, targets


def main(argv: list[str] | None = None) -> int:
    """Validate base claims in body files and optional live PR JSON against merge_base."""
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--merge-base", metavar="HEX", help="live 40-hex merge base")
    source.add_argument("--pull-json", metavar="PATH", help="pull JSON path or '-' for stdin")
    parser.add_argument(
        "--live-body",
        action="store_true",
        help="check the pull JSON body field as 'live PR body'",
    )
    parser.add_argument("files", nargs="*", metavar="FILE", help="body file to check")
    args = parser.parse_args(argv)
    try:
        merge_base, targets = _load_targets(args)
    except (OSError, UnicodeError, ValueError) as error:
        print(f"base_claims: {error}", file=sys.stderr)
        return 2
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
