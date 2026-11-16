# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Unit tests for scripts/raw_configure_measure.py.

Verifies:
1. The verdict allow-list takes an accept and a parsed refusal, and nothing else (planted negatives:
   parser unavailable, the guard's inspection-deadline refusal, timeout, crash, wrong exit status,
   foreign stderr).
2. A missing parser refuses with exit 3 before any guard runs; the parser check runs in a child
   interpreter with the guard's -I flag and environment (planted negatives).
3. A guard copy that only reports the parser as unavailable makes the run exit 3 (planted negative).
4. An empty list exits 3; a list over the bound exits 2 (planted negatives).
5. A missing guard, a guard timeout and a script directory that does not exist are unmeasured.
6. A measured run exits 0 and counts changed and accepted-to-refused verdicts.
7. A parser-unavailable refusal after a long script path is classified on the whole line; the TSV
   keeps the first 200 characters (planted negative).
8. A script path with a space reaches the guard as one shell operand (planted negative).
9. Relative trees and list entries resolve against the invocation directory (planted negative).
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess  # nosec B404 - TimeoutExpired type for the planted timeout.
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from scripts import raw_configure_measure as measure

ACCEPT = "import sys\nsys.exit(0)\n"
REFUSE = (
    "import sys\n"
    'print("raw configure refused: planted (WRITTEN_SCRIPT_DIR)", file=sys.stderr)\n'
    "sys.exit(2)\n"
)
UNAVAILABLE = (
    "import sys\n"
    'print("raw configure refused: planted '
    '(shell script parser unavailable: planted)", file=sys.stderr)\n'
    "sys.exit(2)\n"
)
LONG_UNAVAILABLE = (
    "import sys\n"
    'print("raw configure refused: " + "/long" * 60 + '
    '" (shell script parser unavailable: planted)", file=sys.stderr)\n'
    "sys.exit(2)\n"
)
ECHO_OPERANDS = (
    "import json, shlex, sys\n"
    'command = json.load(sys.stdin)["tool_input"]["command"]\n'
    'print("raw configure refused: " + json.dumps(shlex.split(command)), file=sys.stderr)\n'
    "sys.exit(2)\n"
)
DEADLINE_LINE = (
    "raw configure refused: inspection exceeded the 10 s deadline (stalled filesystem or very "
    "large script); run python3 scripts/ffmpeg_configure.py SRC_DIR BUILD_DIR -- <args>"
)


def _tree(root: Path, name: str, body: str | None) -> Path:
    """Create a script tree whose guard is `body`; None leaves the guard out."""
    tree = root / name
    (tree / "scripts").mkdir(parents=True)
    if body is not None:
        (tree / "scripts" / "raw_configure_guard.py").write_text(body, encoding="utf-8")
    return tree


def _list(root: Path, count: int) -> Path:
    """Create `count` shell scripts and a list file naming them."""
    names = []
    for index in range(count):
        script = root / f"s{index}.sh"
        script.write_text("echo ok\n", encoding="utf-8")
        names.append(str(script))
    listing = root / "list.txt"
    listing.write_text("".join(f"{name}\n" for name in names), encoding="utf-8")
    return listing


def _run(argv: list[str]) -> tuple[int, str, str]:
    """Run main and return its exit status, stdout and stderr."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = measure.main(argv)
    return rc, out.getvalue(), err.getvalue()


class MeasuredAllowListTest(unittest.TestCase):
    def test_allow_list(self) -> None:
        """Only an accept and a parsed refusal count as measured."""
        cases = [
            (0, "", True),
            (0, "anything", True),
            (2, "raw configure refused: x (WRITTEN_SCRIPT_DIR)", True),
            (2, "raw configure refused: x (shell script parser unavailable: no module)", False),
            (2, DEADLINE_LINE, False),
            (2, "raw configure refused: x writes past a deadline (WRITTEN_SCRIPT_DIR)", True),
            (2, "python3: can't open file 'scripts/raw_configure_guard.py'", False),
            (2, "", False),
            (3, "raw configure refused: x", False),
            (1, "Traceback (most recent call last):", False),
            (124, "timeout", False),
            (127, "guard did not start: no such directory", False),
        ]
        for rc, err, expected in cases:
            with self.subTest(rc=rc, err=err):
                self.assertIs(measure.measured(rc, err), expected)


def _child(rc: int, stderr: str = "") -> subprocess.CompletedProcess[str]:
    """Return a finished parser-check child with the given status and stderr."""
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout="", stderr=stderr)


class PreflightTest(unittest.TestCase):
    def test_missing_parser_refuses_before_any_guard_runs(self) -> None:
        """A parser check child that cannot import tree_sitter_bash exits 3 and runs no guard."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "out.tsv"
            missing = _child(1, "ModuleNotFoundError: No module named 'tree_sitter_bash'\n")
            with (
                unittest.mock.patch.object(measure.subprocess, "run", return_value=missing),
                unittest.mock.patch.object(measure, "verdict") as verdict,
            ):
                rc, _, err = _run([str(root), str(root), str(_list(root, 1)), str(out)])
            self.assertEqual(rc, measure.VACUOUS)
            self.assertIn("parser does not load", err)
            self.assertIn("No module named 'tree_sitter_bash'", err)
            verdict.assert_not_called()
            self.assertFalse(out.exists())

    def test_loaded_parser_passes(self) -> None:
        """The preflight returns no problem when the parser check child exits 0."""
        with unittest.mock.patch.object(measure.subprocess, "run", return_value=_child(0)):
            self.assertEqual(measure.preflight(), "")

    def test_check_runs_with_the_guard_flags_and_environment(self) -> None:
        """The parser check runs `-I` under GUARD_ENV, as each guard copy does."""
        with unittest.mock.patch.object(measure.subprocess, "run", return_value=_child(0)) as run:
            measure.preflight()
        self.assertEqual(run.call_args.args[0], [sys.executable, "-I", "-c", measure.PREFLIGHT])
        self.assertEqual(run.call_args.kwargs["env"], measure.GUARD_ENV)

    def test_parent_only_module_is_not_seen(self) -> None:
        """A module only the parent's PYTHONPATH reaches fails the isolated parser check."""
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "planted_parent_only.py").write_text("", encoding="utf-8")
            with (
                unittest.mock.patch.dict(os.environ, {"PYTHONPATH": tmp}),
                unittest.mock.patch.object(measure, "PREFLIGHT", "import planted_parent_only"),
            ):
                problem = measure.preflight()
        self.assertIn("No module named 'planted_parent_only'", problem)

    def test_check_that_does_not_start_refuses(self) -> None:
        """A parser check that times out is a refusal, not a pass."""
        expired = subprocess.TimeoutExpired(cmd="check", timeout=measure.GUARD_TIMEOUT)
        with unittest.mock.patch.object(measure.subprocess, "run", side_effect=expired):
            self.assertIn("parser check did not run", measure.preflight())


class VacuousRunTest(unittest.TestCase):
    def setUp(self) -> None:
        """Pass the preflight so the later barriers run under any test interpreter."""
        patcher = unittest.mock.patch.object(measure, "preflight", return_value="")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_parser_unavailable_guard_refuses(self) -> None:
        """A base guard that only reports the parser unavailable makes every verdict unmeasured."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base, head = _tree(root, "base", UNAVAILABLE), _tree(root, "head", ACCEPT)
            out = root / "out.tsv"
            rc, summary, err = _run([str(base), str(head), str(_list(root, 2)), str(out)])
            self.assertEqual(rc, measure.VACUOUS)
            self.assertIn("unmeasured=2", summary)
            self.assertIn("2 verdicts unmeasured", err)
            self.assertEqual(len(out.read_text(encoding="utf-8").splitlines()), 2)

    def test_missing_guard_refuses(self) -> None:
        """A tree without the guard file yields unmeasured verdicts."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base, head = _tree(root, "base", None), _tree(root, "head", ACCEPT)
            rc, summary, _ = _run(
                [str(base), str(head), str(_list(root, 1)), str(root / "out.tsv")]
            )
            self.assertEqual(rc, measure.VACUOUS)
            self.assertIn("unmeasured=1", summary)

    def test_empty_list_refuses(self) -> None:
        """A list file with only blank lines names no script and exits 3."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            listing = root / "list.txt"
            listing.write_text("\n  \n", encoding="utf-8")
            rc, _, err = _run([str(root), str(root), str(listing), str(root / "out.tsv")])
            self.assertEqual(rc, measure.VACUOUS)
            self.assertIn("names no script", err)

    def test_list_over_bound_refuses(self) -> None:
        """A list longer than MAX_SCRIPTS exits 2 instead of measuring a prefix."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with unittest.mock.patch.object(measure, "MAX_SCRIPTS", 2):
                rc, _, err = _run(
                    [str(root), str(root), str(_list(root, 3)), str(root / "out.tsv")]
                )
            self.assertEqual(rc, measure.INPUT_ERROR)
            self.assertIn("more than 2", err)

    def test_list_at_bound_runs(self) -> None:
        """A list of exactly MAX_SCRIPTS paths is measured."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tree = _tree(root, "tree", ACCEPT)
            with unittest.mock.patch.object(measure, "MAX_SCRIPTS", 2):
                rc, summary, _ = _run(
                    [str(tree), str(tree), str(_list(root, 2)), str(root / "out.tsv")]
                )
            self.assertEqual(rc, 0)
            self.assertIn("scripts=2 ", summary)

    def test_unreadable_list_refuses(self) -> None:
        """A list path that does not exist exits 2."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rc, _, err = _run(
                [str(root), str(root), str(root / "absent.txt"), str(root / "out.tsv")]
            )
            self.assertEqual(rc, measure.INPUT_ERROR)
            self.assertIn("cannot read", err)


class VerdictTest(unittest.TestCase):
    def test_timeout_is_unmeasured(self) -> None:
        """A guard that runs past the timeout yields an unmeasured verdict."""
        expired = subprocess.TimeoutExpired(cmd="guard", timeout=measure.GUARD_TIMEOUT)
        with unittest.mock.patch.object(measure.subprocess, "run", side_effect=expired):
            rc, err = measure.verdict(Path("guard.py"), Path("/tmp/s.sh"))  # nosec B108 - path is never opened.
        self.assertEqual((rc, err), (124, "timeout"))
        self.assertFalse(measure.measured(rc, err))

    def test_space_in_script_path_is_one_operand(self) -> None:
        """A script under a directory with a space reaches the guard as one operand."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            guard = _tree(root, "tree", ECHO_OPERANDS) / measure.GUARD
            script = root / "dir with space" / "s.sh"
            script.parent.mkdir()
            script.write_text("echo ok\n", encoding="utf-8")
            rc, err = measure.verdict(guard, script)
        self.assertEqual(rc, 2)
        self.assertEqual(json.loads(err.removeprefix(measure.REFUSED)), ["bash", str(script)])

    def test_missing_script_directory_is_unmeasured(self) -> None:
        """A script whose directory does not exist yields an unmeasured verdict."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            guard = _tree(root, "tree", ACCEPT) / measure.GUARD
            rc, err = measure.verdict(guard, root / "absent" / "s.sh")
        self.assertEqual(rc, 127)
        self.assertFalse(measure.measured(rc, err))


class MeasuredRunTest(unittest.TestCase):
    def setUp(self) -> None:
        """Pass the preflight so the guard copies run under any test interpreter."""
        patcher = unittest.mock.patch.object(measure, "preflight", return_value="")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_counts_accepted_to_refused(self) -> None:
        """A head guard that refuses what the base accepted is measured and counted."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base, head = _tree(root, "base", ACCEPT), _tree(root, "head", REFUSE)
            out = root / "out.tsv"
            rc, summary, _ = _run([str(base), str(head), str(_list(root, 2)), str(out)])
            self.assertEqual(rc, 0)
            self.assertIn(
                "verdict_changed=2 accepted_base=2 accepted_head=0 "
                "accepted_to_refused=2 unmeasured=0",
                summary,
            )
            rows = [line.split("\t") for line in out.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(
                [row[1:4] for row in rows], [["0", "2", "CHANGED"], ["0", "2", "CHANGED"]]
            )

    def test_long_unavailable_line_is_classified_whole(self) -> None:
        """A parser-unavailable refusal past 200 characters is unmeasured; the TSV truncates it."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base, head = _tree(root, "base", LONG_UNAVAILABLE), _tree(root, "head", ACCEPT)
            out = root / "out.tsv"
            rc, summary, _ = _run([str(base), str(head), str(_list(root, 1)), str(out)])
            self.assertEqual(rc, measure.VACUOUS)
            self.assertIn("unmeasured=1", summary)
            row = out.read_text(encoding="utf-8").splitlines()[0].split("\t")
            self.assertEqual(len(row[4]), measure.SHOWN)

    def test_relative_paths_resolve_against_invocation_directory(self) -> None:
        """Relative trees and list entries are measured from the directory main runs in."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _tree(root, "tree", ACCEPT)
            (root / "sub").mkdir()
            (root / "sub" / "s.sh").write_text("echo ok\n", encoding="utf-8")
            (root / "list.txt").write_text("sub/s.sh\n", encoding="utf-8")
            previous = os.getcwd()
            os.chdir(root)
            try:
                rc, summary, _ = _run(["tree", "tree", "list.txt", "out.tsv"])
            finally:
                os.chdir(previous)
        self.assertEqual(rc, 0)
        self.assertIn("unmeasured=0", summary)

    def test_absolute_paths_stay_unchanged(self) -> None:
        """Absolute trees and list entries pass through unchanged."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            listing = root / "list.txt"
            listing.write_text(f"{root / 's.sh'}\n", encoding="utf-8")
            args = measure.parse_args([str(root), str(root / "h"), str(listing), "out.tsv"])
            self.assertEqual((args.base, args.head), (root, root / "h"))
            self.assertEqual(measure.read_list(listing), [root / "s.sh"])

    def test_same_verdicts(self) -> None:
        """Identical guards change no verdict."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base, head = _tree(root, "base", REFUSE), _tree(root, "head", REFUSE)
            rc, summary, _ = _run(
                [str(base), str(head), str(_list(root, 2)), str(root / "out.tsv")]
            )
            self.assertEqual(rc, 0)
            self.assertIn(
                "verdict_changed=0 accepted_base=0 accepted_head=0 "
                "accepted_to_refused=0 unmeasured=0",
                summary,
            )


if __name__ == "__main__":
    unittest.main()
