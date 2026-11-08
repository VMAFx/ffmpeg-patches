#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Refuse raw configure execution across directories before a shell can write secrets.

Approach: directory-free refusal. Refuse every command-position configure word in
any directory unless configure is started through scripts/ffmpeg_configure.py.
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
MAX_SUBSHELL_DEPTH = 64
SEPARATORS = frozenset((";", "&&", "||", "|", "&", "\n", "(", ")"))
SHELLS = frozenset(("bash", "sh", "dash", "zsh", "ksh"))
UNPARSED = Path("unparsed command")
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z_0-9]*=.*", re.DOTALL)
CONFIGURE_WORD = re.compile(r"(?<![A-Za-z0-9_])configure(?![A-Za-z0-9_])")
PREFIX_SIMPLE = frozenset(
    (
        "if",
        "then",
        "elif",
        "else",
        "do",
        "while",
        "until",
        "!",
        "{",
        "}",
        "time",
        "command",
        "builtin",
        "nohup",
        "setsid",
        "exec",
    )
)
DYNAMIC_VARIABLE = re.compile(r"^\$[A-Za-z_][A-Za-z0-9_]*")
BRACED_VARIABLE = re.compile(r"\$\{[^{}]*\}")
NAMED_VARIABLE = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*")
STRIP_CHARS = str.maketrans("", "", "'\"\\")
INTERPRETERS = re.compile(r"^(?:python|perl|ruby|node|gawk|mawk|awk|php)(?:\d+(?:\.\d+)*[a-z]*)?$")
GLOB_CHARS = frozenset(("*", "?", "["))
DECIDABLE_NAME = re.compile(r"[A-Za-z0-9._+,:@%#=*?\[\]!^-]*")
GIT_DISALLOWED_OPTIONS = frozenset(
    (
        "--config-env",
        "--exec-path",
        "-O",
        "--open-files-in-pager",
        "--ext-diff",
        "-x",
        "--exec",
    )
)
GIT_GLOBAL_OPTS_WITH_ARG = frozenset(
    (
        "-C",
        "--git-dir",
        "--work-tree",
        "--namespace",
        "--super-prefix",
    )
)
REDIRECT_TARGET_OPS = frozenset(
    (
        ">",
        ">>",
        "<",
        "&>",
        "&>>",
        ">|",
        "0<",
        "1>",
        "2>",
        "1>>",
        "2>>",
        "3>",
        "3<",
    )
)
SELF_REDIRECT = re.compile(r"^\d*(?:>&|<&)(?:\d+|-)$")
SED_EXEC_PATTERN = re.compile(r"(?:^|;|\n|\s)\d*(?:[a-z]*e\b|\s*e\s+)(.*)", re.DOTALL)
SED_SUBST_EXEC = re.compile(r"s(/|\||:).*?\1.*?\1.*?e", re.DOTALL)
EXEC_ENV_VARS = frozenset(
    (
        "GIT_PAGER",
        "GIT_EXTERNAL_DIFF",
        "GIT_EDITOR",
        "GIT_SEQUENCE_EDITOR",
        "LESSOPEN",
        "BASH_ENV",
        "PAGER",
        "EDITOR",
        "VISUAL",
        "ENV",
    )
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


def _clean_word(word: str) -> str:
    """Strip assignments, shell command prefixes, and whitespace from a word."""
    if "=" in word:
        word = word.split("=", 1)[1]
    if " " in word:
        parts = word.split()
        word = parts[-1] if parts else ""
    return word.lstrip("+!|")


def _flatten(text: str) -> str:
    """Normalize dynamic and quoted tokens to unquoted literal forms."""
    text = BRACED_VARIABLE.sub("", text)
    text = text.replace("$'", "'")
    text = NAMED_VARIABLE.sub("", text)
    return text.translate(STRIP_CHARS)


def _is_configure_word(word: str) -> bool:
    """Check whether a command word is or could expand to configure."""
    cleaned = _clean_word(word)
    unquoted = cleaned.translate(STRIP_CHARS)
    base = os.path.basename(unquoted)
    if base == "configure":
        return True
    last = os.path.basename(cleaned)
    if _flatten(last) == "configure":
        return True
    if ("$" in word or "`" in word) and CONFIGURE_WORD.search(word):
        return True
    if not DECIDABLE_NAME.fullmatch(last):
        return True
    return bool(GLOB_CHARS.intersection(last)) and fnmatch.fnmatchcase("configure", last)


def _find_configure_word(words: list[str]) -> Path | None:
    """Find the first blocked configure reference across command words."""
    for word in words[: MAX_TOKENS + 1]:
        if _is_configure_word(word):
            return Path(word)
    return None


def _update_quote(char: str, quote: str | None) -> str | None:
    """Update current quote state when encountering quote delimiters."""
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
        quote = _update_quote(char, quote)
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


def _read_delim(line: str, pos: int, line_len: int) -> tuple[str, int]:
    """Read a heredoc delimiter token starting at the given position."""
    if pos < line_len and line[pos] in ("'", '"'):
        q_char = line[pos]
        pos += 1
        start_q = pos
        while pos < line_len and line[pos] != q_char:
            pos += 1
        delim = line[start_q:pos]
        return delim, (pos + 1 if pos < line_len else pos)
    if pos < line_len and line[pos] == "\\":
        pos += 1
    start_d = pos
    while pos < line_len and line[pos] not in " \t;&|<>()\n\r":
        pos += 1
    return line[start_d:pos], pos


def _scan_heredoc_line(line: str, line_len: int) -> tuple[int, int, str, bool] | None:
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
                pos += 2
                tab_strip = pos < line_len and line[pos] == "-"
                pos += 1 if tab_strip else 0
                while pos < line_len and line[pos] in " \t":
                    pos += 1
                delim, pos = _read_delim(line, pos, line_len)
                if delim:
                    return start_hd, pos, delim, tab_strip
        elif ch == quote:
            quote = None
        pos += 1
    return None


def _scan_heredocs(command: str) -> tuple[str, dict[str, str]] | None:
    """Extract heredocs outside quotes and replace declarations with placeholders."""
    if len(command) > MAX_COMMAND:
        return None
    lines = command.splitlines(keepends=True)
    out_lines: list[str] = []
    heredocs: dict[str, str] = {}
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
        start_hd, end_hd, delim, tab_strip = found_hd
        tag = f"__RCG_HD_{len(heredocs)}__"
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
    return "".join(out_lines), heredocs


def _scan_herestrings(command: str) -> tuple[str, dict[str, str]]:
    """Extract here-strings outside quotes and replace with placeholders."""
    herestrings: dict[str, str] = {}
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
                while pos < cmd_len and command[pos] in " \t":
                    pos += 1
                val, pos = _read_delim(command, pos, cmd_len)
                tag = f"__RCG_HS_{len(herestrings)}__"
                herestrings[tag] = val
                pieces.append(tag)
                continue
        elif ch == quote:
            quote = None
        pieces.append(ch)
        pos += 1
    return "".join(pieces), herestrings


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


def _find_literal_assignments(tokens: list[str]) -> dict[str, str]:
    """Find unique literal assignments whose names appear once in the command."""
    assign_counts: dict[str, int] = {}
    assign_literals: dict[str, str] = {}
    for tok in tokens[: MAX_TOKENS + 1]:
        if "=" not in tok:
            continue
        name, val = tok.split("=", 1)
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            continue
        assign_counts[name] = assign_counts.get(name, 0) + 1
        if not ("$" in val or "`" in val or "__RCG_SUB_" in val):
            assign_literals[name] = val.translate(STRIP_CHARS)
    return {
        k: assign_literals[k] for k, c in assign_counts.items() if c == 1 and k in assign_literals
    }


def _substitute_literals(word: str, assignments: dict[str, str]) -> str:
    """Substitute known unique literal assignments into an inspected position."""
    for name, val in assignments.items():
        word = re.sub(r"\$\{" + re.escape(name) + r"\}", val, word)
        word = re.sub(r"\$" + re.escape(name) + r"(?![A-Za-z0-9_])", val, word)
    return word


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
    idx += 1
    words_len = len(words)
    for _ in range(words_len):
        if idx >= words_len:
            break
        word = words[idx]
        if word == "--":
            return idx + 1
        if word in (
            "-n",
            "-u",
            "-g",
            "-h",
            "-p",
            "-C",
            "-s",
            "-k",
            "-I",
            "-P",
            "-d",
            "-E",
            "-a",
            "-w",
            "-c",
        ):
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


def _skip_complex_wrapper(words: list[str], idx: int) -> int:
    """Advance past complex command wrappers with arguments."""
    if idx >= len(words):
        return idx
    name = os.path.basename(words[idx])
    if name == "env":
        return _skip_env(words, idx)
    if name in ("systemd-run", "praetorctl"):
        return _skip_bracketed_runner(words, idx)
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


def _shell_argument(words: list[str]) -> tuple[str | None, bool]:
    """Find a shell script path or its -c program string."""
    idx = 1
    c_seen = False
    for _ in range(min(len(words), MAX_TOKENS + 1)):
        if idx >= len(words):
            break
        word = words[idx]
        if word == "--":
            operand = words[idx + 1] if idx + 1 < len(words) else None
            return operand, c_seen
        if word == "-":
            return word, c_seen
        if word in ("-o", "+o", "-O", "+O", "--rcfile", "--init-file"):
            idx += 2
            continue
        if word.startswith("--"):
            idx += 1
            continue
        if word.startswith("+"):
            idx += 2 if word.endswith(("o", "O")) else 1
            continue
        if word.startswith("-"):
            if "c" in word[1:]:
                c_seen = True
            idx += 2 if word.endswith(("o", "O")) else 1
            continue
        return word, c_seen
    return None, c_seen


def _inspect_interpreter(words: list[str], idx: int, stdin_prog: str | None) -> Path | None:
    """Check interpreter -c/-e inline scripts and stdin programs for configure words."""
    words_len = len(words)
    for i in range(idx + 1, min(words_len, idx + 8)):
        if words[i] in ("-c", "-e") and i + 1 < words_len:
            code = words[i + 1]
            return Path(code) if CONFIGURE_WORD.search(code) else None
    if stdin_prog is not None and (
        CONFIGURE_WORD.search(stdin_prog) or CONFIGURE_WORD.search(_flatten(stdin_prog))
    ):
        return Path(stdin_prog[:50])
    name = os.path.basename(words[idx])
    if name in ("awk", "gawk", "mawk"):
        for arg in words[idx + 1 : idx + 4]:
            if "system(" in arg and CONFIGURE_WORD.search(arg):
                return Path(arg)
    return None


def _inspect_find(words: list[str], idx: int) -> Path | None:
    """Inspect find exec-capable positions for raw configure execution."""
    words_len = len(words)
    has_conf_name = any(
        words[i] in ("-name", "-iname") and i + 1 < words_len and "configure" in words[i + 1]
        for i in range(idx + 1, words_len)
    )
    for i in range(idx + 1, words_len):
        if words[i] in ("-exec", "-execdir", "-ok", "-okdir") and i + 1 < words_len:
            exec_args: list[str] = []
            for j in range(i + 1, min(words_len, i + 32)):
                if words[j] in (";", "\\;", "+"):
                    break
                exec_args.append(words[j])
            if has_conf_name and any("{}" in arg for arg in exec_args):
                return Path("find -exec configure")
            for arg in exec_args:
                if _is_configure_word(arg):
                    return Path(arg)
    return None


def _inspect_sed(words: list[str], idx: int) -> Path | None:
    """Inspect sed scripts for arbitrary command execution via e command or s///e flag."""
    words_len = len(words)
    for i in range(idx + 1, words_len):
        script = words[i + 1] if words[i] == "-e" and i + 1 < words_len else words[i]
        if words[i] == "-e" and i + 1 >= words_len:
            continue
        m_e = SED_EXEC_PATTERN.search(script)
        if m_e and (_is_configure_word(m_e.group(1)) or CONFIGURE_WORD.search(m_e.group(1))):
            return Path(script)
        if SED_SUBST_EXEC.search(script) and CONFIGURE_WORD.search(script):
            return Path(script)
        if not words[i].startswith("-"):
            break
    return None


def _inspect_git(words: list[str], idx: int) -> Path | None:
    """Inspect git command arguments for exec options executing configure."""
    words_len = len(words)
    for i in range(idx + 1, words_len):
        w = words[i]
        if w == "-c" and i + 1 < words_len and CONFIGURE_WORD.search(words[i + 1]):
            return Path(words[i + 1])
        if w.startswith("-c") and CONFIGURE_WORD.search(w):
            return Path(w)
        if w in ("-x", "--exec") and i + 1 < words_len and _is_configure_word(words[i + 1]):
            return Path(words[i + 1])
        if (w.startswith("--exec=") or w.startswith("--exec-path=")) and _is_configure_word(
            w.split("=", 1)[1]
        ):
            return Path(w)
        if (
            w in ("bisect", "submodule")
            and i + 2 < words_len
            and words[i + 1] in ("run", "foreach")
            and _is_configure_word(words[i + 2])
        ):
            return Path(words[i + 2])
    return None


def _inspect_xargs(
    words: list[str], idx: int, stdin_prog: str | None, assignments: dict[str, str]
) -> Path | None:
    """Inspect xargs wrapped command and attached stdin program for configure."""
    words_len = len(words)
    i = idx + 1
    for _ in range(words_len):
        if i >= words_len or not words[i].startswith("-"):
            break
        i += 2 if words[i] in ("-I", "-n", "-P", "-s", "-E", "-d", "-a", "-L") else 1
    if i >= words_len:
        return None
    target_cmd = _substitute_literals(words[i], assignments)
    if _is_configure_word(target_cmd) or not DECIDABLE_NAME.fullmatch(os.path.basename(target_cmd)):
        return Path(target_cmd)
    if os.path.basename(target_cmd) in SHELLS:
        if stdin_prog is not None and (
            _is_configure_word(stdin_prog) or CONFIGURE_WORD.search(stdin_prog)
        ):
            return Path(stdin_prog)
        target, _ = _shell_argument(words[i:])
        if target is not None and _is_configure_word(target):
            return Path(target)
    for arg in words[i + 1 :]:
        if _is_configure_word(arg):
            return Path(arg)
    return None


def _inspect_pager_and_rg(words: list[str], idx: int) -> Path | None:
    """Inspect rg and pager commands for command-execution options."""
    name = os.path.basename(words[idx])
    words_len = len(words)
    if name == "rg":
        for i in range(idx + 1, words_len):
            if words[i] == "--pre" and i + 1 < words_len and _is_configure_word(words[i + 1]):
                return Path(words[i + 1])
            if words[i].startswith("--pre=") and _is_configure_word(words[i].split("=", 1)[1]):
                return Path(words[i])
    elif name in ("less", "more"):
        for i in range(idx + 1, words_len):
            if words[i].startswith("+!") and CONFIGURE_WORD.search(words[i]):
                return Path(words[i])
            if words[i].startswith("+'!") and CONFIGURE_WORD.search(words[i]):
                return Path(words[i])
    return None


def _inspect_shell_or_eval(
    words: list[str],
    idx: int,
    stdin_prog: str | None,
    assignments: dict[str, str],
) -> tuple[Path | None, str | None]:
    """Process shell script operands, inline -c scripts, eval, and stdin programs."""
    name = os.path.basename(words[idx])
    if name in ("source", "."):
        if idx + 1 < len(words):
            target = _substitute_literals(words[idx + 1], assignments)
            if _is_configure_word(target) or not DECIDABLE_NAME.fullmatch(os.path.basename(target)):
                return Path(target), None
    elif name == "eval":
        return None, " ".join(words[idx + 1 :])
    elif name in SHELLS:
        target, inline = _shell_argument(words[idx:])
        if inline and target is not None:
            return None, target
        if stdin_prog is not None:
            return None, stdin_prog
        if target is not None:
            target = _substitute_literals(target, assignments)
            if _is_configure_word(target) or not DECIDABLE_NAME.fullmatch(os.path.basename(target)):
                return Path(target), None
    return None, None


def _tokenize_command(
    command: str,
) -> tuple[list[list[list[str]]], dict[str, str], dict[str, str], list[str]] | None:
    """Extract heredocs, substitutions, literal assignments, and pipeline tokens."""
    hd_info = _scan_heredocs(command)
    if hd_info is None:
        return None
    rewritten_hd, heredocs = hd_info
    rewritten_hs, herestrings = _scan_herestrings(rewritten_hd)
    stdin_map = {**heredocs, **herestrings}
    sub_info = _scan_substitutions(rewritten_hs)
    if sub_info is None:
        return None
    rewritten_sub, inners, reps = sub_info
    raw_tokens = _tokens(rewritten_sub)
    if raw_tokens is None:
        return None
    tokens = _restore_tokens(raw_tokens, reps)
    assignments = _find_literal_assignments(tokens)
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
    return pipelines, assignments, stdin_map, inners


def _inspect_prefix_env(words: list[str]) -> Path | None:
    """Check prefix environment variable assignments for command execution overrides."""
    for w in words:
        if not ASSIGNMENT.fullmatch(w):
            break
        var, val = w.split("=", 1)
        if var in EXEC_ENV_VARS and (_is_configure_word(val) or CONFIGURE_WORD.search(val)):
            return Path(w)
    return None


def _classify_command(
    words: list[str], assignments: dict[str, str], stdin_prog: str | None = None
) -> tuple[Path | None, list[str]]:
    """Determine whether inspected command positions run configure or require queuing."""
    env_blocked = _inspect_prefix_env(words)
    if env_blocked is not None:
        return env_blocked, []
    cmd_idx = _unwrap_command(words, 0)
    if cmd_idx >= len(words):
        return None, []
    raw_cmd = words[cmd_idx]
    cmd_word = _substitute_literals(raw_cmd, assignments)
    if not DECIDABLE_NAME.fullmatch(os.path.basename(cmd_word)) or _is_configure_word(cmd_word):
        return Path(cmd_word), []
    name = os.path.basename(cmd_word)
    blocked: Path | None = None
    if INTERPRETERS.match(name):
        blocked = _inspect_interpreter(words, cmd_idx, stdin_prog)
    elif name == "find":
        blocked = _inspect_find(words, cmd_idx)
    elif name == "sed":
        blocked = _inspect_sed(words, cmd_idx)
    elif name == "git":
        blocked = _inspect_git(words, cmd_idx)
    elif name == "xargs":
        blocked = _inspect_xargs(words, cmd_idx, stdin_prog, assignments)
    else:
        blocked = _inspect_pager_and_rg(words, cmd_idx)
    return blocked, []


def _handle_nested_program(source: str, depth: int, pending: list[tuple[str, int]]) -> Path | None:
    """Enforce nesting depth and queue a nested program fragment."""
    if depth > MAX_DEPTH:
        return Path("configure (nesting limit)")
    pending.append((source, depth))
    return None


def _check_pipe_shell(prev_stage: list[str]) -> Path | None:
    """Refuse pipeline feeding configure text from a reader into a shell."""
    prev_words, _ = _strip_redirections(prev_stage)
    cmd_idx = _unwrap_command(prev_words, 0)
    if cmd_idx < len(prev_words):
        name = os.path.basename(prev_words[cmd_idx])
        if name in ("cat", "echo", "printf"):
            for w in prev_words[cmd_idx + 1 :]:
                if _is_configure_word(w):
                    return Path(w)
    return None


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
        if idx > 0 and cmd_idx < len(words) and os.path.basename(words[cmd_idx]) in SHELLS:
            pipe_blocked = _check_pipe_shell(pipeline[idx - 1])
            if pipe_blocked is not None:
                return pipe_blocked
        if cmd_idx < len(words):
            name = os.path.basename(_substitute_literals(words[cmd_idx], assignments))
            if name in SHELLS or name in ("eval", "source", "."):
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
