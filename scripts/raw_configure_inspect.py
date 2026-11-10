# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Per-program handlers and command classification of the raw configure guard."""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Ensure repository root is on sys.path for test/tool imports
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.raw_configure_body import _source_operand  # noqa: E402
from scripts.raw_configure_files import (  # noqa: E402
    InspectionContext,
    inspect_sourced_file,
)
from scripts.raw_configure_unwrap import (  # noqa: E402
    _unwrap_command,
)
from scripts.raw_configure_words import (  # noqa: E402
    ASSIGNMENT,
    CONFIGURE_WORD,
    DATA_ONLY_COMMANDS,
    DECIDABLE_NAME,
    EXEC_ENV_VARS,
    EXEC_PROGRAM_OPTIONS,
    GIT_DATA_SUBCOMMANDS,
    GIT_GLOBAL_OPTS_WITH_ARG,
    INLINE_PROGRAM_FLAG,
    INTERPRETER_VALUE_OPTIONS,
    INTERPRETERS,
    MAKE_VAR_ASSIGN,
    SED_EXEC_PATTERN,
    SED_SUBST_EXEC,
    SHELLS,
    STRIP_CHARS,
    _flatten,
    _has_command_position_configure,
    _has_configure_token,
    _interpreter_family,
    _is_configure_word,
    _long_option_values,
    _names_configure,
    _shell_argument,
    _substitute_literals,
)


def _check_interpreter_flag(
    w: str, next_word: str | None, val_opts: frozenset[str]
) -> tuple[int, Path | None]:
    """Check an interpreter flag and optional argument for configure references."""
    if CONFIGURE_WORD.search(w):
        return 1, Path(w)
    if INLINE_PROGRAM_FLAG.fullmatch(w) and next_word is not None:
        if CONFIGURE_WORD.search(next_word):
            return 2, Path(next_word)
        return 2, None
    if w in val_opts and next_word is not None:
        if CONFIGURE_WORD.search(next_word):
            return 2, Path(next_word)
        return 2, None
    return 1, None


def _inspect_interpreter_flags(words: list[str], idx: int) -> Path | None:
    """Scan leading interpreter option flags for inline configure code."""
    family = _interpreter_family(os.path.basename(words[idx]))
    val_opts = INTERPRETER_VALUE_OPTIONS.get(family or "", frozenset())
    words_len = len(words)
    bound = min(words_len, idx + 16)
    i = idx + 1
    for _ in range(bound - idx):
        if i >= bound or not words[i].startswith("-"):
            break
        next_w = words[i + 1] if i + 1 < words_len else None
        step, blocked = _check_interpreter_flag(words[i], next_w, val_opts)
        if blocked is not None:
            return blocked
        i += step
    return None


def _inspect_interpreter(words: list[str], idx: int, stdin_prog: str | None) -> Path | None:
    """Check interpreter inline scripts and stdin programs for configure words."""
    blocked = _inspect_interpreter_flags(words, idx)
    if blocked is not None:
        return blocked
    if stdin_prog is not None and (
        CONFIGURE_WORD.search(stdin_prog) or CONFIGURE_WORD.search(_flatten(stdin_prog))
    ):
        return Path(stdin_prog[:50])
    name = os.path.basename(words[idx])
    if name in ("awk", "gawk", "mawk"):
        for arg in words[idx + 1 : idx + 4]:
            if "system(" in arg and CONFIGURE_WORD.search(arg):
                return Path(arg)
    return None


def _inspect_find(words: list[str], idx: int) -> Path | None:
    """Inspect find exec-capable positions for raw configure execution."""
    words_len = len(words)
    has_conf_name = any(
        words[i] in ("-name", "-iname") and i + 1 < words_len and "configure" in words[i + 1]
        for i in range(idx + 1, words_len)
    )
    for i in range(idx + 1, words_len):
        if words[i] in ("-exec", "-execdir", "-ok", "-okdir") and i + 1 < words_len:
            exec_args: list[str] = []
            terminated = False
            for j in range(i + 1, min(words_len, i + 32)):
                if words[j] in (";", "\\;", "+"):
                    terminated = True
                    break
                exec_args.append(words[j])
            if not terminated and words_len >= i + 32:
                return Path("find -exec (argument limit)")
            if exec_args and _is_inert_command(exec_args, 0, {}):
                continue
            if has_conf_name and any("{}" in arg for arg in exec_args):
                return Path("find -exec configure")
            for k in range(len(exec_args)):
                arg = exec_args[k]
                if _is_configure_word(arg) or _has_configure_token(arg, {}):
                    return Path(arg)
    return None


def _inspect_sed(words: list[str], idx: int) -> Path | None:
    """Inspect sed scripts for arbitrary command execution via e command or s///e flag."""
    words_len = len(words)
    for i in range(idx + 1, words_len):
        script = words[i + 1] if words[i] == "-e" and i + 1 < words_len else words[i]
        if words[i] == "-e" and i + 1 >= words_len:
            continue
        m_e = SED_EXEC_PATTERN.search(script)
        if m_e and (
            CONFIGURE_WORD.search(m_e.group(1))
            or any(_names_configure(p) for p in m_e.group(1).split())
        ):
            return Path(script)
        if SED_SUBST_EXEC.search(script) and CONFIGURE_WORD.search(script):
            return Path(script)
        if not words[i].startswith("-"):
            break
    return None


def _inspect_git(words: list[str], idx: int) -> Path | None:
    """Inspect git command arguments for exec options executing configure."""
    words_len = len(words)
    for i in range(idx + 1, words_len):
        w = words[i]
        if w == "-c" and i + 1 < words_len and CONFIGURE_WORD.search(words[i + 1]):
            return Path(words[i + 1])
        if w.startswith("-c") and CONFIGURE_WORD.search(w):
            return Path(w)
        if (
            w in ("-x", "--exec")
            and i + 1 < words_len
            and (_is_configure_word(words[i + 1]) or _has_configure_token(words[i + 1], {}))
        ):
            return Path(words[i + 1])
        if (w.startswith("--exec=") or w.startswith("--exec-path=")) and (
            _is_configure_word(w.split("=", 1)[1]) or _has_configure_token(w.split("=", 1)[1], {})
        ):
            return Path(w)
        if (
            w in ("bisect", "submodule")
            and i + 1 < words_len
            and words[i + 1] in ("run", "foreach")
        ):
            for j in range(i + 2, words_len):
                sub_w = words[j]
                if _is_configure_word(sub_w) or _has_configure_token(sub_w, {}):
                    return Path(sub_w)
    return None


def _inspect_xargs(
    words: list[str], idx: int, stdin_prog: str | None, assignments: dict[str, str]
) -> Path | None:
    """Inspect xargs wrapped command and attached stdin program for configure."""
    words_len = len(words)
    i = idx + 1
    for _ in range(words_len):
        if i >= words_len or not words[i].startswith("-"):
            break
        i += 2 if words[i] in ("-I", "-n", "-P", "-s", "-E", "-d", "-a", "-L") else 1
    if i >= words_len:
        return None
    target_cmd = _substitute_literals(words[i], assignments)
    if _is_configure_word(target_cmd) or not DECIDABLE_NAME.fullmatch(os.path.basename(target_cmd)):
        return Path(target_cmd)
    if os.path.basename(target_cmd) in SHELLS:
        if stdin_prog is not None and (
            _is_configure_word(stdin_prog) or CONFIGURE_WORD.search(stdin_prog)
        ):
            return Path(stdin_prog)
        target, _ = _shell_argument(words[i:])
        if target is not None and (
            _is_configure_word(target) or _has_configure_token(target, assignments)
        ):
            return Path(target)
    for j in range(i + 1, words_len):
        arg = words[j]
        if _is_configure_word(arg) or _has_configure_token(arg, assignments):
            return Path(arg)
    return None


def _inspect_rg(words: list[str], idx: int) -> Path | None:
    """Inspect rg command options for preprocessor configure execution."""
    words_len = len(words)
    for i in range(idx + 1, words_len):
        if words[i] == "--pre" and i + 1 < words_len and _is_configure_word(words[i + 1]):
            return Path(words[i + 1])
        if words[i].startswith("--pre=") and _is_configure_word(words[i].split("=", 1)[1]):
            return Path(words[i])
    return None


def _inspect_shell_or_eval(
    words: list[str],
    idx: int,
    stdin_prog: str | None,
    assignments: dict[str, str],
    file_ctx: tuple[Path, bool, list[int] | None] | None = None,
) -> tuple[Path | None, str | None, bool]:
    """Process shell script operands, inline -c scripts, eval, and stdin programs."""
    name = os.path.basename(words[idx])
    if name in ("source", "."):
        operand, option_refusal = _source_operand(
            [_substitute_literals(word, assignments) for word in words[idx + 1 :]]
        )
        if option_refusal is not None:
            return Path(option_refusal), None, False
        if operand is not None:
            target = operand
            if _is_configure_word(target) or not DECIDABLE_NAME.fullmatch(os.path.basename(target)):
                return Path(target), None, False
            cwd, dir_changed, files_read = (
                file_ctx if file_ctx is not None else (Path.cwd(), False, None)
            )
            read_tracker = files_read if files_read is not None else [0]
            refusal, changed = inspect_sourced_file(
                target,
                InspectionContext(cwd, dir_changed, read_tracker, classify_script_words),
            )
            return refusal, None, changed
    elif name == "eval":
        return None, " ".join(words[idx + 1 :]), False
    return None, None, False


def _assignment_blocked(word: str, assignments: dict[str, str]) -> bool:
    """Check whether an assignment binds an execution-capable configure override."""
    if not ASSIGNMENT.fullmatch(word):
        return False
    var, val = word.split("=", 1)
    if var in EXEC_ENV_VARS and (_is_configure_word(val) or bool(CONFIGURE_WORD.search(val))):
        return True
    return _has_command_position_configure(val, assignments)


def _inspect_prefix_env(words: list[str], assignments: dict[str, str]) -> Path | None:
    """Check prefix environment variable assignments for command execution overrides."""
    for w in words:
        if not ASSIGNMENT.fullmatch(w):
            break
        if _assignment_blocked(w, assignments):
            return Path(w)
    return None


def _inspect_make_eval(words: list[str], idx: int, assignments: dict[str, str]) -> Path | None:
    """Check make -E and --eval options for configure invocations."""
    words_len = len(words)
    bound = min(words_len, idx + 32)
    for i in range(idx + 1, bound):
        w = words[i]
        if w == "-E" and i + 1 < words_len and _has_configure_token(words[i + 1], assignments):
            return Path(words[i + 1])
        if w.startswith("-E") and len(w) > 2 and _has_configure_token(w[2:], assignments):
            return Path(w[2:])
    for val in _long_option_values(words, idx + 1, "--eval", 4):
        if _has_configure_token(val, assignments):
            return Path(val)
    return None


def _inspect_make(
    words: list[str], idx: int, stdin_prog: str | None, assignments: dict[str, str]
) -> Path | None:
    """Inspect make commands for stdin recipes, variable overrides, and eval flags."""
    if stdin_prog is not None and (
        CONFIGURE_WORD.search(stdin_prog) or CONFIGURE_WORD.search(_flatten(stdin_prog))
    ):
        return Path(stdin_prog[:50])
    words_len = len(words)
    bound = min(words_len, idx + 32)
    for i in range(idx + 1, bound):
        m = MAKE_VAR_ASSIGN.match(words[i])
        if m and _has_configure_token(m.group(1), assignments):
            return Path(words[i])
    return _inspect_make_eval(words, idx, assignments)


def _inspect_known_dispatch(
    name: str,
    words: list[str],
    cmd_idx: int,
    stdin_prog: str | None,
    assignments: dict[str, str],
) -> tuple[bool, Path | None]:
    """Inspect dedicated dispatcher targets for interpreters, find, sed, git, xargs, and rg."""
    if INTERPRETERS.match(name):
        return True, _inspect_interpreter(words, cmd_idx, stdin_prog)
    handlers = {
        "find": lambda: _inspect_find(words, cmd_idx),
        "sed": lambda: _inspect_sed(words, cmd_idx),
        "git": lambda: _inspect_git(words, cmd_idx),
        "xargs": lambda: _inspect_xargs(words, cmd_idx, stdin_prog, assignments),
        "rg": lambda: _inspect_rg(words, cmd_idx),
        "make": lambda: _inspect_make(words, cmd_idx, stdin_prog, assignments),
    }
    handler = handlers.get(name)
    if handler is not None:
        return True, handler()
    return False, None


def _skipped_configure(words: list[str], cmd_idx: int, assignments: dict[str, str]) -> Path | None:
    """Inspect skipped wrapper tokens for embedded configure references."""
    bound = min(len(words), max(0, cmd_idx))
    for i in range(bound):
        word = words[i]
        if ASSIGNMENT.fullmatch(word):
            if _assignment_blocked(word, assignments):
                return Path(word)
        elif _has_configure_token(word, assignments):
            return Path(word)
    return None


def _wrapper_blocked(words: list[str], cmd_idx: int, assignments: dict[str, str]) -> Path | None:
    """Check prefix environment assignments and skipped wrapper tokens."""
    env_blocked = _inspect_prefix_env(words, assignments)
    if env_blocked is not None:
        return env_blocked
    return _skipped_configure(words, cmd_idx, assignments)


def _exec_option_blocked(
    name: str, words: list[str], idx: int, assignments: dict[str, str]
) -> Path | None:
    """Check whether a data-only tool option passes an execution-capable program."""
    options = EXEC_PROGRAM_OPTIONS.get(name)
    if not options:
        return None
    for opt in options:
        min_len = 4 if opt == "--compress-program" else 8
        for val in _long_option_values(words, idx + 1, opt, min_len):
            if _is_configure_word(val) or _has_configure_token(val, assignments):
                return Path(val)
    return None


def _is_inert_command(words: list[str], idx: int, assignments: dict[str, str]) -> bool:
    """Check whether a command is data-only without execution-capable options."""
    if idx >= len(words):
        return False
    name = os.path.basename(_substitute_literals(words[idx], assignments))
    return (
        name in DATA_ONLY_COMMANDS and _exec_option_blocked(name, words, idx, assignments) is None
    )


def _inspect_trailing_arguments(
    words: list[str], start_idx: int, assignments: dict[str, str]
) -> Path | None:
    """Check remaining command arguments for configure tokens."""
    for i in range(start_idx, len(words)):
        if _has_command_position_configure(words[i], assignments):
            return Path(words[i])
    return None


def _git_subcommand(words: list[str], start_idx: int) -> str | None:
    """Find the first git subcommand token past global options."""
    idx = start_idx
    words_len = len(words)
    bound = min(words_len, start_idx + 32)
    for _ in range(bound - start_idx):
        if idx >= bound:
            break
        w = words[idx]
        if w == "--":
            idx += 1
            if idx < words_len and not words[idx].startswith("-"):
                return words[idx]
            return None
        if w == "-c" or w in GIT_GLOBAL_OPTS_WITH_ARG:
            idx += 2
            continue
        if w.startswith("-"):
            idx += 1
            continue
        return w
    return None


def _inspect_git_data(words: list[str], start_idx: int) -> bool:
    """Check whether a git invocation targets an allowed data subcommand."""
    subcmd = _git_subcommand(words, start_idx)
    return subcmd is not None and subcmd in GIT_DATA_SUBCOMMANDS


def _classify_target(
    words: list[str],
    cmd_idx: int,
    assignments: dict[str, str],
    stdin_prog: str | None,
) -> Path | None:
    """Classify the unwrapped command target and arguments."""
    raw_cmd = words[cmd_idx]
    cmd_word = _substitute_literals(raw_cmd, assignments)
    if not DECIDABLE_NAME.fullmatch(os.path.basename(cmd_word)) or _is_configure_word(cmd_word):
        return Path(cmd_word)
    name = os.path.basename(cmd_word)
    handled, blocked = _inspect_known_dispatch(name, words, cmd_idx, stdin_prog, assignments)
    if handled and blocked is not None:
        return blocked
    exec_blocked = _exec_option_blocked(name, words, cmd_idx, assignments)
    if exec_blocked is not None:
        return exec_blocked
    if _is_inert_command(words, cmd_idx, assignments) or name in ("find", "sed", "rg", "xargs"):
        return None
    if name == "git" and _inspect_git_data(words, cmd_idx + 1):
        return None
    return _inspect_trailing_arguments(words, cmd_idx + 1, assignments)


def _classify_command(
    words: list[str], assignments: dict[str, str], stdin_prog: str | None = None
) -> tuple[Path | None, list[str]]:
    """Determine whether inspected command positions run configure or require queuing."""
    cmd_idx = _unwrap_command(words, 0)
    blocked = _wrapper_blocked(words, cmd_idx, assignments)
    if blocked is not None:
        return blocked, []
    if cmd_idx >= len(words):
        return None, []
    return _classify_target(words, cmd_idx, assignments, stdin_prog), []


def classify_script_words(words: list[str]) -> Path | None:
    """Classify execution-capable script arguments without reentering file inspection."""
    idx = _unwrap_command(words, 0)
    result = _wrapper_blocked(words, idx, {})
    if idx < len(words):
        name = os.path.basename(words[idx])
        if result is None:
            _, result = _inspect_known_dispatch(name, words, idx, None, {})
        if result is None:
            result = _exec_option_blocked(name, words, idx, {})
    if result is None:
        return None
    text = str(result)
    literal = text.translate(STRIP_CHARS)
    if not (CONFIGURE_WORD.search(literal) or DECIDABLE_NAME.fullmatch(os.path.basename(literal))):
        return None
    return result if _is_configure_word(text) or CONFIGURE_WORD.search(text) else None
