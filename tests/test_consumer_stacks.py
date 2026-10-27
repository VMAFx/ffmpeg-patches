# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Tests for scripts/consumer_stacks.py against a small throwaway FFmpeg stand-in."""

from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import consumer_stacks  # noqa: E402
import series  # noqa: E402

from tests.helpers import run_git as git  # noqa: E402

LINES = [f"int line_{n} = {n};" for n in range(1, 41)]


def edit(tree: Path, index: int, text: str, subject: str) -> None:
    """Commit one changed line of lib.c, written with LF line ends on every system."""
    lines = (tree / "lib.c").read_bytes().decode().splitlines()
    lines[index] = text
    (tree / "lib.c").write_bytes(("\n".join(lines) + "\n").encode())
    git(tree, "commit", "-q", "-am", subject)


class StackFixture:
    """A tree at the series tip and one consumer stack of two patches made on it."""

    def __init__(self, scratch: Path) -> None:
        self.tree = scratch / "ffmpeg"
        self.tree.mkdir()
        git(self.tree, "init", "-q")
        (self.tree / "lib.c").write_bytes(("\n".join(LINES) + "\n").encode())
        git(self.tree, "add", "lib.c")
        git(self.tree, "commit", "-q", "-m", "base")
        edit(self.tree, 4, "int line_5 = 50;", "series: fix line five")
        self.tip = git(self.tree, "rev-parse", "HEAD")
        self.stack = scratch / "consumer"
        self.stack.mkdir()
        edit(self.tree, 14, "int line_15 = 150;", "consumer: change line fifteen")
        edit(self.tree, 29, "int line_30 = 300;", "consumer: change line thirty")
        git(self.tree, "format-patch", "-q", "-o", str(self.stack), f"{self.tip}..HEAD")
        self.names = sorted(path.name for path in self.stack.glob("*.patch"))
        self.write_list(["# consumer stack", "", *self.names])
        git(self.tree, "checkout", "-q", "--detach", self.tip)

    def write_list(self, lines: list[str]) -> None:
        (self.stack / "series.txt").write_bytes(("\n".join(lines) + "\n").encode())

    def series_change(self, index: int, text: str) -> None:
        """Move the series tip: one more series commit under the consumer stack."""
        edit(self.tree, index, text, "series: another fix")
        self.tip = git(self.tree, "rev-parse", "HEAD")


class ConsumerStacksTest(unittest.TestCase):
    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.scratch = Path(scratch.name)
        self.fx = StackFixture(self.scratch)

    def run_main(self, *consumers: str, report: Path | None = None) -> tuple[int, str, str]:
        argv = [str(self.fx.tree), *consumers]
        argv += ["--report", str(report)] if report else []
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = consumer_stacks.main(argv)
        return status, out.getvalue(), err.getvalue()

    def head(self) -> str:
        return git(self.fx.tree, "rev-parse", "HEAD")

    # Positive.

    def test_stacks_apply_each_from_the_tip_and_leave_the_tree_there(self) -> None:
        report = self.scratch / "summary.md"
        status, out, _ = self.run_main(f"a={self.fx.stack}", f"b={self.fx.stack}", report=report)
        self.assertEqual(status, 0)
        for name in ("a", "b"):
            for patch in self.fx.names:
                self.assertIn(f"{name}: applied    {patch}", out)
        self.assertEqual(self.head(), self.fx.tip)
        self.assertEqual(git(self.fx.tree, "status", "--porcelain"), "")
        self.assertIn(f"| b | `{self.fx.names[1]}` | applied |", report.read_text())

    # Negative.

    def test_series_change_under_a_consumer_patch_breaks_its_stack(self) -> None:
        self.fx.series_change(14, "int line_15 = -15;")
        status, out, _ = self.run_main(f"pelorus={self.fx.stack}")
        self.assertEqual(status, 1)
        self.assertIn(f"pelorus: conflict   {self.fx.names[0]}", out)
        self.assertIn(f"pelorus: not-tried  {self.fx.names[1]}", out)
        self.assertIn("::error title=Consumer stack broken::pelorus no longer applies", out)
        self.assertEqual(self.head(), self.fx.tip)
        self.assertFalse(series.am_in_progress(self.fx.tree))

    def test_consumer_patch_the_series_took_over_is_in_series(self) -> None:
        self.fx.series_change(29, "int line_30 = 300;")
        status, out, _ = self.run_main(f"vmafx={self.fx.stack}")
        self.assertEqual(status, 1)
        self.assertIn(f"vmafx: applied    {self.fx.names[0]}", out)
        self.assertIn(f"vmafx: in-series  {self.fx.names[1]}", out)

    def test_one_broken_consumer_does_not_hide_the_other(self) -> None:
        broken = self.scratch / "broken"
        broken.mkdir()
        patch = (self.fx.stack / self.fx.names[0]).read_bytes()
        (broken / "0001-x.patch").write_bytes(patch.replace(b"int line_15 = 15;", b"int nope;"))
        (broken / "series.txt").write_bytes(b"0001-x.patch\n")
        status, out, _ = self.run_main(f"a={broken}", f"b={self.fx.stack}")
        self.assertEqual(status, 1)
        self.assertIn("a: conflict   0001-x.patch", out)
        self.assertIn(f"b: applied    {self.fx.names[1]}", out)
        self.assertNotIn("Consumer stack broken::b", out)

    def assert_tool_error(self, needle: str) -> None:
        status, out, err = self.run_main(f"a={self.fx.stack}")
        self.assertEqual((status, out), (2, ""))
        self.assertIn(needle, err)

    def test_listed_patch_missing_is_a_tool_error(self) -> None:
        self.fx.write_list([*self.fx.names, "0009-gone.patch"])
        self.assert_tool_error("listed patch missing: 0009-gone.patch")

    def test_missing_series_txt_is_a_tool_error(self) -> None:
        (self.fx.stack / "series.txt").unlink()
        self.assert_tool_error("series.txt")

    def test_unclean_tree_is_a_tool_error(self) -> None:
        (self.fx.tree / "lib.c").write_bytes(b"dirty\n")
        self.assert_tool_error("local changes")

    def test_consumer_argument_must_be_name_equals_dir(self) -> None:
        for text in ("pelorus", "=dir", "pelorus="):
            with (
                self.subTest(text=text),
                self.assertRaises(SystemExit),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                consumer_stacks.main([str(self.fx.tree), text])

    # Boundary.

    def test_list_without_a_patch_is_a_tool_error(self) -> None:
        self.fx.write_list(["# nothing", ""])
        self.assert_tool_error("lists no patch")

    def test_relative_stack_directory_is_read_from_the_working_directory(self) -> None:
        # CI passes consumers/<name>/ffmpeg-patches relative to the checkout,
        # while git am runs inside the FFmpeg tree.
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.scratch)
        status, out, _ = self.run_main(f"a={self.fx.stack.name}")
        self.assertEqual(status, 0)
        self.assertEqual(out.count("applied"), 2)

    def test_comments_and_blank_lines_are_skipped(self) -> None:
        self.fx.write_list(["", "# a", *self.fx.names[:1], "   ", "# b"])
        status, out, _ = self.run_main(f"a={self.fx.stack}")
        self.assertEqual(status, 0)
        self.assertEqual(out.count("applied"), 1)


if __name__ == "__main__":
    unittest.main()
