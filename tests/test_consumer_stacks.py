# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Tests for scripts/consumer_stacks.py against a small throwaway FFmpeg stand-in."""

from __future__ import annotations

import contextlib
import io
import os
import subprocess  # nosec B404 - fixed bash argv runs a test fixture script.
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

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
        lines = (self.tree / "lib.c").read_text(encoding="utf-8").splitlines()
        lines[14:16] = ["int line_15 = 150;", "int line_16 = 160;"]
        (self.tree / "lib.c").write_text("\n".join(lines) + "\n", encoding="utf-8")
        if two_files:
            (self.tree / "other.c").write_text(
                "int line_15 = 150;\nint line_16 = 160;\n", encoding="utf-8"
            )
        git(self.tree, "add", ".")
        git(self.tree, "commit", "-q", "-m", "shared consumer fix")
        patch = git(self.tree, "format-patch", "-1", "--stdout") + "\n"
        (self.stack / self.names[0]).write_text(patch, encoding="utf-8")
        self.tip = git(self.tree, "rev-parse", "HEAD")
        root = scratch / "series"
        root.mkdir()
        git(root, "init", "-q")
        (root / "README").write_text("shared series\n", encoding="utf-8")
        git(root, "add", ".")
        git(root, "commit", "-q", "-m", "before handover")
        git(root, "tag", "v0.1.0")
        (root / "patches").mkdir()
        shared = root / "patches" / "0001-shared.patch"
        shared.write_text(patch, encoding="utf-8")
        shared.with_suffix(".toml").write_text('consumers = ["vmafx"]\n', encoding="utf-8")
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
        """Verify declared handover before pin continues stack."""
        shared = self.fx.handover(self.scratch)
        report = self.scratch / "summary.md"
        status, out, err = self.run_handover(report=report)
        self.assertEqual((status, err), (0, ""))
        label = f"{self.fx.names[0]} -> {shared.name}"
        self.assertIn(f"vmafx: handed-over {label}", out)
        self.assertIn(f"| vmafx | `{label}` | handed-over |", report.read_text(encoding="utf-8"))
        self.assertIn(f"vmafx: applied    {self.fx.names[1]}", out)
        self.assertEqual(out.count("::warning::"), 1)
        self.assertIn("vmafx must drop", out)
        self.assertIn("next pin bump", out)
        self.assertEqual(self.head(), self.fx.tip)
        self.assertEqual(git(self.fx.tree, "status", "--porcelain"), "")

    def test_handover_requires_declared_consumer(self) -> None:
        """Verify handover requires declared consumer."""
        shared = self.fx.handover(self.scratch)
        shared.with_suffix(".toml").write_text('consumers = ["pelorus"]\n', encoding="utf-8")
        self.assert_in_series()

    def test_handover_requires_every_added_line(self) -> None:
        """Verify handover requires every added line."""
        shared = self.fx.handover(self.scratch)
        shared.write_text(
            shared.read_text(encoding="utf-8").replace("+int line_16 = 160;", "+int other = 1;"),
            encoding="utf-8",
        )
        self.assert_in_series()

    def test_handover_expires_when_pin_contains_patch(self) -> None:
        self.fx.handover(self.scratch)
        self.assert_in_series("v0.2.0")

    def test_handover_rejects_run_across_separate_hunks(self) -> None:
        """Verify handover rejects run across separate hunks."""
        shared = self.fx.handover(self.scratch)
        shared.write_text(
            "diff --git a/lib.c b/lib.c\n"
            "--- a/lib.c\n+++ b/lib.c\n"
            "@@ -14,2 +14,2 @@\n"
            "-int line_14 = 14;\n-int line_15 = 15;\n"
            "+int line_14 = 140;\n+int line_15 = 150;\n"
            "@@ -30,2 +30,2 @@\n"
            "-int line_30 = 30;\n-int line_31 = 31;\n"
            "+int line_16 = 160;\n+int line_31 = 310;\n",
            encoding="utf-8",
        )
        added = series.added_runs(shared.read_text(encoding="utf-8"))["lib.c"]
        consumer_runs = series.added_runs(
            (self.fx.stack / self.fx.names[0]).read_text(encoding="utf-8")
        )
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
        """Verify handover requires matching additions in every file."""
        shared = self.fx.handover(self.scratch, two_files=True)
        shared.write_text(
            shared.read_text(encoding="utf-8").split("diff --git a/other.c")[0], encoding="utf-8"
        )
        self.assert_in_series()

    def test_handover_requires_sidecar(self) -> None:
        shared = self.fx.handover(self.scratch)
        shared.with_suffix(".toml").unlink()
        self.assert_in_series()

    def test_handover_requires_consumers_list_not_string(self) -> None:
        """Verify handover requires consumers list not string."""
        shared = self.fx.handover(self.scratch)
        shared.with_suffix(".toml").write_text('consumers = "vmafx"\n', encoding="utf-8")
        self.assert_in_series()

    def test_repeated_pins_are_specific_to_each_consumer(self) -> None:
        """Verify repeated pins are specific to each consumer."""
        shared = self.fx.handover(self.scratch)
        shared.with_suffix(".toml").write_text(
            'consumers = ["vmafx", "pelorus"]\n', encoding="utf-8"
        )
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
        """Verify stacks apply each from the tip and leave the tree there."""
        report = self.scratch / "summary.md"
        status, out, _ = self.run_main(f"a={self.fx.stack}", f"b={self.fx.stack}", report=report)
        self.assertEqual(status, 0)
        for name in ("a", "b"):
            for patch in self.fx.names:
                self.assertIn(f"{name}: applied    {patch}", out)
        self.assertEqual(self.head(), self.fx.tip)
        self.assertEqual(git(self.fx.tree, "status", "--porcelain"), "")
        self.assertIn(f"| b | `{self.fx.names[1]}` | applied |", report.read_text(encoding="utf-8"))

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


class ConsumerWorkflowFixture(unittest.TestCase):
    """Run workflow scripts verbatim with local files and inert command stubs."""

    def setUp(self) -> None:
        """Load workflow steps and prepare local files and command stubs."""
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.env_file = self.root / "env"
        workflow = Path(__file__).resolve().parent.parent / ".github/workflows/consumers.yml"
        self.job = yaml.safe_load(workflow.read_text(encoding="utf-8"))["jobs"]["consumers"]
        self.steps = {step["name"]: step["run"] for step in self.job["steps"] if "run" in step}
        self.workflow_env = self.job.get("env", {})
        self.summary = self.root / "step summary.md"
        self.argv_log = self.root / "argv"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.environment = {
            **os.environ,
            **self.workflow_env,
            "GITHUB_ENV": str(self.env_file),
            "GITHUB_STEP_SUMMARY": str(self.summary),
            "ARGV_LOG": str(self.argv_log),
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "PELORUS_REF": "",
            "VMAFX_REF": "",
        }

    def stub(self, name: str, body: str = "") -> None:
        """Record each invocation as NUL-separated arguments without executing it."""
        path = self.bin / name
        path.write_text(
            '#!/bin/bash\nprintf "%s\\0" "$@" >> "$ARGV_LOG"\nprintf "\\0" >> "$ARGV_LOG"\n' + body,
            encoding="utf-8",
        )
        path.chmod(0o755)

    def calls(self) -> list[list[str]]:
        """Read all stub invocations, preserving spaces and argument order."""
        return [
            call.split("\0")
            for call in self.argv_log.read_text(encoding="utf-8").split("\0\0")
            if call
        ]

    def run_step(
        self, name: str, environment: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        """Execute one extracted step with a deadline and fixture-only overrides."""
        return subprocess.run(  # nosec B603 B607 - fixed bash executes the local workflow script.
            ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", self.steps[name]],
            cwd=self.root,
            env={**self.environment, **(environment or {})},
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )


class ConsumerPinWorkflowTest(ConsumerWorkflowFixture):
    """Execute the workflow's pin-reader shell verbatim against local consumer files."""

    def setUp(self) -> None:
        """Prepare directories for both default consumers' pin files."""
        super().setUp()
        for name in ("vmafx", "pelorus"):
            (self.root / "consumers" / name).mkdir(parents=True)

    def read_pins(self, vmafx: str, pelorus: str) -> subprocess.CompletedProcess[str]:
        """Write both consumer configurations and execute the pin reader."""
        for name, value in (("vmafx", vmafx), ("pelorus", pelorus)):
            (self.root / "consumers" / name / "build-config.env").write_text(
                value, encoding="utf-8"
            )
        self.env_file.write_text("", encoding="utf-8")
        return self.run_step("Read the consumer series pins")

    def test_pin_reader_uses_added_consumers_variable(self) -> None:
        """An added map entry supplies its own pin variable without a case branch."""
        root = self.root / "consumers" / "third"
        root.mkdir()
        (root / "build-config.env").write_text(
            "UNRELATED=v9.9.9\nTHIRD_PIN=v7.8.9\n", encoding="utf-8"
        )
        done = self.run_step("Read the consumer series pins", {"CONSUMERS": "third:THIRD_PIN"})
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.env_file.read_text(encoding="utf-8"), "THIRD_SERIES_TAG=v7.8.9\n")

    def test_pin_reader_keeps_each_consumers_value_and_strips_quotes(self) -> None:
        """Verify pin reader keeps each consumers value and strips quotes."""
        for quote in ('"', "'", ""):
            with self.subTest(quote=quote):
                done = self.read_pins(
                    f"FFMPEG_FIX_SERIES_TAG={quote}v1.2.3{quote}\n",
                    f"FFMPEG_SERIES_TAG={quote}v4.5.6{quote}\n",
                )
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertEqual(
                    self.env_file.read_text(encoding="utf-8"),
                    "VMAFX_SERIES_TAG=v1.2.3\nPELORUS_SERIES_TAG=v4.5.6\n",
                )

    def test_pin_reader_refuses_planted_invalid_or_missing_values(self) -> None:
        """Verify pin reader refuses planted invalid or missing values."""
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
                self.assertEqual(self.env_file.read_text(encoding="utf-8"), "")
                self.assertFalse((self.root / "injected").exists())
        for missing in ("", "# FFMPEG_SERIES_TAG=v4.5.6\n", "OTHER_FFMPEG_SERIES_TAG=v4.5.6\n"):
            with self.subTest(missing=missing):
                done = self.read_pins("FFMPEG_FIX_SERIES_TAG=v1.2.3\n", missing)
                self.assertNotEqual(done.returncode, 0)


class ConsumerRefWorkflowTest(ConsumerWorkflowFixture):
    """Validate both dispatch refs before either reaches the GitHub environment."""

    ref_step = "Check the consumer refs"

    def _write_ref_file(self, consumer: str, text: str) -> Path:
        """Write consumer ref text to .github/consumer-refs and return the file path."""
        ref_dir = self.root / ".github" / "consumer-refs"
        ref_dir.mkdir(parents=True, exist_ok=True)
        path = ref_dir / consumer
        path.unlink(missing_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_invalid_refs_write_neither_environment_line(self) -> None:
        """Reject planted invalid branch names for each dispatch input."""
        invalid = ("-x", "a..b", "a b", "a;b", "$(id)", "x.lock", "/x", "x/", "@{u", "a" * 201)
        for name in ("pelorus_ref", "vmafx_ref"):
            for ref in invalid:
                with self.subTest(name=name, ref=ref):
                    self.env_file.unlink(missing_ok=True)
                    done = self.run_step(self.ref_step, {name.upper(): ref})
                    self.assertNotEqual(done.returncode, 0)
                    self.assertIn(f"::error::{name} is not a plain branch name", done.stdout)
                    self.assertEqual(
                        self.env_file.read_text(encoding="utf-8") if self.env_file.exists() else "",
                        "",
                    )

    def test_valid_refs_write_exactly_both_environment_lines(self) -> None:
        """Accept empty, named, and maximum-length refs for both consumers."""
        valid = ("", "chore/ffmpeg-drop-qpfile-loopback", "a", "a" * 200)
        for pelorus, vmafx in ((ref, ref) for ref in valid):
            with self.subTest(pelorus=pelorus, vmafx=vmafx):
                self.env_file.unlink(missing_ok=True)
                done = self.run_step(self.ref_step, {"PELORUS_REF": pelorus, "VMAFX_REF": vmafx})
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertEqual(
                    self.env_file.read_text(encoding="utf-8"),
                    f"PELORUS_REF={pelorus}\nVMAFX_REF={vmafx}\n",
                )

    def test_bad_vmafx_ref_rejects_good_pelorus_ref_without_writing(self) -> None:
        """Validate the complete pair before writing either ref."""
        done = self.run_step(self.ref_step, {"PELORUS_REF": "ci/consumer", "VMAFX_REF": "a..b"})
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("::error::vmafx_ref is not a plain branch name", done.stdout)
        self.assertFalse(self.env_file.exists())

    def test_dispatch_declares_optional_vmafx_ref(self) -> None:
        """Expose an optional string input with an empty default."""
        workflow = Path(__file__).resolve().parent.parent / ".github/workflows/consumers.yml"
        inputs = yaml.safe_load(workflow.read_text(encoding="utf-8"))["on"]["workflow_dispatch"][
            "inputs"
        ]
        self.assertEqual(
            {key: inputs["vmafx_ref"][key] for key in ("type", "required", "default")},
            {"type": "string", "required": False, "default": ""},
        )

    def test_valid_ref_file_sets_environment_and_warns(self) -> None:
        """Accept valid branch names from consumer ref files and emit warnings."""
        branch = "chore/ffmpeg-drop-qpfile-loopback"
        ref_dir = self.root / ".github" / "consumer-refs"
        for consumer in ("vmafx", "pelorus"):
            for newline in ("", "\n"):
                with self.subTest(consumer=consumer, newline=bool(newline)):
                    (ref_dir / "vmafx").unlink(missing_ok=True)
                    (ref_dir / "pelorus").unlink(missing_ok=True)
                    self.env_file.unlink(missing_ok=True)
                    self.summary.unlink(missing_ok=True)
                    self._write_ref_file(consumer, f"{branch}{newline}")
                    done = self.run_step(self.ref_step)
                    self.assertEqual(done.returncode, 0, done.stderr)
                    expected_env = (
                        f"PELORUS_REF={branch}\nVMAFX_REF=\n"
                        if consumer == "pelorus"
                        else f"PELORUS_REF=\nVMAFX_REF={branch}\n"
                    )
                    self.assertEqual(self.env_file.read_text(encoding="utf-8"), expected_env)
                    warning = (
                        f"::warning::{consumer} consumer ref {branch} comes from "
                        f".github/consumer-refs/{consumer}; reset it after the consumer lands"
                    )
                    self.assertIn(warning, done.stdout)
                    self.assertIn(warning, self.summary.read_text(encoding="utf-8"))

    def test_invalid_ref_file_rejects_planted_values(self) -> None:
        """Reject planted invalid ref file values without writing environment."""
        invalid = (
            "-x",
            "a..b",
            "a b",
            "a;b",
            "$(id)",
            "x.lock",
            "/x",
            "x/",
            "@{u",
            "a" * 201,
            "a\nb",
            " ",
            "a" * 300,
        )
        for value in invalid:
            with self.subTest(value=value):
                self.env_file.unlink(missing_ok=True)
                self._write_ref_file("vmafx", value)
                done = self.run_step(self.ref_step)
                self.assertNotEqual(done.returncode, 0)
                self.assertIn("::error::vmafx_ref is not a plain branch name", done.stdout)
                self.assertEqual(
                    self.env_file.read_text(encoding="utf-8") if self.env_file.exists() else "",
                    "",
                )

    def test_symlink_ref_file_is_rejected(self) -> None:
        """Reject symlinked consumer ref files before reading branch names."""
        target = self.root / "target_ref"
        target.write_text("chore/ffmpeg-drop-qpfile-loopback\n", encoding="utf-8")
        ref_dir = self.root / ".github" / "consumer-refs"
        ref_dir.mkdir(parents=True, exist_ok=True)
        ref_file = ref_dir / "vmafx"
        ref_file.unlink(missing_ok=True)
        ref_file.symlink_to(target)
        self.env_file.unlink(missing_ok=True)
        done = self.run_step(self.ref_step)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("::error::vmafx_ref file is not a regular file", done.stdout)
        self.assertEqual(
            self.env_file.read_text(encoding="utf-8") if self.env_file.exists() else "",
            "",
        )

    def test_unreadable_ref_file_is_rejected(self) -> None:
        """Reject unreadable consumer ref files and emit error annotation."""
        if os.geteuid() == 0:
            self.skipTest("root bypasses mode 000 file permissions")
        path = self._write_ref_file("vmafx", "chore/ffmpeg-drop-qpfile-loopback\n")
        self.addCleanup(path.chmod, 0o644)
        path.chmod(0o000)
        self.env_file.unlink(missing_ok=True)
        done = self.run_step(self.ref_step)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("::error::vmafx_ref file could not be read", done.stdout)
        self.assertEqual(
            self.env_file.read_text(encoding="utf-8") if self.env_file.exists() else "",
            "",
        )

    def test_ref_file_read_timeout_is_rejected(self) -> None:
        """Reject consumer ref file when reading exceeds the execution timeout."""
        self._write_ref_file("vmafx", "chore/ffmpeg-drop-qpfile-loopback\n")
        timeout_dir = self.root / "timeout_bin"
        timeout_dir.mkdir(parents=True, exist_ok=True)
        stub = timeout_dir / "timeout"
        stub.write_text("#!/bin/sh\nexit 124\n", encoding="utf-8")
        stub.chmod(0o755)
        self.env_file.unlink(missing_ok=True)
        done = self.run_step(
            self.ref_step,
            {"PATH": f"{timeout_dir}{os.pathsep}{self.environment['PATH']}"},
        )
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("::error::vmafx_ref file could not be read", done.stdout)
        self.assertEqual(
            self.env_file.read_text(encoding="utf-8") if self.env_file.exists() else "",
            "",
        )

    def test_absent_ref_file_with_empty_input_leaves_refs_empty(self) -> None:
        """Default to empty consumer refs when no input or ref file is present."""
        self.env_file.unlink(missing_ok=True)
        done = self.run_step(self.ref_step, {"PELORUS_REF": "", "VMAFX_REF": ""})
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.env_file.read_text(encoding="utf-8"), "PELORUS_REF=\nVMAFX_REF=\n")

    def test_dispatch_input_precedes_ref_file(self) -> None:
        """Dispatch input takes precedence over a committed consumer ref file."""
        self._write_ref_file("vmafx", "ci/y\n")
        self.env_file.unlink(missing_ok=True)
        self.summary.unlink(missing_ok=True)
        done = self.run_step(self.ref_step, {"VMAFX_REF": "ci/x"})
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(
            self.env_file.read_text(encoding="utf-8"), "PELORUS_REF=\nVMAFX_REF=ci/x\n"
        )
        self.assertNotIn("::warning::", done.stdout)
        summary_text = self.summary.read_text(encoding="utf-8") if self.summary.exists() else ""
        self.assertNotIn("::warning::", summary_text)

    def test_valid_input_ignores_invalid_ref_file(self) -> None:
        """Non-empty dispatch input skips reading an invalid consumer ref file."""
        self._write_ref_file("vmafx", "a..b\n")
        done = self.run_step(self.ref_step, {"VMAFX_REF": "ci/x"})
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(
            self.env_file.read_text(encoding="utf-8"), "PELORUS_REF=\nVMAFX_REF=ci/x\n"
        )


class ConsumerMapWorkflowTest(ConsumerWorkflowFixture):
    """Keep consumer configuration centralized and execute the fetch/apply contracts."""

    fetch_step = "Fetch the consumer stacks"
    pin_step = "Read the consumer series pins"
    apply_step = "Apply each consumer stack on top of the series"

    def test_fetch_reads_consumers_after_blank_lines(self) -> None:
        """A preserved newline in the folded map must not truncate the clones."""
        self.stub_git()
        done = self.run_step(
            self.fetch_step, {"CONSUMERS": "alpha:PIN_A\n\n beta:PIN_B\tgamma:PIN_C\n"}
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        clones = [call for call in self.calls() if call[0] == "clone"]
        self.assertEqual(
            [call[-1] for call in clones],
            ["consumers/alpha", "consumers/beta", "consumers/gamma"],
        )

    def test_pin_reader_reads_consumers_after_blank_lines(self) -> None:
        """Every whitespace-separated entry supplies its pin, including later lines."""
        for name, variable, tag in (
            ("alpha", "PIN_A", "v1.2.3"),
            ("beta", "PIN_B", "v4.5.6"),
            ("gamma", "PIN_C", "v7.8.9"),
        ):
            root = self.root / "consumers" / name
            root.mkdir(parents=True)
            (root / "build-config.env").write_text(f"{variable}={tag}\n", encoding="utf-8")
        done = self.run_step(
            self.pin_step, {"CONSUMERS": "alpha:PIN_A\n\n beta:PIN_B\tgamma:PIN_C\n"}
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(
            self.env_file.read_text(encoding="utf-8"),
            "ALPHA_SERIES_TAG=v1.2.3\nBETA_SERIES_TAG=v4.5.6\nGAMMA_SERIES_TAG=v7.8.9\n",
        )

    def test_apply_reads_consumers_after_blank_lines(self) -> None:
        """Later lines reach the apply command as ordered stack and pin arguments."""
        self.stub("python3")
        done = self.run_step(
            self.apply_step,
            {
                "CONSUMERS": "alpha:PIN_A\n\n beta:PIN_B\tgamma:PIN_C\n",
                "ALPHA_SERIES_TAG": "v1.2.3",
                "BETA_SERIES_TAG": "v4.5.6",
                "GAMMA_SERIES_TAG": "v7.8.9",
            },
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(
            self.calls(),
            [
                [
                    "scripts/consumer_stacks.py",
                    "ffmpeg",
                    "alpha=consumers/alpha/ffmpeg-patches",
                    "beta=consumers/beta/ffmpeg-patches",
                    "gamma=consumers/gamma/ffmpeg-patches",
                    "--series-dir",
                    ".",
                    "--pin",
                    "alpha=v1.2.3",
                    "--pin",
                    "beta=v4.5.6",
                    "--pin",
                    "gamma=v7.8.9",
                    "--report",
                    str(self.summary),
                ]
            ],
        )

    def test_fetch_refuses_duplicate_names_before_any_git_call(self) -> None:
        """Repeated names fail whether their pin variables match or differ."""
        self.stub_git()
        for entries in (
            "alpha:PIN_A alpha:PIN_A",
            "alpha:PIN_A beta:PIN_B alpha:OTHER_PIN",
            "alpha:PIN_A\n\nalpha:OTHER_PIN",
        ):
            with self.subTest(entries=entries):
                self.argv_log.unlink(missing_ok=True)
                done = self.run_step(self.fetch_step, {"CONSUMERS": entries})
                self.assertNotEqual(done.returncode, 0)
                self.assertIn("::error::duplicate consumer name: alpha", done.stdout)
                self.assertFalse(self.argv_log.exists())

    def test_fetch_refuses_reserved_keys_before_any_git_call(self) -> None:
        """Derived GITHUB_* and RUNNER_* environment keys cannot reach a clone."""
        self.stub_git()
        for name in ("github", "github-actions", "runner", "runner-cache"):
            with self.subTest(name=name):
                self.argv_log.unlink(missing_ok=True)
                done = self.run_step(self.fetch_step, {"CONSUMERS": f"valid:PIN {name}:PIN"})
                self.assertNotEqual(done.returncode, 0)
                self.assertIn("::error::reserved consumer environment key:", done.stdout)
                self.assertFalse(self.argv_log.exists())

    def test_fetch_limits_map_to_sixteen_entries_before_any_git_call(self) -> None:
        """One and sixteen entries pass; a seventeenth fails before any clone."""
        self.stub_git()
        names = ["githubx", "runnerx", *(f"consumer-{index}" for index in range(15))]
        for count in (1, 16, 17):
            with self.subTest(count=count):
                self.argv_log.unlink(missing_ok=True)
                entries = " ".join(f"{name}:PIN" for name in names[:count])
                done = self.run_step(self.fetch_step, {"CONSUMERS": entries})
                if count <= 16:
                    self.assertEqual(done.returncode, 0, done.stderr)
                    clones = [call for call in self.calls() if call[0] == "clone"]
                    self.assertEqual(
                        [call[-1] for call in clones],
                        [f"consumers/{name}" for name in names[:count]],
                    )
                else:
                    self.assertNotEqual(done.returncode, 0)
                    self.assertIn("::error::consumer map must contain 1 to 16 entries", done.stdout)
                    self.assertFalse(self.argv_log.exists())

    def test_fetch_refuses_empty_map_before_any_git_call(self) -> None:
        """Empty and whitespace-only maps must not silently pass the fetch step."""
        self.stub_git()
        for entries in ("", " \n\t "):
            with self.subTest(entries=entries):
                self.argv_log.unlink(missing_ok=True)
                done = self.run_step(self.fetch_step, {"CONSUMERS": entries})
                self.assertNotEqual(done.returncode, 0)
                self.assertIn("::error::consumer map must contain 1 to 16 entries", done.stdout)
                self.assertFalse(self.argv_log.exists())

    def test_hyphenated_consumer_pin_reaches_apply(self) -> None:
        """A hyphenated consumer shares a valid environment key across both steps."""
        root = self.root / "consumers" / "foo-bar"
        root.mkdir(parents=True)
        (root / "build-config.env").write_text("FOO_TAG=v1.2.3\n", encoding="utf-8")
        environment = {"CONSUMERS": "foo-bar:FOO_TAG"}
        pinned = self.run_step(self.pin_step, environment)
        self.assertEqual(pinned.returncode, 0, pinned.stderr)
        self.assertEqual(self.env_file.read_text(encoding="utf-8"), "FOO_BAR_SERIES_TAG=v1.2.3\n")
        environment.update(
            line.split("=", 1) for line in self.env_file.read_text(encoding="utf-8").splitlines()
        )
        self.stub("python3")
        applied = self.run_step(self.apply_step, environment)
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertEqual(
            self.calls(),
            [
                [
                    "scripts/consumer_stacks.py",
                    "ffmpeg",
                    "foo-bar=consumers/foo-bar/ffmpeg-patches",
                    "--series-dir",
                    ".",
                    "--pin",
                    "foo-bar=v1.2.3",
                    "--report",
                    str(self.summary),
                ]
            ],
        )

    def test_owner_defaults_to_repository_owner(self) -> None:
        """Forks resolve consumer repositories under their own owner by default."""
        self.assertEqual(self.workflow_env.get("CONSUMER_OWNER"), "${{ github.repository_owner }}")

    def test_consumer_steps_share_the_job_map(self) -> None:
        """All three scripts use the map without a fixed owner or consumer loop."""
        self.assertTrue(self.workflow_env.get("CONSUMERS"))
        for name in (self.fetch_step, self.pin_step, self.apply_step):
            with self.subTest(step=name):
                script = self.steps[name]
                self.assertRegex(script, r"\$(?:CONSUMERS\b|\{CONSUMERS\})")
                self.assertNotIn("VMAFx/", script)
                self.assertNotIn("vmafx pelorus", script)

    def test_apply_preserves_map_order_and_argument_boundaries(self) -> None:
        """One and three consumers each produce ordered stack and pin arguments."""
        self.stub("python3")
        consumers = (("delta", "v1.2.3"), ("alpha", "v4.5.6"), ("omega", "v7.8.9"))
        for count in (1, 3):
            with self.subTest(count=count):
                self.argv_log.unlink(missing_ok=True)
                selected = consumers[:count]
                environment = {f"{name.upper()}_SERIES_TAG": tag for name, tag in selected}
                environment["CONSUMERS"] = " ".join(
                    f"{name}:PIN_{name.upper()}" for name, _ in selected
                )
                done = self.run_step(self.apply_step, environment)
                self.assertEqual(done.returncode, 0, done.stderr)
                expected = ["scripts/consumer_stacks.py", "ffmpeg"]
                expected += [f"{name}=consumers/{name}/ffmpeg-patches" for name, _ in selected]
                expected += ["--series-dir", "."]
                expected += [arg for name, tag in selected for arg in ("--pin", f"{name}={tag}")]
                expected += ["--report", str(self.summary)]
                self.assertEqual(self.calls(), [expected])

    def stub_git(self) -> None:
        """Create only the checkout root and pin file needed by the fetch script."""
        self.stub(
            "git",
            'if [ "$1" = clone ]; then\n'
            '  mkdir -p "${@: -1}"\n'
            '  : > "${@: -1}/build-config.env"\n'
            "fi\n",
        )

    def test_fetch_refuses_malformed_map_before_any_git_call(self) -> None:
        """Malformed entries fail even after a valid entry, before Git executes."""
        self.stub_git()
        malformed = (
            "Vmafx:X",
            "vmafx",
            "vmafx:lower",
            "a;b:X",
            ":X",
            "vmafx:",
            f"{'a' * 40}:X",
            f"a:{'A' * 65}",
            "a:X:Y",
        )
        for entry in malformed:
            for prefix in ("", "valid:PIN "):
                with self.subTest(entry=entry, prefix=prefix):
                    self.argv_log.unlink(missing_ok=True)
                    done = self.run_step(self.fetch_step, {"CONSUMERS": prefix + entry})
                    self.assertNotEqual(done.returncode, 0)
                    self.assertIn("::error::", done.stdout)
                    self.assertFalse(self.argv_log.exists())

    def test_fetch_uses_owner_and_keeps_each_dispatch_ref(self) -> None:
        """Clone each named consumer at its own ref and no other consumer at a ref."""
        self.stub_git()
        for pelorus, vmafx in (
            ("", ""),
            ("ci/consumer", ""),
            ("", "chore/ffmpeg-drop-qpfile-loopback"),
            ("p/x", "v/y"),
        ):
            with self.subTest(pelorus=pelorus, vmafx=vmafx):
                self.argv_log.unlink(missing_ok=True)
                done = self.run_step(
                    self.fetch_step,
                    {
                        "CONSUMER_OWNER": "example-fork",
                        "CONSUMERS": self.workflow_env["CONSUMERS"] + " other:OTHER_PIN",
                        "PELORUS_REF": pelorus,
                        "VMAFX_REF": vmafx,
                        "GITHUB_REF": "refs/heads/poison",
                        "OTHER_REF": "poison",
                    },
                )
                self.assertEqual(done.returncode, 0, done.stderr)
                clones = [call for call in self.calls() if call[0] == "clone"]
                names = ["vmafx", "pelorus", "other"]
                expected = []
                for name in names:
                    ref = {"pelorus": pelorus, "vmafx": vmafx}.get(name, "")
                    branch = ["--branch", ref] if ref else []
                    expected.append(
                        [
                            "clone",
                            "-q",
                            "--depth=1",
                            "--filter=blob:none",
                            "--sparse",
                            *branch,
                            f"https://github.com/example-fork/{name}.git",
                            f"consumers/{name}",
                        ]
                    )
                self.assertEqual(clones, expected)

    def test_fetch_accepts_name_and_variable_length_boundaries(self) -> None:
        """Minimal and maximum-length map fields pass validation and reach Git."""
        self.stub_git()
        for name, variable in (("a", "X"), ("a" + "b" * 38, "A" + "B" * 63)):
            with self.subTest(name=name, variable=variable):
                self.argv_log.unlink(missing_ok=True)
                done = self.run_step(
                    self.fetch_step,
                    {"CONSUMERS": f"{name}:{variable}", "CONSUMER_OWNER": "boundary-fork"},
                )
                self.assertEqual(done.returncode, 0, done.stderr)
                clones = [call for call in self.calls() if call[0] == "clone"]
                self.assertEqual(len(clones), 1)
                self.assertEqual(clones[0][-2], f"https://github.com/boundary-fork/{name}.git")


if __name__ == "__main__":
    unittest.main()
