#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
r"""Compare raw configure guard verdicts of two script trees over a list of shell scripts.

A lane judge runs this to show that a guard change turns no accepted script into a refusal.
A run that measured nothing must not pass as "no change", so three vacuous shapes refuse:

1. Preflight: a child of the running interpreter, started with the guard's -I flag and
   environment, must import tree_sitter and tree_sitter_bash. The guard needs them, and without
   them every verdict is the parser-unavailable refusal. Exit 3 before any script runs.
2. Empty list: a list file that names no script measures nothing. Exit 3.
3. Allow-list per verdict: a verdict counts as measured only when the guard exits 0, or exits 2
   with a first stderr line that starts with "raw configure refused:", is neither the guard's
   inspection-deadline refusal nor its "parse error (" refusal, and does not name the parser as
   unavailable. The whole line is classified; the TSV shows its first 200 characters before
   escaping. A timeout, a crash, a missing guard, a deadline refusal, a parse-error refusal or a
   parser-unavailable refusal on either side is unmeasured, and one unmeasured verdict makes the
   run exit 3.

Usage: raw_configure_measure.py BASE HEAD LIST OUT
  BASE, HEAD  trees that hold scripts/raw_configure_guard.py (for example git archive output)
  LIST        file with one existing regular-file shell script path per line, at most 5000
  Relative BASE, HEAD and LIST entries resolve against the invocation directory; the guard
  receives each script path as one shell-quoted operand.
  OUT         TSV written with one row per script: path, base rc, head rc, same or CHANGED,
              base stderr line, head stderr line. Diagnostics are cut to 200 characters before
              encoding; every field replaces \ with \\, tab with \t, LF with \n and CR with \r
              (backslash first; decode_field inverts the encoding).
Run it with the repository venv interpreter; the guard copies run under the same interpreter.
The summary line counts changed verdicts and accepted-to-refused verdicts and names the
interpreter that ran.
Exit status: 0 when every verdict was measured, 3 for a vacuous run, 2 for usage or input errors
(unreadable list, more than 5000 entries, or any listed path that is not a regular file).
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import shlex
import subprocess  # nosec B404 - guard copies run with a fixed argv, no shell, bounded timeout.
import sys
from pathlib import Path

MAX_SCRIPTS = 5000
GUARD_TIMEOUT = 20
VACUOUS = 3
INPUT_ERROR = 2
REFUSED = "raw configure refused:"
DEADLINE = "raw configure refused: inspection exceeded the 10 s deadline"
PARSE_ERROR = "raw configure refused: parse error ("
UNAVAILABLE = "shell script parser unavailable"
GUARD = Path("scripts") / "raw_configure_guard.py"
GUARD_ENV = {"PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8", "HOME": "/nonexistent"}
PREFLIGHT = "import tree_sitter, tree_sitter_bash"
SHOWN = 200
FIELD_UNESCAPES = {"\\": "\\", "t": "\t", "n": "\n", "r": "\r"}


@dataclasses.dataclass
class Tally:
    """Counts over the compared verdicts."""

    changed: int = 0
    unmeasured: int = 0
    accepted_base: int = 0
    accepted_head: int = 0
    accepted_to_refused: int = 0


def preflight() -> str:
    """Return why the parser does not load in the guard's isolated environment, or ''."""
    try:
        result = subprocess.run(  # nosec B603 - fixed argv, no shell.
            [sys.executable, "-I", "-c", PREFLIGHT],
            text=True,
            capture_output=True,
            timeout=GUARD_TIMEOUT,
            check=False,
            env=GUARD_ENV,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"parser check did not run under {sys.executable} -I: {exc}"
    if result.returncode:
        last = (result.stderr.strip().splitlines() or [""])[-1]
        return f"parser does not load under {sys.executable} -I: {last}"
    return ""


def verdict(guard: Path, script: Path) -> tuple[int, str]:
    """Run one guard copy on `bash <script>`; return its exit status and whole first stderr line."""
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": f"bash {shlex.quote(str(script))}"},
        "cwd": str(script.parent),
    }
    try:
        result = subprocess.run(  # nosec B603 - fixed argv, no shell.
            [sys.executable, "-I", str(guard), "--client", "claude"],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            cwd=str(script.parent),
            timeout=GUARD_TIMEOUT,
            check=False,
            env=GUARD_ENV,
        )
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    except OSError as exc:
        return 127, f"guard did not start: {exc}"
    first = (result.stderr.strip().splitlines() or [""])[0]
    return result.returncode, first


def measured(rc: int, err: str) -> bool:
    """Allow-list: the guard accepted, or refused after a clean parse within its deadline."""
    if rc == 0:
        return True
    return (
        rc == 2
        and err.startswith(REFUSED)
        and not err.startswith((DEADLINE, PARSE_ERROR))
        and UNAVAILABLE not in err
    )


def encode_field(value: str) -> str:
    """Escape backslashes, tabs, line feeds and carriage returns for one TSV field."""
    return (
        value.replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n").replace("\r", "\\r")
    )


def decode_field(value: str) -> str:
    """Invert encode_field on one escaped TSV field."""
    out: list[str] = []
    index = 0
    limit = len(value)
    for _ in range(limit):
        if index >= limit:
            break
        char = value[index]
        if char != "\\":
            out.append(char)
            index += 1
            continue
        decoded = FIELD_UNESCAPES.get(value[index + 1 : index + 2])
        if decoded is None:
            raise ValueError(f"invalid TSV field escape at offset {index}: {value!r}")
        out.append(decoded)
        index += 2
    return "".join(out)


def compare(base: Path, head: Path, scripts: list[Path]) -> tuple[list[str], Tally]:
    """Return one escaped TSV row per script and the tally over both guard copies."""
    rows = []
    tally = Tally()
    for script in scripts:
        base_rc, base_err = verdict(base / GUARD, script)
        head_rc, head_err = verdict(head / GUARD, script)
        same = (base_rc, base_err) == (head_rc, head_err)
        tally.changed += int(not same)
        tally.unmeasured += int(not measured(base_rc, base_err)) + int(
            not measured(head_rc, head_err)
        )
        tally.accepted_base += int(base_rc == 0)
        tally.accepted_head += int(head_rc == 0)
        tally.accepted_to_refused += int(base_rc == 0 and head_rc != 0)
        fields = (
            str(script),
            str(base_rc),
            str(head_rc),
            "same" if same else "CHANGED",
            base_err[:SHOWN],
            head_err[:SHOWN],
        )
        rows.append("\t".join(encode_field(field) for field in fields))
    return rows, tally


def read_list(path: Path) -> list[Path]:
    """Return the script paths the list file names, one per non-blank line, made absolute."""
    lines = path.read_text(encoding="utf-8").splitlines()
    return [Path(line).absolute() for line in lines if line.strip()]


def _is_regular_file(path: Path) -> bool:
    """Return True when path points to an accessible regular file."""
    try:
        return path.is_file()
    except OSError:
        return False


def _load_list(list_path: Path) -> tuple[list[Path], int, str]:
    """Read and validate the script list; return (scripts, 0, '') or ([], exit_code, reason)."""
    try:
        scripts = read_list(list_path)
    except (OSError, UnicodeDecodeError) as exc:
        return [], INPUT_ERROR, f"cannot read {list_path}: {exc}"
    if len(scripts) > MAX_SCRIPTS:
        return (
            [],
            INPUT_ERROR,
            f"{list_path} names {len(scripts)} scripts, more than {MAX_SCRIPTS}",
        )
    if not scripts:
        return [], VACUOUS, f"{list_path} names no script"
    bad = [script for script in scripts if not _is_regular_file(script)]
    if bad:
        noun = (
            "path that is not a regular file"
            if len(bad) == 1
            else "paths that are not regular files"
        )
        return [], INPUT_ERROR, f"{list_path} names {len(bad)} {noun} (first: {bad[0]})"
    return scripts, 0, ""


def parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse the two trees, the list file and the output path; make both trees absolute."""
    parser = argparse.ArgumentParser(
        description="Compare raw configure guard verdicts of two script trees."
    )
    parser.add_argument(
        "base", type=Path, help="tree holding the base scripts/raw_configure_guard.py"
    )
    parser.add_argument(
        "head", type=Path, help="tree holding the head scripts/raw_configure_guard.py"
    )
    parser.add_argument("list", type=Path, help="file with one shell script path per line")
    parser.add_argument("out", type=Path, help="TSV written with one row per script")
    args = parser.parse_args(argv)
    args.base, args.head = args.base.absolute(), args.head.absolute()
    return args


def main(argv: list[str] | None = None) -> int:
    """Compare verdicts, write the TSV, and refuse invalid inputs or vacuous runs."""
    args = parse_args(sys.argv[1:] if argv is None else argv)
    problem = preflight()
    if problem:
        print(f"measure refused: {problem}", file=sys.stderr)
        return VACUOUS
    scripts, error_rc, reason = _load_list(args.list)
    if error_rc:
        print(f"measure refused: {reason}", file=sys.stderr)
        return error_rc
    rows, tally = compare(args.base, args.head, scripts)
    args.out.write_text("\n".join(rows) + "\n", encoding="utf-8")
    print(
        f"scripts={len(scripts)} verdict_changed={tally.changed} "
        f"accepted_base={tally.accepted_base} accepted_head={tally.accepted_head} "
        f"accepted_to_refused={tally.accepted_to_refused} "
        f"unmeasured={tally.unmeasured} python={sys.executable}"
    )
    if tally.unmeasured:
        print(
            f"measure refused: {tally.unmeasured} verdicts unmeasured, rows in {args.out}",
            file=sys.stderr,
        )
        return VACUOUS
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
