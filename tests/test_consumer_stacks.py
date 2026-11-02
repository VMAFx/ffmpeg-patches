# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Tests for scripts/consumer_stacks.py against a small throwaway FFmpeg stand-in."""

from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import textwrap
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

    def handover(self, scratch: Path, *, two_files: bool = False) -> Path:
        """Publish a consumer fix in the series after its old pin, with a new pin too."""
        lines = (self.tree / "lib.c").read_text().splitlines()
        lines[14:16] = ["int line_15 = 150;", "int line_16 = 160;"]
        (self.tree / "lib.c").write_text("\n".join(lines) + "\n")
        if two_files:
            (self.tree / "other.c").write_text("int line_15 = 150;\nint line_16 = 160;\n")
        git(self.tree, "add", ".")
        git(self.tree, "commit", "-q", "-m", "shared consumer fix")
        patch = git(self.tree, "format-patch", "-1", "--stdout") + "\n"
        (self.stack / self.names[0]).write_text(patch)
        self.tip = git(self.tree, "rev-parse", "HEAD")
        root = scratch / "series"
        root.mkdir()
        git(root, "init", "-q")
        (root / "README").write_text("shared series\n")
        git(root, "add", ".")
        git(root, "commit", "-q", "-m", "before handover")
        git(root, "tag", "v0.1.0")
        (root / "patches").mkdir()
        shared = root / "patches" / "0001-shared.patch"
        shared.write_text(patch)
        shared.with_suffix(".toml").write_text('consumers = ["vmafx"]\n')
        git(root, "add", ".")
        git(root, "commit", "-q", "-m", "take consumer fix")
        git(root, "tag", "v0.2.0")
        return shared


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

    def run_handover(
        self, pin: str | None = "v0.1.0", *, report: Path | None = None
    ) -> tuple[int, str, str]:
        pins = ["--pin", f"vmafx={pin}"] if pin else []
        return self.run_main(
            f"vmafx={self.fx.stack}",
            "--series-dir",
            str(self.scratch / "series"),
            *pins,
            report=report,
        )

    def assert_in_series(self, pin: str | None = "v0.1.0") -> None:
        status, out, err = self.run_handover(pin)
        self.assertEqual((status, err), (1, ""))
        self.assertIn(f"vmafx: in-series  {self.fx.names[0]}", out)
        self.assertIn(f"vmafx: not-tried  {self.fx.names[1]}", out)
        self.assertNotIn("::warning::", out)
        self.assertEqual(self.head(), self.fx.tip)

    def test_declared_handover_before_pin_continues_stack(self) -> None:
        shared = self.fx.handover(self.scratch)
        report = self.scratch / "summary.md"
        status, out, err = self.run_handover(report=report)
        self.assertEqual((status, err), (0, ""))
        label = f"{self.fx.names[0]} -> {shared.name}"
        self.assertIn(f"vmafx: handed-over {label}", out)
        self.assertIn(f"| vmafx | `{label}` | handed-over |", report.read_text())
        self.assertIn(f"vmafx: applied    {self.fx.names[1]}", out)
        self.assertEqual(out.count("::warning::"), 1)
        self.assertIn("vmafx must drop", out)
        self.assertIn("next pin bump", out)
        self.assertEqual(self.head(), self.fx.tip)
        self.assertEqual(git(self.fx.tree, "status", "--porcelain"), "")

    def test_handover_requires_declared_consumer(self) -> None:
        shared = self.fx.handover(self.scratch)
        shared.with_suffix(".toml").write_text('consumers = ["pelorus"]\n')
        self.assert_in_series()

    def test_handover_requires_every_added_line(self) -> None:
        shared = self.fx.handover(self.scratch)
        shared.write_text(shared.read_text().replace("+int line_16 = 160;", "+int other = 1;"))
        self.assert_in_series()

    def test_handover_expires_when_pin_contains_patch(self) -> None:
        self.fx.handover(self.scratch)
        self.assert_in_series("v0.2.0")

    def test_handover_rejects_run_across_separate_hunks(self) -> None:
        shared = self.fx.handover(self.scratch)
        shared.write_text(
            "diff --git a/lib.c b/lib.c\n"
            "--- a/lib.c\n+++ b/lib.c\n"
            "@@ -14,2 +14,2 @@\n"
            "-int line_14 = 14;\n-int line_15 = 15;\n"
            "+int line_14 = 140;\n+int line_15 = 150;\n"
            "@@ -30,2 +30,2 @@\n"
            "-int line_30 = 30;\n-int line_31 = 31;\n"
            "+int line_16 = 160;\n+int line_31 = 310;\n"
        )
        added = series.added_runs(shared.read_text())["lib.c"]
        consumer_runs = series.added_runs((self.fx.stack / self.fx.names[0]).read_text())
        self.assertEqual(len(added), 2)
        self.assertEqual(consumer_runs["lib.c"], [[added[0][-1], added[1][0]]])
        self.assert_in_series()

    def test_handover_expires_when_pin_contains_renumbered_patch(self) -> None:
        shared = self.fx.handover(self.scratch)
        shared.rename(shared.with_name("0002-shared.patch"))
        shared.with_suffix(".toml").rename(shared.with_name("0002-shared.toml"))
        self.assert_in_series("v0.2.0")

    def test_handover_requires_consumer_pin(self) -> None:
        self.fx.handover(self.scratch)
        self.assert_in_series(None)

    def test_unknown_pin_is_tool_error(self) -> None:
        self.fx.handover(self.scratch)
        status, out, err = self.run_handover("v9.9.9")
        self.assertEqual((status, out), (2, ""))
        self.assertIn("v9.9.9", err)
        self.assertEqual(self.head(), self.fx.tip)

    def test_handover_requires_matching_additions_in_every_file(self) -> None:
        shared = self.fx.handover(self.scratch, two_files=True)
        shared.write_text(shared.read_text().split("diff --git a/other.c")[0])
        self.assert_in_series()

    def test_handover_requires_sidecar(self) -> None:
        shared = self.fx.handover(self.scratch)
        shared.with_suffix(".toml").unlink()
        self.assert_in_series()

    def test_handover_requires_consumers_list_not_string(self) -> None:
        shared = self.fx.handover(self.scratch)
        shared.with_suffix(".toml").write_text('consumers = "vmafx"\n')
        self.assert_in_series()

    def test_repeated_pins_are_specific_to_each_consumer(self) -> None:
        shared = self.fx.handover(self.scratch)
        shared.with_suffix(".toml").write_text('consumers = ["vmafx", "pelorus"]\n')
        status, out, err = self.run_main(
            f"vmafx={self.fx.stack}",
            f"pelorus={self.fx.stack}",
            "--series-dir",
            str(self.scratch / "series"),
            "--pin",
            "vmafx=v0.1.0",
            "--pin",
            "pelorus=v0.2.0",
        )
        self.assertEqual((status, err), (1, ""))
        self.assertIn("vmafx: handed-over", out)
        self.assertIn("pelorus: in-series", out)
        self.assertEqual(out.count("::warning::"), 1)
        self.assertEqual(self.head(), self.fx.tip)

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


class ConsumerPinWorkflowTest(unittest.TestCase):
    """Execute the workflow's pin-reader shell verbatim against local consumer files."""

    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.env_file = self.root / "env"
        workflow = Path(__file__).resolve().parent.parent / ".github/workflows/consumers.yml"
        step = workflow.read_text().split("      - name: Read the consumer series pins\n")[1]
        self.script = textwrap.dedent(step.split("        run: |\n")[1].split("      - ")[0])
        for name in ("vmafx", "pelorus"):
            (self.root / "consumers" / name).mkdir(parents=True)

    def read_pins(self, vmafx: str, pelorus: str) -> subprocess.CompletedProcess[str]:
        for name, value in (("vmafx", vmafx), ("pelorus", pelorus)):
            (self.root / "consumers" / name / "build-config.env").write_text(value)
        self.env_file.write_text("")
        return subprocess.run(  # nosec B603 B607 - fixed bash argv executes a test fixture script.
            ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", self.script],
            cwd=self.root,
            env={**os.environ, "GITHUB_ENV": str(self.env_file)},
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

    def test_pin_reader_keeps_each_consumers_value_and_strips_quotes(self) -> None:
        for quote in ('"', "'", ""):
            with self.subTest(quote=quote):
                done = self.read_pins(
                    f"FFMPEG_FIX_SERIES_TAG={quote}v1.2.3{quote}\n",
                    f"FFMPEG_SERIES_TAG={quote}v4.5.6{quote}\n",
                )
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertEqual(
                    self.env_file.read_text(),
                    "VMAFX_SERIES_TAG=v1.2.3\nPELORUS_SERIES_TAG=v4.5.6\n",
                )

    def test_pin_reader_refuses_planted_invalid_or_missing_values(self) -> None:
        bad = (
            "",
            "v1.2",
            "v1.2.3-rc.1",
            "xv1.2.3",
            "v1a2b3",
            "v1.2.3 ",
            "$(touch injected)",
            "v1.2.3\nFFMPEG_FIX_SERIES_TAG=v2.0.0",
        )
        for value in bad:
            with self.subTest(value=value):
                done = self.read_pins(
                    f"FFMPEG_FIX_SERIES_TAG={value}\n", "FFMPEG_SERIES_TAG=v4.5.6\n"
                )
                self.assertNotEqual(done.returncode, 0)
                self.assertEqual(self.env_file.read_text(), "")
                self.assertFalse((self.root / "injected").exists())
        for missing in ("", "# FFMPEG_SERIES_TAG=v4.5.6\n", "OTHER_FFMPEG_SERIES_TAG=v4.5.6\n"):
            with self.subTest(missing=missing):
                done = self.read_pins("FFMPEG_FIX_SERIES_TAG=v1.2.3\n", missing)
                self.assertNotEqual(done.returncode, 0)


if __name__ == "__main__":
    unittest.main()
