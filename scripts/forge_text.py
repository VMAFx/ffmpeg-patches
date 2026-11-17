#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Check outbound body files for listed assistant-chat filler outside Markdown code and quotes.

This is a phrase list, not a general prose-quality check: other wording with the
same effect passes. Fenced code, quote lines and inline code spans are excluded.
Exit 0 for clean files, 1 for findings, or 2 for unreadable or non-UTF-8 input.
"""

from __future__ import annotations

import argparse
import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType

PHRASES = (
    "i've reviewed your changes",
    "would you like me to",
    "i hope this helps",
    "let me know if you have any questions",
    "let me know if you need anything else",
    "great question",
    "i'd be happy to help",
    "as an ai",
    "feel free to reach out",
    "thanks for your patience",
)
PHRASE_PATTERNS = tuple(
    (phrase, re.compile(rf"(?<!\w){re.escape(phrase)}(?!\w)")) for phrase in PHRASES
)
INLINE_CODE = re.compile(r"(?<!`)(`+)(?!`)(.*?)(?<!`)\1(?!`)", re.DOTALL)


def _load_base_claims() -> ModuleType:
    """Load the sibling fence parser even when isolated Python omits the script directory."""
    path = Path(__file__).resolve().with_name("base_claims.py")
    spec = importlib.util.spec_from_file_location("base_claims", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load fence parser: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BASE_CLAIMS = _load_base_claims()


def _line_phrase(line: str) -> str | None:
    """Return the first listed phrase bounded by non-word characters or line edges."""
    normalized = " ".join(line.lower().replace("\u2019", "'").split())
    return next((phrase for phrase, pattern in PHRASE_PATTERNS if pattern.search(normalized)), None)


def _prose(text: str) -> str:
    """Remove fences, quote lines and inline spans while preserving line numbers."""
    lines: list[str] = []
    fence: tuple[str, int] | None = None
    for line in text.splitlines():
        fence, skipped = BASE_CLAIMS._step_fence(line, fence)
        lines.append("" if skipped or line.lstrip().startswith(">") else line)
    return INLINE_CODE.sub(lambda match: " " + "\n" * match.group().count("\n"), "\n".join(lines))


def _findings(path: str, text: str) -> list[str]:
    """Return one diagnostic per matching prose line outside Markdown exclusions."""
    findings: list[str] = []
    for number, line in enumerate(_prose(text).splitlines(), start=1):
        phrase = _line_phrase(line)
        if phrase is not None:
            findings.append(f"{path}:{number}: filler phrase: {phrase}")
    return findings


def main(argv: list[str] | None = None) -> int:
    """Check every body-file argument and report findings or input errors on stderr."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="+", metavar="FILE", help="body file to check")
    args = parser.parse_args(argv)
    findings: list[str] = []
    for path in args.files:
        try:
            text = Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            print(f"forge_text: {path}: {error}", file=sys.stderr)
            return 2
        findings.extend(_findings(path, text))
    for finding in findings:
        print(finding, file=sys.stderr)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
