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
REBOUND_SCRIPT_DIR = (
    "script path from an own-directory name assigned after a shell function, alias, enable, "
    "hash or bash variable change that can rebind the commands of the idiom; use an absolute path"
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
# Identifier operand with an optional subscript and optional assignment or append value.
VARIABLE_WORD = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)(?:\[[^\]]*\])?(?:\+?=.*)?", re.DOTALL)
# Whole shell identifier in arithmetic let operands.
IDENTIFIER = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z_][A-Za-z0-9_]*")
# Writer option cluster with its target variable name and optional subscript attached.
ATTACHED_OPTION = re.compile(r"-([A-Za-z_][A-Za-z0-9_]*)(?:\[[^\]]*\])?")
# Arithmetic assignment, increment or decrement of an identifier in literal text.
VARIABLE_ARITH_WRITE = re.compile(
    r"(?<![A-Za-z0-9_])([A-Za-z_][A-Za-z0-9_]*)(?![A-Za-z0-9_])"
    r"\s*(?:\[[^\]]*\]\s*)?(?:\*\*|<<|>>|[-+*/%&|^])?=(?!=)"
    r"|(?:\+\+|--)\s*([A-Za-z_][A-Za-z0-9_]*)(?![A-Za-z0-9_])"
    r"|(?<![A-Za-z0-9_])([A-Za-z_][A-Za-z0-9_]*)(?![A-Za-z0-9_])\s*(?:\+\+|--)"
)
# Numeric parameter and length expansions permitted inside arithmetic text.
NUMERIC_EXPANSION = re.compile(r"(?<!\\)(?:\$[#?$!]|\$\{#[A-Za-z_][A-Za-z0-9_]*\})")
# Signed or unsigned decimal integer literal accepted for identifier reads in arithmetic.
DECIMAL_INTEGER = re.compile(r"[+-]?(?:0|[1-9][0-9]*)")
# Plain arithmetic assignment tail excluding equality and compound operators.
PLAIN_ASSIGN_TAIL = re.compile(r"(?:\s*\[[^\]]*\])?\s*=(?!=)")
# Parameter name and optional subscript before a substring expansion colon.
SUBSTRING_HEAD = re.compile(
    rb"\$\{(?:[@*]|[0-9]+|[A-Za-z_][A-Za-z0-9_]*(?:\[[^\]\n]*\])?):(?![-=+?])"
)
# Integer comparison operators whose operands inside [[ ]] evaluate as arithmetic.
ARITH_TEST_OPERATORS = frozenset(["-eq", "-ne", "-lt", "-le", "-gt", "-ge"])
# Writer builtin option specs: (destination flags, value flags, operand slice).
WRITER_SPECS: dict[str, tuple[str, str, slice]] = {
    "read": ("a", "dinNptu", slice(None)),
    "mapfile": ("", "dnOsuCc", slice(0, 1)),
    "readarray": ("", "dnOsuCc", slice(0, 1)),
    "getopts": ("", "", slice(1, 2)),
    "wait": ("p", "", slice(0, 0)),
    "compgen": ("V", "oAGWFCXPS", slice(0, 0)),
}
# Terminal syntax spans whose literal text can feed arithmetic evaluation.
LITERAL_TEXT = frozenset(["word", "raw_string", "string_content", "ansi_c_string", "ERROR"])
# Shell builtins that declare, export, freeze or unset variable bindings.
DECLARATION_BUILTINS = frozenset(["declare", "typeset", "local", "export", "readonly", "unset"])
# Declaration builtins whose -n flag creates a nameref variable.
NAMEREF_BUILTINS = frozenset(["declare", "typeset", "local"])
SUBSHELLS = frozenset(["command_substitution", "process_substitution", "subshell"])
IDIOM_COMMANDS = frozenset(["cd", "pwd", "dirname", "readlink", "realpath"])
REBINDING_COMMANDS = frozenset(["alias", "enable", "hash"])
PATH_WRITERS = frozenset(
    ["read", "printf", "mapfile", "readarray", "getopts", "wait", "compgen", "coproc"]
)
# Assignments before these persist in POSIX mode, and eval and source bodies see them in every mode.
SPECIAL_BUILTINS = frozenset(
    [
        ".",
        ":",
        "break",
        "continue",
        "eval",
        "exec",
        "exit",
        "export",
        "readonly",
        "return",
        "set",
        "shift",
        "source",
        "times",
        "trap",
        "unset",
    ]
)
ARITHMETIC = frozenset(["binary_expression", "postfix_expression", "unary_expression"])
ASSIGNING_EXPANSIONS = frozenset(["=", ":="])
PATH_OPERANDS = frozenset(["variable_name", "word", "subscript"])
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
    pending: list[tuple[str, bool, bool, frozenset[str]]] = field(default_factory=list)
    moved: bool = False
    rebound: bool = False
    assoc_arrays: set[str] = field(default_factory=set)
    integers: set[str] = field(default_factory=set)
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
    """Name the refusal for a word referencing an ambiguous, unresolved or refused script dir."""
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
    root = bash_parser().parse(raw).root_node
    if source:
        scan.source_literals[id(root)] = _literal_map(root, raw, scan.budget)
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
    words, idx, name = _command_info(_single_reader(node), raw, scan.literals)
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


def _names_bash_variable(word: str) -> bool:
    """Match an assignment or target operand naming a documented bash variable."""
    match = VARIABLE_WORD.fullmatch(word)
    return match is not None and match.group(1) in BASH_VARIABLES


def _attached_bash_variable(word: str) -> bool:
    """Match a writer option cluster with a documented bash variable attached."""
    match = ATTACHED_OPTION.fullmatch(word)
    if match is None:
        return False
    cluster = match.group(1)
    return any(
        cluster[:i].isalpha() and cluster[i:] in BASH_VARIABLES for i in range(1, len(cluster))
    )


def _arith_writes_bash_variable(text: str) -> bool:
    """Match an arithmetic write to a documented bash variable in literal text."""
    return any(
        (match.group(1) or match.group(2) or match.group(3)) in BASH_VARIABLES
        for match in VARIABLE_ARITH_WRITE.finditer(text)
    )


def _arith_rebinds(text: str, literals: dict[str, str] | None = None) -> bool:
    """Refuse arithmetic text with non-numeric expansions, writes or non-integer reads."""
    cleaned = NUMERIC_EXPANSION.sub(" 0 ", text)
    if "$" in cleaned or "`" in cleaned:
        return True
    unquoted = cleaned.translate(STRIP_CHARS)
    if _arith_writes_bash_variable(unquoted):
        return True
    known = literals or {}
    for match in IDENTIFIER.finditer(unquoted):
        if PLAIN_ASSIGN_TAIL.match(unquoted, match.end()) is not None:
            continue
        if DECIMAL_INTEGER.fullmatch(known.get(match.group(), "")) is None:
            return True
    return False


def _integer_value_rebinds(text: str, literals: dict[str, str] | None = None) -> bool:
    """Refuse an integer-attributed value holding an expansion or rebinding arithmetic."""
    return "$" in text or "`" in text or _arith_rebinds(text, literals)


def _parse_subscript_target(word: str) -> tuple[str, str] | None:
    """Split a single name[subscript] target with balanced brackets into name and index."""
    match = ASSIGNED_NAME.match(word)
    if match is None or match.end() >= len(word) or word[match.end()] != "[":
        return None
    depth = 0
    start = match.end()
    for idx in range(start, len(word)):
        depth += (word[idx] == "[") - (word[idx] == "]")
        if depth == 0:
            return (match.group(), word[start + 1 : -1]) if idx == len(word) - 1 else None
    return None


def _destination_rebinds(dest: str, scan: _Scan | None = None, *, writer: bool = False) -> bool:
    """Refuse dynamic, bash-variable, integer-writer or unsafe-subscript destinations."""
    parsed = _parse_subscript_target(dest)
    if parsed is not None:
        base, sub = parsed
        assoc = scan.assoc_arrays if scan is not None else ()
        int_write = writer and scan is not None and base in scan.integers
        sub_write = base not in assoc and _arith_rebinds(sub, scan.literals if scan else None)
        return base in BASH_VARIABLES or int_write or sub_write
    int_scalar = writer and scan is not None and dest in scan.integers
    return "$" in dest or "`" in dest or _names_bash_variable(dest) or int_scalar


def _split_assignment_word(word: str) -> tuple[str, bool, str]:
    """Split an operand at the first top-level equals sign outside brackets."""
    depth = 0
    for idx, ch in enumerate(word):
        if ch == "[":
            depth += 1
        elif ch == "]" and depth > 0:
            depth -= 1
        elif ch == "=" and depth == 0:
            return word[:idx].removesuffix("+"), True, word[idx + 1 :]
    if "=" in word and depth > 0:
        head, _, tail = word.partition("=")
        return head.removesuffix("+"), True, tail
    return word, False, ""


def _declaration_flags(name: str, operands: list[str]) -> tuple[bool, bool, bool]:
    """Collect integer, associative-array and rebinding-option flags from a declaration."""
    is_int = False
    is_assoc = False
    rebinds = False
    for word in operands:
        if word == "--":
            break
        if not word.startswith(("-", "+")) or word in ("-", "+"):
            continue
        rebinds |= "$" in word or "`" in word
        if name in NAMEREF_BUILTINS and word.startswith("-"):
            is_int |= "i" in word[1:]
            is_assoc |= "A" in word[1:]
            rebinds |= "n" in word[1:]
    return is_int, is_assoc, rebinds


def _record_declared_name(base: str, is_int: bool, is_assoc: bool, scan: _Scan | None) -> bool:
    """Store declared array or integer attributes on _Scan and report integer status."""
    if scan is not None and base:
        if is_assoc:
            scan.assoc_arrays.add(base)
        if is_int:
            scan.integers.add(base)
    return is_int or (scan is not None and base in scan.integers)


def _declaration_rebinds(
    name: str,
    operands: list[str],
    scan: _Scan | None = None,
    raw_operands: list[str] | None = None,
) -> bool:
    """Record declaration attributes and match dynamic, bash-variable or integer writes."""
    is_int, is_assoc, rebinds = _declaration_flags(name, operands)
    in_options = True
    literals = scan.literals if scan is not None else None
    for word, raw_word in zip(operands, raw_operands or operands, strict=False):
        if in_options and word == "--":
            in_options = False
            continue
        if in_options and word.startswith(("-", "+")) and word not in ("-", "+"):
            continue
        in_options = False
        target, has_eq, _ = _split_assignment_word(word)
        _, _, raw_val = _split_assignment_word(raw_word)
        match = VARIABLE_WORD.fullmatch(target)
        base = match.group(1) if match is not None else ""
        int_target = _record_declared_name(base, is_int, is_assoc, scan)
        rebinds |= _destination_rebinds(target, scan)
        if has_eq and int_target:
            rebinds |= _integer_value_rebinds(raw_val, literals)
    return rebinds


def _printf_writes_bash_variable(args: list[str], scan: _Scan | None = None) -> bool:
    """Match a printf -v option whose separate or attached target rebinds or is dynamic."""
    dests: list[str] = []
    take_next = False
    for arg in args:
        if take_next:
            dests.append(arg)
            take_next = False
        elif not arg.startswith("-") or arg in ("-", "--"):
            break
        elif arg == "-v":
            take_next = True
        elif arg.startswith("-v"):
            dests.append(arg[2:])
        elif "$" in arg or "`" in arg or _attached_bash_variable(arg):
            return True
    return any(_destination_rebinds(dest, scan, writer=True) for dest in dests)


def _consume_writer_flag(
    arg: str, dest_opts: str, val_opts: str
) -> tuple[tuple[str, ...], str | None, bool]:
    """Parse one writer option cluster for an attached destination, value need or expansion."""
    for pos, ch in enumerate(arg[1:], start=1):
        tail = arg[pos + 1 :]
        if ch in dest_opts and tail:
            return (tail,), None, False
        if ch in dest_opts:
            return (), "dest", False
        if ch in val_opts:
            return (), ("val" * (not tail) or None), False
        if not ch.isalpha():
            return (), None, ch in ("$", "`")
    return (), None, False


def _writer_destinations(spec: tuple[str, str, slice], args: list[str]) -> tuple[list[str], bool]:
    """Extract destination words and dynamic-flag state for a writer builtin."""
    dest_opts, val_opts, op_slice = spec
    dests: list[str] = []
    operands: list[str] = []
    in_options = True
    want: str | None = None
    dynamic = False
    for arg in args:
        if want is not None:
            if want == "dest":
                dests.append(arg)
            want = None
        elif not in_options or not arg.startswith("-") or arg == "-":
            in_options = False
            operands.append(arg)
        elif arg == "--":
            in_options = False
        else:
            attached, want, dyn = _consume_writer_flag(arg, dest_opts, val_opts)
            dynamic |= dyn
            dests.extend(attached)
    dests.extend(operands[op_slice])
    return dests, dynamic


def _writes_path(
    name: str,
    args: list[str],
    scan: _Scan | None = None,
    raw_args: list[str] | None = None,
) -> bool:
    """Match a writer builtin, let operand or wrapped declaration binding a bash variable."""
    if name == "printf":
        return _printf_writes_bash_variable(args, scan)
    if name in WRITER_SPECS:
        dests, dynamic = _writer_destinations(WRITER_SPECS[name], args)
        return dynamic or any(_destination_rebinds(dest, scan, writer=True) for dest in dests)
    if name in PATH_WRITERS:
        return any(_names_bash_variable(w) or _attached_bash_variable(w) for w in args)
    if name == "let":
        literals = scan.literals if scan is not None else None
        source = raw_args if raw_args is not None else args
        return any(
            "$" in w or "`" in w or any(ident in BASH_VARIABLES for ident in IDENTIFIER.findall(w))
            for w in args
        ) or any(_arith_rebinds(w, literals) for w in source if w != "--")
    return name in DECLARATION_BUILTINS and _declaration_rebinds(name, args, scan, raw_args)


def _note_rebinding_command(node: Any, raw: bytes, words: list[str], idx: int, scan: _Scan) -> None:
    """Record commands that alias, disable, hash or write a bash variable before the idiom."""
    name = os.path.basename(words[idx])
    args = words[idx + 1 :]
    raw_args = _command_words(node, raw)[idx + 1 :]
    hash_reset = name == "hash" and all(w == "-r" for w in args)
    rebinds = name in REBINDING_COMMANDS and not hash_reset
    writes_path = _writes_path(name, args, scan, raw_args)
    if rebinds or writes_path:
        scan.rebound = True


def _inspect_command(node: Any, raw: bytes, scan: _Scan) -> str | None:
    """Classify command position, wrappers and executable paths without following call cycles."""
    words, idx, name = _command_info(node, raw, scan.literals)
    if idx >= len(words):
        return None
    _note_rebinding_command(node, raw, words, idx, scan)
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
    producers = [_command_info(stage, raw, scan.literals) for stage in stages]
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
        words, idx, name = _command_info(stages[i], raw, scan.literals)
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
    words, idx, name = _command_info(_redirect_owner(node), raw, scan.literals)
    target, inline = _shell_argument(words[idx:]) if name in SHELLS else (None, False)
    shell_input = name in SHELLS and not inline and target in (None, "-")
    for i in range(len(statements)):
        stages = statements[i].named_children
        for j in range(len(stages)):
            _, _, stage_name = _command_info(stages[j], raw, scan.literals)
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


def _visit(node: Any, raw: bytes, data_only: bool, scan: _Scan) -> str | None:
    """Apply the grammar allow-list, push children in order and restore subshell directories."""
    if isinstance(node, _DirState):
        scan.dir_changed, scan.moved = node
        return None
    kind = node.type
    if kind not in STRUCTURE | ASSIGNMENTS | DATA | LEAVES | SPECIAL:
        return f"unsupported shell syntax {kind} near line {node.start_point.row + 1}"
    _note_path_write(node, raw, scan)
    if kind == "ERROR":
        return _error_hit(node, raw)
    if kind in LEAVES:
        return None
    if kind in ("command_substitution", "process_substitution"):
        data_only = False
    entry = _DirState(scan.dir_changed, scan.moved)
    hit, children = _visit_semantics(node, raw, data_only, scan)
    if kind in SUBSHELLS:
        scan.stack.append((entry, raw, False))
    for i in range(len(children) - 1, -1, -1):
        child, mode = children[i]
        if not child.is_missing:
            scan.stack.append((child, raw, mode))
    return hit


def _note_function(node: Any, raw: bytes, scan: _Scan) -> None:
    """Record functions that name an idiom command or use a non-plain function name."""
    first = node.named_children[0] if node.named_children else None
    name = _text(first, raw) if first is not None and first.type == "word" else ""
    if name in IDIOM_COMMANDS or PLAIN_FUNCTION.fullmatch(name) is None:
        scan.rebound = True


def _path_operands(node: Any) -> Iterable[Any]:
    """Select syntax children whose variable names can bind PATH."""
    kind = node.type
    if kind == "for_statement":
        return node.children_by_field_name("variable")
    if kind in ARITHMETIC:
        return node.named_children
    if kind == "expansion" and any(c.type in ASSIGNING_EXPANSIONS for c in node.children):
        return node.named_children
    if (
        kind == "variable_assignment"
        and node.parent is not None
        and node.parent.type == "c_style_for_statement"
    ):
        return node.named_children[:1]
    return ()


def _span_between(
    node: Any, raw: bytes, openers: tuple[str, ...], closers: tuple[str, ...]
) -> str | None:
    """Decode the raw byte slice between the first opener and last closer child token."""
    start: int | None = None
    end: int | None = None
    for child in node.children:
        if start is None and child.type in openers and not child.is_missing:
            start = child.end_byte
        elif start is not None and child.type in closers and not child.is_missing:
            end = child.start_byte
    if start is None:
        return None
    return raw[start : end if end is not None else node.end_byte].decode("utf-8", "surrogateescape")


def _indirect_assign_rebinds(node: Any, raw: bytes, scan: _Scan) -> bool:
    """Match ${!v=...} and ${!v:=...} indirect assignment expansions."""
    children = node.children
    if len(children) < 4 or children[0].type != "${" or children[1].type != "!":
        return False
    if not any(child.type in ASSIGNING_EXPANSIONS for child in children[2:]):
        return False
    target_node = children[2]
    if target_node.type != "variable_name":
        return True
    resolved = scan.literals.get(_text(target_node, raw))
    return resolved is None or _destination_rebinds(resolved, scan, writer=True)


def _closing_brace_byte(raw: bytes, start: int) -> int:
    """Find the matching closing brace byte of a parameter expansion with bounded scan."""
    limit = min(len(raw), start + MAX_SCRIPT_BYTES)
    pos = start
    depth = 1
    for _ in range(limit - start):
        if pos >= limit:
            break
        if raw[pos] == 92:
            pos += 2
        elif raw[pos : pos + 2] == b"${":
            depth += 1
            pos += 2
        elif raw[pos] == 125:
            depth -= 1
            if depth == 0:
                return pos
            pos += 1
        else:
            pos += 1
    return limit


def _split_substring_spec(spec: str) -> list[str]:
    """Split a parameter substring tail into offset and optional length expressions."""
    parens = 0
    ternaries = 0
    for idx, ch in enumerate(spec):
        parens = max(0, parens + (ch in "([{") - (ch in ")]}"))
        if parens > 0:
            continue
        if ch == "?":
            ternaries += 1
        elif ch == ":" and ternaries > 0:
            ternaries -= 1
        elif ch == ":":
            return [spec[:idx], spec[idx + 1 :]]
    return [spec]


def _substring_rebinds(node: Any, raw: bytes, scan: _Scan) -> bool:
    """Match rebinding arithmetic in ${NAME:offset} and ${NAME:offset:length}."""
    head = (
        SUBSTRING_HEAD.search(raw, node.start_byte, node.end_byte)
        if node.type == "ERROR"
        else SUBSTRING_HEAD.match(raw, node.start_byte)
    )
    if head is None:
        return False
    end = _closing_brace_byte(raw, head.end())
    spec = raw[head.end() : end].decode("utf-8", "surrogateescape")
    return any(_arith_rebinds(part, scan.literals) for part in _split_substring_spec(spec))


def _in_double_bracket(node: Any) -> bool:
    """Check whether a syntax node sits inside a [[ ]] conditional command."""
    parent = node.parent
    for _ in range(64):
        if parent is None or parent.type in SUBSHELLS:
            return False
        if parent.type == "test_command":
            return bool(parent.children and parent.children[0].type == "[[")
        parent = parent.parent
    return False


def _dbracket_arith_rebinds(node: Any, raw: bytes, scan: _Scan) -> bool:
    """Match rebinding arithmetic operands of integer comparisons inside [[ ]]."""
    if node.type != "binary_expression" or not _in_double_bracket(node):
        return False
    for child in node.children:
        if child.type == "test_operator" and _text(child, raw) in ARITH_TEST_OPERATORS:
            left = raw[node.start_byte : child.start_byte].decode("utf-8", "surrogateescape")
            right = raw[child.end_byte : node.end_byte].decode("utf-8", "surrogateescape")
            return _arith_rebinds(left, scan.literals) or _arith_rebinds(right, scan.literals)
    return False


def _node_arith_rebinds(node: Any, raw: bytes, scan: _Scan) -> bool:
    """Check arithmetic syntax nodes, subscripts, [[ ]] comparisons and substrings."""
    kind = node.type
    expr: str | None = None
    if kind == "arithmetic_expansion":
        expr = _span_between(node, raw, ("$((", "$["), ("))", "]"))
    elif kind == "c_style_for_statement" or (
        kind == "compound_statement" and node.children and node.children[0].type == "(("
    ):
        expr = _span_between(node, raw, ("((",), ("))",))
    elif kind == "subscript":
        name_node = node.child_by_field_name("name")
        if name_node is None or _text(name_node, raw) not in scan.assoc_arrays:
            expr = _span_between(node, raw, ("[",), ("]",))
    if expr is not None:
        return _arith_rebinds(expr, scan.literals)
    if kind in ("expansion", "ERROR"):
        return _indirect_assign_rebinds(node, raw, scan) or _substring_rebinds(node, raw, scan)
    return _dbracket_arith_rebinds(node, raw, scan)


def _note_path_write(node: Any, raw: bytes, scan: _Scan) -> None:
    """Record literal, loop, arithmetic, subscript and expansion writes before idiom."""
    if node.type in LITERAL_TEXT and _arith_writes_bash_variable(_text(node, raw)):
        scan.rebound = True
        return
    if _node_arith_rebinds(node, raw, scan):
        scan.rebound = True
        return
    for operand in _path_operands(node):
        if operand.type not in PATH_OPERANDS:
            continue
        match = ASSIGNED_NAME.match(_text(operand, raw))
        if match is not None and match.group() in BASH_VARIABLES:
            scan.rebound = True
            return


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
    elif kind == "function_definition":
        _note_function(node, raw, scan)
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
    """Map names assigned once to the own-directory idiom onto their override names."""
    names = [match.group() if (match := ASSIGNED_NAME.match(a)) else "" for a in assignments]
    counts = Counter(names)
    result: dict[str, str | None] = {}
    for i in range(len(assignments)):
        name = names[i]
        if not name or counts[name] != 1 or assignments[i][len(name) : len(name) + 1] != "=":
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


def _declaration_words(node: Any, raw: bytes) -> list[str]:
    """Unquote declaration or unset operands after merging adjacent child spans."""
    spans: list[tuple[int, int, bool]] = []
    for child in node.named_children:
        assignment = child.type == "variable_assignment"
        if spans and not assignment and not spans[-1][2] and spans[-1][1] == child.start_byte:
            spans[-1] = (spans[-1][0], child.end_byte, False)
        else:
            spans.append((child.start_byte, child.end_byte, assignment))
    words: list[str] = []
    for start, end, assignment in spans:
        text = raw[start:end].decode("utf-8", "surrogateescape")
        tokens = [text] if assignment else _tokens(text)
        words.extend(tokens if tokens is not None else [text.translate(STRIP_CHARS)])
    return words


def _note_path_words(node: Any, raw: bytes, scan: _Scan) -> None:
    """Record bash variable declarations, unsets, attributes and dynamic targets."""
    if node.type not in ("declaration_command", "unset_command") or not node.children:
        return
    keyword = _text(node.children[0], raw)
    raw_words = _declaration_words(node, raw)
    words = [_substitute_literals(word, scan.literals) for word in raw_words]
    if _declaration_rebinds(keyword, words, scan, raw_words):
        scan.rebound = True


def _path_binding_persists(assignment: Any, raw: bytes, scan: _Scan) -> bool:
    """Match a bash variable assignment outside a command or before a special builtin."""
    return (
        assignment.parent.type != "command"
        or _command_info(assignment.parent, raw, scan.literals)[2] in SPECIAL_BUILTINS
    )


def _array_element_rebinds(elem: Any, name: str, raw: bytes, scan: _Scan) -> bool:
    """Check subscript and integer-value arithmetic inside a compound array element."""
    text = _text(elem, raw)
    val_text = text
    if text.startswith("["):
        head, has_eq, tail = _split_assignment_word(text)
        if has_eq:
            val_text = tail
        if (
            head.endswith("]")
            and name not in scan.assoc_arrays
            and _arith_rebinds(head[1:-1], scan.literals)
        ):
            return True
    return name in scan.integers and _integer_value_rebinds(val_text, scan.literals)


def _assignment_rebinds(assignment: Any, name: str, raw: bytes, scan: _Scan) -> bool:
    """Check persistent variable, array-element and integer-attribute assignment writes."""
    if not _path_binding_persists(assignment, raw, scan):
        return False
    if name in BASH_VARIABLES:
        return True
    value = assignment.child_by_field_name("value")
    if value is None:
        return False
    if value.type == "array":
        return any(_array_element_rebinds(c, name, raw, scan) for c in value.named_children)
    return name in scan.integers and _integer_value_rebinds(_text(value, raw), scan.literals)


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

    Documented bash variable writes by loops, arithmetic, literal arithmetic text, assign-default
    expansions, writer builtins, attached writer options, let operands, declarations, wrapped
    declarations, unsets, namerefs, quoted declaration operands and special-builtin prefixes
    mark idiom commands rebound; prefixes of other commands do not. A directory path or
    resolved script file name holding a newline, or an assignment after rebinding, refuses
    every script path referencing the name. After a directory change or unread source, with
    its override assigned, or through a top-level declaration, later relative paths refuse;
    nested in a function, branch or loop, every script path referencing the name refuses.
    """
    if not scan.script_dirs:
        return
    _note_path_words(node, raw, scan)
    for assignment in _assignment_nodes(node):
        match = ASSIGNED_NAME.match(_text(assignment, raw))
        name = match.group() if match is not None else ""
        if _assignment_rebinds(assignment, name, raw, scan):
            scan.rebound = True
        if name not in scan.script_dirs:
            continue
        override = scan.script_dirs.pop(name)
        reason = _script_dir_refusal(scan)
        if reason is not None:
            scan.literals.pop(name, None)
            scan.refused_dirs[name] = reason
            continue
        _store_script_dir(assignment, name, override, scan)


def _assignment_texts(root: Any, raw: bytes, budget: BodyBudget) -> tuple[list[str], set[str]]:
    """Collect assignment texts and all assigned names with one bounded syntax walk."""
    stack = [root]
    assignments = []
    assigned = set()
    for _ in range(MAX_BODY_NODES):
        if not stack or budget.nodes >= MAX_BODY_NODES:
            break
        node = stack.pop()
        budget.nodes += 1
        if node.type == "variable_assignment" and node.parent.type != "command":
            value = node.child_by_field_name("value")
            if value is None or value.type != "array":
                assignment = _text(node, raw)
                assignments.append(assignment)
                name = assignment.split("=", 1)[0]
                if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                    assigned.add(name)
        stack.extend(reversed(node.named_children))
    return assignments, assigned


def _literal_map(root: Any, raw: bytes, budget: BodyBudget) -> tuple[dict[str, str], set[str]]:
    """Collect unique literals and all assigned names with one bounded syntax walk."""
    assignments, assigned = _assignment_texts(root, raw, budget)
    return _find_literal_assignments(assignments), assigned


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
    assignments, scan.assigned = _assignment_texts(root, raw, budget)
    scan.literals = _find_literal_assignments(assignments)
    if context.script_dir is not None:
        scan.script_dirs = _script_dir_names(assignments)
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
