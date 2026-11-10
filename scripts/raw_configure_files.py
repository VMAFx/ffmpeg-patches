# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Script file inspection and path resolution for raw configure guard.

Known limits:
Operands built from unresolved variables are not followed.
A variable assigned twice is not substituted.
Scripts written by the command are absent during inspection, which precedes execution.
A script changed between inspection and execution is not re-read.
Programs in NON_SHELL_PROGRAMS (python, perl, make, ...) starting configure are not read.
Unknown interpreters and env -S strings holding a backslash or $ are read as shell text
(fail closed).
A program is identified by its interpreter name, not by the file that name resolves to.
Code given to an allowed program in the header (python3 -c, perl -e, make -f) is not read.
Root-owned programs in directories the user cannot write are trusted.
PATH lookup uses the hook process PATH, which can differ from the session shell PATH.
After cd to an unresolved directory, relative operands are matched against known bases only.
Shell options that change globbing (nullglob, failglob) are not followed.
Readers other than cat, head, tail and line-filter sed feeding a shell are refused, not read.
"""

from __future__ import annotations

import glob
import itertools
import os
import re
import shutil
import stat
import sys
from pathlib import Path
from typing import NamedTuple

from scripts.raw_configure_body import (
    MAX_PROGRAM_PEEKS,
    MAX_SCRIPT_BYTES,
    MAX_SCRIPT_FILES,
    BodyBudget,
    InspectionContext,
    scan_body,
)
from scripts.raw_configure_words import (
    GLOB_CHARS,
    NON_SHELL_PROGRAMS,
    SHELLS,
    VERSIONED_NON_SHELL_PROGRAM,
    _program_name_allowed,
)

_PATH_PROGRAMS: dict[str, Path | None] = {}
_ENV_LONG_OPTIONS = {
    b"argv0": "value",
    b"env0-from": "value",
    b"unset": "value",
    b"chdir": "value",
    b"split-string": "split",
    b"ignore-environment": "flag",
    b"null": "flag",
    b"list-signal-handling": "flag",
    b"debug": "flag",
    b"help": "flag",
    b"version": "flag",
    b"block-signal": "optional",
    b"default-signal": "optional",
    b"ignore-signal": "optional",
}
_ENV_SPLIT_SPAN = re.compile(rb"[^ \t'\"]+|'[^']*'|\"[^\"]*\"")


def _kernel_header(head: bytes) -> tuple[bytes, bytes]:
    """Split a shebang into its interpreter and single optional kernel argument."""
    line = head.split(b"\n", 1)[0][2:].lstrip(b" \t")
    for index, char in enumerate(line):
        if char in (32, 9):
            return line[:index], line[index:].strip(b" \t")
    return line, b""


def _interpreter_kind(interpreter: str) -> str:
    """Skip only known non-shell names and their explicitly versioned forms."""
    if interpreter in SHELLS:
        return "shell"
    if interpreter in NON_SHELL_PROGRAMS or VERSIONED_NON_SHELL_PROGRAM.fullmatch(interpreter):
        return "skip"
    return "shell"


def _program_kind(head: bytes) -> str:
    """Classify a bounded program header by its interpreter or binary marker."""
    if head.startswith(b"\x7fELF"):
        return "skip"
    interpreter_bytes, argument = _kernel_header(head)
    if not head.startswith(b"#!") or not interpreter_bytes:
        return "skip" if b"\x00" in head.split(b"\n", 1)[0] else "shell"
    interpreter = os.path.basename(os.fsdecode(interpreter_bytes))
    if interpreter == "env":
        interpreter = _env_interpreter([argument] if argument else [])
    elif interpreter == "busybox":
        interpreter = next(
            (
                os.path.basename(os.fsdecode(part))
                for part in argument.split()
                if not part.startswith(b"-")
            ),
            "",
        )
    return _interpreter_kind(interpreter)


def _env_short_option(part: bytes) -> tuple[str, bytes | None] | None:
    """Parse a GNU env short cluster and retain its attached required value."""
    for index, letter in enumerate(part[1:], 2):
        if letter in b"iv0":
            continue
        if letter not in b"aCSu":
            return None
        return ("split" if letter == ord("S") else "value"), part[index:] or None
    return "", b""


def _env_long_option(part: bytes) -> tuple[str, bytes | None] | None:
    """Resolve exact or unique GNU env long names and their value conventions."""
    name, equals, value = part[2:].partition(b"=")
    if name not in _ENV_LONG_OPTIONS:
        matches = [option for option in _ENV_LONG_OPTIONS if option.startswith(name)]
        if len(matches) != 1:
            return None
        name = matches[0]
    action = _ENV_LONG_OPTIONS[name]
    if action == "flag":
        return None if equals else ("", b"")
    if action == "optional":
        return "", b""
    return action, value if equals else None


def _env_option(part: bytes, pending: list[bytes]) -> tuple[str, bytes, int] | None:
    """Parse an env option and count any separately consumed required value."""
    parsed = _env_long_option(part) if part.startswith(b"--") else _env_short_option(part)
    if parsed is None:
        return None
    action, value = parsed
    consumed = 0
    if action and value is None:
        if not pending:
            return None
        value = pending.pop()
        consumed = 1
    return action, value or b"", consumed


def _env_split_string(value: bytes) -> list[bytes] | None:
    """Split literal env words with quotes and comments, refusing unsupported syntax."""
    if b"\\" in value or b"$" in value:
        return None
    words: list[bytes] = []
    word: list[bytes] = []
    end = 0
    for match in _ENV_SPLIT_SPAN.finditer(value):
        gap = value[end : match.start()]
        if gap.strip(b" \t"):
            return None
        if gap and word:
            words.append(b"".join(word))
            word = []
        span = match.group()
        if not word and span.startswith(b"#"):
            return words
        word.append(span[1:-1] if span[:1] in (b"'", b'"') else span)
        end = match.end()
        if len(words) > 64:
            return None
    if value[end:].strip(b" \t"):
        return None
    if word:
        words.append(b"".join(word))
    return words if len(words) <= 64 else None


def _env_interpreter(parts: list[bytes]) -> str:
    """Emulate bounded GNU env options without ever following the script path."""
    pending = list(reversed(parts))
    processed = splits = 0
    options = True
    for _ in range(64):
        if not pending or processed >= 64:
            return ""
        part = pending.pop()
        processed += 1
        if options and part in (b"-", b"--"):
            options = part != b"--"
            continue
        if options and part.startswith(b"-"):
            parsed = _env_option(part, pending)
            if parsed is None:
                return ""
            action, value, consumed = parsed
            processed += consumed
            if processed > 64:
                return ""
            if action == "split":
                splits += 1
                words = _env_split_string(value)
                if words is None or splits > 8:
                    return ""
                pending.extend(reversed(words))
            continue
        options = False
        if b"=" not in part:
            program = os.path.basename(os.fsdecode(part))
            return "" if program == "env" else program
    return ""


def _path_program(name: str) -> Path | None:
    """Resolve static external names through a bounded absolute hook PATH with caching."""
    if not _program_name_allowed(name):
        return None
    if name in _PATH_PROGRAMS:
        return _PATH_PROGRAMS[name]
    entries = [
        entry for entry in os.environ.get("PATH", "").split(os.pathsep) if os.path.isabs(entry)
    ][:64]
    if not entries:
        return None
    resolved = shutil.which(name, path=os.pathsep.join(entries))
    result = Path(resolved) if resolved else None
    if len(_PATH_PROGRAMS) < 4096:
        _PATH_PROGRAMS[name] = result
    return result


def _trusted_program(path: Path) -> bool:
    """Trust only root-owned resolved programs unwritable by a non-root caller."""
    try:
        resolved = path.resolve()
        return (
            os.geteuid() != 0
            and resolved.stat().st_uid == 0
            and not os.access(resolved, os.W_OK)
            and not os.access(resolved.parent, os.W_OK)
        )
    except (OSError, RuntimeError, AttributeError):
        return False


def _peek_program(path: Path, budget: BodyBudget) -> bool | Path:
    """Bound program lookups and drop missing, trusted, non-regular or non-shell files."""
    budget.peeks += 1
    if budget.peeks > MAX_PROGRAM_PEEKS:
        return Path("more program lookups than inspect limit")
    try:
        if not path.is_file() or _trusted_program(path):
            return False
        with path.open("rb") as handle:
            return _program_kind(handle.read(4096)) == "shell"
    except OSError:
        return False


# Ensure repository root is on sys.path for test/tool imports
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


class _Candidate(NamedTuple):
    """Candidate script file operand and inspection parameters."""

    operand: str
    is_program: bool
    directory_changed: bool
    strict: bool
    peeked: bool = False


def _resolve_script_path(
    operand: str, cwd: Path, directory_changed: bool
) -> tuple[Path | None, Path | None]:
    """Resolve an operand to a concrete filesystem Path or refusal."""
    if operand == "-":
        return None, None
    expanded = os.path.expanduser(operand) if operand.startswith("~") else operand
    path_obj = Path(expanded)
    if not path_obj.is_absolute():
        if directory_changed:
            refusal = Path(
                f"{operand} (relative script path after cd or with a missing session directory; "
                "use an absolute path)"
            )
            return None, refusal
        return cwd / path_obj, None
    return path_obj, None


def _check_regular_file(
    target_path: Path, operand: str, strict: bool = True
) -> tuple[bool, Path | None]:
    """Verify target path exists and refers to a regular file after following symlinks."""
    try:
        st = target_path.stat()
    except FileNotFoundError:
        return False, None
    except OSError:
        if not strict:
            return False, None
        return False, Path(f"{operand} (unreadable script)")
    if not stat.S_ISREG(st.st_mode):
        if not strict:
            return False, None
        return False, Path(f"{operand} (not a regular file)")
    return True, None


def _read_script_bytes(
    target_path: Path, operand: str, strict: bool = True
) -> tuple[bytes | None, Path | None]:
    """Read up to MAX_SCRIPT_BYTES + 1 bytes from the target path."""
    try:
        with target_path.open("rb") as handle:
            return handle.read(MAX_SCRIPT_BYTES + 1), None
    except FileNotFoundError:
        return None, None
    except OSError:
        if not strict:
            return None, None
        return None, Path(f"{operand} (unreadable script)")


def _validate_script_bytes(
    raw: bytes, operand: str, files_read: list[int] | None, is_program: bool
) -> str | Path | None:
    """Validate script content, check limits, and return decoded text."""
    if is_program and _program_kind(raw[:4096]) != "shell":
        return None
    if files_read is not None and files_read[0] >= MAX_SCRIPT_FILES:
        return Path("configure (script file limit)")
    if len(raw) > MAX_SCRIPT_BYTES:
        return Path(f"{operand} (script larger than inspect limit)")
    if b"\x00" in raw:
        return Path(f"{operand} (binary script)")
    if files_read is not None:
        files_read[0] += 1
    return raw.decode("utf-8", errors="surrogateescape")


def _load_script_bytes(
    operand: str,
    cwd: Path,
    directory_changed: bool,
    strict: bool = True,
) -> tuple[bytes | None, Path | None]:
    """Resolve, stat, and read bytes from candidate script file."""
    target_path, resolve_refusal = _resolve_script_path(operand, cwd, directory_changed)
    if resolve_refusal is not None or target_path is None:
        return None, resolve_refusal
    exists, stat_refusal = _check_regular_file(target_path, operand, strict=strict)
    if stat_refusal is not None or not exists:
        return None, stat_refusal
    return _read_script_bytes(target_path, operand, strict=strict)


def _read_candidate_file(
    candidate: _Candidate,
    cwd: Path,
    files_read: list[int] | None,
    budget: BodyBudget | None = None,
) -> str | Path | None:
    """Resolve and read a script file candidate or return a refusal Path."""
    if candidate.is_program and not candidate.peeked:
        path, refusal = _resolve_script_path(candidate.operand, cwd, candidate.directory_changed)
        if refusal is not None or path is None:
            return refusal
        peek = _peek_program(path, budget if budget is not None else BodyBudget())
        if isinstance(peek, Path):
            return peek
        if not peek:
            return None
    raw, read_refusal = _load_script_bytes(
        candidate.operand, cwd, candidate.directory_changed, strict=candidate.strict
    )
    if read_refusal is not None:
        return read_refusal
    if raw is None:
        return None
    return _validate_script_bytes(raw, candidate.operand, files_read, candidate.is_program)


def _read_script_operand(
    operand: str,
    cwd: Path,
    directory_changed: bool,
    files_read: list[int] | None = None,
) -> str | Path | None:
    """Read a shell script file operand or return a refusal Path."""
    return _read_candidate_file(
        _Candidate(operand, False, directory_changed, True), cwd, files_read
    )


def _read_program_path(
    operand: str,
    cwd: Path,
    directory_changed: bool,
    files_read: list[int] | None = None,
) -> str | Path | None:
    """Read an executable script program path or return a refusal Path."""
    return _read_candidate_file(_Candidate(operand, True, directory_changed, True), cwd, files_read)


read_script_operand = _read_script_operand
read_program_path = _read_program_path


def _search_bases(context: InspectionContext, cd_targets: list[str]) -> list[Path]:
    """Build ordered bounded bases from session, static cd targets and script ancestry."""
    bases = [context.cwd]
    for target in cd_targets:
        path = Path(os.path.expanduser(target))
        if path.is_absolute():
            bases.append(path)
        else:
            bases.append(context.cwd / path)
            if context.script_dir is not None:
                bases.append(context.script_dir / path)
    if context.script_dir is not None:
        bases.append(context.script_dir)
        bases.extend(list(context.script_dir.parents)[:31])
    return list(dict.fromkeys(bases))


def _candidate_paths(
    operand: str, changed: bool, context: InspectionContext, cd_targets: list[str]
) -> list[Path] | Path:
    """Expand bounded globs and search changed relative operands under known bases."""
    path = Path(os.path.expanduser(operand))
    bases = _search_bases(context, cd_targets) if changed else [context.cwd]
    if path.is_absolute():
        bases = [Path(path.anchor)]
    matches: list[Path] = []
    globbed = bool(GLOB_CHARS.intersection(operand))
    for base in bases:
        target = base / path
        if globbed:
            found = sorted(itertools.islice(glob.iglob(str(target)), MAX_SCRIPT_FILES + 2))
            if len(found) > MAX_SCRIPT_FILES + 1:
                return Path("more nested scripts than inspect limit")
            matches.extend(Path(item) for item in found)
        elif not changed or target.is_file():
            matches.append(target)
    if globbed and not matches:
        matches.extend(base / path for base in bases if (base / path).is_file())
    if changed and not matches:
        return Path(
            f"{operand} (relative script path after cd; "
            "not found under the session, cd target or script directories)"
        )
    return list(dict.fromkeys(matches))


def _prepare_nested(
    entry: tuple[str, bool, bool],
    context: InspectionContext,
    cd_targets: list[str],
    budget: BodyBudget,
) -> list[_Candidate] | Path:
    """Resolve one nested operand and discard program peeks before candidate accounting."""
    operand, is_program, changed = entry
    if is_program and "/" not in operand:
        resolved = _path_program(operand)
        if resolved is None:
            return []
        operand, changed = str(resolved), False
    paths = _candidate_paths(operand, changed, context, cd_targets)
    if isinstance(paths, Path):
        return paths
    result = []
    for path in paths:
        if is_program:
            peek = _peek_program(path, budget)
            if isinstance(peek, Path):
                return peek
            if not peek:
                continue
        result.append(_Candidate(str(path), is_program, changed, False, is_program))
    return result


def _nested_candidates(
    nested: list[tuple[str, bool, bool]],
    context: InspectionContext,
    cd_targets: list[str],
    budget: BodyBudget,
) -> list[_Candidate] | Path:
    """Deduplicate prepared operands and reject an overflowing candidate list."""
    result: list[_Candidate] = []
    seen: set[_Candidate] = set()
    for operand, is_program, changed in dict.fromkeys(nested):
        candidates = _prepare_nested((operand, is_program, changed), context, cd_targets, budget)
        if isinstance(candidates, Path):
            return candidates
        for candidate in candidates:
            if candidate not in seen:
                seen.add(candidate)
                result.append(candidate)
                if len(result) > MAX_SCRIPT_FILES + 1:
                    return Path("more nested scripts than inspect limit")
    return result


def _filtered_file_hit(path: Path, operand: str, cwd: Path) -> Path | None:
    """Read bounded regular reader input and reject raw configure substrings."""
    raw, refusal = _load_script_bytes(str(path), cwd, False, strict=False)
    if refusal is not None or raw is None:
        return refusal
    if len(raw) > MAX_SCRIPT_BYTES:
        return Path(f"{operand} (script larger than inspect limit)")
    if b"configure" in raw:
        return Path(f"{operand} (filtered text of this file feeds a shell and names configure)")
    return None


def _filtered_files_hit(
    filtered: list[tuple[str, bool, bool]], context: InspectionContext, cd_targets: list[str]
) -> Path | None:
    """Resolve filtered operands with the same bases and globs as shell operands."""
    for operand, _, changed in filtered:
        paths = _candidate_paths(operand, changed, context, cd_targets)
        if isinstance(paths, Path):
            return paths
        for path in paths:
            hit = _filtered_file_hit(path, operand, context.cwd)
            if hit is not None:
                return hit
    return None


def _body_candidates(
    body: str, context: InspectionContext, budget: BodyBudget
) -> list[_Candidate] | Path:
    """Scan a body and check filtered text before preparing its nested candidates."""
    hit, nested, cd_targets, filtered = scan_body(body, context, budget)
    if hit is not None:
        return Path(hit)
    filtered_hit = _filtered_files_hit(filtered, context, cd_targets)
    if filtered_hit is not None:
        return filtered_hit
    return _nested_candidates(nested, context, cd_targets, budget)


def inspect_script_file(operand: str, context: InspectionContext, is_program: bool) -> Path | None:
    """Inspect ordered nested script files with explicit file, node and inline limits."""
    worklist = [_Candidate(operand, is_program, context.directory_changed, True)]
    budget = BodyBudget()
    for _ in range(MAX_SCRIPT_FILES + 1):
        if not worklist:
            return None
        cur = worklist.pop()
        res = _read_candidate_file(cur, context.cwd, context.files_read, budget)
        if isinstance(res, Path):
            return res
        if res is None:
            continue
        file_context = context._replace(
            directory_changed=cur.directory_changed,
            script_dir=Path(cur.operand).resolve().parent
            if Path(cur.operand).is_absolute()
            else (context.cwd / cur.operand).resolve().parent,
        )
        candidates = _body_candidates(res, file_context, budget)
        if isinstance(candidates, Path):
            return Path(f"{cur.operand} ({candidates})")
        worklist.extend(reversed(candidates))
    if worklist:
        return Path(f"{operand} (more nested scripts than inspect limit)")
    return None
