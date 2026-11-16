#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Compare raw configure guard verdicts of two script trees over a list of shell scripts.

A lane judge runs this to show that a guard change turns no accepted script into a refusal.
A run that measured nothing must not pass as "no change", so three vacuous shapes refuse:

1. Preflight: a child of the running interpreter, started with the guard's -I flag and
   environment, must import tree_sitter and tree_sitter_bash. The guard needs them, and without
   them every verdict is the parser-unavailable refusal. Exit 3 before any script runs.
2. Empty list: a list file that names no script measures nothing. Exit 3.
3. Allow-list per verdict: a verdict counts as measured only when the guard exits 0, or exits 2
   with a first stderr line that starts with "raw configure refused:", is not the guard's own
   inspection-deadline refusal and does not name the parser as unavailable. The whole line is
   classified; the TSV shows its first 200 characters. A timeout, a crash, a missing guard, a
   deadline refusal or a parser-unavailable refusal on either side is unmeasured, and one
   unmeasured verdict makes the run exit 3.

Usage: raw_configure_measure.py BASE HEAD LIST OUT
  BASE, HEAD  trees that hold scripts/raw_configure_guard.py (for example git archive output)
  LIST        file with one shell script path per line, at most 5000
  Relative BASE, HEAD and LIST entries resolve against the invocation directory; the guard
  receives each script path as one shell-quoted operand.
  OUT         TSV written with one row per script: path, base rc, head rc, same or CHANGED,
              base stderr line, head stderr line
Run it with the repository venv interpreter; the guard copies run under the same interpreter.
The summary line counts changed verdicts and accepted-to-refused verdicts and names the
interpreter that ran.
Exit status: 0 when every verdict was measured, 3 for a vacuous run, 2 for usage or input errors.
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
UNAVAILABLE = "shell script parser unavailable"
GUARD = Path("scripts") / "raw_configure_guard.py"
GUARD_ENV = {"PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8", "HOME": "/nonexistent"}
PREFLIGHT = "import tree_sitter, tree_sitter_bash"
SHOWN = 200


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
    """Allow-list: the guard accepted, or refused after a parse that ran within its deadline."""
    if rc == 0:
        return True
    return (
        rc == 2
        and err.startswith(REFUSED)
        and not err.startswith(DEADLINE)
        and UNAVAILABLE not in err
    )


def compare(base: Path, head: Path, scripts: list[Path]) -> tuple[list[str], Tally]:
    """Return one TSV row per script and the tally over both guard copies."""
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
        state = "same" if same else "CHANGED"
        shown = f"{base_err[:SHOWN]}\t{head_err[:SHOWN]}"
        rows.append(f"{script}\t{base_rc}\t{head_rc}\t{state}\t{shown}")
    return rows, tally


def read_list(path: Path) -> list[Path]:
    """Return the script paths the list file names, one per non-blank line, made absolute."""
    lines = path.read_text(encoding="utf-8").splitlines()
    return [Path(line).absolute() for line in lines if line.strip()]


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
    """Compare verdicts, write the TSV, and refuse a run that measured nothing."""
    args = parse_args(sys.argv[1:] if argv is None else argv)
    problem = preflight()
    if problem:
        print(f"measure refused: {problem}", file=sys.stderr)
        return VACUOUS
    try:
        scripts = read_list(args.list)
    except (OSError, UnicodeDecodeError) as exc:
        print(f"measure refused: cannot read {args.list}: {exc}", file=sys.stderr)
        return INPUT_ERROR
    if len(scripts) > MAX_SCRIPTS:
        print(
            f"measure refused: {args.list} names {len(scripts)} scripts, more than {MAX_SCRIPTS}",
            file=sys.stderr,
        )
        return INPUT_ERROR
    if not scripts:
        print(f"measure refused: {args.list} names no script", file=sys.stderr)
        return VACUOUS
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
