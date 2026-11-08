#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Refuse raw configure execution across directories before a shell can write secrets.

Approach: directory-free refusal. Refuse every command-position configure word in
any directory unless configure is started through scripts/ffmpeg_configure.py.

The one pass: scripts/ffmpeg_configure.py runs configure with a filtered environment
so the set dump in config.log holds no secrets.

Residual open shapes the guard does not see: a script file that itself runs configure
(`bash build.sh`), BASH_ENV naming such a script, `find ... -execdir {} \\;` with
-name configure, and words assembled from variables across separate assignments.

Commands such as sed, less, more, and rg are not treated as readers because they
can execute arbitrary commands or preprocessors. Git passes only for allow-listed
subcommands that do not execute their path arguments, and only without the refused
options. A command the guard cannot tokenize (over MAX_COMMAND bytes, over MAX_TOKENS
tokens, or a quote shlex cannot close) is refused everywhere; write such text to a file.
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
SHELLS = frozenset(("bash", "sh"))
UNPARSED = Path("unparsed command")
ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z_0-9]*=.*", re.DOTALL)
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
DANGEROUS_TOOLS = frozenset(("sed", "less", "more", "rg", "find"))
GLOB_CHARS = frozenset(("*", "?", "["))
DECIDABLE_NAME = re.compile(r"[A-Za-z0-9._+,:@%#=*?\[\]!^-]*")
READERS = frozenset(
    (
        "cat",
        "head",
        "tail",
        "wc",
        "nl",
        "grep",
        "egrep",
        "fgrep",
        "diff",
        "cmp",
        "file",
        "stat",
        "ls",
        "sha256sum",
        "md5sum",
        "echo",
        "printf",
    )
)
GIT_READER_SUBCOMMANDS = frozenset(
    (
        "log",
        "show",
        "diff",
        "status",
        "add",
        "rm",
        "mv",
        "restore",
        "blame",
        "grep",
        "ls-files",
        "cat-file",
        "commit",
        "format-patch",
        "am",
        "apply",
        "stash",
    )
)
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


def _clean_word(word: str) -> str:
    """Strip assignments, shell command prefixes, and whitespace from a word."""
    if "=" in word:
        word = word.split("=", 1)[1]
    if " " in word:
        word = word.split()[-1]
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


def _is_git_reader(words: list[str], index: int) -> bool:
    """Check whether a git command invokes only safe reader subcommands."""
    for word in words[index + 1 : index + 1 + MAX_TOKENS]:
        if word.startswith("-c"):
            return False
        opt = word.split("=", 1)[0]
        if opt in GIT_DISALLOWED_OPTIONS or word.startswith(("-x", "-O")):
            return False
    subcommand: str | None = None
    arg_idx = index + 1
    for _ in range(MAX_TOKENS + 1):
        if arg_idx >= len(words):
            break
        word = words[arg_idx]
        if word in GIT_GLOBAL_OPTS_WITH_ARG:
            arg_idx += 2
            continue
        if not word.startswith("-"):
            subcommand = word
            break
        arg_idx += 1
    return subcommand in GIT_READER_SUBCOMMANDS


def _is_reader(words: list[str]) -> bool:
    """Classify whether a simple command is an allowed read-only utility."""
    index = 0
    for word in words[: MAX_TOKENS + 1]:
        if not ASSIGNMENT.fullmatch(word):
            break
        index += 1
    if index >= len(words):
        return False
    name = os.path.basename(words[index])
    if name in READERS:
        return True
    if name == "git":
        return _is_git_reader(words, index)
    return False


def _inspect_interpreter(words: list[str], index: int) -> Path | None:
    """Refuse interpreter one-liners embedding raw configure invocations."""
    name = os.path.basename(words[index]) if index < len(words) else ""
    if not INTERPRETERS.match(name):
        return None
    idx = index + 1
    words_len = len(words)
    for _ in range(words_len):
        if idx >= words_len or not words[idx].startswith("-"):
            break
        if words[idx] in ("-c", "-e") and idx + 1 < words_len:
            code = words[idx + 1]
            return Path(code) if CONFIGURE_WORD.search(code) else None
        idx += 1
    if idx < words_len:
        target = words[idx]
        if name in ("awk", "gawk", "mawk"):
            return Path(target) if CONFIGURE_WORD.search(target) else None
        if _is_configure_word(target):
            return Path(target)
    return None


def _inspect_dangerous_tools(words: list[str], idx: int) -> Path | None:
    """Refuse dangerous tools invoked with configure arguments."""
    if idx >= len(words):
        return None
    name = os.path.basename(words[idx])
    dangerous = name in DANGEROUS_TOOLS or (name == "git" and not _is_git_reader(words, idx))
    if not dangerous:
        return None
    for word in words[idx + 1 : idx + 1 + MAX_TOKENS]:
        if _is_configure_word(word) or CONFIGURE_WORD.search(word):
            return Path(word)
    return None


def _skip_assignment(words: list[str], idx: int) -> tuple[int, Path | None]:
    """Advance past leading assignments and refuse assignments containing configure."""
    words_len = len(words)
    for _ in range(words_len):
        if idx >= words_len:
            break
        word = words[idx]
        if not ASSIGNMENT.fullmatch(word):
            break
        _, value = word.split("=", 1)
        if CONFIGURE_WORD.search(value):
            return idx, Path(word)
        idx += 1
    return idx, None


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
    if name in (
        "nice",
        "timeout",
        "sudo",
        "xargs",
        "stdbuf",
        "flock",
        "chrt",
        "ionice",
        "taskset",
    ):
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
        idx, _ = _skip_assignment(words, idx)
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


def _inspect_shell_or_eval(
    words: list[str], idx: int, depth: int, pending: list[tuple[str, int]]
) -> Path | None:
    """Process shell script operands, inline -c scripts, and eval programs."""
    name = os.path.basename(words[idx])
    if name in ("source", "."):
        if idx + 1 < len(words) and _is_configure_word(words[idx + 1]):
            return Path(words[idx + 1])
        return None
    if name == "eval":
        pending.append((" ".join(words[idx + 1 :]), depth + 1))
        return None
    if name in SHELLS:
        target, inline = _shell_argument(words[idx:])
        if inline and target is not None:
            pending.append((target, depth + 1))
            return None
        if target is not None and _is_configure_word(target):
            return Path(target)
    return None


def _inspect_command_word(
    words: list[str], idx: int, depth: int, pending: list[tuple[str, int]]
) -> Path | None:
    """Unwrap wrappers and inspect the command-position word and arguments."""
    cmd_idx = _unwrap_command(words, idx)
    if cmd_idx >= len(words):
        return None
    cmd_word = words[cmd_idx]
    if _is_configure_word(cmd_word):
        return Path(cmd_word)
    blocked = _inspect_interpreter(words, cmd_idx)
    if blocked is not None:
        return blocked
    if os.path.basename(cmd_word) in SHELLS:
        return _inspect_shell_or_eval(words, cmd_idx, depth, pending)
    return None


def _inspect_simple(words: list[str], depth: int, pending: list[tuple[str, int]]) -> Path | None:
    """Classify a simple command and refuse unsafe configure execution."""
    idx, blocked = _skip_assignment(words, 0)
    if blocked is not None or idx >= len(words):
        return blocked
    blocked = _inspect_interpreter(words, idx)
    if blocked is not None:
        return blocked
    blocked = _inspect_dangerous_tools(words, idx)
    if blocked is not None:
        return blocked
    blocked = _inspect_shell_or_eval(words, idx, depth, pending)
    if blocked is not None or _is_reader(words):
        return blocked
    return _inspect_command_word(words, idx, depth, pending)


def _inspect_pipeline(
    pipeline: list[list[str]], depth: int, pending: list[tuple[str, int]]
) -> Path | None:
    """Inspect each stage of a pipeline and refuse unsafe configure feeds."""
    for stage in pipeline:
        blocked = _inspect_simple(stage, depth, pending)
        if blocked is not None:
            return blocked
    if len(pipeline) > 1 and any(not _is_reader(stage) for stage in pipeline):
        for stage in pipeline:
            if _is_reader(stage):
                blocked = _find_configure_word(stage)
                if blocked is not None:
                    return blocked
    return None


def _inspect_tokens(tokens: list[str], depth: int, pending: list[tuple[str, int]]) -> Path | None:
    """Walk tokens of one shell source and feed pipeline stages."""
    pipeline: list[list[str]] = []
    simple: list[str] = []
    for word in [*tokens, ";"]:
        if word == "|":
            if simple:
                pipeline.append(simple)
                simple = []
            continue
        if word in SEPARATORS:
            if simple:
                pipeline.append(simple)
                simple = []
            if pipeline:
                blocked = _inspect_pipeline(pipeline, depth, pending)
                if blocked is not None:
                    return blocked
                pipeline = []
            continue
        simple.append(word)
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
        if command[idx : idx + 2] == "$(":
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


def _scan_substitutions(
    command: str,
) -> tuple[str, list[str], list[tuple[str, str]]] | None:
    """Find command substitutions before shlex parsing without recursion."""
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
            if command[idx : idx + 2] == "$(":
                bounds = _scan_substitution_body(command, idx + 2)
                if bounds is None:
                    return None
                tag = f"__RCG_SUB_{len(replacements)}__"
                replacements.append((tag, command[idx : bounds[1] + 1]))
                inners.append(command[bounds[0] : bounds[1]])
                pieces.append(tag)
                idx = bounds[1] + 1
                continue
            if char == "`":
                end_idx = _scan_backtick(command, idx)
                if end_idx is None:
                    return None
                tag = f"__RCG_SUB_{len(replacements)}__"
                replacements.append((tag, command[idx : end_idx + 1]))
                inners.append(command[idx + 1 : end_idx])
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


def _parse_source(source: str, depth: int, pending: list[tuple[str, int]]) -> list[str] | None:
    """Extract substitutions, queue inner commands, and return parsed tokens."""
    sub_info = _scan_substitutions(source)
    if sub_info is None:
        return None
    rewritten, inners, reps = sub_info
    for inner in inners:
        pending.append((inner, depth + 1))
    tokens = _tokens(rewritten)
    return _restore_tokens(tokens, reps) if tokens is not None else None


def _inspect(command: str) -> Path | None:
    """Walk bounded shell fragments and inline programs without recursion."""
    pending = [(command, 0)]
    for _ in range(MAX_TOKENS + 1):
        if not pending:
            return None
        source, depth = pending.pop()
        if depth > MAX_DEPTH:
            return Path("configure (nesting limit)")
        tokens = _parse_source(source, depth, pending)
        if tokens is None:
            return UNPARSED
        blocked = _inspect_tokens(tokens, depth, pending)
        if blocked is not None:
            return blocked
    return Path("configure (worklist limit)")


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
            "raw configure refused: run python3 scripts/ffmpeg_configure.py "
            "SRC_DIR BUILD_DIR -- <args>; it runs SRC_DIR/configure for FFmpeg "
            "or any other configure tree (GCC ...) with a filtered environment "
            "so config.log holds no secrets",
            file=sys.stderr,
        )
        return 2
    command = _input(options.client, payload)
    if command is None:
        return 0
    blocked = _inspect(command)
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
    print(
        f"raw configure refused: {blocked}; run python3 scripts/ffmpeg_configure.py "
        "SRC_DIR BUILD_DIR -- <args>; it runs SRC_DIR/configure for FFmpeg "
        "or any other configure tree (GCC ...) with a filtered environment "
        "so config.log holds no secrets",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
