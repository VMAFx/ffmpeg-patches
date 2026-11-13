#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Check an upstream-bound FFmpeg branch before posting it.

Use the pre-commit configuration, commit-message checker and patcheck from
--base, never a branch's replacement. Every step reports its own return code;
patcheck's added-line hints fail only for the documented category allow-list,
even when its process exits zero. Other added-line hints, including unknown
categories, are reported for review. Hook edits fail inside a disposable worktree; dirty trees
are refused. --head must be checked out and --base must be its ancestor.

The added C/header line rules implement developer.texi, "Code formatting
conventions": no TAB characters, no trailing whitespace, and K&R braces.
Brace detection deliberately covers only single-line function declarations and
single-line control headers followed by a standalone brace. Comments, literals,
preprocessor directives, multiline signatures and ambiguous constructs are not
interpreted as declarations. Untouched whitespace violations in changed C/header
files are listed as pre-existing, without failing. This is a mechanical subset,
not a replacement for upstream review or the remaining developer.texi rules.

The fate-deps step checks lavfi video FATE registrations for LAVFI_INDEV and
WRAPPED_AVFRAME_DECODER: lavfi emits wrapped_avframe video packets, so an encoder
alone cannot satisfy decoding. Templates are reported without expansion,
macro argument transforms such as `$(2:%=%_DECODER)` are not expanded,
multi-file variables are reported unresolved, and audio PCM decoders are not checked.
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import shutil
import signal
import subprocess  # nosec B404 - explicit argv, process-group cleanup, bounded deadlines.
import sys
import tempfile
import time
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path

# Ensure repository root is on sys.path for test/tool imports
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.process_group import kill_process_group  # noqa: E402

PRE_COMMIT = "pre-commit"
ALLOWED_EXECUTABLES = frozenset({"git", "pre-commit", "bash"})
# Match tests.helpers' repository isolation without importing test dependencies.
GIT_ENVIRONMENT_NAMES = (
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_CEILING_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_CONFIG",
    "GIT_CONFIG_COUNT",
    "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_NOSYSTEM",
    "GIT_CONFIG_PARAMETERS",
    "GIT_CONFIG_SYSTEM",
    "GIT_DIR",
    "GIT_DISCOVERY_ACROSS_FILESYSTEM",
    "GIT_GRAFT_FILE",
    "GIT_IMPLICIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_NAMESPACE",
    "GIT_NO_REPLACE_OBJECTS",
    "GIT_OBJECT_DIRECTORY",
    "GIT_PREFIX",
    "GIT_REPLACE_REF_BASE",
    "GIT_SHALLOW_FILE",
    "GIT_WORK_TREE",
)
GIT_CONFIG_PREFIXES = ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")
DIFF_COMMON = ("diff", "--no-ext-diff", "--no-textconv", "--no-color", "--no-renames")
MAX_PATHS = 2000
RUN_TIMEOUT = 1800
CLEANUP_TIMEOUT = 30
REAP_TIMEOUT = 10
_DEADLINE: ContextVar[float | None] = ContextVar("deadline", default=None)
CONFIG = ".forgejo/pre-commit/config.yaml"
HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
REFERENCE = re.compile(r"^[^:\n]+?[:-]\d+[:-](.*)$")
FUNCTION = re.compile(r"^(?:[A-Za-z_]\w*[ *]+)+[A-Za-z_]\w*\([^();{}]*\)\s*\{")
CONTROL = re.compile(r"(?:}\s*)?(?:(?:else\s+)?(?:if|for|while|switch)\s*\(.*\)|else|do)")
C_NONCODE = re.compile(
    r'/\*.*?\*/|//[^\n]*|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\''
    r"|^[ \t]*\#[^\n]*(?:\n(?<=\\\n)[^\n]*)*",
    re.DOTALL | re.MULTILINE,
)
# Exact upstream category headers allowed to block added lines. Keep this an
# allow-list: heuristic and newly introduced categories require human review.
# Whitespace and K&R formatting are enforced separately by developer.texi rules.
PATCHECK_BLOCKING_CATEGORIES = frozenset(
    {
        "double ;",
        "common typos",
        "useless casts",
        "useless #if",
        "useless if()",
        "useless & [0]",
        "empty comment",
        "inconsistent doxygen syntax",
        "Misformatted doxygen comment",
        "duplicate word",
        "can be simplified to ++",
        "can be simplified to --",
        "Please use av_malloc",
        "Please use av_assert0, av_assert1 or av_assert2",
        "missing const for pix_fmts array",
        "missing const for sample_fmts array",
        "missing const for supported_framerates array",
        "missing const for channel_layouts array",
        "missing NULL_IF_CONFIG_SMAL",
    }
)


@dataclass(frozen=True, slots=True)
class RunResult:
    """One bounded command's arguments, exit status and captured output."""

    args: tuple[str, ...]
    returncode: int
    stdout: str


@dataclass
class Step:
    """One independently collected check, with an effective exit status."""

    name: str
    rc: int
    output: str


def _validate_executable(argv: list[str]) -> None:
    if not argv or Path(argv[0]).name not in ALLOWED_EXECUTABLES:
        raise ValueError(f"executable refused: {argv[0] if argv else '<empty>'!r}")


def _remaining(timeout: float) -> float:
    deadline = _DEADLINE.get()
    if deadline is None:
        return timeout
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RuntimeError("overall deadline exhausted")
    return min(timeout, remaining)


def _reap_timeout(
    process: subprocess.Popen[bytes], argv: list[str], partial_output: bytes | None = None
) -> RunResult:
    """Kill a timed-out process group and bound the remaining pipe drain."""
    kill_process_group(process)
    try:
        output, _ = process.communicate(timeout=_remaining(REAP_TIMEOUT))
    except subprocess.TimeoutExpired as error:
        output = error.output if error.output is not None else partial_output
        text = (output or b"").decode("utf-8", errors="replace")
        return RunResult(
            tuple(argv), 124, text + "\ncommand timed out (descendant kept the pipe open)\n"
        )
    text = output.decode("utf-8", errors="replace")
    if process.returncode == -signal.SIGKILL:
        return RunResult(tuple(argv), 124, text + "\ncommand timed out\n")
    return RunResult(tuple(argv), process.returncode, text)


def _run(argv: list[str], cwd: Path, data: str | None = None, timeout: float = 60) -> RunResult:
    """Run an allowed tool with isolated Git state and bounded communication."""
    try:
        _validate_executable(argv)
    except ValueError as error:
        return RunResult(tuple(argv), 2, str(error))
    if _DEADLINE.get() is not None:
        timeout = min(timeout, _remaining(timeout + REAP_TIMEOUT) - REAP_TIMEOUT)
        if timeout <= 0:
            raise RuntimeError("overall deadline exhausted (reap budget reserved)")
    environment = {
        name: value
        for name, value in os.environ.items()
        if name not in GIT_ENVIRONMENT_NAMES and not name.startswith(GIT_CONFIG_PREFIXES)
    }
    environment["PRE_COMMIT_NO_CONCURRENCY"] = "1"
    with subprocess.Popen(  # nosec B603 - _validate_executable enforces the tool allow-list.
        # semgrep-reason (expires 2026-11-14): allow-listed argv[0], verified refs, no shell
        # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-tainted-env-args.dangerous-subprocess-use-tainted-env-args  # noqa: E501
        argv,
        cwd=cwd,
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        shell=False,
    ) as process:
        try:
            output, _ = process.communicate(
                None if data is None else data.encode("utf-8"), timeout=timeout
            )
            return RunResult(
                tuple(argv), process.returncode, output.decode("utf-8", errors="replace")
            )
        except subprocess.TimeoutExpired as error:
            return _reap_timeout(process, argv, error.output)


def _git(tree: Path, *args: str) -> str:
    """Return Git output or raise an error with the command's captured result."""
    result: RunResult = _run(["git", *args], tree)
    if result.returncode:
        raise RuntimeError(f"git {shlex.join(args)}: rc={result.returncode}\n{result.stdout}")
    return result.stdout


def _status(tree: Path) -> str:
    return _git(tree, "status", "--porcelain=v1", "-z", "--untracked-files=all")


def _prepare(tree: Path, base: str, head: str) -> tuple[str, str]:
    root = Path(_git(tree, "rev-parse", "--show-toplevel").strip()).resolve()
    if root != tree:
        raise RuntimeError("--tree must name the worktree root")
    if _status(tree):
        raise RuntimeError("dirty tree refused (tracked, staged or untracked files); no stash used")
    refs = [
        _git(tree, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}").strip()
        for ref in (base, head, "HEAD")
    ]
    if refs[1] != refs[2]:
        raise RuntimeError("--head must be the checked-out HEAD; checkout that revision first")
    _git(tree, "merge-base", "--is-ancestor", refs[0], refs[1])
    _changed_paths(tree, refs[0], refs[1])
    return refs[0], refs[1]


def _base_file(tree: Path, base: str, name: str, scratch: Path) -> Path:
    target = scratch / Path(name).name
    target.write_text(_git(tree, "show", f"{base}:{name}"), encoding="utf-8")
    target.chmod(0o700)
    return target


def _pre_commit() -> str:
    """Find pre-commit on PATH once per step."""
    executable = shutil.which(PRE_COMMIT)
    if not executable:
        raise RuntimeError("pre-commit not found: install pre-commit on PATH")
    return str(Path(executable).absolute())


def _precommit(tree: Path, base: str, head: str, scratch: Path) -> Step:
    """Run the resolved pre-commit in a disposable worktree and discard edits."""
    executable = _pre_commit()
    config = _base_file(tree, base, CONFIG, scratch)
    argv = [executable, "run", "-c", str(config), "--from-ref", base, "--to-ref", head]
    worktree = scratch / "hook-tree"
    deadline = _DEADLINE.get()
    # Reserve part of the overall budget for removing the disposable worktree.
    token = _DEADLINE.set(None if deadline is None else deadline - CLEANUP_TIMEOUT)
    try:
        _git(tree, "worktree", "add", "--detach", str(worktree), head)
        result = _run(argv, worktree, timeout=600)
        changed = bool(_status(worktree))
    finally:
        _DEADLINE.reset(token)
        try:
            if worktree.exists():
                _git(tree, "worktree", "remove", "--force", str(worktree))
        finally:
            _git(tree, "worktree", "prune")
    output = f"$ {shlex.join(argv)}\ncommand rc: {result.returncode}\n{result.stdout}"
    if changed:
        output += "\nfixer modified files: failure; disposable worktree discarded\n"
    return Step("pre-commit", result.returncode or int(changed), output)


def _commit_messages(tree: Path, base: str, head: str, scratch: Path) -> Step:
    tool = _base_file(tree, base, "tools/check_commit_msg.sh", scratch)
    argv = ["bash", str(tool), f"{base}..{head}"]
    result = _run(argv, tree)
    return Step("commit-msg", result.returncode, f"$ {shlex.join(argv)}\n{result.stdout}")


def _patcheck_hints(output: str) -> tuple[int, str]:
    """Block allow-listed added-line categories; retain other hints for review."""
    findings, reports, category = 0, [], "unknown"
    for line in ANSI.sub("", output).splitlines():
        match = REFERENCE.match(line)
        source = match[1] if match and not line.startswith("+") else line
        added = source.startswith("+") and not source.startswith("+++ ")
        if added:
            blocking = category in PATCHECK_BLOCKING_CATEGORIES
            findings += int(blocking)
            label = "finding" if blocking else "hint (review)"
            reports.append(f"{label}: {category}: {line}")
        else:
            if not match and line.strip() and not line.startswith(("+", "-", " ", "\t")):
                category = line.strip()
            reports.append(f"informational: {line}")
    return int(findings > 0), "\n".join(reports)


def _patcheck(tree: Path, base: str, head: str, scratch: Path) -> Step:
    tool = _base_file(tree, base, "tools/patcheck", scratch)
    patch = _git(tree, "format-patch", "--stdout", f"{base}..{head}")
    result = _run(["bash", str(tool)], scratch, patch)
    rc, output = _patcheck_hints(result.stdout)
    return Step("patcheck", result.returncode or rc, f"command rc: {result.returncode}\n{output}")


def _added_lines(patch: str) -> set[int]:
    """Collect added physical line numbers from zero-context diff hunks."""
    added = set()
    for line in patch.splitlines():
        match = HUNK.match(line)
        if match:
            start, count = int(match[1]), int(match[2] or "1")
            added.update(range(start, start + count))
    return added


def _code_lines(source: str) -> list[str]:
    return C_NONCODE.sub(lambda match: re.sub(r"[^\n]", " ", match[0]), source).split("\n")


def _line_rules(raw: str, code: str, previous: str) -> list[str]:
    rules = []
    if "\t" in raw:
        rules.append("TAB character")
    if raw.rstrip(" \t\r\v\f") != raw:
        rules.append("trailing whitespace")
    if FUNCTION.match(code):
        rules.append("K&R function opening brace must be on its own line")
    if code.strip() == "{" and CONTROL.fullmatch(previous):
        rules.append("K&R control opening brace must be on the header line")
    return rules


def _file_rules(path: str, source: str, added: set[int]) -> tuple[int, list[str]]:
    lines, code = source.split("\n"), _code_lines(source)
    findings, reports, previous = 0, [], ""
    for number, (raw, clean) in enumerate(zip(lines, code, strict=True), 1):
        rules = _line_rules(raw, clean, previous)
        for rule in rules:
            if number in added:
                findings += 1
                reports.append(f"finding: {path}:{number}: {rule}")
            elif rule in ("TAB character", "trailing whitespace"):
                reports.append(f"pre-existing (untouched): {path}:{number}: {rule}")
        if clean.strip():
            previous = clean.strip()
    return findings, reports


def _changed_paths(
    tree: Path, base: str, head: str, *patterns: str, added_or_modified: bool = False
) -> list[str]:
    names = _git(
        tree,
        "diff",
        "--no-ext-diff",
        "--no-textconv",
        "--no-renames",
        *(["--diff-filter=AM"] if added_or_modified else []),
        "--name-only",
        "-z",
        base,
        head,
        "--",
        *patterns,
    ).split("\0")
    paths = list(filter(None, names))
    if len(paths) > MAX_PATHS:
        raise RuntimeError(f"changed paths exceed MAX_PATHS={MAX_PATHS}: {len(paths)}")
    return paths


def _mechanical(tree: Path, base: str, head: str) -> Step:
    """Check added C and header lines against mechanical formatting rules."""
    names = _changed_paths(tree, base, head, "*.c", "*.h", added_or_modified=True)
    findings, reports = 0, []
    for name in names:
        patch = _git(tree, *DIFF_COMMON, "-U0", base, head, "--", name)
        source = _git(tree, "show", f"{head}:{name}")
        count, lines = _file_rules(name, source, _added_lines(patch))
        findings += count
        reports.extend(lines)
    reports.append(f"{findings} added-line finding(s); developer.texi: Code formatting conventions")
    return Step("developer.texi", int(findings > 0), "\n".join(reports))


FATE_PATTERNS = (
    ("variable", re.compile(r"^([A-Z][A-Z0-9_]*)\s*(\+=|:=|=)\s*(.*)$")),
    ("registration", re.compile(r"^([A-Z][A-Z0-9_]*)-(\$\(.+?|yes)\s*\+=\s*(.*)$")),
    ("command", re.compile(r"^(.+?)\s*:\s*CMD\s*=\s*(.*)$")),
    ("open", re.compile(r"^(?:ifeq|ifneq|ifdef|ifndef)\b.*$")),
    ("else", re.compile(r"^else\b.*$")),
    ("close", re.compile(r"^endif\s*(?:#.*)?$")),
    ("define", re.compile(r"^define\s+\S+.*$")),
    ("endef", re.compile(r"^endef\s*(?:#.*)?$")),
)
FATE_VARIABLE = re.compile(r"\$\(([A-Z][A-Z0-9_]*)\)")
FATE_TOKEN_BUDGET = 4096
FATE_CHAR_BUDGET = 65536
LAVFI_FORMAT = re.compile(r"(?:^|\s)-f\s+lavfi(?=\s|$)")
LAVFI_OPTION = re.compile(r"^-[\w:]+$")
LAVFI_NAME = re.compile(r"""^["']?([a-z0-9_]+)(?=[=,;\s"']|$)""")
FATE_REQUIRED = ("LAVFI_INDEV", "WRAPPED_AVFRAME_DECODER")


@dataclass(frozen=True)
class FateLine:
    """A logical make line and its inclusive physical source range."""

    text: str
    first: int
    last: int


@dataclass(frozen=True)
class FateToken:
    """One target token with the physical lines that contributed it."""

    text: str
    lines: frozenset[int]


@dataclass(frozen=True)
class FateEntry:
    """One command or registration with target provenance and dependency tokens."""

    line: FateLine
    target: FateToken
    deps: frozenset[str] = frozenset()
    command: str = ""
    provenance: frozenset[int] = frozenset()
    classified: bool = True


@dataclass
class FateFile:
    """File-local variable state, registrations, commands and unexpanded templates."""

    variables: dict[str, list[FateToken]] = field(default_factory=dict)
    registrations: dict[str, list[FateEntry]] = field(default_factory=dict)
    commands: dict[str, list[FateEntry]] = field(default_factory=dict)
    templates: list[FateLine] = field(default_factory=list)
    simple: set[str] = field(default_factory=set)
    overflows: set[int] = field(default_factory=set)
    macros: frozenset[str] = frozenset()
    needs_macros: bool = False


@dataclass(frozen=True)
class FateContext:
    """Inject base source, deletion positions and macros into the pure file checker."""

    base: str | None = None
    deleted: frozenset[int] = frozenset()
    macros: frozenset[str] = frozenset()


@dataclass
class FateReport:
    """Accumulate independent file diagnostics and lavfi classification counts."""

    checked: int = 0
    audio: int = 0
    unclassified: int = 0
    findings: int = 0
    unresolved: int = 0
    lines: list[str] = field(default_factory=list)


@dataclass
class FateCondition:
    """One conditional branch with explicit polarity and source provenance."""

    deps: frozenset[str]
    positive: bool | None
    lines: frozenset[int]
    active: bool = True


def _fate_uncomment(text: str) -> str:
    """Strip only make comments whose hash is preceded by an even backslash run."""
    slashes = 0
    for index, char in enumerate(text):
        if char == "#" and slashes % 2 == 0:
            return text[:index]
        slashes = slashes + 1 if char == "\\" else 0
    return text


def _fate_lines(source: str) -> list[FateLine]:
    """Join continuations while retaining both physical endpoints."""
    lines, parts, first = [], [], 1
    for number, text in enumerate(source.splitlines(), 1):
        if not parts:
            first = number
        continued = text.endswith("\\")
        parts.append(text[:-1] if continued else text)
        if not continued:
            lines.append(FateLine(_fate_uncomment(" ".join(parts)).strip(), first, number))
            parts = []
    if parts:
        lines.append(FateLine(_fate_uncomment(" ".join(parts)).strip(), first, number))
    return lines


def _fate_kind(line: FateLine) -> tuple[str, tuple[str, ...]]:
    """Classify a logical line using only the supported positive patterns."""
    for kind, pattern in FATE_PATTERNS:
        match = pattern.fullmatch(line.text)
        if match:
            return kind, match.groups()
    return "", ()


def _fate_characters(tokens: list[FateToken]) -> int:
    """Count token characters including the spaces needed to join them."""
    return sum(len(token.text) for token in tokens) + max(0, len(tokens) - 1)


def _fate_replace(
    token: FateToken, variables: dict[str, list[FateToken]], remaining: int, characters: int
) -> tuple[str, frozenset[int], int] | None:
    """Substitute embedded references only after checking token and character budgets."""
    parts, lines, cursor, cost, size = [], token.lines, 0, 1, 0
    for match in FATE_VARIABLE.finditer(token.text):
        replacements = variables.get(match[1])
        if replacements is None:
            continue
        cost += max(1, len(replacements)) - 1
        size += match.start() - cursor + _fate_characters(replacements)
        if cost > remaining or size > characters:
            return None
        parts.extend((token.text[cursor : match.start()], " ".join(t.text for t in replacements)))
        lines |= frozenset().union(*(t.lines for t in replacements))
        cursor = match.end()
    if cost > remaining or size + len(token.text) - cursor > characters:
        return None
    parts.append(token.text[cursor:])
    return "".join(parts), lines, cost


def _fate_expand_token(
    token: FateToken, variables: dict[str, list[FateToken]], remaining: int, characters: int
) -> tuple[list[FateToken], int] | None:
    """Expand one token's whole or embedded references within both remaining budgets."""
    match = FATE_VARIABLE.fullmatch(token.text)
    replacements = variables.get(match[1]) if match else None
    if replacements is not None:
        if len(replacements) > remaining or _fate_characters(replacements) > characters:
            return None
        return [FateToken(t.text, t.lines | token.lines) for t in replacements], len(replacements)
    result = _fate_replace(token, variables, remaining, characters)
    if result is None:
        return None
    text, lines, cost = result
    expanded: list[FateToken] = []
    for item in re.finditer(r"\S+", text):
        if len(expanded) == remaining:
            return None
        expanded.append(FateToken(item[0], lines))
    return expanded, cost


def _fate_expand_pass(
    tokens: list[FateToken], variables: dict[str, list[FateToken]]
) -> list[FateToken] | None:
    """Bound each expansion pass by tokens and characters while retaining provenance."""
    expanded: list[FateToken] = []
    spent = characters = 0
    for token in tokens:
        result = _fate_expand_token(
            token, variables, FATE_TOKEN_BUDGET - spent, FATE_CHAR_BUDGET - characters
        )
        if result is None:
            return None
        replacements, cost = result
        spent += cost
        characters += _fate_characters(replacements) + bool(expanded and replacements)
        if characters > FATE_CHAR_BUDGET:
            return None
        expanded.extend(replacements)
    return expanded


def _fate_expand(
    tokens: list[FateToken],
    variables: dict[str, list[FateToken]],
    overflows: set[int] | None = None,
) -> list[FateToken]:
    """Expand whole and embedded variables within eight passes and both size budgets."""
    for _ in range(8):
        expanded = _fate_expand_pass(tokens, variables)
        if expanded is None or any(t.text == "$(EXPANSION_OVER_BUDGET)" for t in tokens):
            if overflows is not None:
                overflows.update(number for token in tokens for number in token.lines)
            break
        if expanded == tokens:
            break
        tokens = expanded
    return tokens


def _fate_tokens(text: str, line: FateLine) -> list[FateToken]:
    """Attach physical provenance to make tokens within the expansion budget."""
    tokens: list[FateToken] = []
    lines = frozenset(range(line.first, line.last + 1))
    for match in re.finditer(r"\S+", text):
        if len(tokens) == FATE_TOKEN_BUDGET:
            return [FateToken("$(EXPANSION_OVER_BUDGET)", lines)]
        tokens.append(FateToken(match[0], lines))
    return tokens


def _fate_dependencies(text: str) -> frozenset[str]:
    """Extract only literal uppercase dependency words outside make expressions."""
    plain, depth = [], 0
    for char in text:
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        plain.append(char if depth == 0 and char not in "$()" else " ")
    return frozenset(
        word.removeprefix("CONFIG_")
        for word in "".join(plain).replace(",", " ").split()
        if re.fullmatch(r"[A-Z][A-Z0-9_]*", word)
    )


def _fate_arguments(text: str) -> list[str]:
    """Split make arguments on top-level commas with one bounded character walk."""
    parts, start, depth = [], 0, 0
    for index, char in enumerate(text):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append(text[start:index].strip())
            start = index + 1
        if depth < 0:
            return []
    return [*parts, text[start:].strip()] if depth == 0 else []


def _fate_guard(text: str, macros: frozenset[str]) -> tuple[frozenset[str], bool]:
    """Grant only positive ALLYES wrappers, ignoring conditions in supported if forms."""
    for _ in range(8):
        if text == "yes":
            return frozenset(), True
        if not (text.startswith("$(") and text.endswith(")")):
            break
        args = _fate_arguments(text[2:-1])
        if not args:
            break
        call = re.fullmatch(r"call\s+([A-Z][A-Z0-9_]*)", args[0])
        if call and call[1] in {"ALLYES", *macros}:
            words = " ".join(args[1:])
            return _fate_dependencies(words), True
        if args[0].startswith("if ") and len(args) == 3 and args[1] == "no":
            text = args[2]
        elif args[0].startswith("if ") and len(args) == 2:
            text = args[1]
        else:
            break
    return frozenset(), False


def _fate_condition(line: FateLine) -> FateCondition:
    """Recognize only the six allow-listed make condition polarities."""
    positive: bool | None = None
    variable = ""
    direct = re.fullmatch(r"(ifdef|ifndef)\s+([A-Z][A-Z0-9_]*)", line.text)
    compare = re.fullmatch(
        r"(ifeq|ifneq)\s*\(\s*\$\(([A-Z][A-Z0-9_]*)\)\s*,\s*(yes)?\s*\)", line.text
    )
    if direct:
        variable, positive = direct[2], direct[1] == "ifdef"
    elif compare:
        variable = compare[2]
        positive = (compare[1] == "ifeq") == (compare[3] == "yes")
    return FateCondition(
        _fate_dependencies(variable), positive, frozenset(range(line.first, line.last + 1))
    )


def _fate_scope(conditions: list[list[FateCondition]]) -> tuple[frozenset[str], frozenset[int]]:
    """Combine current branch dependencies and enclosing conditional provenance."""
    frames = [frame for group in conditions for frame in group]
    deps = frozenset().union(*(f.deps for f in frames if f.positive == f.active))
    return deps, frozenset().union(*(f.lines for f in frames))


def _fate_branch(conditions: list[list[FateCondition]], kind: str, line: FateLine) -> None:
    """Update branch groups so a single endif closes an entire else-if chain."""
    if kind == "open":
        conditions.append([_fate_condition(line)])
    elif kind == "close" and conditions:
        conditions.pop()
    elif kind == "else" and conditions:
        conditions[-1][-1].active = False
        tail = line.text[4:].strip()
        if tail:
            conditions[-1].append(_fate_condition(FateLine(tail, line.first, line.last)))


def _fate_freeze(token: FateToken, variables: dict[str, list[FateToken]]) -> FateToken:
    """Freeze undefined references while retaining unresolved target syntax and provenance."""
    text = FATE_VARIABLE.sub(
        lambda match: match[0] if match[1] in variables else f"$(:={match[1]})", token.text
    )
    return FateToken(text, token.lines)


def _fate_assign(parsed: FateFile, line: FateLine, groups: tuple[str, ...]) -> None:
    """Expand simple values now, freezing undefined $(NAME) references as $(:=NAME)."""
    name, operator, value = groups
    if operator != "+=":
        parsed.simple.discard(name)
    if operator == ":=":
        parsed.simple.add(name)
    tokens = _fate_tokens(value, line)
    if name in parsed.simple:
        tokens = _fate_expand(tokens, parsed.variables, parsed.overflows)
        tokens = [_fate_freeze(token, parsed.variables) for token in tokens]
    previous = parsed.variables.get(name, []) if operator == "+=" else []
    characters = _fate_characters(previous) + _fate_characters(tokens) + bool(previous and tokens)
    if len(previous) + len(tokens) > FATE_TOKEN_BUDGET or characters > FATE_CHAR_BUDGET:
        parsed.overflows.add(line.first)
        tokens = [FateToken(f"$({name})", frozenset({line.first}))]
        previous = []
    parsed.variables[name] = previous + tokens


def _fate_record(
    parsed: FateFile,
    line: FateLine,
    statement: tuple[str, tuple[str, ...]],
    scope: tuple[frozenset[str], frozenset[int]],
) -> None:
    """Record every local command variant and registration with dependency provenance."""
    kind, groups = statement
    if kind == "variable":
        _fate_assign(parsed, line, groups)
    elif kind in ("registration", "command"):
        targets = groups[2] if kind == "registration" else groups[0]
        guard = _fate_expand(_fate_tokens(groups[1], line), parsed.variables, parsed.overflows)
        guard_text = " ".join(t.text for t in guard)
        calls = re.findall(r"\$\(call\s+([A-Z][A-Z0-9_]*)", guard_text)
        if kind == "registration" and any(name != "ALLYES" for name in calls):
            parsed.needs_macros = True
        dependencies, classified = _fate_guard(guard_text, parsed.macros)
        provenance = scope[1] | frozenset().union(*(t.lines for t in guard))
        for target in _fate_expand(_fate_tokens(targets, line), parsed.variables, parsed.overflows):
            if not (target.text.startswith("fate-") or "$(" in target.text):
                continue
            entry = FateEntry(
                line,
                target,
                scope[0] | dependencies,
                groups[1] if kind == "command" else "",
                provenance,
                classified,
            )
            entries = parsed.registrations if kind == "registration" else parsed.commands
            entries.setdefault(target.text, []).append(entry)


def _fate_parse(source: str, macros: frozenset[str] = frozenset()) -> FateFile:
    """Parse bounded file-local make statements without executing templates or code."""
    parsed, conditions, depth = FateFile(macros=macros), [], 0
    for line in _fate_lines(source):
        statement = _fate_kind(line)
        kind, groups = statement
        if kind == "define":
            depth += 1
        elif kind == "endef":
            depth = max(0, depth - 1)
        elif depth:
            if kind == "command" and LAVFI_FORMAT.search(groups[1]):
                parsed.templates.append(line)
        elif kind in ("open", "close", "else"):
            _fate_branch(conditions, kind, line)
        else:
            _fate_record(parsed, line, statement, _fate_scope(conditions))
    return parsed


def _fate_touched(entry: FateEntry, added: set[int]) -> bool:
    """Check registration lines plus target, variable and conditional provenance."""
    return bool(
        added.intersection(range(entry.line.first, entry.line.last + 1))
        or added & (entry.target.lines | entry.provenance)
    )


def _fate_video(
    path: str,
    command: FateEntry,
    registrations: list[FateEntry],
    touched: set[int],
    report: FateReport,
) -> None:
    """Assess missing dependencies independently for each registration variant."""
    name = command.target.text
    if not registrations and _fate_touched(command, touched):
        report.lines.append(
            f"informational: {path}:{command.line.first}: {name}: registration not found"
        )
    for registration in registrations:
        missing = " ".join(dep for dep in FATE_REQUIRED if dep not in registration.deps)
        if missing:
            changed = _fate_touched(registration, touched)
            report.findings += int(changed)
            label = "finding" if changed else "pre-existing (untouched)"
            report.lines.append(
                f"{label}: {path}:{registration.line.first}: {name}: "
                f"lavfi video input needs {missing} in its registration"
            )


def _lavfi_words(command: str) -> list[str]:
    """Join adjacent shell fragments with one linear, non-backtracking character walk."""
    words, parts, quote, escaped, active = [], [], "", False, False
    for char in command:
        if escaped:
            parts.append(char)
            escaped = False
        elif char == "\\" and quote != "'":
            escaped, active = True, True
        elif quote:
            if char == quote:
                quote = ""
            else:
                parts.append(char)
        elif char in "\"'":
            quote, active = char, True
        elif char.isspace():
            if active:
                words.append("".join(parts))
                parts, active = [], False
        else:
            parts.append(char)
            active = True
    if active:
        words.append("".join(parts))
    return words


def _lavfi_skip(words: list[str], start: int) -> tuple[int, str | None]:
    """Walk input options, consuming values only when the next word is not an option."""
    skip = start
    for index in range(start, len(words)):
        if index < skip:
            continue
        word = words[index]
        if word == "-i":
            if index + 1 < len(words):
                return index + 2, words[index + 1]
            return index + 1, None
        if word == "-f" or not LAVFI_OPTION.fullmatch(word):
            return index, None
        skip = index + 1
        if skip < len(words) and not words[skip].startswith("-"):
            skip += 1
    return len(words), None


def _lavfi_graph(graph: str) -> list[str]:
    """Collect the first filter of every chain that has no input label."""
    names = []
    for raw in graph.split(";"):
        chain = raw.lstrip()
        if chain.startswith("["):
            continue
        match = LAVFI_NAME.match(chain)
        names.append(match[1] if match else "<unresolved>")
    return names


def _lavfi_inputs(command: str) -> list[str]:
    """Collect all unlabelled lavfi chain sources with a linear shell-word walk."""
    words = _lavfi_words(command)
    names: list[str] = []
    cursor = 0
    for index in range(len(words)):
        if index < cursor:
            continue
        if words[index] == "-f" and index + 1 < len(words) and words[index + 1] == "lavfi":
            stop, graph = _lavfi_skip(words, index + 2)
            if graph is not None:
                names.extend(_lavfi_graph(graph))
                cursor = stop
    return names


def _fate_kinds(command: FateEntry, sources: dict[str, str]) -> set[str]:
    """Classify every source in one command variant without changing report counts."""
    return {sources.get(name, "unknown") for name in _lavfi_inputs(command.command)}


def _fate_command(
    path: str, command: FateEntry, sources: dict[str, str], report: FateReport
) -> bool:
    """Classify every lavfi chain, counting audio-only and unclassified variants."""
    names = _lavfi_inputs(command.command)
    kinds = {sources.get(name, "unknown") for name in names}
    if "video" in kinds:
        return True
    if not kinds or kinds - {"video", "audio"}:
        report.unclassified += 1
        report.lines.append(
            f"informational: {path}:{command.line.first}: {command.target.text}: "
            f"unclassified lavfi source: {', '.join(names) or '<unresolved>'}"
        )
    if kinds == {"audio"}:
        report.audio += 1
    return False


def _fate_target_diagnostics(
    path: str, entry: FateEntry, touched: set[int], report: FateReport, registration: bool
) -> None:
    """Report one unclassified registration or touched unresolved target."""
    name = entry.target.text
    if registration and not entry.classified:
        report.lines.append(
            f"informational: {path}:{entry.line.first}: {name}: registration guard not classified"
        )
    if "$(" in name and _fate_touched(entry, touched):
        report.unresolved += 1
        report.lines.append(f"informational: {path}:{entry.line.first}: unresolved target {name}")


def _fate_diagnostics(path: str, parsed: FateFile, touched: set[int], report: FateReport) -> None:
    """Report touched unresolved targets, templates and bounded expansion failures."""
    for line in parsed.templates:
        if touched.intersection(range(line.first, line.last + 1)):
            report.lines.append(f"informational: {path}:{line.first}: template not checked")
    for number in sorted(parsed.overflows):
        report.lines.append(
            f"informational: {path}:{number}: expansion over {FATE_TOKEN_BUDGET} tokens "
            f"or {FATE_CHAR_BUDGET} characters"
        )
    for entries in (parsed.registrations, parsed.commands):
        for variants in entries.values():
            for entry in variants:
                _fate_target_diagnostics(
                    path, entry, touched, report, entries is parsed.registrations
                )


def _fate_check(
    path: str,
    parsed: FateFile,
    added: set[int],
    sources: dict[str, str],
    base: FateFile | None = None,
) -> FateReport:
    """Check each registration against all video variants and its own touched provenance."""
    report = FateReport()
    _fate_diagnostics(path, parsed, added, report)
    for name, variants in parsed.commands.items():
        if "$(" in name:
            continue
        video = [
            entry
            for entry in variants
            if LAVFI_FORMAT.search(entry.command) and _fate_command(path, entry, sources, report)
        ]
        if not video:
            continue
        report.checked += 1
        old_video = base is not None and any(
            "video" in _fate_kinds(entry, sources) for entry in base.commands.get(name, [])
        )
        newly_video = not old_video and any(_fate_touched(entry, added) for entry in video)
        registrations = parsed.registrations.get(name, [])
        touched = added | ({entry.line.first for entry in registrations} if newly_video else set())
        _fate_video(path, video[0], registrations, touched, report)
    return report


def _fate_file(
    path: str,
    source: str,
    added: set[int],
    sources: dict[str, str],
    context: FateContext | None = None,
) -> FateReport:
    """Expose pure head/base parsing, injected catalogs and deletion-aware checking."""
    context = context or FateContext()
    base = _fate_parse(context.base, context.macros) if context.base is not None else None
    return _fate_check(
        path, _fate_parse(source, context.macros), added | context.deleted, sources, base
    )


def _fate_summary(report: FateReport, rc: int = 0) -> Step:
    """Finish every fate-deps outcome with its classification and finding totals."""
    summary = (
        f"{report.checked} lavfi video test(s) checked, {report.audio} audio skipped, "
        f"{report.unclassified} unclassified, {report.unresolved} unresolved, "
        f"{report.findings} finding(s); "
        "FATE dependencies: wrapped_avframe decoder"
    )
    return Step("fate-deps", rc or int(report.findings > 0), "\n".join([*report.lines, summary]))


def _fate_sources(source: str) -> dict[str, str]:
    """Read source classifications solely from upstream filter declarations."""
    kinds = {"v": "video", "a": "audio", "av": "mixed"}
    return {
        name: kinds[kind]
        for kind, name in re.findall(
            r"extern\s+const\s+FFFilter\s+ff_(v|a|av)src_(\w+)\s*;", source
        )
    }


def _fate_macros(source: str) -> frozenset[str]:
    """Read final direct ALLYES wrapper definitions from head tests/Makefile."""
    names = set()
    for line in _fate_lines(source):
        kind, groups = _fate_kind(line)
        if kind != "variable":
            continue
        name, operator, value = groups
        names.discard(name)
        if operator == "=" and value.startswith("$(") and value.endswith(")"):
            args = _fate_arguments(value[2:-1])
            if args and args[0] == "call ALLYES":
                names.add(name)
    return frozenset(names)


def _fate_deleted_lines(patch: str) -> set[int]:
    """Map deletion-only hunks to their new-file position, including continuations."""
    deleted = set()
    for line in patch.splitlines():
        match = HUNK.match(line)
        if match and match[2] == "0":
            deleted.add(max(1, int(match[1])))
    return deleted


def _fate_merge(report: FateReport, result: FateReport) -> None:
    """Accumulate one file's independent diagnostics and all classification totals."""
    report.checked += result.checked
    report.audio += result.audio
    report.unclassified += result.unclassified
    report.unresolved += result.unresolved
    report.findings += result.findings
    report.lines.extend(result.lines)


def _fate_deps(tree: Path, base: str, head: str) -> Step:
    """Check changed FATE files with lazily loaded head filter and macro catalogs."""
    report, sources, macros, rc = FateReport(), None, None, 0
    try:
        names = _changed_paths(tree, base, head, "tests/fate/*.mak", added_or_modified=True)
        existing = set(
            _git(tree, "ls-tree", "-r", "--name-only", base, "--", "tests/fate").splitlines()
        )
        for name in names:
            patch = _git(tree, *DIFF_COMMON, "-U0", base, head, "--", name)
            source = _git(tree, "show", f"{head}:{name}")
            old = _git(tree, "show", f"{base}:{name}") if name in existing else None
            parsed = _fate_parse(source, macros or frozenset())
            if macros is None and parsed.needs_macros:
                macros = _fate_macros(_git(tree, "show", f"{head}:tests/Makefile"))
                parsed = _fate_parse(source, macros)
            needs_sources = parsed.templates or any(
                LAVFI_FORMAT.search(entry.command)
                for variants in parsed.commands.values()
                for entry in variants
            )
            if sources is None and needs_sources:
                sources = _fate_sources(_git(tree, "show", f"{head}:libavfilter/allfilters.c"))
            touched = _added_lines(patch) | _fate_deleted_lines(patch)
            old_parsed = _fate_parse(old, macros or frozenset()) if old is not None else None
            _fate_merge(report, _fate_check(name, parsed, touched, sources or {}, old_parsed))
    except (OSError, RuntimeError, ValueError) as error:
        rc = 2
        report.lines.append(str(error))
    return _fate_summary(report, rc)


def _collect(name: str, action: Callable[[], Step]) -> Step:
    try:
        return action()
    except (OSError, RuntimeError, ValueError) as error:
        return Step(name, 2, str(error))


def _checks(tree: Path, base: str, head: str) -> list[Step]:
    """Collect every independent check even when another check fails."""
    with tempfile.TemporaryDirectory(prefix="ffmpeg-upstream-lint-") as directory:
        scratch = Path(directory)
        return [
            _collect("pre-commit", lambda: _precommit(tree, base, head, scratch)),
            _collect("commit-msg", lambda: _commit_messages(tree, base, head, scratch)),
            _collect("patcheck", lambda: _patcheck(tree, base, head, scratch)),
            _collect("developer.texi", lambda: _mechanical(tree, base, head)),
            _collect("fate-deps", lambda: _fate_deps(tree, base, head)),
        ]


def main(argv: list[str] | None = None) -> int:
    """Run all upstream checks, print their statuses, and optionally save a report."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tree", type=Path, required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    tree, base, head = args.tree.resolve(), args.base, args.head
    token = _DEADLINE.set(time.monotonic() + RUN_TIMEOUT)
    try:
        base, head = _prepare(tree, base, head)
        steps = _checks(tree, base, head)
        _remaining(0)
    except (OSError, RuntimeError, ValueError) as error:
        steps = [Step("preflight", 2, str(error))]
    finally:
        _DEADLINE.reset(token)
    report = (
        f"tree: {tree}\nbase: {base}\nhead: {head}\n\n"
        + "\n\n".join(f"step: {step.name} rc: {step.rc}\n{step.output.rstrip()}" for step in steps)
        + "\n"
    )
    print(report, end="")
    if args.report:
        try:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(report, encoding="utf-8")
        except OSError as error:
            print(f"report: {error}", file=sys.stderr)
            return 2
    return 2 if steps[0].name == "preflight" else int(any(step.rc for step in steps))


if __name__ == "__main__":
    sys.exit(main())
