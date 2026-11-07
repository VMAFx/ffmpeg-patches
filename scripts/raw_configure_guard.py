#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Refuse raw FFmpeg configure before a shell can write its environment to config.log.

Known limitation: words assembled from variables (e.g. `p=con; ./${p}figure`)
are not expanded across assignments.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import shlex
import sys
from pathlib import Path
from typing import Any

MAX_INPUT = 1024 * 1024
MAX_COMMAND = 65536
MAX_TOKENS = 4000
MAX_DEPTH = 4
SEPARATORS = frozenset((";", "&&", "||", "|", "&", "\n", "(", ")"))
SHELLS = frozenset(("bash", "sh"))
ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z_0-9]*=.*", re.DOTALL)
CONFIGURE_WORD = re.compile(r"(?<![A-Za-z0-9_])configure(?![A-Za-z0-9_])")
DYNAMIC_VARIABLE = re.compile(r"^\$[A-Za-z_][A-Za-z0-9_]*")
INTERPRETERS = re.compile(r"^(?:python|perl|ruby|node|gawk|mawk|awk|php)(?:\d+(?:\.\d+)*[a-z]*)?$")
GLOB_CHARS = frozenset(("*", "?", "["))
READERS = frozenset(
    (
        "cat",
        "head",
        "tail",
        "less",
        "more",
        "wc",
        "nl",
        "grep",
        "egrep",
        "fgrep",
        "rg",
        "diff",
        "cmp",
        "file",
        "stat",
        "ls",
        "sha256sum",
        "md5sum",
        "sed",
        "git",
    )
)


def _input(client: str, payload: dict[str, Any]) -> tuple[str | None, Path]:
    """Extract the documented shell command and effective directory."""
    if client == "opencode":
        if payload.get("tool") != "bash":
            return None, Path.cwd()
        arguments = payload.get("args")
    else:
        expected = "run_shell_command" if client == "gemini" else "Bash"
        if payload.get("tool_name") != expected:
            return None, Path.cwd()
        arguments = payload.get("tool_input")
    if not isinstance(arguments, dict):
        return None, Path.cwd()
    command = arguments.get("command")
    if not isinstance(command, str) or not command:
        return None, Path.cwd()
    cwd = arguments.get("workdir") if client == "opencode" else None
    if client == "gemini":
        cwd = arguments.get("dir_path")
    if not isinstance(cwd, str) or not cwd:
        cwd = payload.get("cwd")
    base = Path.cwd()
    if isinstance(cwd, str) and cwd:
        base = (base / os.path.expanduser(cwd)).resolve()
    return command, base


def _tokens(command: str) -> list[str] | None:
    """Split a shell command without executing substitutions or expansions."""
    if len(command.encode("utf-8", "surrogatepass")) > MAX_COMMAND:
        return None
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()\n")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        tokens = list(lexer)
    except ValueError:
        return None
    return tokens if len(tokens) <= MAX_TOKENS else None


def _shell_argument(words: list[str]) -> tuple[str | None, bool]:
    """Find a shell script path or its -c program string."""
    for index in range(1, len(words)):
        word = words[index]
        if word == "--":
            return (words[index + 1], False) if index + 1 < len(words) else (None, False)
        if word.startswith("-") and word != "-":
            if "c" in word[1:]:
                return (words[index + 1], True) if index + 1 < len(words) else (None, True)
            continue
        return word, False
    return None, False


def _dynamic_configure(word: str) -> bool:
    """Identify whether a dynamic shell word targets the configure script."""
    if "$(" in word or "`" in word:
        return bool(CONFIGURE_WORD.search(word))
    last = word.rsplit("/", 1)[-1]
    if "}" in last:
        last = last.rsplit("}", 1)[-1]
    elif last.startswith("$"):
        last = DYNAMIC_VARIABLE.sub("", last)
    return last == "configure"


def _configure_path(word: str, cwd: Path | None) -> Path | None:
    """Identify a configure word in an FFmpeg tree or unknown directory."""
    if ASSIGNMENT.fullmatch(word):
        word = word.split("=", 1)[1]
    if "$" in word or "`" in word:
        return Path(word) if _dynamic_configure(word) else None
    base = os.path.basename(word)
    is_glob = bool(GLOB_CHARS.intersection(base)) and fnmatch.fnmatchcase("configure", base)
    if base != "configure" and not is_glob:
        return None
    if cwd is None:
        return Path(word)
    parent = (cwd / os.path.expanduser(os.path.dirname(word) or ".")).resolve()
    return (parent / base) if (parent / "libavcodec").is_dir() else None


def _inspect_interpreter(words: list[str], index: int, cwd: Path | None) -> Path | None:
    """Refuse interpreter one-liners embedding raw configure invocations."""
    name = os.path.basename(words[index]) if index < len(words) else ""
    if not INTERPRETERS.match(name) or (cwd is not None and not (cwd / "libavcodec").is_dir()):
        return None
    for word in words[index + 1 : index + 1 + MAX_TOKENS]:
        if CONFIGURE_WORD.search(word):
            return Path(word)
    return None


def _find_configure_path(words: list[str], cwd: Path | None) -> Path | None:
    """Find the first blocked configure reference across command words."""
    for word in words[: MAX_TOKENS + 1]:
        blocked = _configure_path(word, cwd)
        if blocked is not None:
            return blocked
    return None


def _inspect_simple(words: list[str], cwd: Path | None) -> tuple[Path | None, str | None]:
    """Classify a simple command and return an inline program when present."""
    index = 0
    for word in words[: MAX_TOKENS + 1]:
        if not ASSIGNMENT.fullmatch(word):
            break
        index += 1
    blocked = _inspect_interpreter(words, index, cwd)
    if blocked is not None:
        return blocked, None
    name = os.path.basename(words[index]) if index < len(words) else ""
    if name not in READERS:
        blocked = _find_configure_path(words, cwd)
        if blocked is not None:
            return blocked, None
    if name == "eval":
        return None, " ".join(words[index + 1 :])
    if name in SHELLS:
        target, inline = _shell_argument(words[index:])
        if inline:
            return None, target
    return None, None


def _inspect(command: str, cwd: Path) -> Path | None:
    """Walk bounded shell fragments and inline programs without recursion."""
    pending = [(command, cwd, 0)]
    for _ in range(MAX_TOKENS + 1):
        if not pending:
            return None
        source, directory, depth = pending.pop()
        if depth > MAX_DEPTH:
            return Path("configure (nesting limit)")
        tokens = _tokens(source)
        if tokens is None:
            if "configure" in source:
                return Path("configure (parse or size limit)")
            continue
        simple: list[str] = []
        for token in [*tokens, ";"]:
            if token in SEPARATORS:
                if simple:
                    blocked, directory = _step(simple, directory, depth, pending)
                    if blocked is not None:
                        return blocked
                    simple = []
                continue
            simple.append(token)
    return Path("configure (worklist limit)")


def _step(
    words: list[str], cwd: Path | None, depth: int, pending: list[tuple[str, Path | None, int]]
) -> tuple[Path | None, Path | None]:
    """Update directory or queue an inline program from one simple command."""
    blocked, inline = _inspect_simple(words, cwd)
    if inline is not None:
        pending.append((inline, cwd, depth + 1))
    if words[0] == "popd":
        return blocked, None
    if words[0] in ("cd", "pushd"):
        target = "~" if words == ["cd"] else words[1] if len(words) == 2 else ""
        if not target or target == "-" or "$" in target or "`" in target or cwd is None:
            return blocked, None
        return blocked, (cwd / os.path.expanduser(target)).resolve()
    return blocked, cwd


def main(argv: list[str] | None = None) -> int:
    """Read one hook payload and refuse unsafe configure execution."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--client", choices=("claude", "codex", "gemini", "opencode"), required=True
    )
    options = parser.parse_args(argv)
    raw = sys.stdin.buffer.read(MAX_INPUT + 1)
    try:
        payload = json.loads(raw) if len(raw) <= MAX_INPUT else None
    except (UnicodeDecodeError, json.JSONDecodeError):
        payload = None
    if not isinstance(payload, dict):
        print(
            "Raw configure guard refused unreadable hook JSON; use "
            "python3 scripts/ffmpeg_configure.py <SRC_DIR> <BUILD_DIR>",
            file=sys.stderr,
        )
        return 2
    command, cwd = _input(options.client, payload)
    if command is None:
        return 0
    blocked = _inspect(command, cwd)
    if blocked is None:
        return 0
    print(
        f"Raw FFmpeg configure refused: {blocked}; use "
        "python3 scripts/ffmpeg_configure.py <SRC_DIR> <BUILD_DIR>",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
