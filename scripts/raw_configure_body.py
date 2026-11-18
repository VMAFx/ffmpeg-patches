# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Shell syntax tree inspection for raw configure guard."""

from __future__ import annotations

import importlib
import os
import re
import sys
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NamedTuple

from scripts.raw_configure_lex import _tokens
from scripts.raw_configure_unwrap import _unwrap_command
from scripts.raw_configure_words import (
    CONFIGURE_WORD,
    DATA_ONLY_COMMANDS,
    DECIDABLE_NAME,
    INTERPRETERS,
    SHELLS,
    STRIP_CHARS,
    _find_literal_assignments,
    _is_configure_word,
    _names_configure,
    _program_name_allowed,
    _shell_argument,
    _substitute_literals,
)

# Ensure repository root is on sys.path for test/tool imports
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

MAX_SCRIPT_BYTES = 262144
MAX_SCRIPT_FILES = 16
MAX_PROGRAM_PEEKS = 1024
MAX_BODY_NODES = 4 * MAX_SCRIPT_BYTES
MAX_INLINE_BODIES = 256
AMBIGUOUS_SCRIPT_PATH = (
    "script path from a variable assigned in a sourced file and elsewhere; use one assignment"
)
UNRESOLVED_SCRIPT_DIR = (
    "script path from an own-directory name assigned in a function, branch or loop; "
    "assign it once at the top level"
)
NEWLINE_SCRIPT_DIR = (
    "script path from an own-directory name whose directory path holds a newline, "
    "which command substitution can strip; use an absolute path"
)
# Refusal when the resolved script file name holds a newline command substitution strips.
NEWLINE_SCRIPT_NAME = (
    "script path from an own-directory name whose resolved script file name holds a newline, "
    "which command substitution can strip; use an absolute path"
)
# Refusal when a top-level statement before the assignment falls outside the allow-list.
PREFIX_SCRIPT_DIR = (
    "script path from an own-directory name assigned after line {line} ({shape}), a statement "
    "outside the own-directory allow-list that can rebind the commands of the idiom; assign the "
    "name above that statement or use an absolute path"
)
REBOUND_SCRIPT_DIR = (
    "script path from an own-directory name with BASH_ENV, ENV or an exported BASH_FUNC_ "
    "function in the environment that can rebind the commands of the idiom; use an absolute path"
)
WRITTEN_SCRIPT_DIR = (
    "script path from an own-directory name the script also writes another way (printf -v, "
    "read, mapfile, unset, a loop, an arithmetic, append or array assignment, or a special "
    "builtin prefix, or a writer or eval that names a variable at run time); assign it only "
    "with = or use an absolute path"
)
PREFIXED_FUNCTION_SCRIPT_DIR = (
    "script path in a function that the script calls with a prefix assignment of the name "
    "(NAME=value f), which Bash keeps for the whole call; pass the value as an argument or use "
    "an absolute path"
)
SOURCED_PREFIX_CALL = (
    "prefix assignment in a sourced file before a call of a function whose body the guard "
    "already walked with the old value of the name; pass the value as an argument or use an "
    "absolute path"
)
UNKNOWN_SOURCE_PATH = (
    "relative script path after a source the guard cannot read; use an absolute path"
)
UNSUBSTITUTED_SCRIPT_PATH = (
    "script path from an own-directory name with an expansion the guard cannot substitute; "
    "use plain $NAME or ${NAME} and no other expansion"
)
_SCRIPT_DIR_DIRNAME = (
    r'\$\(dirname (?:-- )?"\$\((?:readlink -f|realpath)(?: --)? '
    r'"(?:\$\{BASH_SOURCE(?:\[0\])?\}|\$BASH_SOURCE|\$\{0\}|\$0)"\)"\)'
)
# Own directory of an executed script, symlinks resolved like Path.resolve():
# $(dirname "$(readlink -f "${BASH_SOURCE[0]}")") (with realpath or $0 as variants)
# or $(cd "<dirname>" && pwd) around that dirname value.
# The cd form has the same value: readlink -f or realpath already resolves every symlink.
SCRIPT_DIR_VALUE = re.compile(
    rf'(?:{_SCRIPT_DIR_DIRNAME}|\$\(cd (?:-P )?(?:-- )?"{_SCRIPT_DIR_DIRNAME}" && pwd(?: -P)?\))'
)
# ${OVERRIDE:-value} or ${OVERRIDE-value}: the value applies only while OVERRIDE is unset.
SCRIPT_DIR_DEFAULT = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*):?-(.+)\}")
ASSIGNED_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
PLAIN_FUNCTION = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")
# Word parts tree-sitter-bash builds a shell word from. Bash keeps the text of the static
# parts after quote removal (bash 5.3 manual 3.5.9); every other part is expanded at run time.
STATIC_GROUPS = frozenset(["concatenation", "string", "translated_string"])
STATIC_LEAVES = frozenset(
    ["word", "raw_string", "ansi_c_string", "string_content", "variable_name", "number"]
)
WORD_PARTS = (
    STATIC_GROUPS
    | STATIC_LEAVES
    | frozenset(
        [
            "expansion",
            "simple_expansion",
            "command_substitution",
            "process_substitution",
            "arithmetic_expansion",
        ]
    )
)
# An unquoted word with a glob or brace pattern expands at run time (bash 5.3 manual 3.5.1, 3.5.8).
GLOB_OR_BRACE = re.compile(r"[*?[]|\{[^}]*(?:,|\.\.)")
TRANSLATED_SPAN = re.compile(r'\$"(?:\\.|[^"\\])*"', re.S)
WORD_ESCAPE = re.compile(r"\\(.)", re.S)
STRING_ESCAPE = re.compile(r'\\([$`"\\\n])')
# ANSI-C quoting escapes (bash 5.3 manual 3.1.2.4); an unknown escape keeps its backslash.
ANSI_C_ESCAPE = re.compile(
    r"\\(?:([0-7]{1,3})|x([0-9A-Fa-f]{1,2})|u([0-9A-Fa-f]{1,4})|U([0-9A-Fa-f]{1,8})|c(.)|(.))",
    re.S,
)
ANSI_C_SIMPLE = {
    "a": "\a",
    "b": "\b",
    "e": "\x1b",
    "E": "\x1b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "v": "\v",
    "\\": "\\",
    "'": "'",
    '"': '"',
    "?": "?",
}
ARITHMETIC_WRITES = frozenset(
    ["=", "+=", "-=", "*=", "/=", "%=", "<<=", ">>=", "&=", "^=", "|=", "++", "--"]
)
ARITHMETIC_WRITE = re.compile(r"<<=|>>=|[+*/%&^|-]=|(?<![=!<>])=(?!=)|\+\+|--")
WRITER_NODES = frozenset(["command", "declaration_command", "unset_command"])
# Builtins that run their operands as shell text: eval now, trap when the signal arrives.
EVAL_BUILTINS = frozenset(["eval", "trap"])
SHELL_TEXT_BUILTINS = EVAL_BUILTINS | frozenset([".", "source"])
BRACE_CHOICES = re.compile(r"\{([A-Za-z0-9_.-]+(?:,[A-Za-z0-9_.-]*)+)\}")
PLAIN_BRACE_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*(?:\[[0-9]+\])?)\}")
ARRAY_ALL_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\[[@*]\]\}")
CASE_BRACE_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(\^\^?|,,?)\}")
FALLBACK_BRACE_VAR = re.compile(
    r"\$\{([A-Za-z_][A-Za-z0-9_]*|[0-9]+|[@*#?$!-])(:?[-+=])(.*)\}", re.S
)
# Positional and special parameters: the script's own arguments and shell state, known only at
# run time unless the guard has established their values.
UNKNOWN_PARAM = re.compile(r"[1-9][0-9]*|[@*#?$!-]")
INDEXED_TARGET = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\[([0-9]+)\]")
# Words before a command that keep a shell function or eval in reach: time runs a function,
# and builtin or command can run eval (measured on bash 5.3.20). An external wrapper such as
# env or timeout, and exec, run a program and never a function of this shell.
SHELL_WRAPPERS = frozenset(["builtin", "command", "time"])
# A for loop word list "${!PREFIX@}" binds the loop variable to names that begin with PREFIX.
PREFIX_NAMES = re.compile(r'"\$\{!([A-Za-z_][A-Za-z0-9_]*)@\}"')
# Variables documented in the bash 5.3 manual, section 5.1 Bourne Shell Variables
# and section 5.2 Bash Variables (110 names).
BASH_VARIABLES = frozenset(
    {
        "_",
        "BASH",
        "BASH_ALIASES",
        "BASH_ARGC",
        "BASH_ARGV",
        "BASH_ARGV0",
        "BASH_CMDS",
        "BASH_COMMAND",
        "BASH_COMPAT",
        "BASH_ENV",
        "BASH_EXECUTION_STRING",
        "BASH_LINENO",
        "BASH_LOADABLES_PATH",
        "BASH_MONOSECONDS",
        "BASHOPTS",
        "BASHPID",
        "BASH_REMATCH",
        "BASH_SOURCE",
        "BASH_SUBSHELL",
        "BASH_TRAPSIG",
        "BASH_VERSINFO",
        "BASH_VERSION",
        "BASH_XTRACEFD",
        "CDPATH",
        "CHILD_MAX",
        "COLUMNS",
        "COMP_CWORD",
        "COMP_KEY",
        "COMP_LINE",
        "COMP_POINT",
        "COMPREPLY",
        "COMP_TYPE",
        "COMP_WORDBREAKS",
        "COMP_WORDS",
        "COPROC",
        "DIRSTACK",
        "EMACS",
        "ENV",
        "EPOCHREALTIME",
        "EPOCHSECONDS",
        "EUID",
        "EXECIGNORE",
        "FCEDIT",
        "FIGNORE",
        "FUNCNAME",
        "FUNCNEST",
        "GLOBIGNORE",
        "GLOBSORT",
        "GROUPS",
        "HISTCMD",
        "HISTCONTROL",
        "HISTFILE",
        "HISTFILESIZE",
        "HISTIGNORE",
        "HISTSIZE",
        "HISTTIMEFORMAT",
        "HOME",
        "HOSTFILE",
        "HOSTNAME",
        "HOSTTYPE",
        "IFS",
        "IGNOREEOF",
        "INPUTRC",
        "INSIDE_EMACS",
        "LANG",
        "LC_ALL",
        "LC_COLLATE",
        "LC_CTYPE",
        "LC_MESSAGES",
        "LC_NUMERIC",
        "LC_TIME",
        "LINENO",
        "LINES",
        "MACHTYPE",
        "MAIL",
        "MAILCHECK",
        "MAILPATH",
        "MAPFILE",
        "OLDPWD",
        "OPTARG",
        "OPTERR",
        "OPTIND",
        "OSTYPE",
        "PATH",
        "PIPESTATUS",
        "POSIXLY_CORRECT",
        "PPID",
        "PROMPT_COMMAND",
        "PROMPT_DIRTRIM",
        "PS0",
        "PS1",
        "PS2",
        "PS3",
        "PS4",
        "PWD",
        "RANDOM",
        "READLINE_ARGUMENT",
        "READLINE_LINE",
        "READLINE_MARK",
        "READLINE_POINT",
        "REPLY",
        "SECONDS",
        "SHELL",
        "SHELLOPTS",
        "SHLVL",
        "SRANDOM",
        "TIMEFORMAT",
        "TMOUT",
        "TMPDIR",
        "UID",
    }
)
# glibc sysdeps/generic/unsecvars.h UNSECURE_ENVVARS: non-LD_ variables read by libc or ld.so.
LOADER_VARIABLES = frozenset(
    [
        "GCONV_PATH",
        "GETCONF_DIR",
        "GLIBC_TUNABLES",
        "HOSTALIASES",
        "LOCALDOMAIN",
        "LOCPATH",
        "MALLOC_ARENA_MAX",
        "MALLOC_ARENA_TEST",
        "MALLOC_MMAP_MAX_",
        "MALLOC_MMAP_THRESHOLD_",
        "MALLOC_PERTURB_",
        "MALLOC_TOP_PAD_",
        "MALLOC_TRACE",
        "MALLOC_TRIM_THRESHOLD_",
        "NIS_PATH",
        "NLSPATH",
        "RESOLV_HOST_CONF",
        "RES_OPTIONS",
        "TMPDIR",
        "TZDIR",
    ]
)
SUBSHELLS = frozenset(["command_substitution", "process_substitution", "subshell"])
IDIOM_COMMANDS = frozenset(["cd", "pwd", "dirname", "readlink", "realpath"])
# Builtins heading allow-listed statements or own-directory declarations; functions shadow them.
DECLARATION_KEYWORDS = frozenset(["declare", "typeset", "export", "readonly", "local"])
BLOCKED_FUNCTIONS = (
    IDIOM_COMMANDS
    | DECLARATION_KEYWORDS
    | frozenset(["set", "cd", ".", "source", "true", ":", "exit", "builtin", "command"])
)
SHAPE_DISALLOWED = re.compile(r"[^A-Za-z0-9_.:-]")
DECLARATION_FLAGS = re.compile(r"-[rgx]+")
PLAIN_WORD = re.compile(r"[A-Za-z0-9_./:@%+,=~^-]+")
PLAIN_EXPANSION = re.compile(r"\$\{(?:[A-Za-z_][A-Za-z0-9_]*|[0-9]+|[@*#?$!-])\}")
DEFAULT_EXPANSION = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*:?-[A-Za-z0-9_./:@%+,=~^-]*\}")
# Keep functions whose body prints or assigns a Bash variable (e.g. echo 'PATH=7') disallowed.
BODY_ASSIGNED_NAME = re.compile(r"(?<![A-Za-z0-9_])([A-Za-z_][A-Za-z0-9_]*)=")
READ_BRACE_TAIL = re.compile(rb"\}|:(?!=)|[-+?#%/^,@]")
# POSIX special builtins (bash 5.3 manual 4.4); in POSIX mode a prefix assignment stays set.
SPECIAL_BUILTINS = frozenset(
    [
        "break",
        ":",
        ".",
        "source",
        "continue",
        "eval",
        "exec",
        "exit",
        "export",
        "readonly",
        "return",
        "set",
        "shift",
        "times",
        "trap",
        "unset",
    ]
)
BARE_ATTRIBUTE = re.compile(rb"[ \t]*(?:export|readonly)[ \t]+")
BARE_ENDS = (b"", b"\n", b";", b" ", b"\t")
SET_FLAGS = re.compile(r"[-+][eu]+")
SET_OPTION_CLUSTER = re.compile(r"[-+][eu]*o")
EXIT_STATUS = re.compile(r"[0-9]+")
SET_OPTIONS = frozenset(["pipefail", "errexit", "nounset"])
COMMAND_ARG_TYPES = frozenset(
    [
        "word",
        "string",
        "raw_string",
        "simple_expansion",
        "expansion",
        "number",
        "concatenation",
        "ansi_c_string",
    ]
)
STRUCTURE = frozenset(
    [
        "program",
        "list",
        "pipeline",
        "redirected_statement",
        "subshell",
        "compound_statement",
        "do_group",
        "if_statement",
        "elif_clause",
        "else_clause",
        "while_statement",
        "negated_command",
        "function_definition",
        "command_substitution",
        "process_substitution",
    ]
)
ASSIGNMENTS = frozenset(
    ["variable_assignment", "variable_assignments", "declaration_command", "unset_command"]
)
DATA = frozenset(
    [
        "word",
        "string",
        "concatenation",
        "expansion",
        "simple_expansion",
        "arithmetic_expansion",
        "binary_expression",
        "unary_expression",
        "ternary_expression",
        "postfix_expression",
        "parenthesized_expression",
        "subscript",
        "array",
        "brace_expression",
        "extglob_pattern",
        "translated_string",
        "command_name",
        "heredoc_body",
    ]
)
LEAVES = frozenset(
    [
        "raw_string",
        "ansi_c_string",
        "comment",
        "string_content",
        "number",
        "variable_name",
        "special_variable_name",
        "test_operator",
        "file_descriptor",
        "regex",
        "heredoc_start",
        "heredoc_end",
        "heredoc_content",
    ]
)
SPECIAL = frozenset(
    [
        "command",
        "for_statement",
        "c_style_for_statement",
        "case_statement",
        "case_item",
        "test_command",
        "file_redirect",
        "heredoc_redirect",
        "herestring_redirect",
        "ERROR",
    ]
)
ERROR_COMMANDS = SHELLS | frozenset(
    (
        "source",
        ".",
        "eval",
        "exec",
        "xargs",
        "env",
        "nohup",
        "timeout",
        "command",
        "builtin",
        "sudo",
    )
)
_PARSERS: list[Any] = []


class InspectionContext(NamedTuple):
    """Bundle filesystem state and the acyclic command classifier."""

    cwd: Path
    directory_changed: bool
    files_read: list[int]
    classifier: Callable[[list[str]], Path | None]
    script_dir: Path | None = None
    script_path: Path | None = None
    source_reader: Callable[[str, bool, InspectionContext, list[str]], list[str] | str] | None = (
        None
    )
    command_prefix: tuple[str, ...] = ()


@dataclass
class BodyBudget:
    """Share syntax node and inline body accounting across nested files."""

    nodes: int = 0
    inline_bodies: int = 0
    peeks: int = 0
    rebound_scripts: set[Path] = field(default_factory=set)
    rebound_unknown: bool = False


class _DirState(NamedTuple):
    """Caller directory state restored after a subshell's children."""

    dir_changed: bool
    moved: bool
    command_rebound: bool


class _Writer(NamedTuple):
    """A builtin that assigns or unsets the variables it names (bash 5.3 manual 4.2).

    with_argument: option letters that take an argument; naming: option letters whose argument
    names a variable; operands: which operands name a variable (all, last, second, names for
    NAME[=value] operands, any for shell text, empty for none); functions: option letters that
    make the operands function names.
    """

    with_argument: str
    naming: str
    operands: str
    functions: str = ""
    referents: bool = False


DECLARING = _Writer("", "", "names", "fF")
NAMEREFS = _Writer("", "", "names", "fF", True)
# wait -p is from bash 5.1 and compgen -V from bash 5.3.
WRITERS = {
    "printf": _Writer("v", "v", ""),
    "read": _Writer("adinNptu", "a", "all"),
    "mapfile": _Writer("dnOsuCc", "", "last"),
    "readarray": _Writer("dnOsuCc", "", "last"),
    "getopts": _Writer("", "", "second"),
    "wait": _Writer("p", "p", ""),
    "compgen": _Writer("AGWFCXPSoV", "V", ""),
    "unset": _Writer("", "", "all", "f"),
    "declare": NAMEREFS,
    "typeset": NAMEREFS,
    "local": NAMEREFS,
    "export": DECLARING,
    "readonly": DECLARING,
    "let": _Writer("", "", "arithmetic"),
    "eval": _Writer("", "", "any"),
    "trap": _Writer("", "", "any"),
}


@dataclass(frozen=True)
class _VarScope:
    """Hold resolved variable and array values, locale names and assigned names."""

    values: dict[str, tuple[str, ...]] = field(default_factory=dict)
    arrays: dict[str, tuple[str, ...]] = field(default_factory=dict)
    assigned: frozenset[str] = frozenset()
    locale_vars: frozenset[str] = frozenset()


@dataclass
class _Scan:
    """Keep one file's ordered traversal and candidate state."""

    context: InspectionContext
    budget: BodyBudget
    dir_changed: bool = False
    source_unknown: bool = False
    candidates: list[tuple[str, bool, bool]] = field(default_factory=list)
    filtered: list[tuple[str, bool, bool]] = field(default_factory=list)
    cd_targets: list[str] = field(default_factory=list)
    literals: dict[str, str] = field(default_factory=dict)
    assigned: set[str] = field(default_factory=set)
    ambiguous: set[str] = field(default_factory=set)
    source_literals: dict[int, tuple[dict[str, str], set[str]]] = field(default_factory=dict)
    script_dirs: dict[str, str | None] = field(default_factory=dict)
    script_literals: set[str] = field(default_factory=set)
    unresolved_dirs: set[str] = field(default_factory=set)
    refused_dirs: dict[str, str] = field(default_factory=dict)
    prefixed_functions: dict[str, frozenset[str]] = field(default_factory=dict)
    prefix_calls: dict[str, set[str]] = field(default_factory=dict)
    function_bodies: dict[str, list[str]] = field(default_factory=dict)
    walked_functions: set[str] = field(default_factory=set)
    prefix_spans: list[tuple[bytes, int, int, frozenset[str]]] = field(default_factory=list)
    span_names: frozenset[str] = frozenset()
    span_held: frozenset[str] = frozenset()
    function_calls: dict[str, set[str]] = field(default_factory=dict)
    dynamic_callers: set[str] = field(default_factory=set)
    late_dirs: set[str] = field(default_factory=set)
    pending: list[tuple[str, bool, bool, frozenset[str]]] = field(default_factory=list)
    var_scope: _VarScope = field(default_factory=_VarScope)
    moved: bool = False
    rebound: bool = False
    command_rebound: bool = False
    child_rebound: bool = False
    prefix: str | None = None
    stack: list[tuple[Any, bytes, bool]] = field(default_factory=list)
    refusal: str | None = None


def bash_parser() -> Any | str:
    """Load and cache the Bash parser or explain how to install its pinned dependencies."""
    if _PARSERS:
        return _PARSERS[0]
    try:
        tree_sitter = importlib.import_module("tree_sitter")
        tree_sitter_bash = importlib.import_module("tree_sitter_bash")
        parser = tree_sitter.Parser(tree_sitter.Language(tree_sitter_bash.language()))
    except (ImportError, OSError):
        return (
            "shell script parser unavailable: tree-sitter-bash does not load under "
            f"{sys.executable}; set up the checkout venv: python3 -m venv .venv && "
            ".venv/bin/python3 -m pip install --require-hashes -r requirements-dev.txt"
        )
    _PARSERS.append(parser)
    return parser


def _text(node: Any, raw: bytes) -> str:
    """Decode a node's exact byte span without losing undecodable bytes."""
    return raw[node.start_byte : node.end_byte].decode("utf-8", "surrogateescape")


def _configure_reference(word: str) -> bool:
    """Recognize configure references while leaving unrelated dynamic data undecided."""
    masked = TRANSLATED_SPAN.sub("$", word).translate(STRIP_CHARS)
    decidable = DECIDABLE_NAME.fullmatch(os.path.basename(masked))
    literal = word.translate(STRIP_CHARS)
    return _is_configure_word(word) and bool(decidable or CONFIGURE_WORD.search(literal))


def _candidate_allowed(operand: str) -> bool:
    """Accept concrete script operands independently of directory state."""
    return bool(operand and operand != "-" and "$" not in operand and "`" not in operand)


def _remember_rebinding(operand: str, scan: _Scan) -> None:
    """Carry a possibly rebound command environment into scans of child file candidates."""
    if not scan.child_rebound:
        return
    if not _candidate_allowed(operand) or (scan.dir_changed and not os.path.isabs(operand)):
        scan.budget.rebound_unknown = True
        return
    try:
        scan.budget.rebound_scripts.add((scan.context.cwd / operand).resolve())
    except (OSError, RuntimeError):
        scan.budget.rebound_unknown = True


def _record_candidate(operand: str, is_program: bool, scan: _Scan) -> None:
    """Preserve the directory state at each concrete candidate's encounter.

    Words arrive with plain references to resolved own-directory names substituted, so a
    reference still left (an operator form such as ${N%/}) refuses.
    """
    _remember_rebinding(operand, scan)
    reason = _reference_refusal(operand, scan)
    if reason is not None:
        scan.refusal = reason
        return
    if _unknown_source_relative(operand, is_program, scan):
        scan.refusal = UNKNOWN_SOURCE_PATH
        return
    if _candidate_allowed(operand):
        changed = scan.dir_changed and not os.path.isabs(operand) and not operand.startswith("~")
        scan.candidates.append((operand, is_program, changed))
        return
    if _referenced(operand, scan.script_literals):
        scan.refusal = UNSUBSTITUTED_SCRIPT_PATH
        return
    unsettled = _referenced(operand, scan.script_dirs)
    if unsettled:
        scan.pending.append((operand, is_program, scan.dir_changed, unsettled))


def _referenced(word: str, names: Iterable[str]) -> frozenset[str]:
    """Return the names a word references.

    Matches simple references and braced ones with or without an operator ($N, ${N},
    ${N:-x}, ${N%/}); a longer name sharing the prefix does not match.
    """
    return frozenset(
        name
        for name in names
        if re.search(r"\$\{?" + re.escape(name) + r"(?![A-Za-z0-9_])", word) is not None
    )


def _reference_refusal(word: str, scan: _Scan) -> str | None:
    """Name the refusal for a prefix-held, unresolved, refused or ambiguous reference."""
    if _referenced(word, scan.span_names):
        return PREFIXED_FUNCTION_SCRIPT_DIR
    if _referenced(word, scan.unresolved_dirs):
        return UNRESOLVED_SCRIPT_DIR
    refused = _referenced(word, scan.refused_dirs)
    if refused:
        return scan.refused_dirs[min(refused)]
    if _referenced(word, scan.ambiguous):
        return AMBIGUOUS_SCRIPT_PATH
    return None


def _settle_pending(scan: _Scan) -> str | None:
    """Recheck script paths walked before their own-directory name was assigned.

    A function body is walked at its definition, so a reference in it can precede an
    assignment that runs before the call. A name that ended nested refuses as unresolved, one
    that ended uncertain refuses as after an unread source, and one resolved at the top level
    turns the reference into a concrete candidate. A word that still holds an expansion after
    substitution (an operator form such as ${N:-x}, or another variable) refuses, since the
    guard cannot name the file it runs.
    """
    for operand, is_program, dir_changed, names in scan.pending:
        reason = _reference_refusal(operand, scan)
        if reason is not None:
            return reason
        if not names <= scan.script_literals:
            return UNKNOWN_SOURCE_PATH
        resolved = _substitute_literals(operand, scan.literals)
        if "$" in resolved or "`" in resolved:
            return UNSUBSTITUTED_SCRIPT_PATH
        if _candidate_allowed(resolved):
            changed = dir_changed and not os.path.isabs(resolved) and not resolved.startswith("~")
            scan.candidates.append((resolved, is_program, changed))
    return None


def _unknown_source_relative(operand: str, is_program: bool, scan: _Scan) -> bool:
    """Identify relative file operands whose base an unread source may have changed."""
    return (
        scan.source_unknown
        and not os.path.isabs(operand)
        and not operand.startswith("~")
        and (not is_program or "/" in operand)
    )


def _record_directory(words: list[str], idx: int, scan: _Scan) -> None:
    """Record bounded static cd targets and mark subsequent relative paths changed."""
    if os.path.basename(words[idx]) in ("cd", "pushd"):
        targets = [w for w in words[idx + 1 :] if w not in ("--", "-L", "-P", "-e", "-@")]
        if (
            targets
            and _candidate_allowed(targets[0])
            and len(scan.cd_targets) < 16
            and targets[0] not in scan.cd_targets
        ):
            scan.cd_targets.append(targets[0])
    scan.dir_changed = scan.moved = True


def _queue_inline(body: str, scan: _Scan, *, source: bool = False) -> None:
    """Queue an inline syntax tree or record an explicit shared-budget refusal."""
    if scan.budget.inline_bodies >= MAX_INLINE_BODIES:
        scan.refusal = "more than 256 inline shell bodies"
        return
    scan.budget.inline_bodies += 1
    raw = body.encode("utf-8", "surrogateescape")
    if scan.span_held:
        scan.prefix_spans.append((raw, 0, len(raw) + 1, scan.span_held))
    root = bash_parser().parse(raw).root_node
    if source:
        watched = set(scan.literals) | {o for o in scan.script_dirs.values() if o is not None}
        names = _assignment_texts(root, raw, scan.budget, scan.var_scope)
        scan.var_scope = _merge_scopes(scan.var_scope, names.var_scope)
        scan.source_literals[id(root)] = _literal_map(names, raw, watched)
        _note_prefix_calls(names, scan)
    scan.stack.append((root, raw, False))


def _file_target(target: str | None, scan: _Scan) -> str | None:
    """Classify a shell file operand and preserve candidates in encounter order."""
    if target is None or target == "-":
        return None
    if _configure_reference(target):
        return target
    _record_candidate(target, False, scan)
    return None


def _translated_string(node: Any, raw: bytes) -> bool:
    """Identify a locale-translated $"..." node in either tree-sitter-bash shape."""
    if node.type == "translated_string":
        return True
    prev = node.prev_sibling if node.type == "string" else None
    return prev is not None and (_anonymous_dollar(prev) or _dollar_prefix(prev, raw))


def _dollar_prefix(node: Any, raw: bytes) -> bool:
    """Identify a multi-byte $ token immediately preceding a translated string."""
    span = raw[node.start_byte : node.end_byte]
    is_prefix = (
        not node.is_named
        and node.type == "$"
        and len(span) > 1
        and not span.endswith((b"\\$", b"$$"))
    )
    nxt = node.next_sibling if is_prefix else None
    return nxt is not None and nxt.type == "string" and node.end_byte == nxt.start_byte


def _anonymous_dollar(node: Any) -> bool:
    """Identify a one-byte anonymous $ token immediately preceding a string node."""
    nxt = node.next_sibling
    return (
        not node.is_named
        and node.type == "$"
        and node.end_byte - node.start_byte == 1
        and nxt is not None
        and nxt.type == "string"
        and node.end_byte == nxt.start_byte
    )


def _part_start(node: Any, raw: bytes) -> int:
    """Return a word part's start byte, including any leading anonymous $ token."""
    if node.type == "string" and _translated_string(node, raw) and not node.prev_sibling.is_named:
        return node.prev_sibling.start_byte
    return node.start_byte


def _translated_spans(node: Any, raw: bytes) -> list[tuple[int, int]]:
    """Collect byte spans of locale-translated $"..." strings within a command."""
    spans: list[tuple[int, int]] = []
    stack = [node]
    for _ in range(MAX_BODY_NODES):
        if not stack:
            break
        cur = stack.pop()
        if _translated_string(cur, raw):
            start = cur.prev_sibling.start_byte if cur.type == "string" else cur.start_byte
            spans.append((start, cur.end_byte))
        elif cur.type not in SUBSHELLS:
            stack.extend(reversed(cur.named_children))
    return sorted(spans)


def _tokenize_span(
    raw: bytes, start: int, end: int, translated: list[tuple[int, int]]
) -> list[str]:
    """Tokenize a non-assignment command span while keeping $"..." segments intact."""
    local = [(s, e) for s, e in translated if start <= s and e <= end]
    if not local:
        text = raw[start:end].decode("utf-8", "surrogateescape")
        tokens = _tokens(text)
        return tokens if tokens is not None else [text.translate(STRIP_CHARS)]
    parts: list[bytes] = []
    reps: list[tuple[str, str]] = []
    cursor = start
    for idx, (s, e) in enumerate(local):
        tag = f"__RCG_TR_{idx}__"
        parts.extend((raw[cursor:s], tag.encode("ascii")))
        reps.append((tag, raw[s:e].decode("utf-8", "surrogateescape")))
        cursor = e
    parts.append(raw[cursor:end])
    rewritten = b"".join(parts).decode("utf-8", "surrogateescape")
    tokens = _tokens(rewritten)
    out = tokens if tokens is not None else [rewritten.translate(STRIP_CHARS)]
    for tag, orig in reps:
        out = [tok.replace(tag, orig) for tok in out]
    return out


def _ignored_command_child(node: Any, idx: int, child: Any, raw: bytes) -> bool:
    """Tell whether a command child is punctuation, a redirect or an anonymous $ prefix."""
    if _anonymous_dollar(child):
        return True
    field_name = node.field_name_for_child(idx)
    if child.type != "variable_assignment" and field_name not in ("name", "argument"):
        return True
    value = _text(child, raw)
    return bool(re.fullmatch(r"\{[A-Za-z_][A-Za-z_0-9]*\}", value)) and raw[
        child.end_byte : child.end_byte + 1
    ] in (b"<", b">")


def _command_spans(node: Any, raw: bytes) -> list[tuple[int, int, bool]]:
    """Collect adjacent command fields while keeping assignments in their own spans."""
    spans: list[tuple[int, int, bool]] = []
    for i, child in enumerate(node.children):
        if _ignored_command_child(node, i, child, raw):
            continue
        assignment = child.type == "variable_assignment"
        if spans and not assignment and not spans[-1][2] and spans[-1][1] == child.start_byte:
            spans[-1] = (spans[-1][0], child.end_byte, False)
        else:
            spans.append((_part_start(child, raw), child.end_byte, assignment))
    return spans


def _command_words(node: Any, raw: bytes) -> list[str]:
    """Tokenize command fields while preserving each assignment as a single word."""
    words: list[str] = []
    translated = _translated_spans(node, raw)
    for start, end, assignment in _command_spans(node, raw):
        if assignment:
            words.append(raw[start:end].decode("utf-8", "surrogateescape"))
        else:
            words.extend(_tokenize_span(raw, start, end, translated))
    return [word for word in words if word not in ("(", ")")]


def _command_info(
    node: Any, raw: bytes, literals: dict[str, str] | None = None
) -> tuple[list[str], int, str]:
    """Return unwrapped command words, position and basename for a syntax node."""
    if node is None:
        return [], 0, ""
    if node.type == "redirected_statement":
        node = node.child_by_field_name("body")
    if node is None or node.type != "command":
        return [], 0, ""
    words = [_substitute_literals(word, literals or {}) for word in _command_words(node, raw)]
    idx = _unwrap_command(words, 0)
    return words, idx, os.path.basename(words[idx]) if idx < len(words) else ""


def _input_redirects(node: Any) -> list[Any]:
    """Collect input file redirections attached to a command or its wrapper."""
    parent = node.parent
    owners = [node]
    if parent is not None and parent.type == "redirected_statement":
        owners.append(parent)
    result = []
    for i in range(len(owners)):
        children = owners[i].named_children
        for j in range(len(children)):
            child = children[j]
            if child.type == "file_redirect" and any(c.type == "<" for c in child.children):
                result.append(child.child_by_field_name("destination"))
    return [n for n in result if n is not None]


def _sed_regex_end(program: str, start: int, delimiter: str) -> int | None:
    """Find a sed regex delimiter while honoring escaped characters."""
    escaped = False
    for i in range(start, len(program)):
        char = program[i]
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == delimiter:
            return i + 1
    return None


def _sed_line_filter(program: str) -> bool:
    """Allow only line-filter commands after removing delimited address regexes."""
    cursor = 0
    remaining = []
    for i, char in enumerate(program):
        if i < cursor:
            continue
        start = i + 1
        delimiter = char
        if char == "\\" and start < len(program):
            delimiter = program[start]
            start += 1
        elif char != "/":
            remaining.append(char)
            continue
        end = _sed_regex_end(program, start, delimiter)
        if end is None:
            return False
        cursor = end
    return re.fullmatch(r"[\s0-9$,~+!;{}pdqQ=IM]*", "".join(remaining)) is not None


def _sed_operands(words: list[str]) -> list[str] | None:
    """Extract sed file operands only when all options and programs are line filters."""
    programs: list[str] = []
    operands: list[str] = []
    options = True
    expression = False
    allowed = frozenset(("-n", "--quiet", "--silent", "-E", "-r", "--regexp-extended"))
    for word in words:
        if expression:
            programs.append(word)
            expression = False
        elif options and word == "--":
            options = False
        elif options and word.startswith("-") and word != "-":
            if word == "-e":
                expression = True
            elif word not in allowed:
                return None
        else:
            operands.append(word)
    if not programs and operands:
        programs.append(operands.pop(0))
    if expression or not programs or not all(_sed_line_filter(program) for program in programs):
        return None
    return operands


def _reader_operands(words: list[str], idx: int) -> list[str] | None:
    """Return cat, head, tail or line-filter sed file operands after reader options."""
    if idx >= len(words):
        return None
    name = os.path.basename(words[idx])
    if name == "sed":
        return _sed_operands(words[idx + 1 :])
    if name not in ("cat", "head", "tail"):
        return None
    operands = []
    options = True
    skip = False
    values = frozenset(("-n", "-c", "--lines", "--bytes")) if name != "cat" else frozenset()
    for word in words[idx + 1 :]:
        if skip:
            skip = False
        elif options and word == "--":
            options = False
        elif options and word in values:
            skip = True
        elif not options or word == "-" or not word.startswith("-"):
            operands.append(word)
    return None if skip else operands


def _record_filtered(operand: str, scan: _Scan) -> None:
    """Bound concrete reader operands and preserve their encountered directory state."""
    if not _candidate_allowed(operand):
        return
    changed = scan.dir_changed and not os.path.isabs(operand) and not operand.startswith("~")
    entry = (operand, False, changed)
    if entry not in scan.filtered:
        if len(scan.filtered) >= MAX_SCRIPT_FILES:
            scan.refusal = "more nested scripts than inspect limit"
        else:
            scan.filtered.append(entry)


def _reader_hit(
    words: list[str], idx: int, operands: list[str], scan: _Scan, *, filtered: bool
) -> str | None:
    """Inspect unquoted reader words and record shell file and filtered text candidates."""
    joined = " ".join(words[idx:])
    if CONFIGURE_WORD.search(joined) or any(_names_configure(word) for word in words[idx:]):
        return joined
    for operand in operands:
        hit = _file_target(operand, scan)
        if hit is not None:
            return hit
        if filtered:
            _record_filtered(operand, scan)
    return None


def _single_reader(node: Any) -> Any:
    """Select a single command substitution body with no nested substitutions or lists."""
    children = node.named_children
    if len(children) != 1 or children[0].type != "command":
        return None
    command = children[0]
    stack = list(command.named_children)
    for _ in range(MAX_BODY_NODES):
        if not stack:
            return command
        child = stack.pop()
        if child.type in ("command_substitution", "process_substitution"):
            return None
        stack.extend(child.named_children)
    return None


def _process_reader_hit(node: Any, raw: bytes, scan: _Scan) -> str | None:
    """Inspect a sole file reader supplying shell text through process substitution."""
    words, idx, name = _command_info(_single_reader(node), raw, _live_literals(scan))
    operands = _reader_operands(words, idx)
    if not operands or "-" in operands:
        return "shell reads a process substitution the guard cannot inspect"
    return _reader_hit(words, idx, operands, scan, filtered=name != "cat" or len(operands) != 1)


def _shell_command(
    node: Any, raw: bytes, words: list[str], scan: _Scan, *, source: bool = False
) -> str | None:
    """Inspect shell inline programs, file operands and redirected process inputs."""
    if source:
        target, refusal = _source_operand(words[1:])
        if refusal is not None:
            return refusal
        inline = False
    else:
        target, inline = _shell_argument(words)
    if inline:
        if target is not None:
            _queue_inline(target, scan)
        return None
    return _shell_inputs(node, raw, target, scan, source=source)


def _source_operand(words: list[str]) -> tuple[str | None, str | None]:
    """Allow one leading double dash and refuse every other source option word."""
    idx = 1 if words and words[0] == "--" else 0
    if idx >= len(words):
        return None, None
    if idx == 0 and words[idx].startswith("-"):
        return None, f"source option the guard does not follow: {words[idx]}"
    return words[idx], None


def _source_target(target: str | None, scan: _Scan) -> str | None:
    """Inline readable source texts into the caller and mark unresolved directories unknown."""
    if target is None:
        return None
    if _configure_reference(target):
        return target
    if _unknown_source_relative(target, False, scan):
        return UNKNOWN_SOURCE_PATH
    reader = scan.context.source_reader
    if not _candidate_allowed(target) or reader is None:
        _record_candidate(target, False, scan)
        _forget_script_literals(scan)
        scan.dir_changed = scan.source_unknown = True
        return None
    changed = scan.dir_changed and not os.path.isabs(target) and not target.startswith("~")
    texts = reader(target, changed, scan.context, scan.cd_targets)
    if isinstance(texts, str):
        return texts
    for text in reversed(texts):
        _queue_inline(text, scan, source=True)
    return None


def _shell_inputs(
    node: Any, raw: bytes, target: str | None, scan: _Scan, *, source: bool = False
) -> str | None:
    """Inspect a shell's process substitutions and plain redirected input filenames."""
    children = node.named_children
    for i in range(len(children)):
        child = children[i]
        if child.type == "process_substitution":
            hit = _process_reader_hit(child, raw, scan)
            if hit is not None:
                return hit
            if target in ("<", ">"):
                target = None
    redirects = _input_redirects(node)
    for i in range(len(redirects)):
        dest = redirects[i]
        if dest.type == "process_substitution":
            hit = _process_reader_hit(dest, raw, scan)
            if hit is not None:
                return hit
        elif target is None and dest.type == "word":
            target = _text(dest, raw)
    return _source_target(target, scan) if source else _file_target(target, scan)


def _data_hit(text: str) -> str | None:
    """Find concrete configure paths among whitespace-separated data words."""
    for part in text.translate(STRIP_CHARS).split():
        if "/" in part and _names_configure(part):
            return part
    return None


def _without_substitutions(node: Any, raw: bytes) -> str:
    """Replace descendant shell substitutions with spaces using a bounded stack."""
    stack = [node]
    spans = []
    for _ in range(MAX_BODY_NODES):
        if not stack:
            break
        child = stack.pop()
        if child.type in ("command_substitution", "process_substitution"):
            spans.append((child.start_byte, child.end_byte))
        else:
            stack.extend(reversed(child.named_children))
    parts = []
    cursor = node.start_byte
    for start, end in spans:
        parts.extend((raw[cursor:start], b" "))
        cursor = end
    parts.append(raw[cursor : node.end_byte])
    return b"".join(parts).decode("utf-8", "surrogateescape")


def _assignment_hit(node: Any, raw: bytes) -> str | None:
    """Check assignment values and array elements without their substitution bodies."""
    assignments = (
        [node]
        if node.type == "variable_assignment"
        else [child for child in node.named_children if child.type == "variable_assignment"]
    )
    for assignment in assignments:
        value = assignment.child_by_field_name("value")
        if value is None or value.type in ("command_substitution", "process_substitution"):
            continue
        values = value.named_children if value.type == "array" else [value]
        for part in values:
            hit = _data_hit(_without_substitutions(part, raw))
            if hit is not None:
                return hit
    return None


def _dispatch_command(
    node: Any, raw: bytes, tail: list[str], scan: _Scan
) -> tuple[bool, str | None]:
    """Dispatch shell, source, eval, trap or direct configure words for a command head."""
    name = os.path.basename(tail[0])
    if name in SHELLS:
        return True, _shell_command(node, raw, tail, scan)
    if name in ("source", "."):
        return True, _shell_command(node, raw, tail, scan, source=True)
    if name in EVAL_BUILTINS:
        if name == "eval":
            _queue_inline(" ".join(tail[1:]), scan)
        else:
            _trap_program(tail[1:], scan)
        return True, None
    if "$" not in tail[0] and "`" not in tail[0] and _is_configure_word(tail[0]):
        return True, tail[0]
    return False, None


def _inspect_resolved_command(
    node: Any, raw: bytes, words: list[str], idx: int, scan: _Scan
) -> tuple[bool, str | None]:
    """Dispatch candidate invocations resolved from a run-time or clustered command head."""
    base_idx, calls, _ = _resolved_invocations(node, raw, scan.var_scope)
    any_handled = False
    for head, extra, pos in calls:
        tail = [head, *extra, *words[idx + max(pos - base_idx, 1) :]]
        sub_handled, sub_hit = _dispatch_command(node, raw, tail, scan)
        if not sub_handled:
            sub_hit = _ordinary_command(tail, 0, scan)
        if sub_hit is not None or scan.refusal is not None:
            return True, sub_hit
        any_handled |= sub_handled
    return any_handled, None


def _inspect_command(node: Any, raw: bytes, scan: _Scan) -> str | None:
    """Classify command position, wrappers and executable paths without following call cycles."""
    words, idx, _ = _command_info(node, raw, _live_literals(scan))
    if idx >= len(words):
        return None
    scan.child_rebound = scan.rebound or scan.command_rebound or _prefix_rebinds(words[:idx])
    handled, hit = _dispatch_command(node, raw, words[idx:], scan)
    if handled:
        return hit
    clustered = idx > 0 and words[idx - 1] in ("command", "builtin") and words[idx].startswith("-")
    if "$" in words[idx] or clustered:
        res_handled, res_hit = _inspect_resolved_command(node, raw, words, idx, scan)
        if res_handled:
            return res_hit
    return _ordinary_command(words, idx, scan)


def _trap_program(words: list[str], scan: _Scan) -> None:
    """Queue the first trap program after an optional double dash."""
    idx = 1 if words and words[0] == "--" else 0
    if idx < len(words) and words[idx] and not words[idx].startswith("-"):
        _queue_inline(words[idx], scan)


def _later_operands(words: list[str], idx: int, scan: _Scan) -> str | None:
    """Inspect concrete data paths and shell operands following helper commands."""
    for i in range(idx + 1, len(words)):
        word = words[i]
        hit = _data_hit(word)
        if hit is not None:
            return hit
        if os.path.basename(word) in SHELLS:
            target, inline = _shell_argument(words[i:])
            if inline:
                if target is not None:
                    _queue_inline(target, scan)
            else:
                hit = _file_target(target, scan)
                if hit is not None:
                    return hit
    return None


def _ordinary_command(words: list[str], idx: int, scan: _Scan) -> str | None:
    """Check direct configure execution, dispatchers and later executable operands."""
    word = words[idx]
    name = os.path.basename(word)
    static_command = "$" not in word and "`" not in word
    if _configure_reference(word) or (static_command and _is_configure_word(word)):
        return word
    classified = scan.context.classifier(words)
    if classified is not None:
        return str(classified)
    if name not in DATA_ONLY_COMMANDS:
        hit = _later_operands(words, idx, scan)
        if hit is not None:
            return hit
    if "/" in word or _program_name_allowed(word) or _reference_refusal(word, scan) is not None:
        _record_candidate(word, True, scan)
    if name in ("cd", "pushd", "popd"):
        _record_directory(words, idx, scan)
    return None


def _literal_pipe(stages: list[Any], raw: bytes, scan: _Scan) -> str | None:
    """Inspect literal producers and bounded file readers piped into a shell."""
    producers = [_command_info(stage, raw, _live_literals(scan)) for stage in stages]
    filtered = any(
        name != "cat" or len(_reader_operands(words, idx) or []) != 1
        for words, idx, name in producers[1:]
    )
    for i, (words, idx, name) in enumerate(producers):
        if name in ("echo", "printf"):
            _queue_inline(" ".join(words[idx + 1 :]), scan)
            continue
        operands = _reader_operands(words, idx)
        if operands is None or (i == 0 and not any(word != "-" for word in operands)):
            return "shell reads piped input the guard cannot inspect"
        hit = _reader_hit(
            words,
            idx,
            operands,
            scan,
            filtered=filtered or i != 0 or name != "cat" or len(operands) != 1,
        )
        if hit is not None:
            return hit
    return None


def _pipeline_hit(node: Any, raw: bytes, scan: _Scan) -> str | None:
    """Inspect shell stdin producers and configure data piped into executable consumers."""
    stages = node.named_children
    for i in range(1, len(stages)):
        words, idx, name = _command_info(stages[i], raw, _live_literals(scan))
        consumer = name in ("xargs", "make", "eval", "source", ".") or INTERPRETERS.fullmatch(name)
        if name in SHELLS:
            target, inline = _shell_argument(words[idx:])
            if not inline and target in (None, "-"):
                hit = _literal_pipe(stages[:i], raw, scan)
                if hit is not None:
                    return hit
            consumer = False
        earlier = raw[node.start_byte : stages[i].start_byte].decode("utf-8", "surrogateescape")
        if consumer and CONFIGURE_WORD.search(earlier):
            return earlier
    return None


def _error_hit(node: Any, raw: bytes) -> str | None:
    """Refuse malformed spans that can name commands and leave other spans as data."""
    text = _text(node, raw)
    dangerous = bool(CONFIGURE_WORD.search(text))
    words = re.split(r"[\s;&|()<>]+", text)
    for i in range(len(words)):
        word = words[i].strip("\"'")
        base = os.path.basename(word)
        path = "/" in word and "$" not in word and "`" not in word
        concrete = bool(DECIDABLE_NAME.fullmatch(base) and re.search(r"[A-Za-z0-9]", base))
        dangerous |= base in ERROR_COMMANDS or (path and concrete)
    if dangerous:
        return f"unparsable shell text naming a command near line {node.start_point.row + 1}"
    return None


def _redirect_owner(node: Any) -> Any:
    """Find the command owning a redirect without walking through unrelated ancestors."""
    parent = node.parent
    if parent is not None and parent.type == "redirected_statement":
        return parent.child_by_field_name("body")
    return parent


def _heredoc_parts(node: Any, raw: bytes) -> tuple[str, bool, Any, list[Any]]:
    """Extract redirect payload, expansion policy and redirect-local pipelines."""
    body = None
    quoted = False
    statements = []
    children = node.named_children
    for i in range(len(children)):
        child = children[i]
        if child.type == "heredoc_start":
            quoted = any(c in _text(child, raw) for c in "'\"\\")
        elif child.type in ("pipeline", "command", "list"):
            statements.append(child)
        elif child.type == "heredoc_body" or (
            node.type == "herestring_redirect" and child.type != "ERROR"
        ):
            body = child
    text = _text(body, raw) if body is not None else ""
    if node.type == "herestring_redirect":
        text = " ".join(_tokens(text) or [text.translate(STRIP_CHARS)])
    return text, quoted, body, statements


def _heredoc(node: Any, raw: bytes, scan: _Scan) -> tuple[str | None, list[tuple[Any, bool]]]:
    """Inspect executable redirect payloads and expanding substitutions in data payloads."""
    text, quoted, body, statements = _heredoc_parts(node, raw)
    words, idx, name = _command_info(_redirect_owner(node), raw, _live_literals(scan))
    target, inline = _shell_argument(words[idx:]) if name in SHELLS else (None, False)
    shell_input = name in SHELLS and not inline and target in (None, "-")
    for i in range(len(statements)):
        stages = statements[i].named_children
        for j in range(len(stages)):
            _, _, stage_name = _command_info(stages[j], raw, _live_literals(scan))
            shell_input |= stage_name in SHELLS
    children = [(n, False) for n in node.named_children if n != body]
    if shell_input:
        _queue_inline(text, scan)
    elif name in DATA_ONLY_COMMANDS and not statements:
        if body is not None and not quoted:
            children.append((body, True))
    elif CONFIGURE_WORD.search(text):
        return text, children
    return None, children


def _child_modes(node: Any, data_only: bool) -> list[tuple[Any, bool]]:
    """Select structural bodies while retaining substitution discovery in data fields."""
    result = []
    children = node.named_children
    for i in range(len(children)):
        child = children[i]
        mode = data_only
        if node.type in ("for_statement", "c_style_for_statement", "function_definition"):
            mode = child != node.child_by_field_name("body")
        elif node.type in ("case_statement", "case_item"):
            mode = child in node.children_by_field_name("value")
        result.append((child, mode))
    return result


def _plain_expansion(node: Any, raw: bytes, *, defaults: bool = False) -> bool:
    """Accept a plain braced expansion or an optional read-only default expansion."""
    if node.has_error:
        return False
    text = _text(node, raw)
    return PLAIN_EXPANSION.fullmatch(text) is not None or (
        defaults and DEFAULT_EXPANSION.fullmatch(text) is not None
    )


def _plain_string(node: Any, raw: bytes, *, defaults: bool = False) -> bool:
    """Accept a double-quoted string of literals, plain expansions or optional defaults."""
    for child in node.named_children:
        if child.has_error:
            return False
        if child.type in ("string_content", "simple_expansion"):
            continue
        if child.type != "expansion" or not _plain_expansion(child, raw, defaults=defaults):
            return False
    return True


def _plain_part(node: Any, raw: bytes, *, defaults: bool = False) -> bool:
    """Accept one static, plain-expansion or optional read-only default value part."""
    if node.has_error:
        return False
    kind = node.type
    if kind == "word":
        return not node.named_children and PLAIN_WORD.fullmatch(_text(node, raw)) is not None
    if kind in ("raw_string", "ansi_c_string", "number", "simple_expansion"):
        return True
    if kind == "expansion":
        return _plain_expansion(node, raw, defaults=defaults)
    return kind == "string" and _plain_string(node, raw, defaults=defaults)


def _plain_value(node: Any, raw: bytes, *, defaults: bool = False) -> bool:
    """Accept an absent value, plain part or concatenation with optional default expansions."""
    if node is None:
        return True
    if node.has_error or node.type == "array":
        return False
    if node.type == "concatenation":
        named = node.named_children
        return (
            bool(named)
            and len(node.children) == len(named)
            and all(_plain_part(child, raw, defaults=defaults) for child in named)
        )
    return _plain_part(node, raw, defaults=defaults)


def _plain_assignment_name(node: Any, raw: bytes) -> bool:
    """Accept a plain variable_name target outside BASH_VARIABLES and os.environ."""
    name_node = node.child_by_field_name("name")
    if name_node is None or name_node.type != "variable_name":
        return False
    name = _text(name_node, raw)
    if ASSIGNED_NAME.fullmatch(name) is None:
        return False
    return name not in BASH_VARIABLES and name not in os.environ


def _plain_assignment(node: Any, raw: bytes) -> bool:
    """Accept a command-free scalar assignment with a plain or read-only default value."""
    parent = node.parent
    if node.type != "variable_assignment" or node.has_error or parent is None:
        return False
    if parent.type not in ("program", "variable_assignments"):
        return False
    children = node.children
    if len(children) not in (2, 3) or children[1].type not in ("=", "+="):
        return False
    return _plain_assignment_name(node, raw) and _plain_value(
        node.child_by_field_name("value"), raw, defaults=True
    )


def _protected_name(name: str) -> bool:
    """Identify a Bash, environment or loader variable name that an assignment must not target."""
    return (
        name in BASH_VARIABLES
        or name in os.environ
        or name.startswith("LD_")
        or name in LOADER_VARIABLES
    )


def _own_dir_assignment(node: Any, raw: bytes, scan: _Scan) -> bool:
    """Accept a scalar assignment binding a non-protected own-directory name to the idiom."""
    if node.type != "variable_assignment" or node.has_error:
        return False
    name_node = node.child_by_field_name("name")
    value = node.child_by_field_name("value")
    if name_node is None or name_node.type != "variable_name" or value is None:
        return False
    if len(node.children) != 3 or node.children[1].type != "=":
        return False
    name = _text(name_node, raw)
    if _protected_name(name):
        return False
    return name in scan.script_dirs and _script_dir_value(_text(value, raw))[0]


def _own_dir_declaration(node: Any, raw: bytes, scan: _Scan) -> bool:
    """Accept a declaration of one own-directory assignment with optional -[rgx]+ flags."""
    children = node.children
    if len(children) < 2 or children[0].is_named:
        return False
    for flag in children[1:-1]:
        if flag.type != "word" or DECLARATION_FLAGS.fullmatch(_text(flag, raw)) is None:
            return False
    return _own_dir_assignment(children[-1], raw, scan)


def _allowed_export_name(name: str) -> bool:
    """Accept an exported variable identifier that is not a protected name."""
    return ASSIGNED_NAME.fullmatch(name) is not None and not _protected_name(name)


def _allowed_export_assignment(node: Any, raw: bytes) -> bool:
    """Accept an export NAME=word assignment with a safe name and plain-word value."""
    name_node = node.child_by_field_name("name")
    value = node.child_by_field_name("value")
    if name_node is None or name_node.type != "variable_name" or value is None:
        return False
    if len(node.children) != 3 or node.children[1].type != "=":
        return False
    if value.has_error or value.type != "word" or value.named_children:
        return False
    return _allowed_export_name(_text(name_node, raw)) and (
        PLAIN_WORD.fullmatch(_text(value, raw)) is not None
    )


def _allowed_export(node: Any, raw: bytes) -> bool:
    """Accept top-level export NAME or export NAME=word for safe variable names."""
    children = node.children
    if node.has_error or len(children) != 2 or children[0].is_named:
        return False
    if children[0].type != "export" or children[1].has_error:
        return False
    operand = children[1]
    if operand.type == "variable_name":
        return not operand.named_children and _allowed_export_name(_text(operand, raw))
    return operand.type == "variable_assignment" and _allowed_export_assignment(operand, raw)


def _allowed_assignments(node: Any, raw: bytes, scan: _Scan) -> bool:
    """Accept compound variable_assignments whose members are own-directory or plain."""
    children = node.children
    return bool(children) and all(
        _own_dir_assignment(child, raw, scan) or _plain_assignment(child, raw) for child in children
    )


def _part_dash_state(part: Any, raw: bytes) -> bool | None:
    """Classify a plain part as empty (None), dash-prefixed (True) or other (False)."""
    kind = part.type
    text = _text(part, raw)
    if kind in ("raw_string", "ansi_c_string"):
        inner = text[1:-1] if kind == "raw_string" else text[2:-1]
        if not inner:
            return None
        return inner.startswith("-") or (kind == "ansi_c_string" and inner.startswith("\\"))
    if kind == "string":
        if not part.named_children:
            return None
        first = part.named_children[0]
        return first.type == "string_content" and _text(first, raw).startswith("-")
    return text.startswith("-")


def _starts_with_dash(node: Any, raw: bytes) -> bool:
    """Detect whether a plain operand starts with a literal or ANSI-escaped dash."""
    parts = node.named_children if node.type == "concatenation" else [node]
    for part in parts:
        state = _part_dash_state(part, raw)
        if state is not None:
            return state
    return False


def _allowed_set(args: list[Any], raw: bytes) -> bool:
    """Accept non-empty set flags from [-+][eu]+ and [-+][eu]*o pipefail|errexit|nounset."""
    if not args:
        return False
    want_option = False
    for arg in args:
        word = _text(arg, raw) if arg.type == "word" else ""
        if want_option:
            if word not in SET_OPTIONS:
                return False
            want_option = False
        elif SET_OPTION_CLUSTER.fullmatch(word) is not None:
            want_option = True
        elif SET_FLAGS.fullmatch(word) is None:
            return False
    return not want_option


def _allowed_cd(args: list[Any], raw: bytes) -> bool:
    """Accept cd with an optional -P, -L or -- flag and at most one non-oldpwd plain target."""
    idx = 0
    dashdash = False
    if args and args[0].type == "word" and _text(args[0], raw) in ("-P", "-L", "--"):
        dashdash = _text(args[0], raw) == "--"
        idx = 1
    rest = args[idx:]
    if not rest:
        return True
    if len(rest) > 1 or not _plain_value(rest[0], raw):
        return False
    if not dashdash and _starts_with_dash(rest[0], raw):
        return False
    return _tokens(_text(rest[0], raw)) != ["-"]


def _allowed_source(args: list[Any], raw: bytes) -> bool:
    """Accept source or dot with a single non-option plain path operand."""
    return len(args) == 1 and _plain_value(args[0], raw) and not _starts_with_dash(args[0], raw)


def _allowed_exit(args: list[Any], raw: bytes) -> bool:
    """Accept exit with no status or a single unquoted non-negative integer status."""
    return not args or (len(args) == 1 and EXIT_STATUS.fullmatch(_text(args[0], raw)) is not None)


def _allowed_command_args(name: str, args: list[Any], raw: bytes) -> bool:
    """Validate arguments for an allow-listed top-level builtin or utility."""
    if name == "set":
        return _allowed_set(args, raw)
    if name == "cd":
        return _allowed_cd(args, raw)
    if name in (".", "source"):
        return _allowed_source(args, raw)
    if name in ("true", ":"):
        return not args
    return name == "exit" and _allowed_exit(args, raw)


def _command_head_name(node: Any, raw: bytes) -> str | None:
    """Extract an unquoted command word when no prefix or redirect wraps the command."""
    parent = node.parent
    if node.type != "command" or (parent is not None and parent.type == "redirected_statement"):
        return None
    named = node.named_children
    if not named or len(node.children) != len(named) or named[0].type != "command_name":
        return None
    head = named[0].children
    return _text(head[0], raw) if len(head) == 1 and head[0].type == "word" else None


def _allowed_command(node: Any, raw: bytes) -> bool:
    """Accept an unredirected allow-listed command with plain-typed argument nodes."""
    if node.has_error:
        return False
    name = _command_head_name(node, raw)
    if name is None:
        return False
    args = node.named_children[1:]
    if any(arg.type not in COMMAND_ARG_TYPES or arg.has_error for arg in args):
        return False
    return _allowed_command_args(name, args, raw)


def _function_body_allowed(body_node: Any, raw: bytes) -> bool:
    """Refuse function bodies naming PATH or a documented Bash variable before =."""
    return not any(
        match.group(1) == "PATH" or match.group(1) in BASH_VARIABLES
        for match in BODY_ASSIGNED_NAME.finditer(_text(body_node, raw))
    )


def _allowed_function(node: Any, raw: bytes) -> bool:
    """Accept an unredirected plain function shadowing no builtin and naming no Bash var=."""
    name_node = node.child_by_field_name("name")
    body_node = node.child_by_field_name("body")
    if name_node is None or name_node.type != "word" or body_node is None:
        return False
    allowed_parts = {name_node, body_node}
    if body_node.type == "redirected_statement" or any(
        c not in allowed_parts and c.type != "comment" for c in node.named_children
    ):
        return False
    name = _text(name_node, raw)
    return (
        PLAIN_FUNCTION.fullmatch(name) is not None
        and name not in BLOCKED_FUNCTIONS
        and _function_body_allowed(body_node, raw)
    )


def _step_list_node(current: Any, raw: bytes, stack: list[Any]) -> bool:
    """Validate one list node's operators and queue nested && or || sublists."""
    if current.has_error or not current.named_children:
        return False
    for child in current.children:
        if child.type in ("&&", "||", "comment"):
            continue
        if child.type == "list":
            stack.append(child)
        elif not child.is_named or not _allowed_command(child, raw):
            return False
    return True


def _allowed_list(node: Any, raw: bytes) -> bool:
    """Walk an && or || command list with a bounded explicit stack."""
    stack = [node]
    for _ in range(MAX_BODY_NODES):
        if not stack:
            return True
        if not _step_list_node(stack.pop(), raw, stack):
            return False
    return False


def _assignment_statement_allowed(node: Any, raw: bytes, scan: _Scan) -> bool:
    """Accept top-level own-directory, plain assignment or safe export statements."""
    kind = node.type
    if kind == "variable_assignment":
        return _own_dir_assignment(node, raw, scan) or _plain_assignment(node, raw)
    if kind == "declaration_command":
        return _own_dir_declaration(node, raw, scan) or _allowed_export(node, raw)
    return kind == "variable_assignments" and _allowed_assignments(node, raw, scan)


def _statement_allowed(node: Any, raw: bytes, scan: _Scan) -> bool:
    """Accept allow-listed top-level assignments, exports, commands, functions and lists."""
    if node.has_error or (node.next_sibling is not None and node.next_sibling.type == "&"):
        return False
    kind = node.type
    if kind == "comment" or _assignment_statement_allowed(node, raw, scan):
        return True
    if kind == "command":
        return _allowed_command(node, raw)
    if kind == "function_definition":
        return _allowed_function(node, raw)
    return kind == "list" and _allowed_list(node, raw)


def _statement_shape(node: Any, raw: bytes) -> str:
    """Format a disallowed top-level statement's command word or syntax node type."""
    name_node = node.child_by_field_name("name") if node.type == "command" else None
    source = _text(name_node, raw) if name_node is not None else node.type
    return SHAPE_DISALLOWED.sub("", source)[:32] or node.type


def _note_statement(node: Any, raw: bytes, scan: _Scan) -> None:
    """Record the first top-level statement outside the allow-list before own-dir resolution."""
    if not scan.script_dirs or scan.prefix is not None:
        return
    if not _statement_allowed(node, raw, scan):
        scan.prefix = PREFIX_SCRIPT_DIR.format(
            line=node.start_point.row + 1,
            shape=_statement_shape(node, raw),
        )


def _push_visit_children(
    kind: str, entry: _DirState, children: list[tuple[Any, bool]], raw: bytes, scan: _Scan
) -> None:
    """Restore subshell directory state after pushing non-missing child syntax nodes."""
    if kind in SUBSHELLS:
        scan.stack.append((entry, raw, False))
    for i in range(len(children) - 1, -1, -1):
        child, mode = children[i]
        if not child.is_missing:
            scan.stack.append((child, raw, mode))


def _visit(node: Any, raw: bytes, data_only: bool, scan: _Scan) -> str | None:
    """Apply the grammar allow-list, push children in order and restore subshell directories."""
    if isinstance(node, _DirState):
        scan.dir_changed, scan.moved, scan.command_rebound = node
        return None
    kind = node.type
    if kind not in STRUCTURE | ASSIGNMENTS | DATA | LEAVES | SPECIAL:
        return f"unsupported shell syntax {kind} near line {node.start_point.row + 1}"
    if kind == "function_definition":
        _note_prefix_span(node, raw, scan)
    scan.span_names = _span_names(node, raw, scan)
    if node.parent is not None and node.parent.type == "program":
        _note_statement(node, raw, scan)
    if kind == "ERROR":
        return _error_hit(node, raw)
    if kind in LEAVES:
        return None
    if kind in ("command_substitution", "process_substitution"):
        data_only = False
    entry = _DirState(scan.dir_changed, scan.moved, scan.command_rebound)
    hit, children = _visit_semantics(node, raw, data_only, scan)
    _push_visit_children(kind, entry, children, raw, scan)
    return hit


def _function_key(node: Any, raw: bytes) -> str | None:
    """Return a function's quote-removed name, or $-prefixed text when dynamic."""
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return None
    value, dynamic = _static_value([name_node], raw)
    return f"${_text(name_node, raw)}" if dynamic >= 0 else value


def _note_prefix_span(node: Any, raw: bytes, scan: _Scan) -> None:
    """Note a walked function and mark its body if the script calls it under a prefix assignment."""
    name = _function_key(node, raw)
    body = node.child_by_field_name("body")
    if name is None or body is None:
        return
    scan.walked_functions.add(name)
    held = scan.prefixed_functions.get(name)
    if held:
        scan.prefix_spans.append((raw, body.start_byte, body.end_byte, held))


def _span_names(node: Any, raw: bytes, scan: _Scan) -> frozenset[str]:
    """Return the trusted names a prefix assignment holds in every marked body around a node.

    Only a trusted literal or an own-directory name counts: the guard substitutes no other
    name, so the prefix value of any other name changes no path the guard judges. Every held
    name stays in span_held, so an inline body queued from this node (eval, trap, bash -c,
    a sourced file, a here-document or a pipe into a shell) is marked with the same names.
    """
    held: frozenset[str] = frozenset()
    for span_raw, start, end, names in scan.prefix_spans:
        if span_raw is raw and start <= node.start_byte < end:
            held |= names
    scan.span_held = held
    if not held:
        return held
    return held & (set(scan.literals) | set(scan.script_dirs))


def _live_literals(scan: _Scan) -> dict[str, str]:
    """Return the literals to substitute, leaving out the names a prefix holds right here."""
    if not scan.span_names:
        return scan.literals
    return {k: v for k, v in scan.literals.items() if k not in scan.span_names}


def _visit_semantics(
    node: Any, raw: bytes, data_only: bool, scan: _Scan
) -> tuple[str | None, list[tuple[Any, bool]]]:
    """Dispatch execution checks while keeping data traversal limited to substitutions."""
    kind = node.type
    if data_only:
        return None, _child_modes(node, True)
    if kind in ("heredoc_redirect", "herestring_redirect"):
        return _heredoc(node, raw, scan)
    hit = None
    if kind == "command":
        hit = _inspect_command(node, raw, scan)
    elif kind == "pipeline":
        hit = _pipeline_hit(node, raw, scan)
    elif kind in ASSIGNMENTS:
        scan.command_rebound |= _prefix_rebinds([_text(a, raw) for a in _assignment_nodes(node)])
        _settle_script_dir(node, raw, scan)
        hit = _assignment_hit(node, raw)
    children = _child_modes(node, kind in DATA | ASSIGNMENTS | {"test_command", "file_redirect"})
    return hit, children


def _script_dir_value(value: str) -> tuple[bool, str | None]:
    """Match the own-directory idiom, alone or as the default of an override unset in the env."""
    if len(value) > 1 and value[0] == value[-1] == '"':
        value = value[1:-1]
    override = None
    default = SCRIPT_DIR_DEFAULT.fullmatch(value)
    if default is not None:
        override, value = default.groups()
        if override in os.environ:
            return False, None
    return SCRIPT_DIR_VALUE.fullmatch(value) is not None, override


def _script_dir_names(assignments: list[str]) -> dict[str, str | None]:
    """Map non-protected names assigned once to the own-directory idiom onto their overrides."""
    names = [match.group() if (match := ASSIGNED_NAME.match(a)) else "" for a in assignments]
    counts = Counter(names)
    result: dict[str, str | None] = {}
    for i in range(len(assignments)):
        name = names[i]
        if (
            not name
            or _protected_name(name)
            or counts[name] != 1
            or assignments[i][len(name) : len(name) + 1] != "="
        ):
            continue
        matched, override = _script_dir_value(assignments[i][len(name) + 1 :])
        if matched:
            result[name] = override
    return result


def _top_level(node: Any) -> bool:
    """Accept an assignment only as a statement of the file, never in a function, branch or loop."""
    parent = node.parent
    for _ in range(3):
        if parent is None or parent.type not in ("variable_assignments", "declaration_command"):
            break
        parent = parent.parent
    return parent is not None and parent.type == "program"


def _forget_script_literals(scan: _Scan) -> None:
    """Drop own-directory literals at an unread source, which may reassign their names."""
    for name in scan.script_literals:
        scan.literals.pop(name, None)
    scan.script_literals.clear()


def _script_dir_refusal(scan: _Scan) -> str | None:
    """Refuse own-directory resolution for newline paths or rebound idiom commands."""
    if "\n" in str(scan.context.script_dir):
        return NEWLINE_SCRIPT_DIR
    if scan.context.script_path is not None and "\n" in scan.context.script_path.name:
        return NEWLINE_SCRIPT_NAME
    if scan.rebound:
        return REBOUND_SCRIPT_DIR
    return None


def _assignment_nodes(node: Any) -> list[Any]:
    """Return a variable assignment or the assignment children of a compound statement."""
    if node.type == "variable_assignment":
        return [node]
    return [child for child in node.named_children if child.type == "variable_assignment"]


def _store_script_dir(assignment: Any, name: str, override: str | None, scan: _Scan) -> None:
    """Store a resolved own-directory literal or mark the assignment uncertain or nested."""
    overridden = override is not None and override in scan.assigned
    uncertain = scan.moved or scan.source_unknown or overridden
    top = _top_level(assignment)
    if uncertain or (top and assignment.parent.type == "declaration_command"):
        scan.literals.pop(name, None)
        scan.source_unknown = scan.dir_changed = True
    elif top:
        scan.literals[name] = str(scan.context.script_dir)
        scan.script_literals.add(name)
    else:
        scan.unresolved_dirs.add(name)


def _settle_script_dir(node: Any, raw: bytes, scan: _Scan) -> None:
    """Resolve an own-directory name from its top-level assignment on, never before it.

    A top-level statement outside the own-directory allow-list (which includes safe export
    declarations, read-only default expansions in plain assignments, and functions whose body
    names no PATH or Bash variable before =) before the assignment, or BASH_ENV, ENV or an
    exported BASH_FUNC_ function in the environment, marks idiom commands rebound and refuses
    top-level resolution. A directory path or resolved script file name holding a newline
    refuses every script path referencing the name. After a directory change or unread source,
    with its override assigned, or through a top-level declaration, later relative paths
    refuse; nested in a function, branch or loop, every script path referencing the name
    refuses.
    """
    if not scan.script_dirs:
        return
    for assignment in _assignment_nodes(node):
        match = ASSIGNED_NAME.match(_text(assignment, raw))
        name = match.group() if match is not None else ""
        if name not in scan.script_dirs:
            continue
        override = scan.script_dirs.pop(name)
        reason = _script_dir_refusal(scan)
        if reason is None and scan.prefix is not None and _top_level(assignment):
            reason = scan.prefix
        if reason is None and name in scan.late_dirs:
            reason = WRITTEN_SCRIPT_DIR
        if reason is not None:
            scan.literals.pop(name, None)
            scan.refused_dirs[name] = reason
            continue
        _store_script_dir(assignment, name, override, scan)


@dataclass
class _Names:
    """Statement assignments, assigned names, plain-assignment name starts and skipped spans."""

    assignments: list[str] = field(default_factory=list)
    assigned: set[str] = field(default_factory=set)
    starts: set[int] = field(default_factory=set)
    skipped: list[tuple[int, int]] = field(default_factory=list)
    prefixed: dict[str, set[str]] = field(default_factory=dict)
    bodies: dict[str, list[str]] = field(default_factory=dict)
    spelled: list[str] = field(default_factory=list)
    dynamic_write: bool = False
    prefix_writes: set[str] = field(default_factory=set)
    calls: dict[str, set[str]] = field(default_factory=dict)
    dynamic_callers: set[str] = field(default_factory=set)
    inline: list[str] = field(default_factory=list)
    raw_bindings: list[tuple[str, str, list[Any], bytes]] = field(default_factory=list)
    static_bindings: list[tuple[str, str, str]] = field(default_factory=list)
    array_bindings: list[tuple[str, list[Any], bytes]] = field(default_factory=list)
    writer_nodes: list[tuple[Any, bytes, tuple[str, ...]]] = field(default_factory=list)
    unknown_names: set[str] = field(default_factory=set)
    var_scope: _VarScope = field(default_factory=_VarScope)
    parsed: int = 0


def _merge_scopes(left: _VarScope, right: _VarScope) -> _VarScope:
    """Combine variable scopes from a caller and an inlined sourced script."""
    values = dict(left.values)
    for key, items in right.values.items():
        values[key] = tuple(dict.fromkeys((*values.get(key, ()), *items)))[:16]
    arrays = dict(left.arrays)
    for key, items in right.arrays.items():
        arrays[key] = tuple(dict.fromkeys((*arrays.get(key, ()), *items)))[:16]
    return _VarScope(
        values,
        arrays,
        left.assigned | right.assigned,
        left.locale_vars | right.locale_vars,
    )


def _plain_start(node: Any) -> int | None:
    """Return the name start of a NAME=value assignment; None for +=, an index or other names."""
    name_node = node.child_by_field_name("name")
    if name_node is None or name_node.type != "variable_name":
        return None
    if len(node.children) < 2 or node.children[1].type != "=":
        return None
    return name_node.start_byte


def _note_assignment(node: Any, raw: bytes, names: _Names) -> None:
    """Record a statement assignment, its variable name and, for a plain =, the name's start."""
    value = node.child_by_field_name("value")
    if value is not None and value.type == "array":
        return
    names.assignments.append(_text(node, raw))
    name_node = node.child_by_field_name("name")
    if name_node is None or name_node.type != "variable_name":
        return
    names.assigned.add(_text(name_node, raw))
    start = _plain_start(node)
    if start is not None:
        names.starts.add(start)


def _prefix_head(node: Any, raw: bytes) -> str | None:
    """Return the unquoted plain first word of a prefixed command, if there is one."""
    command_name = node.parent.child_by_field_name("name")
    parts = command_name.named_children if command_name is not None else []
    if len(parts) != 1 or parts[0].type != "word" or parts[0].named_children:
        return None
    word = _text(parts[0], raw)
    return word if PLAIN_WORD.fullmatch(word) is not None else None


def _dynamic_shell_flag(value: str, dynamic: int, has_more: bool) -> bool:
    """Tell whether an option-position shell argument can supply a -c flag at run time."""
    if dynamic < 0:
        return False
    return value.startswith("-") or (dynamic == 0 and has_more)


def _runs_shell_text(target: str, args: list[tuple[str, int]]) -> bool:
    """Tell whether a prefixed target command can execute inline or sourced shell text."""
    if target in SHELL_TEXT_BUILTINS:
        return True
    if os.path.basename(target) not in SHELLS:
        return False
    for pos, (value, dynamic) in enumerate(args):
        if dynamic < 0 and value in ("--", "-"):
            break
        if _dynamic_shell_flag(value, dynamic, pos + 1 < len(args)):
            return True
        if not value.startswith(("-", "+")):
            break
    operand, inline = _shell_argument([target, *(value for value, _ in args)])
    return inline or operand in (None, "-")


def _classify_wrapper_opt(wrapper: str, opt: str, dynamic: int) -> str:
    """Classify one option word after a command or builtin wrapper."""
    if opt == "--" and dynamic < 0:
        return "dashdash"
    if dynamic >= 0:
        return "dynamic"
    if not opt.startswith("-") or opt == "-":
        return "operand"
    if wrapper == "builtin" or any(ch not in "pVv" for ch in opt[1:]):
        return "invalid"
    return "describe" if ("v" in opt[1:] or "V" in opt[1:]) else "keep"


def _step_prefix_wrapper(wrapper: str, values: list[tuple[str, int]], idx: int) -> int | None:
    """Advance past one command or builtin wrapper's options; None marks a run-time option."""
    describe = False
    end = len(values)
    for pos in range(idx, end):
        kind = _classify_wrapper_opt(wrapper, values[pos][0], values[pos][1])
        if kind == "dynamic":
            return None
        if kind == "invalid":
            return end
        if kind in ("dashdash", "operand"):
            if describe:
                return end
            return pos + int(kind == "dashdash")
        describe |= kind == "describe"
    return end


def _unwrap_prefix_chain(values: list[tuple[str, int]]) -> tuple[int, bool] | None:
    """Unwrap a leading chain of command and builtin wrappers and track command presence."""
    idx = 0
    saw_command = False
    for _ in range(len(values)):
        if idx >= len(values) or values[idx][1] >= 0:
            break
        wrapper = values[idx][0]
        if wrapper not in ("command", "builtin"):
            break
        saw_command |= wrapper == "command"
        stepped = _step_prefix_wrapper(wrapper, values, idx + 1)
        if stepped is None:
            return None
        idx = stepped
    return idx, saw_command


def _prefix_unwrapped_shell(values: list[tuple[str, int]], start: int) -> bool:
    """Tell whether wrappers starting at start unwrap to an inline or stdin shell."""
    idx = _unwrap_command([value for value, _ in values], start)
    if idx <= start or idx >= len(values):
        return False
    target, dynamic = values[idx]
    if dynamic >= 0 or os.path.basename(target) not in SHELLS:
        return False
    return _runs_shell_text(target, values[idx + 1 :])


def _prefix_command(node: Any, raw: bytes) -> str | None:
    """Return the safe command name for a prefix assignment, or None when it may write."""
    head = _prefix_head(node, raw)
    if head is None:
        return None
    values = _command_values(node.parent, raw)
    if head not in ("command", "builtin"):
        bad = (
            head in SPECIAL_BUILTINS
            or _runs_shell_text(head, values[1:])
            or _prefix_unwrapped_shell(values, 0)
        )
        return None if bad else head
    chain = _unwrap_prefix_chain(values)
    if chain is None:
        return None
    idx, saw_command = chain
    if idx >= len(values):
        return ""
    target, dynamic = values[idx]
    special = not saw_command and target in SPECIAL_BUILTINS
    if dynamic >= 0 or special or _runs_shell_text(target, values[idx + 1 :]):
        return None
    return None if _prefix_unwrapped_shell(values, idx) else ""


def _note_prefix(node: Any, raw: bytes, names: _Names) -> None:
    """Record the name start and callee of a prefix assignment that cannot write or eval."""
    command = _prefix_command(node, raw)
    if command is None or command in SPECIAL_BUILTINS:
        return
    start = _plain_start(node)
    if start is not None:
        names.starts.add(start)
        if command:
            name = _text(node.child_by_field_name("name"), raw)
            names.prefixed.setdefault(command, set()).add(name)


def _note_function(node: Any, raw: bytes, names: _Names) -> None:
    """Record a function's name and body text for the prefix call closure."""
    name = _function_key(node, raw)
    body = node.child_by_field_name("body")
    if name is not None and body is not None:
        names.bodies.setdefault(name, []).append(_text(body, raw))


def _note_heredoc(node: Any, raw: bytes, names: _Names) -> None:
    """Skip a quoted here-document body, which Bash passes on without any expansion."""
    _, quoted, body, _ = _heredoc_parts(node, raw)
    if quoted and body is not None:
        names.skipped.append((body.start_byte, body.end_byte))


def _kept_escape(match: re.Match[str]) -> str:
    """Drop the backslash of an escape, and a backslash-newline line continuation whole."""
    char = match.group(1)
    return "" if char == "\n" else char


def _locale_escape(match: re.Match[str]) -> bool:
    """Tell a Unicode escape whose decoded text depends on the run-time locale.

    Bash 5.3.20 writes a four- or eight-digit Unicode escape of U+0080 to U+7FFFFFFF as UTF-8
    bytes (generalized past U+10FFFF and for surrogates) in a UTF-8 locale and keeps the
    escape text in the C and POSIX locales (measured).
    """
    digits = match.group(3) or match.group(4)
    return digits is not None and 0x80 <= int(digits, 16) <= 0x7FFFFFFF


def _ansi_c_char(match: re.Match[str]) -> str:
    """Decode one ANSI-C escape as bash 5.3.20 does (measured).

    Octal keeps its low eight bits, and a Unicode escape past U+7FFFFFFF writes nothing. A
    locale-dependent escape keeps its escape text, as in the C locale, so the decoded word
    stays encodable; _leaf_value counts such a string as run-time text.
    """
    octal, hex2, hex4, hex8, control, other = match.groups()
    if octal is not None:
        return chr(int(octal, 8) & 0xFF)
    if hex2 is not None:
        return chr(int(hex2, 16))
    if _locale_escape(match):
        return match.group(0)
    digits = hex4 or hex8
    if digits is not None:
        return chr(int(digits, 16)) if int(digits, 16) < 0x80 else ""
    if control is not None:
        return chr(ord(control) & 0x1F)
    return ANSI_C_SIMPLE.get(other, "\\" + other)


def _leaf_value(node: Any, raw: bytes) -> tuple[str, bool]:
    """Return a static word part's text after quote removal and whether it may still expand."""
    text = _text(node, raw)
    if node.type == "raw_string":
        return text[1:-1], False
    if node.type == "ansi_c_string":
        # Bash ends an ANSI-C string at a decoded NUL byte; a locale-dependent escape makes the
        # string run-time text.
        body = text[2:-1]
        locale = any(_locale_escape(match) for match in ANSI_C_ESCAPE.finditer(body))
        return ANSI_C_ESCAPE.sub(_ansi_c_char, body).split("\x00", 1)[0], locale
    if node.type == "string_content":
        return STRING_ESCAPE.sub(_kept_escape, text), False
    expands = node.type == "word" and GLOB_OR_BRACE.search(text) is not None
    return WORD_ESCAPE.sub(_kept_escape, text), expands


def _part_static_leaf(part: Any, raw: bytes) -> tuple[str, bool]:
    """Evaluate one non-group word part to its static text and run-time expansion flag."""
    if _dollar_prefix(part, raw):
        prefix = _text(part, raw)[:-1]
        return WORD_ESCAPE.sub(_kept_escape, prefix), GLOB_OR_BRACE.search(prefix) is not None
    if part.type in STATIC_LEAVES:
        return _leaf_value(part, raw)
    return "", True


def _static_value(nodes: list[Any], raw: bytes) -> tuple[str, int]:
    """Return a word's text after quote removal and the offset of its first run-time part."""
    parts: list[str] = []
    dynamic = -1
    stack = list(reversed(nodes))
    for _ in range(MAX_BODY_NODES):
        if not stack:
            break
        part = stack.pop()
        if part.type in STATIC_GROUPS and not _translated_string(part, raw):
            children = part.children if part.type == "concatenation" else part.named_children
            stack.extend(reversed(children))
            continue
        text, expands = _part_static_leaf(part, raw)
        if expands and dynamic < 0:
            dynamic = sum(len(p) for p in parts)
        parts.append(text)
    return "".join(parts), dynamic


def _word_runs(node: Any, raw: bytes) -> list[list[Any]]:
    """Group a node's adjacent word-part children into the shell words they form."""
    runs: list[list[Any]] = []
    end = -1
    for child in node.children:
        if not child.is_named and not _dollar_prefix(child, raw):
            continue
        if child.type not in WORD_PARTS and not _dollar_prefix(child, raw):
            end = -1
            continue
        if runs and child.start_byte == end:
            runs[-1].append(child)
        else:
            runs.append([child])
        end = child.end_byte
    return runs


def _note_runs(node: Any, raw: bytes, names: _Names, owners: tuple[str, ...]) -> None:
    """Record each word whose text changes in quote removal, and its names for every owner."""
    for run in _word_runs(node, raw):
        value, _ = _static_value(run, raw)
        if value == raw[run[0].start_byte : run[-1].end_byte].decode("utf-8", "surrogateescape"):
            continue
        names.spelled.append(value)
        for owner in owners:
            names.calls.setdefault(owner, set()).update(PLAIN_FUNCTION.findall(value))


def _command_runs(node: Any, raw: bytes) -> list[list[Any]]:
    """Return a writer node's word-part runs, starting with its command or keyword head."""
    if node.type != "command":
        return [[node.children[0]], *_word_runs(node, raw)]
    name = node.child_by_field_name("name")
    if name is None:
        return []
    return [name.named_children, *_word_runs(node, raw)]


def _assignment_operand(node: Any, raw: bytes) -> tuple[str, int]:
    """Keep a declaration's assignment prefix and its value's first expansion offset."""
    value = node.child_by_field_name("value")
    if value is None:
        return _text(node, raw), -1
    prefix = raw[node.start_byte : value.start_byte].decode("utf-8", "surrogateescape")
    text, dynamic = _static_value([value], raw)
    return prefix + text, len(prefix) + dynamic if dynamic >= 0 else -1


def _command_values(node: Any, raw: bytes) -> list[tuple[str, int]]:
    """Return a command's quote-removed words, its name first, with their run-time offsets."""
    if node.type != "command":
        operands = [(run[0].start_byte, _static_value(run, raw)) for run in _word_runs(node, raw)]
        operands.extend(
            (child.start_byte, _assignment_operand(child, raw))
            for child in node.named_children
            if child.type == "variable_assignment"
        )
        return [(_text(node.children[0], raw), -1), *(value for _, value in sorted(operands))]
    return [_static_value(run, raw) for run in _command_runs(node, raw)]


def _lookup_var(name: str, scope: _VarScope) -> tuple[str, ...]:
    """Return resolved values for a variable, environment entry or unassigned empty name."""
    if name in scope.values:
        return scope.values[name]
    if name in os.environ:
        return (os.environ[name][:64],)
    if _unknown_param(name, scope):
        return ()
    base = name.split("[", 1)[0]
    return () if base in scope.assigned else ("",)


def _unknown_param(name: str, scope: _VarScope) -> bool:
    """Tell whether name is a positional or special parameter the guard has not established."""
    return UNKNOWN_PARAM.fullmatch(name) is not None and name not in scope.values


def _mark_unknown(loc_vars: set[str], key: str) -> None:
    """Record that a variable may also hold text known only at run time."""
    loc_vars.update((key, key.split("[", 1)[0]))


def _apply_case(value: str, op: str) -> str:
    """Apply Bash ^, ^^, , or ,, case-modification operators to a string."""
    if op == "^^":
        return value.upper()
    if op == ",,":
        return value.lower()
    if op == "^":
        return value[:1].upper() + value[1:]
    return value[:1].lower() + value[1:]


def _plain_tail_values(tail: str) -> tuple[str, ...]:
    """Return a single literal fallback string when it carries no shell expansion."""
    if not tail:
        return ("",)
    return (tail[:64],) if "$" not in tail and "`" not in tail else ()


def _fallback_values(
    part: Any, match: re.Match[str], raw: bytes, scope: _VarScope
) -> tuple[tuple[str, ...] | None, list[Any], bool]:
    """Resolve a ${name:-word}, ${name:=word} or ${name:+word} expansion."""
    var, op, tail = match.groups()
    loc = var in scope.locale_vars or _unknown_param(var, scope)
    rest = part.named_children[1:]
    has_var = var in scope.values or var in os.environ
    known = _lookup_var(var, scope) if has_var else ()
    if op.endswith("+") or not known:
        if tail and rest:
            return None, rest, loc
        return _plain_tail_values(tail), [], loc
    if rest:
        extra, dyn = _static_value(rest, raw)
        extra_vals = (extra[:64],) if dyn < 0 else ()
    else:
        extra_vals = _plain_tail_values(tail)
    merged = tuple(dict.fromkeys((*known, *extra_vals)))
    return merged[:16], [], loc or len(merged) > 16


def _brace_values(
    part: Any, raw: bytes, scope: _VarScope
) -> tuple[tuple[str, ...] | None, list[Any], bool]:
    """Resolve a braced parameter expansion or return fallback child nodes to push."""
    expr = _text(part, raw)
    all_arr = ARRAY_ALL_VAR.fullmatch(expr)
    if all_arr is not None:
        name = all_arr.group(1)
        return scope.arrays.get(name) or _lookup_var(name, scope), [], name in scope.locale_vars
    plain = PLAIN_BRACE_VAR.fullmatch(expr)
    if plain is not None:
        key = plain.group(1)
        loc = key in scope.locale_vars or key.split("[", 1)[0] in scope.locale_vars
        if key.endswith("[0]") and key not in scope.values:
            key = key[:-3]
        return _lookup_var(key, scope), [], loc
    cased = CASE_BRACE_VAR.fullmatch(expr)
    if cased is not None:
        name, op = cased.groups()
        vals = tuple(_apply_case(v, op) for v in _lookup_var(name, scope))
        return vals, [], name in scope.locale_vars
    fallback = FALLBACK_BRACE_VAR.fullmatch(expr)
    if fallback is not None:
        return _fallback_values(part, fallback, raw, scope)
    return (), [], False


def _append_choices(acc: tuple[str, ...], choices: tuple[str, ...]) -> tuple[tuple[str, ...], bool]:
    """Concatenate prefixes and choices within a 16-word cap and report a dropped candidate."""
    if not acc or not choices:
        return (), False
    joined = tuple(dict.fromkeys((a + c)[:64] for a in acc for c in choices))
    return joined[:16], len(joined) > 16


def _part_choices(part: Any, raw: bytes, scope: _VarScope) -> tuple[tuple[str, ...], bool]:
    """Evaluate one leaf, brace or simple expansion part and report locale dependence."""
    if part.type in STATIC_LEAVES:
        text, expands = _leaf_value(part, raw)
        if part.type == "ansi_c_string" and expands:
            return (), True
        return (() if expands else (text,)), False
    if part.type == "brace_expression":
        match = BRACE_CHOICES.fullmatch(_text(part, raw))
        return (tuple(match.group(1).split(","))[:16] if match else ()), False
    if part.type == "simple_expansion":
        children = part.named_children
        if children and children[0].type == "variable_name":
            var = _text(children[0], raw)
            return _lookup_var(var, scope), var in scope.locale_vars
    return (), False


def _step_expansion_part(
    part: Any, quoted: bool, raw: bytes, scope: _VarScope
) -> tuple[tuple[str, ...] | None, list[Any], bool, bool]:
    """Evaluate one braced or simple word part to choices, pushed nodes, locale and split."""
    if part.type in ("expansion", "ERROR"):
        split = not quoted or ARRAY_ALL_VAR.fullmatch(_text(part, raw)) is not None
        vals, rest, loc = _brace_values(part, raw, scope)
        return vals, rest, loc, split
    split = part.type == "simple_expansion" and not quoted
    vals, loc = _part_choices(part, raw, scope)
    return vals, [], loc, split


def _word_candidates(
    nodes: list[Any], raw: bytes, scope: _VarScope
) -> tuple[tuple[str, ...], bool, bool]:
    """Return bounded candidate strings, locale flag and word-splitting flag for a word."""
    acc: tuple[str, ...] = ("",)
    locale = split = False
    stack: list[tuple[Any, bool]] = [(node, False) for node in reversed(nodes)]
    for _ in range(MAX_BODY_NODES):
        if not stack:
            break
        part, quoted = stack.pop()
        if _translated_string(part, raw) or _dollar_prefix(part, raw):
            locale = True
            acc = ()
        elif part.type in STATIC_GROUPS:
            inner_q = quoted or part.type == "string"
            stack.extend((child, inner_q) for child in reversed(part.named_children))
        else:
            vals, rest, loc, part_split = _step_expansion_part(part, quoted, raw, scope)
            locale |= loc
            split |= part_split
            if vals is None:
                stack.extend((child, quoted) for child in reversed(rest))
            else:
                acc, dropped = _append_choices(acc, vals)
                locale |= dropped
    return acc, locale, split


def _binding_target(name_node: Any, raw: bytes) -> str | None:
    """Extract a plain variable or literal numeric subscript target name."""
    if name_node is None:
        return None
    text = _text(name_node, raw)
    if name_node.type == "variable_name" and ASSIGNED_NAME.fullmatch(text) is not None:
        return text
    if name_node.type == "subscript":
        match = INDEXED_TARGET.fullmatch(text)
        if match is not None:
            return f"{match.group(1)}[{int(match.group(2))}]"
    return None


def _record_array_binding(target: str, items: list[Any], raw: bytes, names: _Names) -> None:
    """Bound array candidate storage and mark the array unknown whenever elements are dropped."""
    if items:
        names.raw_bindings.append((target, "=", [items[0]], raw))
        names.array_bindings.append((target, items[:16], raw))
    for idx, item in enumerate(items[:16]):
        names.raw_bindings.append((f"{target}[{idx}]", "=", [item], raw))
    if len(items) > 16:
        names.unknown_names.add(target)


def _record_binding(node: Any, raw: bytes, names: _Names) -> None:
    """Record a variable or array assignment's target, operator and RHS syntax nodes."""
    target = _binding_target(node.child_by_field_name("name"), raw)
    if target is None or len(node.children) < 2 or node.children[1].type not in ("=", "+="):
        return
    op = node.children[1].type
    value = node.child_by_field_name("value")
    if value is None:
        names.static_bindings.append((target, op, ""))
    elif value.type == "array" and "[" not in target:
        _record_array_binding(target, value.named_children, raw, names)
    elif value.type != "array":
        names.raw_bindings.append((target, op, [value], raw))
        if target.endswith("[0]"):
            names.raw_bindings.append((target[:-3], op, [value], raw))


def _record_extra_bindings(node: Any, raw: bytes, names: _Names) -> None:
    """Record variable bindings from quoted declarations and for-loop word lists."""
    if node.type == "declaration_command":
        for run in _word_runs(node, raw):
            val, dyn = _static_value(run, raw)
            if dyn < 0 and "=" in val:
                lhs, _, rhs = val.partition("=")
                op = "+=" if lhs.endswith("+") else "="
                var = lhs[:-1] if op == "+=" else lhs
                if ASSIGNED_NAME.fullmatch(var):
                    names.static_bindings.append((var, op, rhs))
    elif node.type == "for_statement":
        var_node = node.child_by_field_name("variable")
        if var_node is not None:
            var = _text(var_node, raw)
            for item in node.children_by_field_name("value"):
                names.raw_bindings.append((var, "=", [item], raw))


def _record_node_bindings(node: Any, raw: bytes, names: _Names) -> None:
    """Collect variable, array, declaration and for-loop bindings from one syntax node."""
    if node.type == "variable_assignment":
        _record_binding(node, raw, names)
    elif node.type in ("declaration_command", "for_statement"):
        _record_extra_bindings(node, raw, names)


def _add_resolved(
    store: dict[str, list[str]], key: str, op: str, items: Iterable[str], live: _VarScope
) -> bool:
    """Merge candidate strings into a 16-item bucket and report whether the cap dropped one."""
    prefixes = (live.values.get(key) or ("",)) if op == "+=" else ("",)
    bucket = store.setdefault(key, [])
    dropped = False
    for prefix in prefixes:
        for item in items:
            merged = (prefix + item)[:64]
            if merged in bucket:
                continue
            if len(bucket) < 16:
                bucket.append(merged)
            else:
                dropped = True
    return dropped


def _array_element_strings(
    items: list[Any], raw: bytes, scope: _VarScope
) -> tuple[tuple[str, ...], bool]:
    """Join leading resolved elements of one array literal and report locale dependence."""
    parts: tuple[str, ...] = ("",)
    locale = False
    for item in items:
        vals, loc, _ = _word_candidates([item], raw, scope)
        locale |= loc
        if not vals:
            break
        sep = " " if parts != ("",) else ""
        joined = tuple(dict.fromkeys(f"{p}{sep}{v}" for p in parts for v in vals))
        parts = tuple(value[:64] for value in joined[:16])
        locale |= len(joined) > 16 or any(len(value) > 64 for value in joined)
    return parts, locale


def _resolve_arrays(
    names: _Names, scope: _VarScope, loc_vars: set[str]
) -> dict[str, tuple[str, ...]]:
    """Join leading resolved elements of array assignments for ${arr[@]} expansion."""
    arrays: dict[str, list[str]] = {k: list(v) for k, v in scope.arrays.items()}
    for target, items, raw in names.array_bindings:
        parts, loc = _array_element_strings(items, raw, scope)
        if loc:
            loc_vars.add(target)
        bucket = arrays.setdefault(target, [])
        for part in parts:
            if not part or part in bucket:
                continue
            if len(bucket) < 16:
                bucket.append(part)
            else:
                loc_vars.add(target)
    return {k: tuple(v) for k, v in arrays.items() if v}


def _binding_candidates(
    nodes: list[Any], raw: bytes, live: _VarScope, names: _Names
) -> tuple[tuple[str, ...], bool]:
    """Keep unresolved bindings unknown unless their own-directory form establishes them."""
    vals, loc, _ = _word_candidates(nodes, raw, live)
    if vals or loc:
        return vals, loc
    if len(nodes) == 1 and _script_dir_value(_text(nodes[0], raw))[0]:
        return vals, False
    # Directory placeholders establish the form only; never store them as real candidates.
    dirs = {name: ("/",) for name in _script_dir_names(names.assignments)}
    known = _VarScope({**live.values, **dirs}, live.arrays, live.assigned, live.locale_vars)
    established, unknown, _ = _word_candidates(nodes, raw, known)
    return vals, unknown or not established


def _step_binding_pass(
    names: _Names, base: _VarScope, live: _VarScope, loc_vars: set[str]
) -> dict[str, tuple[str, ...]]:
    """Run one forward sweep over static and syntax-node variable bindings."""
    store: dict[str, list[str]] = {k: list(v) for k, v in base.values.items()}
    for key, op, rhs in names.static_bindings:
        if _add_resolved(store, key, op, (rhs,), live):
            _mark_unknown(loc_vars, key)
        live.values[key] = tuple(store[key])
    for key, op, nodes, raw in names.raw_bindings:
        vals, loc = _binding_candidates(nodes, raw, live, names)
        if loc:
            _mark_unknown(loc_vars, key)
        if vals:
            if _add_resolved(store, key, op, vals, live):
                _mark_unknown(loc_vars, key)
            live.values[key] = tuple(store[key])
    return {k: tuple(v) for k, v in store.items() if v}


def _resolve_bindings(names: _Names, seed: _VarScope | None = None) -> _VarScope:
    """Compute bounded fixed-point candidate values for variables assigned in a script."""
    base = seed if seed is not None else _VarScope()
    assigned = frozenset(
        set(base.assigned)
        | names.assigned
        | {k.split("[", 1)[0] for k, _, _ in names.static_bindings}
        | {k.split("[", 1)[0] for k, _, _, _ in names.raw_bindings}
    )
    current: dict[str, tuple[str, ...]] = dict(base.values)
    arrays: dict[str, tuple[str, ...]] = dict(base.arrays)
    loc_vars: set[str] = set(base.locale_vars) | names.unknown_names
    for _ in range(6):
        prev_loc = frozenset(loc_vars)
        live = _VarScope(dict(current), arrays, assigned, prev_loc)
        nxt = _step_binding_pass(names, base, live, loc_vars)
        nxt_arrays = _resolve_arrays(
            names, _VarScope(nxt, arrays, assigned, frozenset(loc_vars)), loc_vars
        )
        if nxt == current and nxt_arrays == arrays and frozenset(loc_vars) == prev_loc:
            break
        current, arrays = nxt, nxt_arrays
    return _VarScope(current, arrays, assigned, frozenset(loc_vars))


def _option_letters(
    spec: _Writer, word: tuple[str, int], following: tuple[str, int] | None
) -> tuple[list[tuple[str, int]], bool] | None:
    """Read one option word: the naming arguments and whether it took the next word."""
    value, dynamic = word
    letters = value[1:dynamic] if dynamic > 0 else value[1:]
    for pos, letter in enumerate(letters, 2):
        if letter in spec.functions:
            return None
        if letter not in spec.with_argument:
            continue
        attached = pos < len(value) or dynamic >= 0
        argument = (value[pos:], dynamic) if attached else following
        named = [argument] if letter in spec.naming and argument is not None else []
        return named, not attached
    return ([(value, dynamic)] if dynamic > 0 and spec.naming else []), False


def _writer_operands(
    spec: _Writer, args: list[tuple[str, int]]
) -> tuple[list[tuple[str, int]], list[tuple[str, int]]] | None:
    """Split a writer builtin's words into operands and naming option arguments."""
    named: list[tuple[str, int]] = []
    idx = 0
    for _ in range(len(args) + 1):
        if idx >= len(args) or args[idx][0] == "--":
            return args[idx + 1 :], named
        value, dynamic = args[idx]
        if dynamic == 0 and spec.naming and idx + 1 < len(args):
            return args[idx:], [*named, args[idx]]
        if value[:1] not in ("-", "+") or dynamic == 0 or (len(value) < 2 and dynamic < 0):
            return args[idx:], named
        following = args[idx + 1] if idx + 1 < len(args) else None
        read = _option_letters(spec, args[idx], following)
        if read is None:
            return None
        named += read[0]
        idx += 1 + int(read[1])
    return [], named


def _dynamic_name(value: str, dynamic: int) -> bool:
    """Tell whether a NAME[=value] operand has a run-time part before its first =."""
    equals = value.find("=")
    return dynamic >= 0 and (equals < 0 or dynamic <= equals)


def _nameref_enabled(args: list[tuple[str, int]]) -> bool:
    """Track literal -n and +n declaration options before the first operand."""
    enabled = False
    for value, dynamic in args:
        if dynamic >= 0 or value == "--" or value[:1] not in ("-", "+"):
            break
        if "n" in value[1:]:
            enabled = value.startswith("-")
    return enabled


def _names_dynamic(
    spec: _Writer, args: list[tuple[str, int]], operands: list[tuple[str, int]]
) -> bool:
    """Treat an unknown declaration name or enabled nameref's referent as a possible writer."""
    referents = spec.referents and _nameref_enabled(args)
    return any(
        _dynamic_name(value, dynamic) or (referents and dynamic >= 0) for value, dynamic in operands
    )


def _writer_dynamic(spec: _Writer, args: list[tuple[str, int]]) -> bool:
    """Tell whether a writer builtin may name a variable through a run-time word."""
    split = _writer_operands(spec, args)
    if split is None:
        return False
    operands, named = split
    if spec.operands == "arithmetic":
        return any(
            dynamic >= 0 and _arithmetic_writes(value[:dynamic] + "${_}" + value[dynamic:])
            for value, dynamic in operands
        )
    if spec.operands == "names":
        return _names_dynamic(spec, args, operands)
    picks = {"all": operands, "any": operands, "last": operands[-1:], "second": operands[:2]}
    return any(dynamic >= 0 for _, dynamic in named + picks.get(spec.operands, []))


def _option_named(args: list[tuple[str, int]]) -> bool:
    """Tell whether a literal naming option of any writer builtin takes a run-time argument."""
    for idx, word in enumerate(args):
        if word[1] == 0 or word[0][:1] != "-" or word[0] == "--":
            continue
        following = args[idx + 1] if idx + 1 < len(args) else None
        for spec in WRITERS.values():
            read = _option_letters(spec, word, following) if spec.naming else None
            if read is not None and any(dynamic >= 0 for _, dynamic in read[0]):
                return True
    return False


def _step_wrapper_word(
    wrapper: str, prefix: list[str], pos: int
) -> tuple[list[str], int, str] | None:
    """Step one resolved word under an active command, builtin or time wrapper."""
    head, tail = prefix[0], prefix[1:]
    if head == "--":
        return tail, pos, ""
    if head.startswith("-") and head != "-":
        if wrapper in ("command", "time") and set(head[1:]) <= {"p"}:
            return tail, pos, wrapper
        return None
    return prefix, pos, ""


def _expand_run_state(
    pos: int, runs: list[list[Any]], values: list[tuple[str, int]], raw: bytes, scope: _VarScope
) -> tuple[list[tuple[list[str], int]], bool]:
    """Expand one command-position run into queued word lists and report locale writes."""
    if pos >= len(runs):
        return [], False
    text, dyn = values[pos]
    if dyn < 0:
        return [([text], pos + 1)], False
    cands, locale, split = _word_candidates(runs[pos], raw, scope)
    states: list[tuple[list[str], int]] = []
    for cand in cands:
        parts = cand.split() if split else [cand]
        if parts and parts[0]:
            states.append((parts, pos + 1))
    can_write = locale and "/" not in text and (split or pos + 1 < len(values))
    return states, can_write


def _step_invocation(
    state: tuple[list[str], int, str],
    queue: list[tuple[list[str], int, str]],
    calls: list[tuple[str, list[str], int]],
) -> None:
    """Advance one non-empty invocation prefix through wrappers or into resolved calls."""
    prefix, pos, wrapper = state
    if wrapper:
        stepped = _step_wrapper_word(wrapper, prefix, pos)
        if stepped is not None:
            queue.append(stepped)
    elif prefix[0] in SHELL_WRAPPERS:
        queue.append((prefix[1:], pos, prefix[0]))
    else:
        calls.append((prefix[0], prefix[1:], pos))


def _resolved_invocations(
    node: Any, raw: bytes, scope: _VarScope
) -> tuple[int, list[tuple[str, list[str], int]], bool]:
    """Resolve unwrapped command words to (head, extra_args, next_pos) and locale status."""
    runs = _command_runs(node, raw)
    values = [_static_value(run, raw) for run in runs]
    base_idx = _unwrap_command([value for value, _ in values], 0)
    backed = (
        0 < base_idx < len(values)
        and values[base_idx - 1][0] in ("command", "builtin")
        and values[base_idx][0].startswith("-")
    )
    queue: list[tuple[list[str], int, str]] = [([], base_idx - int(backed), "")]
    calls: list[tuple[str, list[str], int]] = []
    locale_hit = False
    for _ in range(48):
        if not queue:
            break
        prefix, pos, wrapper = queue.pop()
        if not prefix:
            states, loc = _expand_run_state(pos, runs, values, raw, scope)
            queue.extend((parts, nxt_pos, wrapper) for parts, nxt_pos in states)
            locale_hit |= loc
        else:
            _step_invocation((prefix, pos, wrapper), queue, calls)
    return base_idx, calls, locale_hit


def _invocation_writes(head: str, args: list[tuple[str, int]]) -> bool:
    """Tell whether a resolved command name and remaining arguments may write a variable."""
    if "/" in head:
        return False
    if head in SHELL_TEXT_BUILTINS:
        return bool(args)
    spec = WRITERS.get(head)
    return spec is not None and _writer_dynamic(spec, args)


def _shell_reachable(values: list[tuple[str, int]], idx: int) -> bool:
    """Check that only shell wrappers and options precede the command word at idx."""
    return all(d >= 0 or v in SHELL_WRAPPERS or v.startswith("-") for v, d in values[:idx])


def _may_write_node(node: Any, raw: bytes, scope: _VarScope) -> bool:
    """Tell whether a writer syntax node may write a variable named at run time."""
    values = _command_values(node, raw)
    idx = _unwrap_command([value for value, _ in values], 0)
    if idx >= len(values):
        return False
    command, dynamic = values[idx]
    args = values[idx + 1 :]
    clustered = idx > 0 and values[idx - 1][0] in ("command", "builtin") and command.startswith("-")
    if dynamic < 0 and not clustered:
        spec = WRITERS.get(command)
        return spec is not None and _writer_dynamic(spec, args)
    if not _shell_reachable(values, idx) or "/" in command:
        return False
    if dynamic >= 0 and _option_named(args):
        return True
    _, calls, locale_hit = _resolved_invocations(node, raw, scope)
    return locale_hit or any(
        _invocation_writes(head, [*((w, -1) for w in extra), *values[pos:]])
        for head, extra, pos in calls
    )


def _dynamic_call(values: list[tuple[str, int]]) -> bool:
    """Tell whether a command word built at run time may name a shell function or eval."""
    for value, dynamic in values:
        if dynamic >= 0:
            return True
        if value not in SHELL_WRAPPERS and not value.startswith("-"):
            return False
    return False


def _loop_prefix(node: Any, raw: bytes) -> str | None:
    """Return PREFIX when a writer is the only command of a for loop over "${!PREFIX@}"."""
    group = node.parent
    loop = group.parent if group is not None and group.type == "do_group" else None
    if loop is None or loop.type != "for_statement" or len(group.named_children) != 1:
        return None
    variable = loop.child_by_field_name("variable")
    listed = loop.children_by_field_name("value")
    match = PREFIX_NAMES.fullmatch(_text(listed[0], raw)) if len(listed) == 1 else None
    if variable is None or match is None:
        return None
    name = _text(variable, raw)
    loop_words = {f'"${name}"', f'"${{{name}}}"'}
    for run in _word_runs(node, raw):
        text = raw[_part_start(run[0], raw) : run[-1].end_byte].decode("utf-8", "surrogateescape")
        if _static_value(run, raw)[1] >= 0 and text not in loop_words:
            return None
    return match.group(1)


def _arithmetic_writes(text: str) -> bool:
    """Refuse expanded arithmetic destinations, including write syntax the parser cannot model."""
    if not any(mark in text for mark in ("$", "`")) or not ARITHMETIC_WRITE.search(text):
        return False
    raw = (": $((" + text + "))").encode("utf-8", "surrogateescape")
    root = bash_parser().parse(raw).root_node
    if root.has_error:
        return True
    stack = [root]
    for _ in range(MAX_BODY_NODES):
        if not stack:
            return False
        node = stack.pop()
        operator = node.child_by_field_name("operator")
        if operator is not None and _text(operator, raw) in ARITHMETIC_WRITES:
            target = node.child_by_field_name("left") or node.named_children[0]
            if _static_value([target], raw)[1] >= 0:
                return True
        stack.extend(node.named_children)
    return True


def _arithmetic_node_writes(node: Any, raw: bytes) -> bool:
    """Check arithmetic expansions, commands and array indices through the same writer path."""
    text = _text(node, raw)
    if node.type == "arithmetic_expansion":
        return _arithmetic_writes(text[3:-2])
    if node.type == "compound_statement" and text.startswith("(("):
        return _arithmetic_writes(text[2:-2])
    if node.type == "subscript":
        index = node.child_by_field_name("index")
        return index is not None and _arithmetic_writes(_text(index, raw))
    return False


def _stage_writer(node: Any, raw: bytes, names: _Names, owners: tuple[str, ...]) -> None:
    """Queue static eval or trap texts immediately and stage writer nodes for resolution."""
    values = _command_values(node, raw)
    idx = _unwrap_command([value for value, _ in values], 0)
    if idx >= len(values):
        return
    command, dynamic = values[idx]
    args = values[idx + 1 :]
    if _dynamic_call(values) or (command in EVAL_BUILTINS and any(d >= 0 for _, d in args)):
        names.dynamic_callers.update(owners)
    if dynamic < 0 and command in EVAL_BUILTINS:
        names.inline.append(" ".join(value for value, _ in args))
    names.writer_nodes.append((node, raw, owners))


def _note_offsets(node: Any, raw: bytes, names: _Names) -> None:
    """Record the comments, quoted here-documents, assignments and functions of the top text."""
    if node.type == "comment":
        names.skipped.append((node.start_byte, node.end_byte))
    elif node.type == "heredoc_redirect":
        _note_heredoc(node, raw, names)
    elif node.type == "variable_assignment" and node.parent.type == "command":
        _note_prefix(node, raw, names)
    elif node.type == "variable_assignment":
        _note_assignment(node, raw, names)
    elif node.type == "function_definition":
        _note_function(node, raw, names)


def _note_words(node: Any, raw: bytes, names: _Names, owners: tuple[str, ...]) -> tuple[str, ...]:
    """Record one node's spelled words and writer nodes; return the owners of its children."""
    if node.type not in STATIC_GROUPS:
        _note_runs(node, raw, names, owners)
    if node.type in WRITER_NODES:
        _stage_writer(node, raw, names, owners)
    if _arithmetic_node_writes(node, raw):
        names.dynamic_write = True
    name = _function_key(node, raw) if node.type == "function_definition" else None
    return owners if name is None else (*owners, name)


def _inline_roots(
    names: _Names, owners: tuple[str, ...]
) -> list[tuple[Any, bytes, tuple[str, ...], bool]]:
    """Parse the eval and trap texts one node queued, within the inline body bound."""
    roots: list[tuple[Any, bytes, tuple[str, ...], bool]] = []
    for body in names.inline:
        if names.parsed >= MAX_INLINE_BODIES:
            names.dynamic_write = True
            names.dynamic_callers.update(owners)
            continue
        names.parsed += 1
        data = body.encode("utf-8", "surrogateescape")
        roots.append((bash_parser().parse(data).root_node, data, owners, True))
    names.inline.clear()
    return roots


def _walk_roots(
    roots: list[tuple[Any, bytes, tuple[str, ...], bool]], names: _Names, budget: BodyBudget
) -> None:
    """Walk syntax roots and static eval or trap inline bodies within the shared node bound."""
    stack = list(roots)
    for _ in range(MAX_BODY_NODES):
        if not stack or budget.nodes >= MAX_BODY_NODES:
            break
        node, text, owners, inline = stack.pop()
        budget.nodes += 1
        if not inline:
            _note_offsets(node, text, names)
        elif node.type == "function_definition":
            _note_function(node, text, names)
        _record_node_bindings(node, text, names)
        inner = _note_words(node, text, names, owners)
        stack.extend(_inline_roots(names, owners))
        stack.extend((child, text, inner, inline) for child in reversed(node.named_children))


def _resolved_inline_roots(
    names: _Names, scope: _VarScope
) -> list[tuple[Any, bytes, tuple[str, ...], bool]]:
    """Parse static eval or trap bodies reached through resolved run-time command words."""
    roots: list[tuple[Any, bytes, tuple[str, ...], bool]] = []
    for node, raw, owners in list(names.writer_nodes):
        values = _command_values(node, raw)
        idx = _unwrap_command([value for value, _ in values], 0)
        if idx >= len(values) or values[idx][1] < 0 or not _shell_reachable(values, idx):
            continue
        _, calls, _ = _resolved_invocations(node, raw, scope)
        for head, extra, pos in calls:
            rem = [*((w, -1) for w in extra), *values[pos:]]
            if head in EVAL_BUILTINS and rem and all(d < 0 for _, d in rem):
                names.inline.append(" ".join(v for v, _ in rem))
                roots.extend(_inline_roots(names, owners))
    return roots


def _finish_writers(names: _Names, scope: _VarScope) -> None:
    """Evaluate staged writer nodes against the resolved variable scope."""
    for node, raw, _ in names.writer_nodes:
        if not _may_write_node(node, raw, scope):
            continue
        prefix = _loop_prefix(node, raw)
        if prefix is None:
            names.dynamic_write = True
        else:
            names.prefix_writes.add(prefix)


def _assignment_texts(
    root: Any, raw: bytes, budget: BodyBudget, seed: _VarScope | None = None
) -> _Names:
    """Collect assignments, spelled words, resolved scopes and dynamic writers."""
    names = _Names()
    _walk_roots([(root, raw, (), False)], names, budget)
    scope = _resolve_bindings(names, seed)
    extra = _resolved_inline_roots(names, scope)
    if extra:
        _walk_roots(extra, names, budget)
        scope = _resolve_bindings(names, seed)
    names.var_scope = scope
    _finish_writers(names, scope)
    return names


def _write_occurrence(code: bytes, start: int, end: int) -> bool:
    """Tell a use that may write a name from a read reference, bare export or bare readonly."""
    if code[max(start - 1, 0) : start] == b"$":
        return False
    if code[max(start - 2, 0) : start] == b"${":
        return READ_BRACE_TAIL.match(code, end) is None
    if code[max(start - 3, 0) : start] in (b"${#", b"${!"):
        return code[end : end + 1] != b"}"
    head = code[code.rfind(b"\n", 0, start) + 1 : start]
    return BARE_ATTRIBUTE.fullmatch(head) is None or code[end : end + 1] not in BARE_ENDS


def _written_names(raw: bytes, watched: set[str], names: _Names) -> set[str]:
    """Return watched names used outside a plain = assignment, a read reference or a bare export."""
    code = bytearray(raw)
    for start, end in names.skipped:
        code[start:end] = b" " * (end - start)
    text = bytes(code)
    spelled = "\n".join(names.spelled).encode("utf-8", "surrogateescape")
    written = set()
    for name in sorted(watched):
        word = re.compile(rb"(?<![A-Za-z0-9_])" + re.escape(name.encode()) + rb"(?![A-Za-z0-9_])")
        if any(
            match.start() not in names.starts
            and _write_occurrence(text, match.start(), match.end())
            for match in word.finditer(text)
        ) or any(_write_occurrence(spelled, m.start(), m.end()) for m in word.finditer(spelled)):
            written.add(name)
    return written | {n for n in watched if any(n.startswith(p) for p in names.prefix_writes)}


def _literal_map(names: _Names, raw: bytes, watched: set[str]) -> tuple[dict[str, str], set[str]]:
    """Collect unique literals and assigned names, counting a write of a watched name as one."""
    every = names.assigned | watched
    written = every if names.dynamic_write else _written_names(raw, every, names)
    literals = _find_literal_assignments(names.assignments)
    return {k: v for k, v in literals.items() if k not in written}, names.assigned | written


def _hold_closure(scan: _Scan) -> dict[str, frozenset[str]]:
    """Map each called function to the names a prefix assignment holds in its body."""
    held = {f: set(n) for f, n in scan.prefix_calls.items()}
    bodies = scan.function_bodies
    calls = {
        f: set(PLAIN_FUNCTION.findall(" ".join(t))) | scan.function_calls.get(f, set())
        for f, t in bodies.items()
    }
    for caller in sorted(scan.dynamic_callers & set(calls)):
        calls[caller] = set(bodies)
    for _ in range(len(calls) + 1):
        grown = False
        for caller in sorted(held):
            for callee in sorted(calls.get(caller, ())):
                if not held[caller] <= held.setdefault(callee, set()):
                    held[callee] |= held[caller]
                    grown = True
        if not grown:
            break
    return {f: frozenset(n) for f, n in held.items() if n}


def _seed_dynamic_functions(scan: _Scan) -> None:
    """Propagate all prefix-held names into functions whose defined name is run-time text."""
    if not scan.prefix_calls:
        return
    every = set().union(*scan.prefix_calls.values())
    for function in scan.function_bodies:
        if function.startswith("$"):
            scan.prefix_calls.setdefault(function, set()).update(every)


def _note_prefix_calls(names: _Names, scan: _Scan) -> None:
    """Add one text's prefix calls and function bodies, then recompute the held names."""
    for command, held in names.prefixed.items():
        scan.prefix_calls.setdefault(command, set()).update(held)
    for function, texts in names.bodies.items():
        scan.function_bodies.setdefault(function, []).extend(texts)
    for function, called in names.calls.items():
        scan.function_calls.setdefault(function, set()).update(called)
    scan.dynamic_callers |= names.dynamic_callers
    _seed_dynamic_functions(scan)
    before = scan.prefixed_functions
    scan.prefixed_functions = _hold_closure(scan)
    trusted = scan.assigned | names.assigned | set(scan.literals) | set(scan.script_dirs)
    for function in sorted(scan.walked_functions):
        now = scan.prefixed_functions.get(function, frozenset())
        gained = (now - before.get(function, frozenset())) & trusted
        if _referenced(" ".join(scan.function_bodies.get(function, [])), gained):
            scan.refusal = SOURCED_PREFIX_CALL


def _prime_names(scan: _Scan, names: _Names, raw: bytes) -> None:
    """Seed literals, assigned and own-directory names; refuse own-directory names it writes."""
    scan.var_scope = names.var_scope
    dirs = _script_dir_names(names.assignments) if scan.context.script_dir is not None else {}
    overrides = {override for override in dirs.values() if override is not None}
    watched = names.assigned | overrides
    written = _written_names(raw, watched, names)
    unsure = watched if names.dynamic_write else written
    scan.assigned = names.assigned | unsure
    literals = _find_literal_assignments(names.assignments)
    scan.literals = {k: v for k, v in literals.items() if k not in unsure}
    scan.script_dirs = {k: v for k, v in dirs.items() if k not in written}
    if names.dynamic_write:
        scan.late_dirs = set(scan.script_dirs)
    for name in sorted(written & set(dirs)):
        scan.refused_dirs[name] = WRITTEN_SCRIPT_DIR
    _note_prefix_calls(names, scan)


def _merge_source_literals(node: Any, scan: _Scan) -> None:
    """Merge sourced assignments at entry while permanently removing conflicting names."""
    literals, assigned = scan.source_literals.pop(id(node), ({}, set()))
    for name in assigned:
        if name in scan.ambiguous:
            continue
        value = literals.get(name)
        if name in scan.assigned and (value is None or scan.literals.get(name) != value):
            scan.literals.pop(name, None)
            scan.ambiguous.add(name)
        elif value is not None:
            scan.literals[name] = value
    scan.assigned.update(assigned)


def _finish(
    scan: _Scan, directory_state: list[bool] | None
) -> tuple[str | None, list[tuple[str, bool, bool]], list[str], list[tuple[str, bool, bool]]]:
    """Settle pending references and return the file's verdict and candidates."""
    scan.span_names = frozenset()
    refusal = _settle_pending(scan)
    if refusal is not None:
        return refusal, [], [], []
    if directory_state is not None:
        directory_state[0] = scan.dir_changed
    return None, scan.candidates, scan.cd_targets, scan.filtered


def _prefix_rebinds(words: list[str]) -> bool:
    """Recognize assignments of names already excluded by the command-rebinding allow-list."""
    return any(
        _protected_name(word.split("=", 1)[0].removesuffix("+")) for word in words if "=" in word
    )


def _environment_rebinds(context: InspectionContext, budget: BodyBudget) -> bool:
    """Detect inherited startup hooks, exported functions or a caller's rebound environment."""
    has_func = any(key.startswith("BASH_FUNC_") for key in os.environ)
    return (
        "BASH_ENV" in os.environ
        or "ENV" in os.environ
        or has_func
        or _prefix_rebinds(list(context.command_prefix))
        or budget.rebound_unknown
        or context.script_path in budget.rebound_scripts
    )


def scan_body(
    body: str,
    context: InspectionContext,
    budget: BodyBudget,
    directory_state: list[bool] | None = None,
) -> tuple[str | None, list[tuple[str, bool, bool]], list[str], list[tuple[str, bool, bool]]]:
    """Walk a file and inline bodies with one bounded stack and shared node accounting."""
    parser = bash_parser()
    if isinstance(parser, str):
        return parser, [], [], []
    raw = body.encode("utf-8", "surrogateescape")
    scan = _Scan(context, budget, dir_changed=context.directory_changed)
    root = parser.parse(raw).root_node
    _prime_names(scan, _assignment_texts(root, raw, budget), raw)
    if context.script_dir is not None:
        scan.rebound = _environment_rebinds(context, budget)
    scan.stack.append((root, raw, False))
    for _ in range(MAX_BODY_NODES):
        if not scan.stack:
            return _finish(scan, directory_state)
        if budget.nodes >= MAX_BODY_NODES:
            return "shell syntax tree larger than inspect limit", [], [], []
        node, source, mode = scan.stack.pop()
        _merge_source_literals(node, scan)
        budget.nodes += 1
        hit = _visit(node, source, mode, scan)
        if hit is not None or scan.refusal is not None:
            return hit or scan.refusal, [], [], []
    if scan.stack:
        return "shell syntax tree larger than inspect limit", [], [], []
    return _finish(scan, directory_state)
