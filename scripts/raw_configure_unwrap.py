# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Command-prefix and wrapper unwrapping of the raw configure guard."""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Ensure repository root is on sys.path for test/tool imports
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.raw_configure_words import (  # noqa: E402
    ASSIGNMENT,
    PREFIX_SIMPLE,
    WRAPPER_VALUE_OPTIONS,
)


def _skip_assignment(words: list[str], idx: int) -> int:
    """Advance past leading assignments without inspecting their values."""
    words_len = len(words)
    for _ in range(words_len):
        if idx >= words_len or not ASSIGNMENT.fullmatch(words[idx]):
            break
        idx += 1
    return idx


def _skip_simple_prefix(words: list[str], idx: int) -> int:
    """Skip one simple shell keyword or utility prefix if present."""
    if idx >= len(words):
        return idx
    word = words[idx]
    if word in PREFIX_SIMPLE:
        idx += 1
        while idx < len(words) and words[idx] in ("-p", "--", "-c", "-w", "--wait"):
            idx += 1
        return idx
    if word == "function":
        idx += 2 if idx + 1 < len(words) else 1
        return idx + 1 if idx < len(words) and words[idx] == "()" else idx
    if idx + 1 < len(words) and words[idx + 1] == "()":
        return idx + 2
    return idx


def _skip_env(words: list[str], idx: int) -> int:
    """Advance past env options and environment variable assignments."""
    idx += 1
    words_len = len(words)
    for _ in range(words_len):
        if idx >= words_len:
            break
        word = words[idx]
        if word == "--":
            return idx + 1
        if word in ("-u", "--unset", "-C", "--chdir", "-S", "--split-string"):
            idx += 2
        elif (
            word.startswith(("--unset=", "--chdir=", "--split-string="))
            or word.startswith("-")
            or ASSIGNMENT.fullmatch(word)
        ):
            idx += 1
        else:
            break
    return idx


def _skip_bracketed_runner(words: list[str], idx: int) -> int:
    """Advance past command wrappers delimited by double dashes."""
    if "--" in words[idx:]:
        return idx + words[idx:].index("--") + 1
    idx += 1
    words_len = len(words)
    for _ in range(words_len):
        if idx >= words_len:
            break
        if (
            words[idx] in ("-p", "--property", "--unit", "-u", "--description")
            and idx + 1 < words_len
        ):
            idx += 2
        elif words[idx].startswith("-"):
            idx += 1
        else:
            break
    return idx


def _skip_optioned_wrapper(words: list[str], idx: int) -> int:
    """Advance past a Unix execution wrapper and its option arguments."""
    name = os.path.basename(words[idx])
    value_options = WRAPPER_VALUE_OPTIONS.get(name, frozenset())
    idx += 1
    words_len = len(words)
    for _ in range(words_len):
        if idx >= words_len:
            break
        word = words[idx]
        if word == "--":
            return idx + 1
        if word in value_options:
            idx += 2 if idx + 1 < words_len else 1
            continue
        if word.startswith(("-", "+")):
            idx += 1
            continue
        if name in ("timeout", "flock", "chrt", "taskset"):
            idx += 1
            name = ""
            continue
        break
    if idx < len(words) and words[idx] == "--":
        idx += 1
    return idx


def _skip_applet_wrapper(words: list[str], idx: int) -> int:
    """Advance past busybox or toybox wrapper and leading options to the applet."""
    idx += 1
    words_len = len(words)
    for _ in range(words_len):
        if idx >= words_len or not words[idx].startswith("-"):
            break
        idx += 1
    return idx


def _skip_complex_wrapper(words: list[str], idx: int) -> int:
    """Advance past complex command wrappers with arguments."""
    if idx >= len(words):
        return idx
    name = os.path.basename(words[idx])
    if name == "env":
        return _skip_env(words, idx)
    if name in ("systemd-run", "praetorctl"):
        return _skip_bracketed_runner(words, idx)
    if name in ("busybox", "toybox"):
        return _skip_applet_wrapper(words, idx)
    if name in ("nice", "timeout", "sudo", "stdbuf", "flock", "chrt", "ionice", "taskset"):
        return _skip_optioned_wrapper(words, idx)
    return idx


def _unwrap_command(words: list[str], start: int) -> int:
    """Find the command-position index by unwrapping shell prefixes and wrappers."""
    idx = start
    words_len = len(words)
    for _ in range(words_len):
        if idx >= words_len:
            break
        prev = idx
        idx = _skip_assignment(words, idx)
        idx = _skip_simple_prefix(words, idx)
        if idx != prev:
            continue
        idx = _skip_complex_wrapper(words, idx)
        if idx == prev:
            break
    return idx


def _command_start(words: list[str]) -> int:
    """Find the index of the first command word after shell prefix tokens."""
    return _unwrap_command(words, 0)
