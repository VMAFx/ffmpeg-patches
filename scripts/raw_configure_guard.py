#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Refuse raw configure execution across directories before a shell can write secrets.

Approach: directory-free refusal. Refuse every command-position configure word in
any directory unless configure is started through scripts/ffmpeg_configure.py.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

# Ensure repository root is on sys.path for test/tool imports
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.raw_configure_inspect import (  # noqa: E402
    _classify_command,
    _inspect_shell_or_eval,
    _shell_argument,
    _unwrap_command,
)
from scripts.raw_configure_lex import (  # noqa: E402
    _restore_tokens,
    _scan_heredocs,
    _scan_herestrings,
    _scan_substitutions,
    _strip_redirections,
    _tokens,
)
from scripts.raw_configure_words import (  # noqa: E402
    DECIDABLE_NAME,
    MAX_DEPTH,
    MAX_INPUT,
    MAX_TOKENS,
    SEPARATORS,
    SHELLS,
    UNPARSED,
    _find_literal_assignments,
    _is_configure_word,
    _substitute_literals,
)


def _input(client: str, payload: dict[str, Any]) -> str | None:
    """Extract the documented shell command from the hook payload."""
    if client == "opencode":
        if payload.get("tool") != "bash":
            return None
        arguments = payload.get("args")
    else:
        expected = "run_shell_command" if client == "gemini" else "Bash"
        if payload.get("tool_name") != expected:
            return None
        arguments = payload.get("tool_input")
    if not isinstance(arguments, dict):
        return None
    command = arguments.get("command")
    return command if isinstance(command, str) and command else None


def _collect_extra_inners(
    heredocs: dict[str, str], expanding_tags: set[str], hs_to_scan: list[str]
) -> list[str] | None:
    """Collect command substitutions from unquoted heredocs and here-strings."""
    inners: list[str] = []
    for tag in expanding_tags:
        body_sub = _scan_substitutions(heredocs[tag], literal_quotes=True)
        if body_sub is None:
            return None
        inners.extend(body_sub[1])
    for hs_val in hs_to_scan:
        hs_sub = _scan_substitutions(hs_val, literal_quotes=True)
        if hs_sub is None:
            return None
        inners.extend(hs_sub[1])
    return inners


def _split_pipelines(tokens: list[str]) -> list[list[list[str]]]:
    """Group tokens separated by control operators into pipelines."""
    pipelines: list[list[list[str]]] = []
    curr_pipeline: list[list[str]] = []
    curr_stage: list[str] = []
    for tok in [*tokens, ";"]:
        if tok == "|":
            if curr_stage:
                curr_pipeline.append(curr_stage)
                curr_stage = []
            continue
        if tok in SEPARATORS:
            if curr_stage:
                curr_pipeline.append(curr_stage)
                curr_stage = []
            if curr_pipeline:
                pipelines.append(curr_pipeline)
                curr_pipeline = []
            continue
        curr_stage.append(tok)
    return pipelines


def _tokenize_command(
    command: str,
) -> tuple[list[list[list[str]]], dict[str, str], dict[str, str], list[str]] | None:
    """Extract heredocs, substitutions, literal assignments, and pipeline tokens."""
    hd_info = _scan_heredocs(command)
    if hd_info is None:
        return None
    rewritten_hd, heredocs, expanding_tags = hd_info
    rewritten_hs, herestrings, hs_to_scan = _scan_herestrings(rewritten_hd)
    stdin_map = {**heredocs, **herestrings}
    sub_info = _scan_substitutions(rewritten_hs)
    if sub_info is None:
        return None
    rewritten_sub, inners, reps = sub_info
    extra_inners = _collect_extra_inners(heredocs, expanding_tags, hs_to_scan)
    if extra_inners is None:
        return None
    inners.extend(extra_inners)
    raw_tokens = _tokens(rewritten_sub)
    if raw_tokens is None:
        return None
    tokens = _restore_tokens(raw_tokens, reps)
    assignments = _find_literal_assignments(tokens)
    return _split_pipelines(tokens), assignments, stdin_map, inners


def _handle_nested_program(source: str, depth: int, pending: list[tuple[str, int]]) -> Path | None:
    """Enforce nesting depth and queue a nested program fragment."""
    if depth > MAX_DEPTH:
        return Path("configure (nesting limit)")
    pending.append((source, depth))
    return None


def _translate_printf(fmt: str, extra_args: list[str]) -> str | None:
    """Translate printf format with safe backslash escapes or fail closed."""
    if "%" in fmt:
        return None
    out: list[str] = []
    i = 0
    fmt_len = len(fmt)
    for _ in range(fmt_len):
        if i >= fmt_len:
            break
        ch = fmt[i]
        if ch == "\\":
            if i + 1 >= fmt_len:
                return None
            nxt = fmt[i + 1]
            if nxt == "n":
                out.append("\n")
            elif nxt == "t":
                out.append("\t")
            elif nxt == "\\":
                out.append("\\")
            else:
                return None
            i += 2
            continue
        out.append(ch)
        i += 1
    translated = "".join(out)
    return "\n".join([translated, *extra_args]) if extra_args else translated


def _has_unresolved_var_or_sub(arg: str) -> bool:
    """Check if an argument contains unresolved variables or command substitutions."""
    return bool(re.search(r"\$[A-Za-z_{]", arg) or "`" in arg or "__RCG_SUB_" in arg)


def _inspect_echo_printf(
    prev_name: str,
    prev_words: list[str],
    prev_idx: int,
    assignments: dict[str, str],
) -> tuple[bool, str | None]:
    """Inspect echo or printf producer stage and build nested program text if valid."""
    raw_args = prev_words[prev_idx + 1 :]
    sub_args = [_substitute_literals(a, assignments) for a in raw_args]
    if any(_has_unresolved_var_or_sub(a) for a in sub_args):
        return False, None
    if prev_name == "echo":
        return True, " ".join(sub_args)
    if not sub_args:
        return False, None
    translated = _translate_printf(sub_args[0], sub_args[1:])
    return (True, translated) if translated is not None else (False, None)


def _inspect_cat_stdin(
    prev_words: list[str],
    prev_idx: int,
    prev_attached: list[str],
    stdin_map: dict[str, str],
) -> tuple[bool, str | None]:
    """Inspect cat producer stage and extract body if reading from heredoc stdin."""
    cat_args = prev_words[prev_idx + 1 :]
    if any(not a.startswith("-") or a == "--" for a in cat_args):
        return False, None
    if prev_attached and prev_attached[0] in stdin_map:
        return True, stdin_map.get(prev_attached[0])
    return False, None


def _decide_shell_stdin(
    prev_stage: list[str] | None,
    stdin_prog: str | None,
    assignments: dict[str, str],
    stdin_map: dict[str, str],
    shell_word: str = "shell stdin",
) -> tuple[Path | None, str | None]:
    """Determine the stdin program source for a shell without script or inline option."""
    if stdin_prog is not None:
        return None, stdin_prog
    if prev_stage is None:
        return Path(shell_word), None
    prev_words, prev_attached = _strip_redirections(prev_stage)
    prev_idx = _unwrap_command(prev_words, 0)
    if prev_idx >= len(prev_words):
        return Path(shell_word), None
    prev_name = os.path.basename(_substitute_literals(prev_words[prev_idx], assignments))
    if prev_name in ("echo", "printf"):
        ok, text = _inspect_echo_printf(prev_name, prev_words, prev_idx, assignments)
        return (None, text) if (ok and text is not None) else (Path(shell_word), None)
    if prev_name == "cat":
        ok, body = _inspect_cat_stdin(prev_words, prev_idx, prev_attached, stdin_map)
        return (None, body) if (ok and body is not None) else (Path(shell_word), None)
    return Path(shell_word), None


def _inspect_shell_stage(
    shell_words: list[str],
    prev_stage: list[str] | None,
    stdin_prog: str | None,
    assignments: dict[str, str],
    stdin_map: dict[str, str],
) -> tuple[Path | None, str | None]:
    """Inspect a shell stage for inline script, file operand, or stdin feeding."""
    target, inline = _shell_argument(shell_words)
    if inline and target is not None:
        return None, target
    if not inline and target is not None and target != "-":
        target = _substitute_literals(target, assignments)
        if _is_configure_word(target) or not DECIDABLE_NAME.fullmatch(os.path.basename(target)):
            return Path(target), None
        return None, None
    return _decide_shell_stdin(prev_stage, stdin_prog, assignments, stdin_map, shell_words[0])


def _inspect_pipeline(
    pipeline: list[list[str]],
    depth: int,
    pending: list[tuple[str, int]],
    assignments: dict[str, str],
    stdin_map: dict[str, str],
) -> Path | None:
    """Inspect pipeline stages and refuse unsafe shell execution feeds."""
    for idx, raw_stage in enumerate(pipeline):
        words, attached = _strip_redirections(raw_stage)
        stdin_prog = stdin_map.get(attached[0]) if attached else None
        cmd_idx = _unwrap_command(words, 0)
        if cmd_idx < len(words):
            name = os.path.basename(_substitute_literals(words[cmd_idx], assignments))
            if name in SHELLS:
                prev_stage = pipeline[idx - 1] if idx > 0 else None
                blocked, to_queue = _inspect_shell_stage(
                    words[cmd_idx:], prev_stage, stdin_prog, assignments, stdin_map
                )
                if blocked is not None:
                    return blocked
                if to_queue is not None:
                    pending.append((to_queue, depth + 1))
                continue
            if name in ("eval", "source", "."):
                blocked, to_queue = _inspect_shell_or_eval(words, cmd_idx, stdin_prog, assignments)
                if blocked is not None:
                    return blocked
                if to_queue is not None:
                    pending.append((to_queue, depth + 1))
                continue
        blocked, _ = _classify_command(words, assignments, stdin_prog)
        if blocked is not None:
            return blocked
    return None


def _inspect(command: str) -> Path | None:
    """Walk bounded shell fragments and inline programs without recursion."""
    pending = [(command, 0)]
    for _ in range(MAX_TOKENS + 1):
        if not pending:
            return None
        source, depth = pending.pop()
        if depth > MAX_DEPTH:
            return Path("configure (nesting limit)")
        tok_result = _tokenize_command(source)
        if tok_result is None:
            return UNPARSED
        pipelines, assignments, stdin_map, inners = tok_result
        for inner in inners:
            nested_err = _handle_nested_program(inner, depth + 1, pending)
            if nested_err is not None:
                return nested_err
        for pipeline in pipelines:
            blocked = _inspect_pipeline(pipeline, depth, pending, assignments, stdin_map)
            if blocked is not None:
                return blocked
    return Path("configure (worklist limit)")


def _print_refusal(detail: str = "") -> None:
    """Print the standardized raw configure refusal error message."""
    prefix = f"raw configure refused: {detail}; " if detail else "raw configure refused: "
    print(
        f"{prefix}run python3 scripts/ffmpeg_configure.py "
        "SRC_DIR BUILD_DIR -- <args>; it runs SRC_DIR/configure for FFmpeg "
        "or any other configure tree (GCC ...) with a filtered environment "
        "so config.log holds no secrets",
        file=sys.stderr,
    )


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
        _print_refusal()
        return 2
    try:
        command = _input(options.client, payload)
        if command is None:
            return 0
        blocked = _inspect(command)
    except Exception as exc:
        _print_refusal(f"parse error ({exc})")
        return 2
    if blocked is None:
        return 0
    if blocked is UNPARSED:
        print(
            "Raw configure guard refused a command it cannot parse (size limit, "
            "unbalanced quote, or heredoc or comment text with an apostrophe); "
            "split the command, or move quoted text into a file (git commit -F FILE, "
            "gh pr create --body-file FILE)",
            file=sys.stderr,
        )
        return 2
    _print_refusal(str(blocked))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
