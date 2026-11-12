# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Shell syntax tree inspection for raw configure guard."""

from __future__ import annotations

import importlib
import os
import re
import sys
from collections import Counter
from collections.abc import Callable
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
# Own directory of an executed script, symlinks resolved like Path.resolve():
# $(dirname "$(readlink -f "${BASH_SOURCE[0]}")"), with realpath or $0 as variants.
SCRIPT_DIR_VALUE = re.compile(
    r'\$\(dirname (?:-- )?"\$\((?:readlink -f|realpath)(?: --)? '
    r'"(?:\$\{BASH_SOURCE(?:\[0\])?\}|\$BASH_SOURCE|\$\{0\}|\$0)"\)"\)'
)
# ${OVERRIDE:-value} or ${OVERRIDE-value}: the value applies only while OVERRIDE is unset.
SCRIPT_DIR_DEFAULT = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*):?-(.+)\}")
ASSIGNED_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
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
    source_reader: Callable[[str, bool, InspectionContext, list[str]], list[str] | str] | None = (
        None
    )


@dataclass
class BodyBudget:
    """Share syntax node and inline body accounting across nested files."""

    nodes: int = 0
    inline_bodies: int = 0
    peeks: int = 0


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
    moved: bool = False
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
    """Preserve the directory state at each concrete candidate's encounter."""
    if _ambiguous_reference(operand, scan):
        scan.refusal = AMBIGUOUS_SCRIPT_PATH
        return
    if _unknown_source_relative(operand, is_program, scan):
        scan.refusal = (
            "relative script path after a source the guard cannot read; use an absolute path"
        )
        return
    if _candidate_allowed(operand):
        changed = scan.dir_changed and not os.path.isabs(operand) and not operand.startswith("~")
        scan.candidates.append((operand, is_program, changed))


def _ambiguous_reference(word: str, scan: _Scan) -> bool:
    """Recognize exact simple or braced references to ambiguous assignment names."""
    return any(
        re.search(
            r"\$(?:\{" + re.escape(name) + r"\}|" + re.escape(name) + r"(?![A-Za-z0-9_]))", word
        )
        is not None
        for name in scan.ambiguous
    )


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
    if target is None or _ambiguous_reference(target, scan):
        if target is not None:
            scan.refusal = AMBIGUOUS_SCRIPT_PATH
        return None
    if _configure_reference(target):
        return target
    if _unknown_source_relative(target, False, scan):
        return "relative script path after a source the guard cannot read; use an absolute path"
    reader = scan.context.source_reader
    if not _candidate_allowed(target) or reader is None:
        _record_candidate(target, False, scan)
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
    words, idx, name = _command_info(node, raw, scan.literals)
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
    if "/" in word or _program_name_allowed(word) or _ambiguous_reference(word, scan):
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
    """Apply the explicit grammar allow-list and push children in source order."""
    kind = node.type
    if kind == "ERROR":
        return _error_hit(node, raw)
    if kind not in STRUCTURE | ASSIGNMENTS | DATA | LEAVES | SPECIAL:
        return f"unsupported shell syntax {kind} near line {node.start_point.row + 1}"
    if kind in LEAVES:
        return None
    if kind in ("command_substitution", "process_substitution"):
        data_only = False
    hit, children = _visit_semantics(node, raw, data_only, scan)
    for i in range(len(children) - 1, -1, -1):
        child, mode = children[i]
        if not child.is_missing:
            scan.stack.append((child, raw, mode))
    return hit


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


def _settle_script_dir(node: Any, raw: bytes, scan: _Scan) -> None:
    """Resolve an own-directory name from its top-level assignment on, never before it.

    After a directory change or an unread source, with its override assigned anywhere in the
    file, or nested in a function, branch or loop, the name stays unresolved: paths built from
    it refuse as after a source the guard cannot read.
    """
    if not scan.script_dirs:
        return
    assignments = (
        [node]
        if node.type == "variable_assignment"
        else [child for child in node.named_children if child.type == "variable_assignment"]
    )
    for assignment in assignments:
        match = ASSIGNED_NAME.match(_text(assignment, raw))
        if match is None or match.group() not in scan.script_dirs:
            continue
        name = match.group()
        override = scan.script_dirs.pop(name)
        if (
            scan.moved
            or scan.source_unknown
            or (override is not None and override in scan.assigned)
        ):
            scan.literals.pop(name, None)
            scan.source_unknown = scan.dir_changed = True
        elif _top_level(assignment):
            scan.literals[name] = str(scan.context.script_dir)


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
    scan.stack.append((root, raw, False))
    for _ in range(MAX_BODY_NODES):
        if not scan.stack:
            if directory_state is not None:
                directory_state[0] = scan.dir_changed
            return None, scan.candidates, scan.cd_targets, scan.filtered
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
    return None, scan.candidates, scan.cd_targets, scan.filtered
