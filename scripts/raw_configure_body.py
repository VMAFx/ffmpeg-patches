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
WRITER_NODES = frozenset(["command", "declaration_command", "unset_command"])
# Builtins that run their operands as shell text: eval now, trap when the signal arrives.
EVAL_BUILTINS = frozenset(["eval", "trap"])
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


@dataclass
class BodyBudget:
    """Share syntax node and inline body accounting across nested files."""

    nodes: int = 0
    inline_bodies: int = 0
    peeks: int = 0


class _DirState(NamedTuple):
    """Caller directory state restored after a subshell's children."""

    dir_changed: bool
    moved: bool


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


DECLARING = _Writer("", "", "names", "fF")
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
    "declare": DECLARING,
    "typeset": DECLARING,
    "local": DECLARING,
    "export": DECLARING,
    "readonly": DECLARING,
    "let": _Writer("", "", "names"),
    "eval": _Writer("", "", "any"),
    "trap": _Writer("", "", "any"),
}


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
    moved: bool = False
    rebound: bool = False
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
    literal = word.translate(STRIP_CHARS)
    decidable = DECIDABLE_NAME.fullmatch(os.path.basename(literal))
    return _is_configure_word(word) and bool(decidable or CONFIGURE_WORD.search(literal))


def _candidate_allowed(operand: str) -> bool:
    """Accept concrete script operands independently of directory state."""
    return bool(operand and operand != "-" and "$" not in operand and "`" not in operand)


def _record_candidate(operand: str, is_program: bool, scan: _Scan) -> None:
    """Preserve the directory state at each concrete candidate's encounter.

    Words arrive with plain references to resolved own-directory names substituted, so a
    reference still left (an operator form such as ${N%/}) refuses.
    """
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
    """Name the refusal for a word referencing an ambiguous, unresolved or refused script dir.

    A name that a prefix assignment holds in the function body being walked refuses first.
    """
    if _referenced(word, scan.span_names):
        return PREFIXED_FUNCTION_SCRIPT_DIR
    if _referenced(word, scan.ambiguous):
        return AMBIGUOUS_SCRIPT_PATH
    if _referenced(word, scan.unresolved_dirs):
        return UNRESOLVED_SCRIPT_DIR
    refused = _referenced(word, scan.refused_dirs)
    if refused:
        return scan.refused_dirs[min(refused)]
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
        names = _assignment_texts(root, raw, scan.budget)
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


def _command_spans(node: Any, raw: bytes) -> list[tuple[int, int, bool]]:
    """Collect adjacent command fields while keeping assignments in their own spans."""
    spans: list[tuple[int, int, bool]] = []
    children = node.children
    for i in range(len(children)):
        child = children[i]
        field_name = node.field_name_for_child(i)
        if child.type != "variable_assignment" and field_name not in ("name", "argument"):
            continue
        value = _text(child, raw)
        if re.fullmatch(r"\{[A-Za-z_][A-Za-z_0-9]*\}", value) and raw[
            child.end_byte : child.end_byte + 1
        ] in (b"<", b">"):
            continue
        assignment = child.type == "variable_assignment"
        if spans and not assignment and not spans[-1][2] and spans[-1][1] == child.start_byte:
            spans[-1] = (spans[-1][0], child.end_byte, False)
        else:
            spans.append((child.start_byte, child.end_byte, assignment))
    return spans


def _command_words(node: Any, raw: bytes) -> list[str]:
    """Tokenize command fields while preserving each assignment as a single word."""
    words: list[str] = []
    for start, end, assignment in _command_spans(node, raw):
        text = raw[start:end].decode("utf-8", "surrogateescape")
        tokens = [text] if assignment else _tokens(text)
        words.extend(tokens if tokens is not None else [text.translate(STRIP_CHARS)])
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


def _inspect_command(node: Any, raw: bytes, scan: _Scan) -> str | None:
    """Classify command position, wrappers and executable paths without following call cycles."""
    words, idx, name = _command_info(node, raw, _live_literals(scan))
    if idx >= len(words):
        return None
    if name in SHELLS:
        return _shell_command(node, raw, words[idx:], scan)
    if name in ("source", "."):
        return _shell_command(node, raw, words[idx:], scan, source=True)
    if name == "eval":
        _queue_inline(" ".join(words[idx + 1 :]), scan)
        return None
    if name == "trap":
        _trap_program(words[idx + 1 :], scan)
        return None
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
        scan.dir_changed, scan.moved = node
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
    entry = _DirState(scan.dir_changed, scan.moved)
    hit, children = _visit_semantics(node, raw, data_only, scan)
    _push_visit_children(kind, entry, children, raw, scan)
    return hit


def _note_prefix_span(node: Any, raw: bytes, scan: _Scan) -> None:
    """Note a walked function and mark its body if the script calls it under a prefix assignment."""
    name_node = node.child_by_field_name("name")
    body = node.child_by_field_name("body")
    if name_node is None or body is None:
        return
    name = _text(name_node, raw)
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
    """Statement assignments, assigned names, plain-assignment name starts and skipped spans.

    Skipped spans are comments and quoted here-document bodies, which Bash never expands.
    Spelled words are words whose text differs after quote removal; calls holds the words
    each function body spells that way, and inline the eval and trap texts left to parse.
    dynamic_write marks a writer that names a variable at run time, and prefix_writes the
    prefixes such a writer is narrowed to by a "${!PREFIX@}" loop.
    """

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
    parsed: int = 0


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


def _prefix_command(node: Any, raw: bytes) -> str | None:
    """Return the plain word naming the command a prefix assignment runs with, if there is one."""
    command_name = node.parent.child_by_field_name("name")
    parts = command_name.named_children if command_name is not None else []
    if len(parts) != 1 or parts[0].type != "word" or parts[0].named_children:
        return None
    word = _text(parts[0], raw)
    return word if PLAIN_WORD.fullmatch(word) is not None else None


def _note_prefix(node: Any, raw: bytes, names: _Names) -> None:
    """Record the name start and command of an = prefix before a plain non-special command.

    Bash keeps such a prefix assignment for that one command and restores the old value after
    it (bash 5.3 manual 4.4: only a special builtin in POSIX mode keeps it), so it writes no
    value for later statements. A prefix before a special builtin, a quoted or computed
    command name, or no command at all counts as a write. The command word is kept, because a
    function called this way sees the prefix value in its whole body.
    """
    command = _prefix_command(node, raw)
    if command is None or command in SPECIAL_BUILTINS:
        return
    start = _plain_start(node)
    if start is not None:
        names.starts.add(start)
        name = _text(node.child_by_field_name("name"), raw)
        names.prefixed.setdefault(command, set()).add(name)


def _note_function(node: Any, raw: bytes, names: _Names) -> None:
    """Record a function's name and body text for the prefix call closure."""
    name_node = node.child_by_field_name("name")
    body = node.child_by_field_name("body")
    if name_node is not None and body is not None:
        names.bodies.setdefault(_text(name_node, raw), []).append(_text(body, raw))


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


def _static_value(nodes: list[Any], raw: bytes) -> tuple[str, int]:
    """Return a word's text after quote removal and the offset of its first run-time part.

    A run-time part (an expansion, a substitution or any part type outside the allow-list)
    counts as empty text, and a glob or brace word keeps its text; the offset is -1 when the
    word has no run-time part.
    """
    parts: list[str] = []
    dynamic = -1
    stack = list(reversed(nodes))
    for _ in range(MAX_BODY_NODES):
        if not stack:
            break
        part = stack.pop()
        if part.type in STATIC_GROUPS:
            stack.extend(reversed(part.named_children))
            continue
        text, expands = _leaf_value(part, raw) if part.type in STATIC_LEAVES else ("", True)
        if expands and dynamic < 0:
            dynamic = sum(len(p) for p in parts)
        parts.append(text)
    return "".join(parts), dynamic


def _word_runs(node: Any) -> list[list[Any]]:
    """Group a node's adjacent word-part children into the shell words they form.

    tree-sitter-bash splits a declaration operand such as H''ERE=1 into a variable_name and
    an adjacent concatenation; Bash reads one word.
    """
    runs: list[list[Any]] = []
    end = -1
    for child in node.named_children:
        if child.type not in WORD_PARTS:
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
    for run in _word_runs(node):
        value, _ = _static_value(run, raw)
        if value == raw[run[0].start_byte : run[-1].end_byte].decode("utf-8", "surrogateescape"):
            continue
        names.spelled.append(value)
        for owner in owners:
            names.calls.setdefault(owner, set()).update(PLAIN_FUNCTION.findall(value))


def _command_values(node: Any, raw: bytes) -> list[tuple[str, int]]:
    """Return a command's quote-removed words, its name first, with their run-time offsets."""
    if node.type != "command":
        head = (_text(node.children[0], raw), -1)
    else:
        name = node.child_by_field_name("name")
        if name is None:
            return []
        head = _static_value(name.named_children, raw)
    return [head] + [_static_value(run, raw) for run in _word_runs(node)]


def _option_letters(
    spec: _Writer, word: tuple[str, int], following: tuple[str, int] | None
) -> tuple[list[tuple[str, int]], bool] | None:
    """Read one option word: the naming arguments and whether it took the next word.

    None means an option made the operands function names. The letters end at the word's
    run-time part: a letter that takes an argument takes the rest of the word when any follows,
    and a run-time part in place of the letters may spell a naming option, so it counts as a
    run-time naming argument for a writer that has one.
    """
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
    """Split a writer builtin's words into operands and naming option arguments.

    Options end at --, at a word without a leading - or +, at a lone - or +, and at a word
    that starts with a run-time part; a run-time part after the leading - or + keeps the word
    an option word. Bash expands a word before the builtin reads its options, so a word that
    starts with a run-time part and has a word after it may spell a naming option (printf
    $opt NAME with opt=-v) and counts as a naming argument for a writer that has one. None
    means an option made the operands function names.
    """
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


def _writer_dynamic(spec: _Writer, args: list[tuple[str, int]]) -> bool:
    """Tell whether a writer builtin may name a variable through a run-time word."""
    split = _writer_operands(spec, args)
    if split is None:
        return False
    operands, named = split
    if spec.operands == "names":
        return any(_dynamic_name(value, dynamic) for value, dynamic in operands)
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


def _may_write(values: list[tuple[str, int]], idx: int) -> bool:
    """Tell whether the command at idx may write a variable that a run-time word names.

    A command word built at run time names a builtin only where Bash may still pick one (the
    first word or a word after builtin, command or time) and only when its literal text has no
    slash; it then counts as a writer when a literal naming option (printf -v, read -a, wait
    -p, compgen -V) takes a run-time argument.
    """
    command, dynamic = values[idx]
    args = values[idx + 1 :]
    if dynamic >= 0:
        return _dynamic_call(values) and "/" not in command and _option_named(args)
    spec = WRITERS.get(command)
    return spec is not None and _writer_dynamic(spec, args)


def _dynamic_call(values: list[tuple[str, int]]) -> bool:
    """Tell whether a command word built at run time may name a shell function or eval.

    Bash looks a run-time command word up as a function, even one whose name holds a slash
    (measured on bash 5.3.20). Only the words of SHELL_WRAPPERS and their options keep that
    lookup in reach; any other first word runs a program or a builtin of its own.
    """
    for value, dynamic in values:
        if dynamic >= 0:
            return True
        if value not in SHELL_WRAPPERS and not value.startswith("-"):
            return False
    return False


def _loop_prefix(node: Any, raw: bytes) -> str | None:
    """Return PREFIX when a writer is the only command of a for loop over "${!PREFIX@}".

    The quoted list binds the loop variable to whole names that begin with PREFIX (bash 5.3
    manual 3.5.3), so a writer whose every run-time word is the quoted loop variable names
    only those. Any other run-time word, or a second command in the loop body, gives None.
    """
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
    for run in _word_runs(node):
        text = raw[run[0].start_byte : run[-1].end_byte].decode("utf-8", "surrogateescape")
        if _static_value(run, raw)[1] >= 0 and text not in loop_words:
            return None
    return match.group(1)


def _note_writer(node: Any, raw: bytes, names: _Names, owners: tuple[str, ...]) -> None:
    """Note a writer builtin that names a variable at run time and a call named at run time.

    The command word is unwrapped from builtin, command and the other known wrappers. An
    eval or trap text is queued for one more parse, because Bash removes its quotes again.
    """
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
    if not _may_write(values, idx):
        return
    prefix = _loop_prefix(node, raw)
    if prefix is None:
        names.dynamic_write = True
    else:
        names.prefix_writes.add(prefix)


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
    """Record one node's spelled words and writer builtins; return the owners of its children."""
    if node.type not in STATIC_GROUPS:
        _note_runs(node, raw, names, owners)
    if node.type in WRITER_NODES:
        _note_writer(node, raw, names, owners)
    name = node.child_by_field_name("name") if node.type == "function_definition" else None
    return owners if name is None else (*owners, _text(name, raw))


def _inline_roots(
    names: _Names, owners: tuple[str, ...]
) -> list[tuple[Any, bytes, tuple[str, ...], bool]]:
    """Parse the eval and trap texts one node queued, within the inline body bound.

    Past the bound a text counts as unknown: it may write any name and call any function.
    """
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


def _assignment_texts(root: Any, raw: bytes, budget: BodyBudget) -> _Names:
    """Collect assignments, assigned names, plain = starts and skipped spans in one bounded walk.

    The walk also records spelled words, writer builtins that name a variable at run time and
    the words each function body calls. An eval or trap text is parsed once more and walked
    for those three and its function bodies only, since its byte offsets are not the top
    text's; a function it defines joins the prefix call closure like any other.
    """
    stack: list[tuple[Any, bytes, tuple[str, ...], bool]] = [(root, raw, (), False)]
    names = _Names()
    for _ in range(MAX_BODY_NODES):
        if not stack or budget.nodes >= MAX_BODY_NODES:
            break
        node, text, owners, inline = stack.pop()
        budget.nodes += 1
        if not inline:
            _note_offsets(node, text, names)
        elif node.type == "function_definition":
            _note_function(node, text, names)
        inner = _note_words(node, text, names, owners)
        stack.extend(_inline_roots(names, owners))
        stack.extend((child, text, inner, inline) for child in reversed(node.named_children))
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
    """Return watched names used outside a plain = assignment, a read reference or a bare export.

    Comments and quoted here-document bodies are skipped, and a plain = prefix before a plain
    command other than a special builtin is no write. Every other use counts as a write, among
    them printf -v, read, mapfile, unset, an arithmetic, append or array assignment,
    ${NAME:=value}, a for loop variable and any text that names the variable, such
    as an eval or trap string. A word counts with its text after quote removal too, so
    H''ERE, H\\ERE and $'\\x48ERE' name HERE, and a writer narrowed to a "${!PREFIX@}" loop
    writes every name that begins with PREFIX. Callers count every name as written when a
    writer builtin or eval names a variable through any other run-time word (dynamic_write).
    A function called under a prefix assignment is handled by _hold_closure. Not detected: a
    command word built at run time that names a variable through an operand ($c "$n" with
    c=read), a last word that starts with a run-time part and spells a naming option with its
    argument (wait "$p" with p=-pNAME), a name built by arithmetic evaluation, and alias
    expansion.
    """
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
    """Map each called function to the names a prefix assignment holds in its body.

    Bash keeps a prefix assignment before a function call for the whole call, callees included
    (bash 5.3 manual 3.7.4 and 3.3), so the names pass on to every word a function body holds,
    before and after quote removal, over at most one round per function body. A body that
    runs a command named at run time, or an eval or trap text built at run time, may call
    every function. Every name counts, because a sourced file can assign a trusted literal of
    that name later.
    """
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


def _note_prefix_calls(names: _Names, scan: _Scan) -> None:
    """Add one text's prefix calls and function bodies, then recompute the held names.

    The top script and each sourced file add theirs before the guard walks them. A sourced file
    can call a function under a prefix assignment after the guard walked that function's body
    with the old value, so a walked body that references a newly held name refuses.
    """
    for command, held in names.prefixed.items():
        scan.prefix_calls.setdefault(command, set()).update(held)
    for function, texts in names.bodies.items():
        scan.function_bodies.setdefault(function, []).extend(texts)
    for function, called in names.calls.items():
        scan.function_calls.setdefault(function, set()).update(called)
    scan.dynamic_callers |= names.dynamic_callers
    before = scan.prefixed_functions
    scan.prefixed_functions = _hold_closure(scan)
    trusted = scan.assigned | names.assigned | set(scan.literals) | set(scan.script_dirs)
    for function in sorted(scan.walked_functions):
        now = scan.prefixed_functions.get(function, frozenset())
        gained = (now - before.get(function, frozenset())) & trusted
        if _referenced(" ".join(scan.function_bodies.get(function, [])), gained):
            scan.refusal = SOURCED_PREFIX_CALL


def _prime_names(scan: _Scan, names: _Names, raw: bytes) -> None:
    """Seed literals, assigned and own-directory names; refuse own-directory names it writes.

    A writer that names a variable at run time leaves no literal trusted and refuses each
    own-directory name at its assignment, after the refusal of an earlier statement outside
    the allow-list, which names the line.
    """
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


def _environment_rebinds() -> bool:
    """Detect environment startup files or exported Bash functions that can rebind commands."""
    has_func = any(key.startswith("BASH_FUNC_") for key in os.environ)
    return "BASH_ENV" in os.environ or "ENV" in os.environ or has_func


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
        scan.rebound = _environment_rebinds()
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
