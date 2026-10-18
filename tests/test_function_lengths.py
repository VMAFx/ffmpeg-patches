# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Tests for the HISS-04 function length gate in scripts/series.py."""

from __future__ import annotations

import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import series  # noqa: E402


def run_git(tree: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(tree), "-c", "user.name=Dev", "-c", "user.email=dev@invalid", *args],
        env=series.git_env(),
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return result.stdout.strip()


def build_c_function(
    name: str,
    total_lines: int,
    multiline: bool = False,
    nested: bool = False,
) -> list[str]:
    lines = [f"int {name}(int a,", "      int b)", "{"] if multiline else [f"int {name}(void)", "{"]
    body_target = total_lines - len(lines) - 1
    if nested and body_target >= 3:
        lines.extend(["    if (1) {", "        int x = 0;", "    }"])
        body_target -= 3
    for i in range(body_target):
        lines.append(f"    int v_{i} = {i};")
    lines.append("}")
    return lines


class LengthFixture:
    def __init__(self, scratch: Path) -> None:
        self.tree = scratch / "upstream"
        self.root = scratch / "series"
        self.tree.mkdir()
        self.root.mkdir()
        run_git(self.tree, "init", "-q")
        self.write_file("lib.c", build_c_function("f", 58))
        run_git(self.tree, "add", "lib.c")
        run_git(self.tree, "commit", "-q", "-m", "base")
        self.base = run_git(self.tree, "rev-parse", "HEAD")
        (self.root / "base.env").write_text(
            f"FFMPEG_REMOTE=https://example.invalid/up.git\nFFMPEG_TAG=v1\nFFMPEG_COMMIT={self.base}\n"
        )
        (self.root / "series.txt").write_text("# header\n")

    def write_file(self, relpath: str, lines: list[str]) -> None:
        path = self.tree / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n")

    def commit_file(self, relpath: str, lines: list[str], msg: str) -> None:
        self.write_file(relpath, lines)
        run_git(self.tree, "add", relpath)
        run_git(self.tree, "commit", "-q", "-s", "-m", msg)

    def export(self) -> list[str]:
        with contextlib.redirect_stderr(io.StringIO()):
            names = series.export_series(self.root, self.tree)
        for name in names:
            sidecar = (self.root / "patches" / name).with_suffix(".toml")
            if not sidecar.exists():
                sidecar.write_text('status = "unsubmitted"\nconsumers = ["a"]\n')
        return names

    def set_exception(self, patch_name: str, lines: int, **overrides: str) -> None:
        path = overrides.get("path", "lib.c")
        function = overrides.get("function", "f")
        category = overrides.get("category", "B")
        entered = overrides.get("entered", "2026-10-18")
        expires = overrides.get("expires", "2026-10-21")
        reason = overrides.get("reason", "upstream function bloat")
        sidecar = (self.root / "patches" / patch_name).with_suffix(".toml")
        content = (
            'status = "unsubmitted"\n'
            'consumers = ["a"]\n\n'
            "[[exceptions]]\n"
            'rule = "HISS-04"\n'
            f'path = "{path}"\n'
            f'function = "{function}"\n'
            f"lines = {lines}\n"
            f'category = "{category}"\n'
            f'entered = "{entered}"\n'
            f'expires = "{expires}"\n'
            f'reason = "{reason}"\n'
        )
        sidecar.write_text(content)


def run_series_cli(root: Path, *args: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = series.main(["--root", str(root), *args])
    return code, out.getvalue(), err.getvalue()


class TestFunctionLengths(unittest.TestCase):
    def setUp(self) -> None:
        self._scratch = tempfile.TemporaryDirectory()
        self.fx = LengthFixture(Path(self._scratch.name))

    def tearDown(self) -> None:
        self._scratch.cleanup()

    def test_planted_growth_without_exception_fails(self) -> None:
        self.fx.commit_file("lib.c", build_c_function("f", 61), "grow f to 61")
        self.fx.export()
        rc, out, _ = run_series_cli(
            self.fx.root, "lengths", str(self.fx.tree), "--today", "2026-10-18"
        )
        self.assertEqual(rc, 1)
        msg = "58 -> 61 lines, over the 60-line cap (HISS-04) without a live exception"
        self.assertIn(msg, out)
        self.assertIn("1 function(s) over 60 lines, 0 excused, 1 failing", out)

    def test_boundary_growth_passes(self) -> None:
        self.fx.commit_file("lib.c", build_c_function("f", 60), "grow f to 60")
        self.fx.export()
        rc, out, _ = run_series_cli(
            self.fx.root, "lengths", str(self.fx.tree), "--today", "2026-10-18"
        )
        self.assertEqual(rc, 0)
        self.assertIn("0 function(s) over 60 lines, 0 excused, 0 failing", out)

    def test_live_exception_calibration_and_boundaries(self) -> None:
        self.fx.commit_file("lib.c", build_c_function("f", 61), "grow f to 61")
        names = self.fx.export()
        patch_name = names[0]

        self.fx.set_exception(patch_name, lines=61)
        rc, out, _ = run_series_cli(
            self.fx.root, "lengths", str(self.fx.tree), "--today", "2026-10-18"
        )
        self.assertEqual(rc, 0)
        self.assertIn("58 -> 61 lines (excused)", out)
        self.assertIn("1 function(s) over 60 lines, 1 excused, 0 failing", out)

        self.fx.set_exception(patch_name, lines=60)
        rc, out, _ = run_series_cli(
            self.fx.root, "lengths", str(self.fx.tree), "--today", "2026-10-18"
        )
        self.assertEqual(rc, 1)
        self.assertIn("without a live exception", out)

        self.fx.set_exception(patch_name, lines=62)
        rc, out, err = run_series_cli(
            self.fx.root, "lengths", str(self.fx.tree), "--today", "2026-10-18"
        )
        self.assertEqual(rc, 1)
        self.assertIn("lines=62 > measured 61", err)

    def test_expired_exception_fails(self) -> None:
        self.fx.commit_file("lib.c", build_c_function("f", 61), "grow f to 61")
        names = self.fx.export()
        self.fx.set_exception(names[0], lines=61, entered="2026-01-01", expires="2026-01-04")
        rc, out, _ = run_series_cli(
            self.fx.root, "lengths", str(self.fx.tree), "--today", "2026-01-05"
        )
        self.assertEqual(rc, 1)
        self.assertIn("without a live exception", out)

    def test_unreadable_metadata_fails(self) -> None:
        self.fx.commit_file("lib.c", build_c_function("f", 59), "grow f to 59")
        names = self.fx.export()
        sidecar = (self.fx.root / "patches" / names[0]).with_suffix(".toml")
        sidecar.write_text("[[exceptions]\n")
        rc, _, err = run_series_cli(
            self.fx.root, "lengths", str(self.fx.tree), "--today", "2026-10-18"
        )
        self.assertEqual(rc, 1)
        self.assertIn(sidecar.name, err)

    def test_stale_exception_fails(self) -> None:
        lines = build_c_function("f", 58)
        lines[1] = "    /* touched without growing */"
        self.fx.commit_file("lib.c", lines, "touch without growing")
        names = self.fx.export()
        self.fx.set_exception(names[0], lines=65)
        rc, _, err = run_series_cli(
            self.fx.root, "lengths", str(self.fx.tree), "--today", "2026-10-18"
        )
        self.assertEqual(rc, 1)
        self.assertIn("stale exception for lib.c f", err)

    def test_new_function_over_cap_fails(self) -> None:
        lines = build_c_function("f", 58) + [""] + build_c_function("new_f", 61)
        self.fx.commit_file("lib.c", lines, "add new function 61 lines")
        self.fx.export()
        rc, out, _ = run_series_cli(
            self.fx.root, "lengths", str(self.fx.tree), "--today", "2026-10-18"
        )
        self.assertEqual(rc, 1)
        msg = "new_f 0 -> 61 lines, over the 60-line cap (HISS-04) without a live exception"
        self.assertIn(msg, out)

    def test_untouched_and_shortened_functions_not_flagged(self) -> None:
        base_lines = build_c_function("f", 58) + [""] + build_c_function("long_fn", 80)
        self.fx.commit_file("lib.c", base_lines, "base with long_fn")
        run_git(self.fx.tree, "commit", "--amend", "-q", "-m", "base with long_fn")
        self.fx.base = run_git(self.fx.tree, "rev-parse", "HEAD")
        (self.fx.root / "base.env").write_text(
            f"FFMPEG_REMOTE=https://example.invalid/up.git\nFFMPEG_TAG=v1\nFFMPEG_COMMIT={self.fx.base}\n"
        )

        lines_untouched = build_c_function("f", 59) + [""] + build_c_function("long_fn", 80)
        self.fx.commit_file("lib.c", lines_untouched, "grow f, long_fn untouched")
        self.fx.export()
        rc, out, _ = run_series_cli(
            self.fx.root, "lengths", str(self.fx.tree), "--today", "2026-10-18"
        )
        self.assertEqual(rc, 0)
        self.assertIn("0 function(s) over 60 lines, 0 excused, 0 failing", out)

        # Shortened, with one changed line inside, so the patch adds a line to it.
        long_fn = build_c_function("long_fn", 75)
        long_fn[2] = "    int changed = 1;"
        lines_shortened = build_c_function("f", 58) + [""] + long_fn
        run_git(self.fx.tree, "reset", "--hard", self.fx.base)
        self.fx.commit_file("lib.c", lines_shortened, "shorten long_fn")
        self.fx.export()
        rc, out, _ = run_series_cli(
            self.fx.root, "lengths", str(self.fx.tree), "--today", "2026-10-18"
        )
        self.assertEqual(rc, 0)
        self.assertIn("0 function(s) over 60 lines, 0 excused, 0 failing", out)

    def test_multiline_signature_counts_from_first_line(self) -> None:
        lines = build_c_function("multi_f", 61, multiline=True, nested=True)
        self.fx.commit_file("lib.c", lines, "add multi_f 61 lines")
        names = self.fx.export()

        rc, out, _ = run_series_cli(
            self.fx.root, "lengths", str(self.fx.tree), "--today", "2026-10-18"
        )
        self.assertEqual(rc, 1)
        self.assertIn("multi_f 0 -> 61 lines", out)

        self.fx.set_exception(names[0], lines=61, function="multi_f")
        rc, out, _ = run_series_cli(
            self.fx.root, "lengths", str(self.fx.tree), "--today", "2026-10-18"
        )
        self.assertEqual(rc, 0)
        self.assertIn("multi_f 0 -> 61 lines (excused)", out)

    def test_commit_count_mismatch_exits_two(self) -> None:
        self.fx.commit_file("lib.c", build_c_function("f", 59), "commit one")
        self.fx.export()
        self.fx.commit_file("lib.c", build_c_function("f", 60), "commit two unexported")
        rc, _, err = run_series_cli(self.fx.root, "lengths", str(self.fx.tree))
        self.assertEqual(rc, 2)
        self.assertIn("commit count 2 differs from series length 1", err)

    def test_metadata_validation_errors(self) -> None:
        sidecar = self.fx.root / "test.toml"

        def write_sidecar(exc_table: str) -> series.Findings:
            f = series.Findings()
            content = f'status = "unsubmitted"\nconsumers = ["a"]\n\n[[exceptions]]\n{exc_table}\n'
            sidecar.write_text(content)
            series.check_metadata(sidecar, f, today=date(2026, 10, 18))
            return f

        # Category D with entered + 119 days
        f = write_sidecar(
            'rule = "HISS-04"\npath = "lib.c"\nfunction = "f"\nlines = 70\n'
            'category = "D"\nentered = "2026-01-01"\nexpires = "2026-04-30"\nreason = "r"'
        )
        self.assertTrue(any("interval mismatch: category D" in e for e in f.errors))

        # Category A with entered + 91 days
        f = write_sidecar(
            'rule = "HISS-04"\npath = "lib.c"\nfunction = "f"\nlines = 70\n'
            'category = "A"\nentered = "2026-01-01"\nexpires = "2026-04-03"\nreason = "r"'
        )
        self.assertTrue(any("interval mismatch: category A" in e for e in f.errors))

        # Unknown key
        f = write_sidecar(
            'rule = "HISS-04"\npath = "lib.c"\nfunction = "f"\nlines = 70\n'
            'category = "B"\nentered = "2026-10-18"\nexpires = "2026-10-21"\n'
            'reason = "r"\nextra = "bad"'
        )
        self.assertTrue(any("unknown key 'extra'" in e for e in f.errors))

        # lines <= 60
        f = write_sidecar(
            'rule = "HISS-04"\npath = "lib.c"\nfunction = "f"\nlines = 60\n'
            'category = "B"\nentered = "2026-10-18"\nexpires = "2026-10-21"\nreason = "r"'
        )
        self.assertTrue(any("'lines' must be > 60" in e for e in f.errors))

    def test_report_file_appends_summary(self) -> None:
        self.fx.commit_file("lib.c", build_c_function("f", 60), "grow f to 60")
        self.fx.export()
        report_file = self.fx.root / "report.md"
        report_file.write_text("# Initial Report\n\n")
        rc, _, _ = run_series_cli(
            self.fx.root,
            "lengths",
            str(self.fx.tree),
            "--report",
            str(report_file),
            "--today",
            "2026-10-18",
        )
        self.assertEqual(rc, 0)
        content = report_file.read_text()
        expected = "# Initial Report\n\n0 function(s) over 60 lines, 0 excused, 0 failing\n"
        self.assertIn(expected, content)
