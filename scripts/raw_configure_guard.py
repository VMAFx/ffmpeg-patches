#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Refuse raw FFmpeg configure before a shell can write its environment to config.log.

Commands such as sed, less, more, and rg are not treated as readers because they
can execute arbitrary commands or preprocessors. Git passes only for allow-listed
subcommands that do not execute their path arguments, and only without the refused
options.

Known limitation: words assembled from variables (e.g. `p=con; ./${p}figure`)
are not expanded across assignments; directory existence is read when the hook
runs; a command that removes or creates the target before its cd runs is judged
by the state at hook time. A word whose directory and basename are both
undecidable (e.g. `$X/$Y`) passes.
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
ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z_0-9]*=.*", re.DOTALL)
CONFIGURE_WORD = re.compile(r"(?<![A-Za-z0-9_])configure(?![A-Za-z0-9_])")
DYNAMIC_VARIABLE = re.compile(r"^\$[A-Za-z_][A-Za-z0-9_]*")
BRACED_VARIABLE = re.compile(r"\$\{[^{}]*\}")
NAMED_VARIABLE = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*")
STRIP_CHARS = str.maketrans("", "", "'\"\\")
INTERPRETERS = re.compile(r"^(?:python|perl|ruby|node|gawk|mawk|awk|php)(?:\d+(?:\.\d+)*[a-z]*)?$")
GLOB_CHARS = frozenset(("*", "?", "["))
# Literal name characters plus the glob characters fnmatch already decides.
DECIDABLE_NAME = re.compile(r"[A-Za-z0-9._+,:@%#=*?\[\]!^-]*")
DECIDABLE_DIR = re.compile(r"~?[A-Za-z0-9._+,:@%#=*?\[\]!^/-]*")
REDIRECT_OP = re.compile(r"^(?:&>>?|\d*>>?|\d*<<?|\d*>[&|]?|\d*<&)$")
REDIRECT_FULL = re.compile(r"^(?:&>>?|\d*>>?|\d*<<?|\d*>[&|]?|\d*<&)[^<>&|].*$")
DIGITS_REDIRECT = re.compile(r"^\d+(?:&>>?|>>?|<<?|>[&|]?|<&)")
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


def _flatten(text: str) -> str:
    """Normalize dynamic and quoted tokens to unquoted literal forms."""
    text = BRACED_VARIABLE.sub("", text)
    text = text.replace("$'", "'")
    text = NAMED_VARIABLE.sub("", text)
    return text.translate(STRIP_CHARS)


def _dynamic_configure(word: str) -> bool:
    """Identify whether a dynamic shell word targets the configure script."""
    if "$(" in word or "`" in word:
        return bool(CONFIGURE_WORD.search(word))
    if "/" not in word:
        return False
    last = word.rsplit("/", 1)[-1]
    return _flatten(last) == "configure"


def _clean_word(word: str) -> str:
    """Strip assignments, shell command prefixes, and whitespace from a word."""
    if "=" in word:
        word = word.split("=", 1)[1]
    if " " in word:
        word = word.split()[-1]
    return word.lstrip("+!|")


def _undecidable_in_tree(cleaned: str, cwd: Path | None) -> bool:
    """Identify an undecidable command targeting an FFmpeg tree."""
    if "/" not in cleaned or DECIDABLE_NAME.fullmatch(os.path.basename(cleaned)):
        return False
    dirname = os.path.dirname(cleaned) or "."
    if not DECIDABLE_DIR.fullmatch(dirname):
        return False
    if cwd is None:
        return True
    parent = (cwd / os.path.expanduser(dirname)).resolve()
    return (parent / "libavcodec").is_dir()


def _configure_path(word: str, cwd: Path | None) -> Path | None:
    """Identify a configure word in an FFmpeg tree or unknown directory."""
    cleaned = _clean_word(word)
    if _undecidable_in_tree(cleaned, cwd):
        return Path(word)
    if "$" in cleaned or "`" in cleaned:
        return Path(word) if _dynamic_configure(cleaned) else None
    base = os.path.basename(cleaned)
    is_glob = bool(GLOB_CHARS.intersection(base)) and fnmatch.fnmatchcase("configure", base)
    if base != "configure" and not is_glob:
        return None
    dirname = os.path.dirname(cleaned) or "."
    if not DECIDABLE_DIR.fullmatch(dirname) or cwd is None:
        return Path(word)
    parent = (cwd / os.path.expanduser(dirname)).resolve()
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


def _scan_inline(words: list[str], index: int) -> str | None:
    """Find an inline shell program or eval arguments across command words."""
    for pos in range(index, min(len(words), index + MAX_TOKENS)):
        name = os.path.basename(words[pos])
        if name == "eval":
            return " ".join(words[pos + 1 :])
        if name in SHELLS:
            target, inline = _shell_argument(words[pos:])
            if inline:
                return target
    return None


def _inspect_simple(words: list[str], cwd: Path | None) -> tuple[Path | None, str | None]:
    """Classify a simple command and return an inline program when present."""
    index = 0
    for word in words[: MAX_TOKENS + 1]:
        if not ASSIGNMENT.fullmatch(word):
            break
        _, value = word.split("=", 1)
        if CONFIGURE_WORD.search(value) and (cwd is None or (cwd / "libavcodec").is_dir()):
            return Path(word), None
        index += 1
    blocked = _inspect_interpreter(words, index, cwd)
    if blocked is not None:
        return blocked, None
    if not _is_reader(words):
        blocked = _find_configure_path(words, cwd)
        if blocked is not None:
            return blocked, None
    return None, _scan_inline(words, index)


def _inspect_pipeline(
    pipeline: list[list[str]],
    directory: Path | None,
    depth: int,
    pending: list[tuple[str, Path | None, int]],
) -> tuple[Path | None, Path | None]:
    """Inspect each stage of a pipeline and refuse unsafe configure feeds."""
    if len(pipeline) == 1:
        return _step(pipeline[0], directory, depth, pending)
    for stage in pipeline:
        blocked, _ = _step(stage, directory, depth, pending)
        if blocked is not None:
            return blocked, directory
    if any(not _is_reader(stage) for stage in pipeline):
        for stage in pipeline:
            if _is_reader(stage):
                blocked = _find_configure_path(stage, directory)
                if blocked is not None:
                    return blocked, directory
    return None, directory


def _subshell_step(token: str, directory: Path | None, subshells: list[Path | None]) -> Path | None:
    """Update subshell stack and effective directory on grouping tokens."""
    if token == "(":
        if len(subshells) < MAX_SUBSHELL_DEPTH:
            subshells.append(directory)
            return directory
        subshells.append(None)
        return None
    if token == ")":
        return subshells.pop() if subshells else None
    return directory


def _separator_step(
    token: str, directory: Path | None, list_start: Path | None, subshells: list[Path | None]
) -> tuple[Path | None, Path | None]:
    """Update effective directory and list start on shell separator tokens."""
    if token == "&":
        directory = list_start
    elif token in ("(", ")"):
        directory = _subshell_step(token, directory, subshells)
    if token in (";", "\n", "&", "(", ")"):
        list_start = directory
    return directory, list_start


def _inspect_tokens(
    tokens: list[str],
    directory: Path | None,
    depth: int,
    pending: list[tuple[str, Path | None, int]],
) -> Path | None:
    """Walk tokens of one shell source, feeding pipelines and updating directory."""
    pipeline: list[list[str]] = []
    simple: list[str] = []
    subshells: list[Path | None] = []
    list_start = directory
    for token in [*tokens, ";"]:
        if token == "|":
            if simple:
                pipeline.append(simple)
                simple = []
            continue
        if token in SEPARATORS:
            if simple:
                pipeline.append(simple)
                simple = []
            if pipeline:
                blocked, directory = _inspect_pipeline(pipeline, directory, depth, pending)
                if blocked is not None:
                    return blocked
                pipeline = []
            directory, list_start = _separator_step(token, directory, list_start, subshells)
            continue
        simple.append(token)
    return None


def _has_unparsed_configure(source: str) -> bool:
    """Detect direct or globbed configure references in unparsed command text."""
    flattened = _flatten(source)
    if "configure" in flattened:
        return True
    for word in flattened.split()[:MAX_INPUT]:
        base = os.path.basename(word)
        if bool(GLOB_CHARS.intersection(base)) and fnmatch.fnmatchcase("configure", base):
            return True
    return False


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
            if _has_unparsed_configure(source):
                return Path("configure (parse or size limit)")
            continue
        blocked = _inspect_tokens(tokens, directory, depth, pending)
        if blocked is not None:
            return blocked
    return Path("configure (worklist limit)")


def _valid_cd_words(words: list[str]) -> bool:
    """Verify cd arguments consist of at most one directory plus redirections."""
    if len(words) < 2:
        return False
    if len(words) == 2:
        return True
    idx = 2
    for _ in range(len(words)):
        if idx >= len(words):
            return True
        word = words[idx]
        if REDIRECT_FULL.match(word):
            idx += 1
            continue
        if REDIRECT_OP.match(word) and idx + 1 < len(words):
            idx += 2
            continue
        return False
    return True


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
        target = "~" if words == ["cd"] else words[1] if _valid_cd_words(words) else ""
        if (
            not target
            or target == "-"
            or "$" in target
            or "`" in target
            or cwd is None
            or REDIRECT_FULL.match(target)
            or REDIRECT_OP.match(target)
            or DIGITS_REDIRECT.match(target)
        ):
            return blocked, None
        if os.environ.get("CDPATH") and not (
            target.startswith(("/", "./", "../", "~")) or target in (".", "..")
        ):
            return blocked, None
        resolved = Path(os.path.normpath(cwd / os.path.expanduser(target))).resolve()
        if not resolved.is_dir() or not os.access(resolved, os.X_OK):
            return blocked, cwd
        return blocked, resolved
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
