# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Exercise lane brief rendering, validation, and error handling."""

from __future__ import annotations

import contextlib
import io
import os
import signal
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import lane_brief

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "scripts" / "lane_brief_template.md"
CONFIG = ROOT / ".config" / "lane-brief.yaml"
ARGS = [
    "render",
    "--branch",
    "feature",
    "--task",
    "work",
    "--finding",
    "problem",
    "--fix",
    "repair",
    "--tests",
    "unit",
    "--modules",
    "tests.test_example tests.test_other",
    "--paths",
    "scripts/a.py",
    "--issue",
    "42",
    "--round",
    "2",
]


class LaneBriefTest(unittest.TestCase):
    """Check the CLI against real declarations and temporary briefs."""

    def setUp(self) -> None:
        """Create a temporary directory for input and output files."""
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def run_cli(self, *args: str) -> tuple[int, str, str]:
        """Capture the command result and both output streams."""
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                rc = lane_brief.main(list(args))
            except SystemExit as error:
                rc = int(error.code)
        return rc, stdout.getvalue(), stderr.getvalue()

    def rendered(self) -> str:
        """Render a complete brief and return its contents."""
        rc, output, error = self.run_cli(*ARGS)
        self.assertEqual(rc, 0, error)
        return output

    @unittest.skipUnless(
        hasattr(os, "mkfifo") and hasattr(signal, "SIGALRM"), "FIFO alarm unavailable"
    )
    def test_brief_fifo_rejected(self) -> None:
        """Reject a FIFO brief without waiting for a writer."""
        brief = self.root / "brief.fifo"
        os.mkfifo(brief)
        previous = signal.signal(signal.SIGALRM, self._alarm_handler)
        try:
            signal.alarm(2)
            rc, _, error = self.run_cli("check", str(brief))
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, previous)
        self.assertEqual(rc, 2)
        self.assertIn("not a regular file", error)

    @staticmethod
    def _alarm_handler(signum: int, frame: object) -> None:
        """Interrupt a blocked FIFO read during the regression test."""
        raise TimeoutError("FIFO read blocked")

    def test_render_and_check(self) -> None:
        """Render all arguments and validate the result against repository standards."""
        brief = self.root / "brief.md"
        brief.write_text(self.rendered(), encoding="utf-8")
        self.assertEqual(self.run_cli("check", str(brief))[0], 0)
        self.assertIn("issue #42, round 2", brief.read_text(encoding="utf-8"))
        self.assertIn(
            "python -m unittest tests.test_example tests.test_other",
            brief.read_text(encoding="utf-8"),
        )
        self.assertNotIn("<modules>", brief.read_text(encoding="utf-8"))

    def test_each_missing_required_line_fails(self) -> None:
        """Reject each independently removed required substring."""
        content = self.rendered()
        brief = self.root / "brief.md"
        for required in lane_brief.read_required(CONFIG):
            with self.subTest(required=required):
                brief.write_text(content.replace(required, ""), encoding="utf-8")
                rc, _, error = self.run_cli("check", str(brief))
                self.assertEqual(rc, 1)
                self.assertIn(f"missing required line: {required}", error)

    def test_template_has_unfilled_placeholders(self) -> None:
        """Reject the unrendered template and identify its placeholders."""
        rc, _, error = self.run_cli("check", str(TEMPLATE))
        self.assertEqual(rc, 1)
        for placeholder in lane_brief.NAMES:
            self.assertIn(f"unfilled placeholder <{placeholder}>", error)

    def test_missing_inputs_fail(self) -> None:
        """Return usage errors for absent files and absent or empty required lists."""
        brief = self.root / "brief.md"
        brief.write_text(self.rendered(), encoding="utf-8")
        standards = self.root / "config.yaml"
        cases = [
            ("check", str(self.root / "missing")),
            ("check", str(brief), "--config", str(self.root / "missing")),
        ]
        for args in cases:
            with self.subTest(args=args):
                self.assertEqual(self.run_cli(*args)[0], 2)
        for content in ("other: value\n", "required:\n"):
            with self.subTest(content=content):
                standards.write_text(content, encoding="utf-8")
                rc, _, error = self.run_cli("check", str(brief), "--config", str(standards))
                self.assertEqual(rc, 2)
                self.assertIn("required list", error)

    def test_render_requires_arguments(self) -> None:
        """Reject a render command missing required arguments."""
        self.assertEqual(self.run_cli("render", "--branch", "feature")[0], 2)
        args = ARGS.copy()
        index = args.index("--modules")
        del args[index : index + 2]
        self.assertEqual(self.run_cli(*args)[0], 2)

    def test_directory_brief_rejected(self) -> None:
        """Reject a directory as a brief."""
        rc, _, error = self.run_cli("check", str(self.root))
        self.assertEqual(rc, 2)
        self.assertIn("not a regular file", error)

    def test_brief_size_boundary(self) -> None:
        """Accept one MiB and reject a brief one byte larger."""
        brief = self.root / "brief.md"
        padding = b"x" * 59999 + b"\n"
        content = self.rendered().encode()
        remaining = 1024 * 1024 - len(content)
        content += padding * (remaining // len(padding)) + b"x" * (remaining % len(padding))
        brief.write_bytes(content)
        self.assertEqual(self.run_cli("check", str(brief))[0], 0)
        brief.write_bytes(content + b"x")
        rc, _, error = self.run_cli("check", str(brief))
        self.assertEqual(rc, 2)
        self.assertIn("size", error)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO unavailable")
    def test_config_fifo_rejected(self) -> None:
        """Reject a FIFO config without waiting for a writer."""
        config = self.root / "config.fifo"
        brief = self.root / "brief.md"
        os.mkfifo(config)
        brief.write_text(self.rendered(), encoding="utf-8")
        rc, _, error = self.run_cli("check", str(brief), "--config", str(config))
        self.assertEqual(rc, 2)
        self.assertIn("not a regular file", error)

    def test_config_item_boundaries(self) -> None:
        """Accept maximum list and item lengths and reject values above them."""
        config = self.root / "config.yaml"
        brief = self.root / "brief.md"
        brief.write_text("a" * 201, encoding="utf-8")
        for count, length, expected, reason in (
            (32, 1, 0, ""),
            (33, 1, 2, "32 items"),
            (1, 200, 0, ""),
            (1, 201, 2, "200 characters"),
        ):
            with self.subTest(count=count, length=length):
                config.write_text(
                    "required:\n" + ("  - " + "a" * length + "\n") * count, encoding="utf-8"
                )
                rc, _, error = self.run_cli("check", str(brief), "--config", str(config))
                self.assertEqual(rc, expected, error)
                self.assertIn(reason, error)

    def test_declaration_matches_template(self) -> None:
        """Keep the real declaration nonempty and present in the tracked template."""
        required = lane_brief.read_required(CONFIG)
        self.assertTrue(required)
        content = TEMPLATE.read_text(encoding="utf-8")
        for item in required:
            with self.subTest(item=item):
                self.assertIn(item, content)

    def test_reader_accepts_bare_and_quoted_items_and_stops(self) -> None:
        """Read both item forms without consuming a neighboring section."""
        standards = self.root / "config.yaml"
        standards.write_text(
            '---\n# comment\nrequired:\n  - "quoted"\n  - bare value\n'
            "other:\n  - ignored\nnext:\n  - ignored\n",
            encoding="utf-8",
        )
        self.assertEqual(lane_brief.read_required(standards), ["quoted", "bare value"])

    def test_render_refuses_invalid_result_and_does_not_write(self) -> None:
        """Do not emit a rendered brief if an argument leaves a placeholder."""
        destination = self.root / "out.md"
        args = [*ARGS, "--finding", "<task>", "-o", str(destination)]
        rc, output, error = self.run_cli(*args)
        self.assertEqual(rc, 1)
        self.assertEqual(output, "")
        self.assertFalse(destination.exists())
        self.assertIn("unfilled placeholder <task>", error)

    def test_line_count_boundaries_for_brief_and_config(self) -> None:
        """Accept 10000 lines and reject 10001 lines in both inputs."""
        brief = self.root / "brief.md"
        config = self.root / "config.yaml"
        for target in (brief, config):
            with self.subTest(target=target.name):
                for count in (10000, 10001):
                    brief.write_text("present\n", encoding="utf-8")
                    config.write_text("required:\n  - present\n", encoding="utf-8")
                    prefix = "present\n" if target == brief else "required:\n  - present\n"
                    target.write_text(
                        prefix + "\n" * (count - prefix.count("\n")), encoding="utf-8"
                    )
                    rc, _, error = self.run_cli("check", str(brief), "--config", str(config))
                    self.assertEqual(rc, 0 if count == 10000 else 2, error)
                    if count == 10001:
                        self.assertIn("exceeds 10000 lines", error)

    def test_duplicate_required_block_rejected(self) -> None:
        """Reject a second required block even after another section."""
        brief = self.root / "brief.md"
        config = self.root / "config.yaml"
        brief.write_text("first\n", encoding="utf-8")
        config.write_text(
            "required:\n  - first\nother: value\nrequired:\n  - second\n", encoding="utf-8"
        )
        rc, output, error = self.run_cli("check", str(brief), "--config", str(config))
        self.assertEqual(rc, 2)
        self.assertEqual(output, "")
        self.assertIn("duplicate required key", error)

    def test_render_line_limit_refuses_file_and_stdout(self) -> None:
        """Reject combined render values exceeding the output line limit."""
        destination = self.root / "out.md"
        args = [*ARGS, "--finding", "\n" * 4000, "--fix", "\n" * 4000, "--tests", "\n" * 4000]
        for extra in ([], ["-o", str(destination)]):
            with self.subTest(extra=extra):
                rc, output, error = self.run_cli(*args, *extra)
                self.assertEqual(rc, 2)
                self.assertEqual(output, "")
                self.assertFalse(destination.exists())
                self.assertIn("exceeds 10000 lines", error)

    def test_render_argument_character_boundaries(self) -> None:
        """Accept 8192 characters and reject 8193 for every render value."""
        for name in (
            "branch",
            "task",
            "finding",
            "fix",
            "tests",
            "paths",
            "modules",
            "issue",
            "round",
        ):
            for count in (8192, 8193):
                with self.subTest(name=name, count=count):
                    rc, output, error = self.run_cli(*ARGS, f"--{name}", "x" * count)
                    self.assertEqual(rc, 0 if count == 8192 else 2, error)
                    if count == 8193:
                        self.assertEqual(output, "")
                        self.assertIn(f"--{name}", error)

    def test_render_byte_limits_refuse_output(self) -> None:
        """Reject oversized rendered lines and content before writing output."""
        template = self.root / "template.md"
        destination = self.root / "out.md"
        for content, reason in (
            ("<finding>" * 3, "line exceeds"),
            ("<finding>\n" * 33, "size exceeds"),
        ):
            with self.subTest(reason=reason):
                template.write_text(content, encoding="utf-8")
                with patch.object(lane_brief, "TEMPLATE", template):
                    rc, output, error = self.run_cli(
                        *ARGS, "--finding", "😀" * 8192, "-o", str(destination)
                    )
                self.assertEqual(rc, 2)
                self.assertEqual(output, "")
                self.assertFalse(destination.exists())
                self.assertIn(reason, error)


if __name__ == "__main__":
    unittest.main()
