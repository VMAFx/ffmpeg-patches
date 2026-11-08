# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Constants and word predicates of the raw configure guard."""

from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path

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
INLINE_PROGRAM_FLAG = re.compile(r"-[A-Za-z]*[cepErR]|--(?:eval|print)")
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
GIT_DATA_SUBCOMMANDS: frozenset[str] = frozenset(
    (
        "add",
        "am",
        "apply",
        "blame",
        "branch",
        "cat-file",
        "checkout",
        "cherry-pick",
        "commit",
        "describe",
        "diff",
        "for-each-ref",
        "format-patch",
        "grep",
        "log",
        "ls-files",
        "ls-tree",
        "merge",
        "merge-base",
        "mv",
        "range-diff",
        "rebase",
        "reflog",
        "restore",
        "rev-list",
        "rev-parse",
        "revert",
        "rm",
        "shortlog",
        "show",
        "stash",
        "status",
        "switch",
        "tag",
        "worktree",
    )
)
WRAPPER_VALUE_OPTIONS: dict[str, frozenset[str]] = {
    "sudo": frozenset(("-u", "-g", "-h", "-p", "-C", "-D", "-r", "-t", "-T", "-U")),
    "timeout": frozenset(("-s", "-k")),
    "flock": frozenset(("-E", "-w")),
    "nice": frozenset(("-n",)),
    "ionice": frozenset(("-c", "-n", "-p")),
    "taskset": frozenset(("-c",)),
    "chrt": frozenset(),
    "stdbuf": frozenset(("-i", "-o", "-e")),
}
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
DATA_ONLY_COMMANDS = frozenset(
    (
        "cat",
        "tac",
        "head",
        "tail",
        "grep",
        "egrep",
        "fgrep",
        "zgrep",
        "wc",
        "ls",
        "stat",
        "file",
        "diff",
        "cmp",
        "comm",
        "sort",
        "uniq",
        "cut",
        "tr",
        "paste",
        "join",
        "column",
        "nl",
        "fold",
        "fmt",
        "od",
        "xxd",
        "hexdump",
        "strings",
        "md5sum",
        "sha1sum",
        "sha256sum",
        "sha512sum",
        "b2sum",
        "cksum",
        "touch",
        "mkdir",
        "rmdir",
        "cp",
        "mv",
        "rm",
        "ln",
        "install",
        "chmod",
        "chown",
        "readlink",
        "realpath",
        "basename",
        "dirname",
        "test",
        "[",
        "echo",
        "printf",
        "tee",
        "du",
        "df",
        "jq",
        "yq",
        "shellcheck",
        "shfmt",
        "patch",
        "cd",
        "pushd",
        "popd",
        "true",
        "false",
        "make",
    )
)
CONFIGURE_TOKEN_DELIMS = re.compile(r"[\s;&|()`<>!{}'\"]+")
COMMAND_POSITION_TOKEN = re.compile(r"&&|\|\||\$\(|[;&|()`\n{}]|[^\s;&|()`<>{}'\"]+")
COMMAND_SEPARATORS: frozenset[str] = frozenset(
    ("&&", "||", "$(", ";", "&", "|", "(", ")", "`", "\n", "{", "}")
)
COMMAND_PREFIX_WORDS: frozenset[str] = frozenset(
    (
        "sh",
        "bash",
        "dash",
        "zsh",
        "ksh",
        "exec",
        "env",
        "command",
        "nohup",
        "nice",
        "sudo",
        "time",
        "then",
        "do",
        "else",
        "if",
        "while",
        "until",
        "!",
        "eval",
        "source",
        ".",
    )
)


def _is_data_only_command(command: str) -> bool:
    """Check whether a command basename is in the data-only allow list."""
    return os.path.basename(command) in DATA_ONLY_COMMANDS


def _split_configure_tokens(word: str, assignments: dict[str, str] | None = None) -> list[str]:
    """Split a command argument on shell delimiters into constituent tokens."""
    if assignments:
        word = _substitute_literals(word, assignments)
    return [tok for tok in CONFIGURE_TOKEN_DELIMS.split(word) if tok]


def _has_configure_token(word: str, assignments: dict[str, str] | None = None) -> bool:
    """Check whether any constituent token equals configure in its last path component."""
    for tok in _split_configure_tokens(word, assignments):
        if tok.rsplit("/", 1)[-1] == "configure":
            return True
    return False


def _has_command_position_configure(word: str, assignments: dict[str, str]) -> bool:
    """Check whether a configure command appears in command position within an argument."""
    if assignments:
        word = _substitute_literals(word, assignments)
    toks = COMMAND_POSITION_TOKEN.findall(word)
    if len(toks) <= 1:
        return _has_configure_token(word, {})
    pos = True
    start = True
    after_prefix = False
    for token in toks[:4096]:
        if token in COMMAND_SEPARATORS:
            pos = True
            start = False
            after_prefix = False
            continue
        if not pos:
            continue
        if ASSIGNMENT.fullmatch(token):
            continue
        base = token.rsplit("/", 1)[-1]
        if base in COMMAND_PREFIX_WORDS:
            after_prefix = True
            continue
        if base == "configure" and ("/" in token or not start or after_prefix):
            return True
        pos = False
    return False


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
