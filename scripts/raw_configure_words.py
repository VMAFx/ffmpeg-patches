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
SHELLS = frozenset(
    ("bash", "sh", "dash", "zsh", "ksh", "ash", "mksh", "lksh", "posh", "yash", "hush")
)
NON_SHELL_PROGRAMS = frozenset(
    (
        "python",
        "python2",
        "python3",
        "pypy",
        "pypy3",
        "perl",
        "ruby",
        "node",
        "nodejs",
        "deno",
        "bun",
        "lua",
        "luajit",
        "php",
        "tclsh",
        "wish",
        "awk",
        "gawk",
        "mawk",
        "nawk",
        "Rscript",
        "julia",
        "uv",
        "make",
        "gmake",
    )
)
VERSIONED_NON_SHELL_PROGRAM = re.compile(
    r"(?:python|pypy|perl|ruby|lua|php|tclsh|node)[0-9][0-9.]*"
)
BASH_BUILTINS = frozenset(
    [
        "alias",
        "bg",
        "bind",
        "break",
        "builtin",
        "caller",
        "cd",
        "command",
        "compgen",
        "complete",
        "compopt",
        "continue",
        "declare",
        "dirs",
        "disown",
        "echo",
        "enable",
        "eval",
        "exec",
        "exit",
        "export",
        "false",
        "fc",
        "fg",
        "getopts",
        "hash",
        "help",
        "history",
        "jobs",
        "kill",
        "let",
        "local",
        "logout",
        "mapfile",
        "popd",
        "printf",
        "pushd",
        "pwd",
        "read",
        "readarray",
        "readonly",
        "return",
        "set",
        "shift",
        "shopt",
        "source",
        "suspend",
        "test",
        "times",
        "trap",
        "true",
        "type",
        "typeset",
        "ulimit",
        "umask",
        "unalias",
        "unset",
        "wait",
        "[",
        "[[",
        ".",
        ":",
        "if",
        "then",
        "else",
        "elif",
        "fi",
        "case",
        "esac",
        "for",
        "select",
        "while",
        "until",
        "do",
        "done",
        "in",
        "function",
        "time",
        "{",
        "}",
        "!",
    ]
)
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
        "GIT_SSH",
        "GIT_SSH_COMMAND",
        "GIT_ASKPASS",
        "SSH_ASKPASS",
        "SUDO_ASKPASS",
        "GIT_PROXY_COMMAND",
        "LESSOPEN",
        "LESSCLOSE",
        "MANPAGER",
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
        "[[",
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
    )
)
INTERPRETER_FAMILY_RE = re.compile(
    r"^(python|perl|ruby|node|php|gawk|mawk|awk)(?:\d+(?:\.\d+)*[a-z]*)?$"
)
INTERPRETER_VALUE_OPTIONS: dict[str, frozenset[str]] = {
    "python": frozenset(("-W", "-X", "--check-hash-based-pycs")),
    "perl": frozenset(("-I", "-M", "-m")),
    "ruby": frozenset(("-I", "-C", "-r")),
    "node": frozenset(
        (
            "--require",
            "--import",
            "--loader",
            "--experimental-loader",
            "-C",
            "--conditions",
            "--input-type",
            "--title",
        )
    ),
    "php": frozenset(("-d", "-z", "-t", "-S", "-c")),
    "awk": frozenset(
        (
            "-f",
            "-v",
            "-F",
            "-i",
            "-l",
            "--file",
            "--assign",
            "--field-separator",
            "--include",
            "--load",
        )
    ),
}
EXEC_PROGRAM_OPTIONS: dict[str, tuple[str, ...]] = {
    "sort": ("--compress-program",),
    "install": ("--strip-program",),
}
MAKE_VAR_ASSIGN = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_.-]*(?:::[:=]|::=|:=|\!=|\?=|\+=|=)(.*)", re.DOTALL
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
        "ash",
        "mksh",
        "lksh",
        "posh",
        "yash",
        "hush",
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


def _interpreter_family(name: str) -> str | None:
    """Normalize interpreter executable name to canonical language family."""
    m = INTERPRETER_FAMILY_RE.match(name)
    if m is None:
        return None
    family = m.group(1)
    return "awk" if family in ("gawk", "mawk", "awk") else family


def _long_option_values(words: list[str], start: int, option: str, min_len: int) -> list[str]:
    """Extract values of a long option and its valid prefixes from argument words."""
    values: list[str] = []
    words_len = len(words)
    bound = min(words_len, start + 32)
    for i in range(start, bound):
        w = words[i]
        if w == "--":
            break
        if not w.startswith("--"):
            continue
        if "=" in w:
            opt_part, val = w.split("=", 1)
            if len(opt_part) >= min_len and option.startswith(opt_part):
                values.append(val)
        elif len(w) >= min_len and option.startswith(w) and i + 1 < words_len:
            values.append(words[i + 1])
    return values


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


def _names_configure(word: str) -> bool:
    """Use concrete configure naming for data words, no fail-closed guess."""
    cleaned = _clean_word(word)
    base = os.path.basename(cleaned.translate(STRIP_CHARS))
    if base == "configure" or _flatten(os.path.basename(cleaned)) == "configure":
        return True
    if ("$" in word or "`" in word) and CONFIGURE_WORD.search(word):
        return True
    return (
        bool(GLOB_CHARS.intersection(base))
        and re.search(r"[A-Za-z]", base) is not None
        and fnmatch.fnmatchcase("configure", base)
    )


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


def _program_name_allowed(name: str) -> bool:
    """Recognize static bare external command names eligible for PATH lookup."""
    return bool(
        name
        and not any(c in name for c in "/$`")
        and name not in BASH_BUILTINS | SHELLS
        and DECIDABLE_NAME.fullmatch(name)
    )
