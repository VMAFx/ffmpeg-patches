# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Script file inspection and path resolution for raw configure guard."""

from __future__ import annotations

import os
import re
import shlex
import stat
import sys
from pathlib import Path
from typing import NamedTuple

# Ensure repository root is on sys.path for test/tool imports
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.raw_configure_words import (  # noqa: E402
    ASSIGNMENT,
    DECIDABLE_NAME,
    PREFIX_SIMPLE,
    SHELLS,
)

MAX_SCRIPT_BYTES = 262144
MAX_SCRIPT_FILES = 16
SHELL_FAMILY = SHELLS | frozenset(("source", "."))
COMMAND_SEPARATORS = frozenset((";", "&&", "||", "|", "&", "(", "\n", "then", "do", "else"))
FALLBACK_REGEX = re.compile(r"/configure([\"'\s;&|)]|$)")


class _Candidate(NamedTuple):
    """Candidate script file operand and inspection parameters."""

    operand: str
    is_program: bool
    directory_changed: bool
    strict: bool


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
            refusal = Path(f"{operand} (relative script path after cd; use an absolute path)")
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
    if is_program and not raw.startswith(b"#!"):
        return None
    if files_read is not None and files_read[0] >= MAX_SCRIPT_FILES:
        return Path("configure (script file limit)")
    if len(raw) > MAX_SCRIPT_BYTES:
        return Path(f"{operand} (script larger than inspect limit)")
    if b"\x00" in raw:
        return Path(f"{operand} (binary script)")
    if files_read is not None:
        files_read[0] += 1
    return raw.decode("utf-8", errors="replace")


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
) -> str | Path | None:
    """Resolve and read a script file candidate or return a refusal Path."""
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


def _body_words(body: str) -> list[list[str]]:
    """Split script body into words using shlex with line-by-line fallback."""
    raw_lines = body.splitlines()[:MAX_SCRIPT_BYTES]
    filtered_lines: list[str] = []
    for line in raw_lines:
        if not line.lstrip().startswith("#"):
            filtered_lines.append(line)
    clean_body = "\n".join(filtered_lines)
    try:
        lexer = shlex.shlex(clean_body, posix=True, punctuation_chars=";&|<>()\n")
        lexer.whitespace = " \t\r"
        lexer.whitespace_split = True
        words: list[str] = []
        for _ in range(MAX_SCRIPT_BYTES):
            token = lexer.get_token()
            if token == lexer.eof:
                break
            words.append(token)
        return [words]
    except ValueError:
        pass

    result: list[list[str]] = []
    for line in filtered_lines[:MAX_SCRIPT_BYTES]:
        try:
            line_lexer = shlex.shlex(line, posix=True, punctuation_chars=";&|<>()\n")
            line_lexer.whitespace = " \t\r"
            line_lexer.whitespace_split = True
            line_words: list[str] = []
            for _ in range(MAX_SCRIPT_BYTES):
                tok = line_lexer.get_token()
                if tok == line_lexer.eof:
                    break
                line_words.append(tok)
            result.append(line_words)
        except ValueError:
            result.append(["\0regex:" + line])
    return result


def _is_configure_hit(word: str, prev_word: str) -> bool:
    """Check whether a token is a configure command hit."""
    if os.path.basename(word) != "configure":
        return False
    prev_base = os.path.basename(prev_word) if prev_word else ""
    return "/" in word or prev_base in SHELL_FAMILY


def _is_valid_candidate(operand: str) -> bool:
    """Check whether a nested candidate path is decidable and non-dynamic."""
    if not operand or operand == "-":
        return False
    if "$" in operand or "`" in operand:
        return False
    base = os.path.basename(operand)
    return bool(base and DECIDABLE_NAME.fullmatch(base))


def _candidate_allowed(operand: str, dir_changed: bool) -> bool:
    """Check whether candidate path is decidable and allowed given directory state."""
    if not _is_valid_candidate(operand):
        return False
    return not dir_changed or os.path.isabs(operand) or operand.startswith("~")


def _extract_shell_target(tokens: list[str], start_idx: int) -> tuple[str | None, str | None]:
    """Extract inline program body or script file operand after a shell word."""
    j = start_idx + 1
    skipped_c = False
    tokens_len = len(tokens)
    for _ in range(4):
        if j >= tokens_len or not tokens[j].startswith("-"):
            break
        if tokens[j] == "-c":
            skipped_c = True
            j += 1
            break
        j += 1
    if j >= tokens_len or tokens[j] in COMMAND_SEPARATORS:
        return None, None
    target = tokens[j]
    if skipped_c:
        return target, None
    return None, target


def _process_command_word(
    word: str,
    candidates: list[tuple[str, bool]],
    dir_state: list[bool],
) -> bool:
    """Process a token in command position and return whether prefix preserves position."""
    base = os.path.basename(word)
    if base in ("cd", "pushd", "popd"):
        dir_state[0] = True
    elif "/" in word and _candidate_allowed(word, dir_state[0]):
        candidates.append((word, True))
    return bool(ASSIGNMENT.fullmatch(word) or word in PREFIX_SIMPLE)


def _process_shell_word(
    tokens: list[str],
    idx: int,
    candidates: list[tuple[str, bool]],
    dir_state: list[bool],
    bodies: list[str],
) -> str | None:
    """Extract shell targets, queuing inline bodies or adding file candidates."""
    inline_body, file_op = _extract_shell_target(tokens, idx)
    if inline_body is not None:
        bodies.append(inline_body)
        return None
    if file_op is not None:
        if os.path.basename(file_op) == "configure":
            return file_op
        if _candidate_allowed(file_op, dir_state[0]):
            candidates.append((file_op, False))
    return None


def _scan_token_list(
    tokens: list[str],
    candidates: list[tuple[str, bool]],
    dir_state: list[bool],
    bodies: list[str],
) -> str | None:
    """Scan a token sequence for configure hits, directory changes, and candidates."""
    in_command_pos = True
    prev_word = ""
    bound = min(len(tokens), MAX_SCRIPT_BYTES)
    for i in range(bound):
        word = tokens[i]
        if word.startswith("\0regex:"):
            line = word[len("\0regex:") :]
            if FALLBACK_REGEX.search(line):
                return "configure"
            in_command_pos = True
            prev_word = ""
            continue
        if _is_configure_hit(word, prev_word):
            return word
        if word in COMMAND_SEPARATORS:
            in_command_pos = True
            prev_word = word
            continue
        if in_command_pos:
            in_command_pos = _process_command_word(word, candidates, dir_state)
        if os.path.basename(word) in SHELL_FAMILY:
            hit = _process_shell_word(tokens, i, candidates, dir_state, bodies)
            if hit is not None:
                return hit
        prev_word = word
    return None


def _scan_body(body: str) -> tuple[str | None, list[tuple[str, bool]]]:
    """Scan script body for configure command words and nested script invocations."""
    bodies: list[str] = [body]
    candidates: list[tuple[str, bool]] = []
    dir_state = [False]
    for _ in range(MAX_SCRIPT_FILES + 1):
        if not bodies:
            break
        cur_body = bodies.pop()
        token_lists = _body_words(cur_body)
        for tokens in token_lists[:MAX_SCRIPT_BYTES]:
            hit = _scan_token_list(tokens, candidates, dir_state, bodies)
            if hit is not None:
                return hit, candidates
    if dir_state[0]:
        candidates = [c for c in candidates if os.path.isabs(c[0]) or c[0].startswith("~")]
    return None, candidates


def inspect_script_file(
    operand: str,
    cwd: Path,
    directory_changed: bool,
    files_read: list[int],
    is_program: bool,
) -> Path | None:
    """Inspect script file operand and nested scripts using a lexical worklist."""
    worklist: list[_Candidate] = [_Candidate(operand, is_program, directory_changed, True)]
    for _ in range(MAX_SCRIPT_FILES + 1):
        if not worklist:
            break
        cur = worklist.pop()
        res = _read_candidate_file(cur, cwd, files_read)
        if isinstance(res, Path):
            return res
        if res is None:
            continue
        hit, nested = _scan_body(res)
        if hit is not None:
            return Path(f"{hit} (in script {cur.operand})")
        for cand_op, cand_is_prog in nested[: MAX_SCRIPT_FILES + 1]:
            resolved = (
                cand_op if os.path.isabs(cand_op) or cand_op.startswith("~") else str(cwd / cand_op)
            )
            worklist.append(_Candidate(resolved, cand_is_prog, False, False))
    return None
