#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Refuse raw FFmpeg configure before a shell can write its environment to config.log."""

from __future__ import annotations

import argparse
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
SHELLS = frozenset(("bash", "sh", "dash", "zsh", "ksh"))
ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z_0-9]*=.*", re.DOTALL)
SYSTEMD_VALUES = frozenset(
    ("-p", "--property", "-u", "--unit", "-E", "--setenv", "-M", "--machine", "--slice", "-G")
)
ENV_VALUES = frozenset(("-u", "--unset", "-C", "--chdir", "-S", "--split-string"))


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


def _after_options(words: list[str], start: int, values: frozenset[str]) -> int:
    """Skip bounded wrapper options and their documented separate values."""
    index = start
    for _ in range(len(words)):
        if index >= len(words):
            break
        word = words[index]
        if word == "--":
            return index + 1
        if not word.startswith("-") or word == "-":
            break
        index += 2 if word in values else 1
    return min(index, len(words))


def _peel(words: list[str]) -> list[str]:
    """Remove leading assignments and supported process wrappers."""
    index = 0
    for _ in range(len(words)):
        if index >= len(words):
            break
        next_index = _wrapper_end(words, index)
        if next_index == index:
            break
        index = next_index
    return words[index:]


def _wrapper_end(words: list[str], index: int) -> int:
    """Return the next command index after one supported wrapper."""
    name = os.path.basename(words[index])
    end = index
    if ASSIGNMENT.fullmatch(words[index]):
        end = index + 1
    elif name == "env":
        end = _after_env(words, index + 1)
    elif name in ("nice", "ionice", "chrt", "time", "timeout", "stdbuf"):
        values = {
            "nice": frozenset(("-n",)),
            "ionice": frozenset(("-c", "-n")),
            "timeout": frozenset(("-k",)),
            "stdbuf": frozenset(("-o", "-e", "-i")),
        }.get(name, frozenset())
        end = _after_options(words, index + 1, values)
        end = min(end + int(name in ("timeout", "chrt")), len(words))
    elif name == "taskset":
        end = min(index + 2, len(words))
    elif name in ("nohup", "setsid", "exec", "command", "builtin"):
        end = index + 1
    elif name == "systemd-run":
        end = _after_options(words, index + 1, SYSTEMD_VALUES)
    elif name == "praetorctl" and words[index + 1 : index + 2] == ["admit"]:
        try:
            end = words.index("--", index + 2) + 1
        except ValueError:
            end = len(words)
    return end


def _after_env(words: list[str], start: int) -> int:
    """Skip env options and its leading variable assignments."""
    end = _after_options(words, start, ENV_VALUES)
    for _ in range(len(words)):
        if end >= len(words) or not ASSIGNMENT.fullmatch(words[end]):
            break
        end += 1
    return end


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


def _configure_path(word: str, cwd: Path) -> Path | None:
    """Return the configure file when its source tree has libavcodec."""
    if "$" in word or "`" in word:
        return Path(word) if "configure" in word else None
    if os.path.basename(word) != "configure":
        return None
    path = (cwd / os.path.expanduser(word)).resolve()
    return path if (path.parent / "libavcodec").is_dir() else None


def _inspect_simple(words: list[str], cwd: Path) -> tuple[Path | None, str | None]:
    """Classify a simple command and return an inline program when present."""
    words = _peel(words)
    if not words:
        return None, None
    name = os.path.basename(words[0])
    if name == "eval":
        return None, " ".join(words[1:])
    if name in SHELLS:
        target, inline = _shell_argument(words)
        if target is None:
            return None, None
        if inline:
            return None, target
        return _configure_path(target, cwd), None
    return _configure_path(words[0], cwd), None


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
    words: list[str], cwd: Path, depth: int, pending: list[tuple[str, Path, int]]
) -> tuple[Path | None, Path]:
    """Update directory or queue an inline program from one simple command."""
    if words[0] in ("cd", "pushd") and len(words) > 1:
        return None, (cwd / os.path.expanduser(words[1])).resolve()
    blocked, inline = _inspect_simple(words, cwd)
    if inline is not None:
        pending.append((inline, cwd, depth + 1))
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
