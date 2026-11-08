# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Shell tokenizer, substitution and heredoc scanners of the raw configure guard."""

from __future__ import annotations

import re
import shlex
import sys
from pathlib import Path

# Ensure repository root is on sys.path for test/tool imports
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.raw_configure_words import (  # noqa: E402
    MAX_COMMAND,
    MAX_SUBSHELL_DEPTH,
    MAX_TOKENS,
    REDIRECT_TARGET_OPS,
    SELF_REDIRECT,
    SEPARATORS,
)


def _append_redirect_fd(punc: str, tokens: list[str], raw_tokens: list[str], idx: int) -> int:
    """Attach a redirection operator and any immediate target descriptor."""
    if tokens and tokens[-1].isdigit():
        tokens[-1] += punc
    else:
        tokens.append(punc)
    if idx < len(raw_tokens) and (raw_tokens[idx].isdigit() or raw_tokens[idx] == "-"):
        tokens[-1] += raw_tokens[idx]
        return idx + 1
    return idx


def _append_punctuation(token: str, tokens: list[str], raw_tokens: list[str], idx: int) -> int:
    """Expand one punctuation run and merge shell redirect operators."""
    pos = 0
    for _ in range(len(token)):
        if pos >= len(token):
            break
        if token[pos : pos + 3] == "&>>":
            tokens.append("&>>")
            pos += 3
        elif token[pos : pos + 2] in (">&", "<&"):
            idx = _append_redirect_fd(token[pos : pos + 2], tokens, raw_tokens, idx)
            pos += 2
        elif token[pos : pos + 2] == "&>":
            tokens.append("&>")
            pos += 2
        elif token[pos : pos + 2] == ">|":
            tokens.append(">")
            tokens.append("|")
            pos += 2
        elif token[pos : pos + 2] in ("&&", "||", ">>", "<<"):
            tokens.append(token[pos : pos + 2])
            pos += 2
        elif token[pos : pos + 2] == "|&":
            tokens.append("|")
            pos += 2
        else:
            tokens.append(token[pos])
            pos += 1
    return idx


def _update_quote(char: str, quote: str | None, allow_quotes: bool = True) -> str | None:
    """Update current quote state when encountering quote delimiters."""
    if not allow_quotes:
        return None
    if quote is None and char in ("'", '"'):
        return char
    if quote is not None and char == quote:
        return None
    return quote


def _scan_substitution_body(command: str, start: int) -> tuple[int, int] | None:
    """Find the closing parenthesis of a command substitution without recursion."""
    depth = 1
    idx = start
    quote: str | None = None
    cmd_len = len(command)
    for _ in range(cmd_len):
        if idx >= cmd_len:
            break
        char = command[idx]
        if char == "\\":
            idx += 2
            continue
        if quote is not None:
            quote = _update_quote(char, quote)
            idx += 1
            continue
        if char in ("'", '"'):
            quote = char
            idx += 1
            continue
        if command[idx : idx + 2] in ("$(", "<(", ">("):
            depth += 1
            if depth > MAX_SUBSHELL_DEPTH:
                return None
            idx += 2
            continue
        if char == ")":
            depth -= 1
            if depth == 0:
                return start, idx
        idx += 1
    return None


def _scan_backtick(command: str, idx: int) -> int | None:
    """Find the closing backtick of a command substitution."""
    end_idx = command.find("`", idx + 1)
    return None if end_idx == -1 else end_idx


def _record_sub(full_sub: str, inner: str, reps: list[tuple[str, str]], inners: list[str]) -> str:
    """Record one substitution placeholder and inner command."""
    tag = f"__RCG_SUB_{len(reps)}__"
    reps.append((tag, full_sub))
    inners.append(inner)
    return tag


def _scan_substitutions(
    command: str,
    *,
    literal_quotes: bool = False,
) -> tuple[str, list[str], list[tuple[str, str]]] | None:
    """Find command and process substitutions before shlex parsing without recursion."""
    if len(command.encode("utf-8", "surrogatepass")) > MAX_COMMAND:
        return None
    pieces: list[str] = []
    inners: list[str] = []
    replacements: list[tuple[str, str]] = []
    idx = 0
    quote: str | None = None
    cmd_len = len(command)
    for _ in range(cmd_len):
        if idx >= cmd_len:
            break
        char = command[idx]
        if char == "\\":
            pieces.append(command[idx : idx + 2])
            idx += 2
            continue
        if quote != "'":
            if command[idx : idx + 2] in ("$(", "<(", ">("):
                b = _scan_substitution_body(command, idx + 2)
                if b is None:
                    return None
                tag = _record_sub(
                    command[idx : b[1] + 1], command[b[0] : b[1]], replacements, inners
                )
                pieces.append(tag)
                idx = b[1] + 1
                continue
            if char == "`":
                end_idx = _scan_backtick(command, idx)
                if end_idx is None:
                    return None
                tag = _record_sub(
                    command[idx : end_idx + 1], command[idx + 1 : end_idx], replacements, inners
                )
                pieces.append(tag)
                idx = end_idx + 1
                continue
        quote = _update_quote(char, quote, not literal_quotes)
        pieces.append(char)
        idx += 1
    return "".join(pieces), inners, replacements


def _restore_tokens(tokens: list[str], replacements: list[tuple[str, str]]) -> list[str]:
    """Restore command substitution placeholders in parsed tokens."""
    if not replacements:
        return tokens
    restored = list(tokens)
    for tag, full_sub in replacements:
        restored = [tok.replace(tag, full_sub) for tok in restored]
    return restored


def _read_delim(line: str, pos: int, line_len: int) -> tuple[str, int, str | None]:
    """Read a heredoc delimiter token starting at the given position."""
    if pos < line_len and line[pos] in ("'", '"'):
        q_char = line[pos]
        pos += 1
        start_q = pos
        for _ in range(line_len - pos + 1):
            if pos >= line_len or line[pos] == q_char:
                break
            pos += 1
        delim = line[start_q:pos]
        return delim, (pos + 1 if pos < line_len else pos), q_char
    quoted: str | None = None
    if pos < line_len and line[pos] == "\\":
        quoted = "\\"
        pos += 1
    start_d = pos
    for _ in range(line_len - pos + 1):
        if pos >= line_len or line[pos] in " \t;&|<>()\n\r":
            break
        if line[pos] in ("\\", "'", '"'):
            quoted = "\\"
        pos += 1
    return line[start_d:pos], pos, quoted


def _parse_heredoc_at(line: str, pos: int, line_len: int) -> tuple[int, str, bool, bool] | None:
    """Parse heredoc marker details starting after << operator."""
    tab_strip = pos < line_len and line[pos] == "-"
    pos += 1 if tab_strip else 0
    for _ in range(line_len - pos):
        if line[pos] not in " \t":
            break
        pos += 1
    delim, pos, q_delim = _read_delim(line, pos, line_len)
    if delim:
        return pos, delim, tab_strip, q_delim is not None
    return None


def _scan_heredoc_line(line: str, line_len: int) -> tuple[int, int, str, bool, bool] | None:
    """Find one heredoc marker and delimiter on a line outside quotes."""
    quote = None
    pos = 0
    for _ in range(line_len):
        if pos >= line_len:
            break
        ch = line[pos]
        if quote is None:
            if ch in ("'", '"'):
                quote = ch
            elif line[pos : pos + 3] == "<<<":
                pos += 3
                continue
            elif line[pos : pos + 2] == "<<":
                start_hd = pos
                parsed = _parse_heredoc_at(line, pos + 2, line_len)
                if parsed is not None:
                    end_pos, delim, tab_strip, is_quoted = parsed
                    return start_hd, end_pos, delim, tab_strip, is_quoted
        elif ch == quote:
            quote = None
        pos += 1
    return None


def _scan_heredocs(command: str) -> tuple[str, dict[str, str], set[str]] | None:
    """Extract heredocs outside quotes and replace declarations with placeholders."""
    if len(command) > MAX_COMMAND:
        return None
    lines = command.splitlines(keepends=True)
    out_lines: list[str] = []
    heredocs: dict[str, str] = {}
    expanding_tags: set[str] = set()
    idx = 0
    num_lines = len(lines)
    for _ in range(num_lines):
        if idx >= num_lines:
            break
        line = lines[idx]
        idx += 1
        found_hd = _scan_heredoc_line(line, len(line))
        if found_hd is None:
            out_lines.append(line)
            continue
        start_hd, end_hd, delim, tab_strip, is_quoted = found_hd
        tag = f"__RCG_HD_{len(heredocs)}__"
        if not is_quoted:
            expanding_tags.add(tag)
        out_lines.append(line[:start_hd] + tag + line[end_hd:])
        body: list[str] = []
        closed = False
        for _ in range(num_lines - idx + 1):
            if idx >= num_lines:
                break
            b_line = lines[idx]
            idx += 1
            check = b_line.lstrip("\t") if tab_strip else b_line
            if check.rstrip("\r\n") == delim:
                closed = True
                break
            body.append(b_line)
        if not closed:
            return None
        heredocs[tag] = "".join(body)
    return "".join(out_lines), heredocs, expanding_tags


def _scan_herestrings(command: str) -> tuple[str, dict[str, str], list[str]]:
    """Extract here-strings outside quotes and replace with placeholders."""
    herestrings: dict[str, str] = {}
    hs_to_scan: list[str] = []
    quote = None
    pos = 0
    cmd_len = len(command)
    pieces: list[str] = []
    for _ in range(cmd_len):
        if pos >= cmd_len:
            break
        ch = command[pos]
        if quote is None:
            if ch in ("'", '"'):
                quote = ch
            elif command[pos : pos + 3] == "<<<":
                pos += 3
                for _ in range(cmd_len - pos):
                    if command[pos] not in " \t":
                        break
                    pos += 1
                val, pos, q_delim = _read_delim(command, pos, cmd_len)
                tag = f"__RCG_HS_{len(herestrings)}__"
                herestrings[tag] = val
                if q_delim != "'":
                    hs_to_scan.append(val)
                pieces.append(tag)
                continue
        elif ch == quote:
            quote = None
        pieces.append(ch)
        pos += 1
    return "".join(pieces), herestrings, hs_to_scan


def _tokens(command: str) -> list[str] | None:
    """Split a shell command without executing substitutions or expansions."""
    if len(command.encode("utf-8", "surrogatepass")) > MAX_COMMAND:
        return None
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>()\n")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        raw_tokens = list(lexer)
    except ValueError:
        return None
    tokens: list[str] = []
    idx = 0
    raw_slice = raw_tokens[: MAX_TOKENS + 2]
    for _ in range(len(raw_slice)):
        if idx >= len(raw_slice):
            break
        token = raw_slice[idx]
        idx += 1
        if all(c in ";&|<>()\n" for c in token):
            idx = _append_punctuation(token, tokens, raw_slice, idx)
            continue
        tokens.append(token)
    return tokens if len(tokens) <= MAX_TOKENS else None


def _strip_redirections(tokens: list[str]) -> tuple[list[str], list[str]]:
    """Separate command words from redirection operators, targets, and heredoc tags."""
    words: list[str] = []
    attached: list[str] = []
    idx = 0
    tok_len = len(tokens)
    for _ in range(tok_len):
        if idx >= tok_len:
            break
        tok = tokens[idx]
        idx += 1
        if tok.startswith("__RCG_HD_") or tok.startswith("__RCG_HS_"):
            attached.append(tok)
            continue
        if tok in REDIRECT_TARGET_OPS or re.fullmatch(r"^\d*(?:>>|>|<|&>|&>>|>\|)$", tok):
            if idx < tok_len and tokens[idx] not in SEPARATORS:
                idx += 1
            continue
        if SELF_REDIRECT.fullmatch(tok) or tok in (">&", "<&"):
            continue
        words.append(tok)
    return words, attached
