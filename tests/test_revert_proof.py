# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Revert-proof core regression tests using temporary Git repositories."""

from __future__ import annotations

import argparse
import io
import json
import os
import signal
import sys
import unittest
import unittest.mock
from pathlib import Path

from scripts import revert_proof, upstream_lint
from tests.revert_proof_helpers import (
    BASE_SOURCE,
    FIXED_SOURCE,
    ProcessOutputFixture,
    RevertProofFixture,
)


class ProcessOutputTest(ProcessOutputFixture):
    """Keep executable statuses through interpreter shutdown with closed pipe readers."""

    def test_both_script_help_statuses_survive_closed_stdout(self) -> None:
        """Both real entry points retain help status zero without shutdown diagnostics."""
        for name in ("upstream_lint", "revert_proof")[:2]:
            self.assert_pipe_result(
                [f"scripts/{name}.py", "--help"], self.parser_result(name, ["--help"]), "stdout"
            )

    def test_both_script_usage_statuses_survive_closed_stderr(self) -> None:
        """Both real entry points retain usage status two with a closed error reader."""
        for name in ("upstream_lint", "revert_proof")[:2]:
            self.assert_pipe_result([f"scripts/{name}.py"], self.parser_result(name, []), "stderr")

    def test_producer_tool_error_survives_closed_stderr(self) -> None:
        """An actual preflight error retains two and its open-pipe diagnostic is complete."""
        arguments = [
            "scripts/revert_proof.py",
            "--tree",
            ".",
            "--commit",
            "HEAD",
            "--record",
            "unused.json",
        ]
        self.assert_pipe_result(
            arguments, (2, b"", b"revert_proof: test command is empty\n"), "stderr"
        )

    def test_final_flush_only_failures_keep_all_decided_statuses(self) -> None:
        """A first flush failure at exit preserves buffered output's decided status zero to five."""
        for name in ("stdout", "stderr")[:2]:
            for status in range(6):
                source = (
                    "import sys\nfrom scripts import upstream_lint\n"
                    f"sys.{name}.reconfigure(line_buffering=False, write_through=False)\n"
                    f"sys.{name}.write('buffered')\n"
                    f"try:\n    sys.exit({status})\n"
                    "finally:\n    upstream_lint.finish_process_output()\n"
                )
                expected = (
                    status,
                    b"buffered" if name == "stdout" else b"",
                    b"buffered" if name == "stderr" else b"",
                )
                self.assert_pipe_result(["-c", source], expected, name)

    def test_process_finalizer_leaves_replacement_streams_untouched(self) -> None:
        """Patching current and original stream aliases cannot transfer stream ownership."""
        stream = unittest.mock.Mock()
        with (
            unittest.mock.patch.object(sys, "stdout", stream),
            unittest.mock.patch.object(sys, "stderr", stream),
            unittest.mock.patch.object(sys, "__stdout__", stream),
            unittest.mock.patch.object(sys, "__stderr__", stream),
        ):
            upstream_lint.finish_process_output()
            self.assertIs(sys.stdout, stream)
            self.assertIs(sys.stderr, stream)
        self.assertEqual(stream.mock_calls, [])


class DisplayTextTest(unittest.TestCase):
    """Keep display escaping separate from lossless Git and JSON transport."""

    def test_display_text_escapes_surrogates_without_changing_input(self) -> None:
        """A filename-free byte escape renders visibly without changing its stored value."""
        name = "libavfilter/vf_\udcff.c"
        self.assertEqual(upstream_lint.display_text(name), r"libavfilter/vf_\udcff.c")
        self.assertEqual(name.encode("utf-8", "surrogateescape"), b"libavfilter/vf_\xff.c")
        self.assertEqual(json.loads(json.dumps(name)), name)

    def test_display_text_preserves_valid_unicode_and_literal_backslashes(self) -> None:
        """Valid Unicode and literal escape spellings keep their existing display."""
        text = "caf\u00e9 " + r"vf_\udcff.c"
        self.assertEqual(upstream_lint.display_text(text), text)

    def test_display_text_escapes_non_filesystem_surrogates(self) -> None:
        """Handwritten JSON surrogates outside the filesystem escape range also render."""
        text = "\ud800\udc01\udcff"
        self.assertEqual(upstream_lint.display_text(text), r"\ud800\udc01\udcff")


class DisplayBoundaryTest(RevertProofFixture):
    """Exercise CLI display boundaries without requiring byte-named filesystem entries."""

    def setUp(self) -> None:
        """Expose colour detection and pin parser width and untranslated messages."""
        super().setUp()
        environment = unittest.mock.patch.dict(
            os.environ,
            {
                "COLUMNS": "80",
                "TERM": "xterm-256color",
                "NO_COLOR": "",
                "FORCE_COLOR": "",
                "PYTHON_COLORS": "",
            },
        )
        environment.start()
        self.addCleanup(environment.stop)
        language = unittest.mock.patch.object(argparse, "_", lambda message: message)
        language.start()
        self.addCleanup(language.stop)

    def render_step(
        self,
        step: upstream_lint.Step,
        report: Path | None = None,
        stdout_errors: str = "strict",
        stdout_encoding: str = "utf-8",
        stderr_encoding: str = "utf-8",
    ) -> tuple[int, str, str]:
        """Render one supplied step through real CLI output and strict stderr."""
        output_bytes, error_bytes = io.BytesIO(), io.BytesIO()
        argv = ["--tree", str(self.tree), "--base", "base"]
        if report is not None:
            argv.extend(["--report", str(report)])
        with (
            io.TextIOWrapper(
                output_bytes, encoding=stdout_encoding, errors=stdout_errors
            ) as output,
            io.TextIOWrapper(error_bytes, encoding=stderr_encoding, errors="strict") as errors,
            unittest.mock.patch.object(upstream_lint, "_prepare", return_value=("base", "head")),
            unittest.mock.patch.object(upstream_lint, "_checks", return_value=[step]),
            unittest.mock.patch.object(sys, "stdout", output),
            unittest.mock.patch.object(sys, "stderr", errors),
        ):
            rc = upstream_lint.main(argv)
            output.flush()
            errors.flush()
            return (
                rc,
                output_bytes.getvalue().decode(stdout_encoding, errors=stdout_errors),
                error_bytes.getvalue().decode(stderr_encoding),
            )

    def test_accented_report_parent_error_keeps_status_on_ascii_stderr(self) -> None:
        """A real accented blocking file yields an escaped diagnostic and exit two."""
        blocker = self.root / "caf\u00e9"
        blocker.write_text("blocked", encoding="utf-8")
        result = self.render_step(
            upstream_lint.Step("fixture", 0, "ok"),
            blocker / "report.txt",
            stderr_encoding="ascii",
        )
        self.assertEqual(result[0], 2, result)
        escaped = str(blocker).replace("\u00e9", r"\xe9")
        self.assertEqual(result[2], f"report: [Errno 17] {os.strerror(17)}: '{escaped}'\n")

    def test_accented_missing_tree_keeps_status_on_ascii_stderr(self) -> None:
        """An actual missing accented tree yields an escaped diagnostic and exit two."""
        missing = self.root / "caf\u00e9"
        argv = [
            "--tree",
            str(missing),
            "--commit",
            "HEAD",
            "--record",
            str(self.record_path),
            "--",
            sys.executable,
            "-c",
            "pass",
        ]
        buffer = io.BytesIO()
        with (
            io.TextIOWrapper(buffer, encoding="ascii", errors="strict") as errors,
            unittest.mock.patch.object(sys, "stderr", errors),
        ):
            self.assertEqual(revert_proof.main(argv), 2)
            errors.flush()
            escaped = str(missing).replace("\u00e9", r"\xe9")
            self.assertEqual(
                buffer.getvalue().decode("ascii"),
                f"revert_proof: work tree not found: {escaped}\n",
            )

    def mechanical_step(self, name: str) -> upstream_lint.Step:
        """Produce a real TAB finding for a supplied filename without filesystem dependence."""
        count, findings = upstream_lint._file_rules(name, "\treturn 1;\n", {1})
        self.assertEqual(count, 1)
        return upstream_lint.Step("developer.texi", 1, "\n".join(findings))

    def expected_report(self, step: upstream_lint.Step) -> str:
        """Spell out the entire one-step fixture report independently of its renderer."""
        return (
            f"tree: {self.tree}\nbase: base\nhead: head\n\n"
            f"step: {step.name} rc: {step.rc}\n{step.output}\n"
        )

    def test_accented_finding_keeps_status_on_ascii_stdout(self) -> None:
        """A real accented source finding preserves ordinary exit one on ASCII stdout."""
        name = "libavfilter/vf_caf\u00e9.c"
        self.write(name, "\treturn 1;\n")
        ordinary = self.render_step(self.mechanical_step("libavfilter/vf_x.c"))
        step = self.mechanical_step(name)
        result = self.render_step(step, stdout_encoding="ascii")
        self.assertEqual(ordinary[0], 1, ordinary)
        self.assertEqual(
            result, (ordinary[0], self.expected_report(step).replace("\u00e9", r"\xe9"), "")
        )

    def parser_output(self, parser, argv: list[str]) -> tuple[int, str, str]:
        """Capture the complete parser result through strict ASCII output streams."""
        output_bytes, error_bytes = io.BytesIO(), io.BytesIO()
        with (
            io.TextIOWrapper(output_bytes, encoding="ascii", errors="strict") as output,
            io.TextIOWrapper(error_bytes, encoding="ascii", errors="strict") as errors,
            unittest.mock.patch.object(sys, "stdout", output),
            unittest.mock.patch.object(sys, "stderr", errors),
        ):
            with self.assertRaises(SystemExit) as caught:
                parser.parse_args(argv)
            output.flush()
            errors.flush()
            return (
                caught.exception.code,
                output_bytes.getvalue().decode("ascii"),
                error_bytes.getvalue().decode("ascii"),
            )

    def test_both_parsers_emit_accented_help_on_ascii_stdout(self) -> None:
        """Both help paths preserve complete escaped help and status zero on ASCII."""
        parsers = (upstream_lint._parser(), revert_proof.build_parser())
        for parser in parsers[:2]:
            with self.subTest(parser=parser):
                parser.prog = "caf\u00e9"
                expected = parser.format_help().replace("\u00e9", r"\xe9")
                self.assertEqual(self.parser_output(parser, ["--help"]), (0, expected, ""))

    def test_both_parsers_emit_accented_errors_on_ascii_stderr(self) -> None:
        """Both error paths preserve complete escaped usage and status two on ASCII."""
        producer = ["--tree", str(self.tree), "--commit", "HEAD", "--record", str(self.record_path)]
        cases = (
            (upstream_lint._parser(), ["--tree", str(self.tree), "--base", "base"]),
            (revert_proof.build_parser(), producer),
        )
        for parser, argv in cases[:2]:
            with self.subTest(parser=parser):
                parser.prog = "caf\u00e9"
                expected = (
                    (
                        parser.format_usage()
                        + "caf\u00e9: error: unrecognized arguments: --bad\u00e9\udcff\n"
                    )
                    .replace("\u00e9", r"\xe9")
                    .replace("\udcff", r"\udcff")
                )
                self.assertEqual(
                    self.parser_output(parser, [*argv, "--bad\u00e9\udcff"]), (2, "", expected)
                )

    def test_byte_name_finding_completes_through_strict_stdout(self) -> None:
        """Byte-name and ordinary-name findings both finish with exit one on strict stdout."""
        ordinary = self.render_step(self.mechanical_step("libavfilter/vf_x.c"))
        escaped = self.render_step(self.mechanical_step("libavfilter/vf_\udcff.c"))
        self.assertEqual(ordinary[0], 1, ordinary)
        self.assertEqual(escaped[0], ordinary[0], escaped)
        expected = self.expected_report(self.mechanical_step(r"libavfilter/vf_\udcff.c"))
        self.assertEqual(escaped, (1, expected, ""))

    def test_utf8_report_preserves_accents_independently_of_stdout(self) -> None:
        """UTF-8 reports retain accents and escape byte names through either stdout codec."""
        name = "libavfilter/vf_caf\u00e9.c"
        self.write(name, "\treturn 1;\n")
        accent = self.mechanical_step(name)
        byte_name = self.mechanical_step("libavfilter/vf_\udcff.c")
        step = upstream_lint.Step("developer.texi", 1, accent.output + "\n" + byte_name.output)
        expected = self.expected_report(step).replace("\udcff", r"\udcff")
        target = self.root / "caf\u00e9.txt"
        for encoding in ("utf-8", "ascii"):
            with self.subTest(encoding=encoding):
                result = self.render_step(step, target, stdout_encoding=encoding)
                shown = expected if encoding == "utf-8" else expected.replace("\u00e9", r"\xe9")
                self.assertEqual(result, (1, shown, ""))
                self.assertEqual(target.read_bytes(), expected.encode("utf-8"))

    def broken_stream(self, operation: str = "write", error: Exception | None = None):
        """Return a stream object that raises an operating-system error on emission."""
        stream = unittest.mock.Mock(encoding="ascii")
        getattr(stream, operation).side_effect = error or BrokenPipeError("planted closed pipe")
        return stream

    def test_lint_diagnostic_write_failure_keeps_exit_two(self) -> None:
        """A broken stderr cannot replace a real report failure's tool-error status."""
        blocker = self.root / "caf\u00e9"
        blocker.write_text("blocked", encoding="utf-8")
        errors = self.broken_stream()
        with (
            unittest.mock.patch.object(upstream_lint, "_prepare", return_value=("base", "head")),
            unittest.mock.patch.object(
                upstream_lint, "_checks", return_value=[upstream_lint.Step("fixture", 0, "ok")]
            ),
            unittest.mock.patch.object(sys, "stdout", io.StringIO()),
            unittest.mock.patch.object(sys, "stderr", errors),
        ):
            argv = ["--tree", str(self.tree), "--base", "base", "--report", str(blocker / "x")]
            self.assertEqual(upstream_lint.main(argv), 2)
        escaped = str(blocker).replace("\u00e9", r"\xe9")
        errors.write.assert_called_once_with(f"report: [Errno 17] {os.strerror(17)}: '{escaped}'\n")

    def test_producer_diagnostic_emission_failure_keeps_all_statuses(self) -> None:
        """Broken writes and flushes retain each of the producer's four failure statuses."""
        cases = (
            (revert_proof._PreflightError, 2),
            (revert_proof._HeadTestError, 3),
            (revert_proof._ReverseApplyError, 4),
            (revert_proof._ScratchRemovalError, 5),
        )
        argv = [
            "--tree",
            str(self.tree),
            "--commit",
            "HEAD",
            "--record",
            str(self.record_path),
            "--",
            sys.executable,
            "-c",
            "pass",
        ]
        for operation in ("write", "flush"):
            for error_type, expected in cases[:4]:
                with self.subTest(operation=operation, error_type=error_type):
                    errors = self.broken_stream(operation)
                    with (
                        unittest.mock.patch.object(sys, "stderr", errors),
                        unittest.mock.patch.object(
                            revert_proof, "run_proof", side_effect=error_type("caf\u00e9")
                        ),
                    ):
                        self.assertEqual(revert_proof.main(argv), expected)
                    errors.write.assert_called_once_with("revert_proof: caf\\xe9\n")
                    self.assertEqual(errors.flush.call_count, int(operation == "flush"))

    def test_stdout_emission_failure_keeps_finding_status_and_utf8_report(self) -> None:
        """A failed stdout write or flush retains the finding and still saves the report."""
        step = self.mechanical_step("libavfilter/vf_x.c")
        for operation in ("write", "flush"):
            with self.subTest(operation=operation):
                output = self.broken_stream(operation)
                errors = io.StringIO()
                with (
                    unittest.mock.patch.object(
                        upstream_lint, "_prepare", return_value=("base", "head")
                    ),
                    unittest.mock.patch.object(upstream_lint, "_checks", return_value=[step]),
                    unittest.mock.patch.object(sys, "stdout", output),
                    unittest.mock.patch.object(sys, "stderr", errors),
                ):
                    argv = [
                        "--tree",
                        str(self.tree),
                        "--base",
                        "base",
                        "--report",
                        str(self.record_path),
                    ]
                    self.assertEqual(upstream_lint.main(argv), 1)
                expected = self.expected_report(step)
                self.assertEqual(self.record_path.read_bytes(), expected.encode("utf-8"))
                output.write.assert_called_once_with(expected)
                self.assertEqual(errors.getvalue(), "")

    def test_both_parsers_keep_exit_codes_when_stream_writes_fail(self) -> None:
        """Broken and closed help or diagnostic streams retain argparse's exit statuses."""
        for parser in (upstream_lint._parser(), revert_proof.build_parser()):
            for argv, expected in ((["--help"], 0), ([], 2)):
                for error in (BrokenPipeError("broken pipe"), ValueError("closed stream")):
                    with self.subTest(parser=parser, argv=argv, error=error):
                        stream = self.broken_stream(error=error)
                        with (
                            unittest.mock.patch.object(sys, "stdout", stream),
                            unittest.mock.patch.object(sys, "stderr", stream),
                        ):
                            with self.assertRaises(SystemExit) as caught:
                                parser.parse_args(argv)
                            self.assertEqual(caught.exception.code, expected)
                        self.assertEqual(stream.write.call_count, 1 if expected == 0 else 2)

    def assert_parser_stream_statuses(self, stream) -> None:
        """Check formatting, explicit printing and both parser exits on a supplied stream."""
        for parser in (upstream_lint._parser(), revert_proof.build_parser()):
            with (
                unittest.mock.patch.object(sys, "stdout", stream),
                unittest.mock.patch.object(sys, "stderr", stream),
            ):
                self.assertNotIn("\x1b[", parser.format_help())
                self.assertNotIn("\x1b[", parser.format_usage())
                parser.print_help()
                parser.print_usage()
                for argv, expected in ((["--help"], 0), ([], 2)):
                    with self.subTest(parser=parser, argv=argv):
                        with self.assertRaises(SystemExit) as caught:
                            parser.parse_args(argv)
                        self.assertEqual(caught.exception.code, expected)

    def test_both_parsers_do_not_query_destination_descriptors_for_colour(self) -> None:
        """Invalid and raising descriptors cannot affect parser formatting or exit status."""
        for error in (None, ValueError("closed stream"), io.UnsupportedOperation("fileno")):
            with self.subTest(error=error):
                stream = unittest.mock.Mock(encoding="utf-8")
                stream.fileno.side_effect = error
                self.assert_parser_stream_statuses(stream)
                stream.fileno.assert_not_called()

    def test_both_parsers_keep_statuses_with_real_closed_stream(self) -> None:
        """A real closed file cannot turn help or a usage error into a traceback."""
        with (self.root / "closed-output.txt").open("w", encoding="utf-8") as stream:
            self.assertIsInstance(stream.fileno(), int)
        with self.assertRaises(ValueError):
            stream.fileno()
        self.assert_parser_stream_statuses(stream)

    def test_both_parsers_keep_statuses_with_real_detached_stream(self) -> None:
        """A detached text wrapper cannot replace either parser's decided status."""
        stream = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
        buffer = stream.detach()
        self.addCleanup(buffer.close)
        with self.assertRaises(ValueError):
            stream.fileno()
        self.assert_parser_stream_statuses(stream)

    def test_both_parsers_keep_statuses_without_stream_descriptor(self) -> None:
        """Writable streams without a descriptor keep plain parser text and both exits."""
        stream = unittest.mock.Mock(spec=("encoding", "write", "flush"), encoding="utf-8")
        self.assert_parser_stream_statuses(stream)
        self.assertTrue(stream.write.called)

    def test_both_parsers_stay_plain_when_color_is_forced(self) -> None:
        """Even forced terminal colour leaves both tools' help and usage plain."""
        with unittest.mock.patch.dict(os.environ, {"FORCE_COLOR": "1", "NO_COLOR": ""}):
            self.assert_parser_stream_statuses(io.StringIO())

    def test_shared_parser_accepts_constructor_without_color_switch(self) -> None:
        """Interpreters without the colour keyword still construct the shared parser."""
        descriptions = []

        def legacy_init(parser, *, description):
            """Model the older constructor's rejection of unknown keyword arguments."""
            descriptions.append(description)

        with unittest.mock.patch.object(argparse.ArgumentParser, "__init__", legacy_init):
            upstream_lint.DisplayArgumentParser(description="legacy parser")
        self.assertEqual(descriptions, ["legacy parser"])

    def test_exact_parser_text_uses_fixed_fixture_width(self) -> None:
        """Exact parser comparisons use the fixture width despite inherited COLUMNS."""
        for parser in (upstream_lint._parser(), revert_proof.build_parser()):
            with self.subTest(parser=parser):
                self.assertEqual(parser._get_formatter()._width, 78)

    def test_byte_name_finding_completes_through_utf8_report(self) -> None:
        """The strict UTF-8 report retains the finding independently of stdout encoding."""
        ordinary = self.render_step(self.mechanical_step("libavfilter/vf_x.c"))
        target = self.root / "report.txt"
        result = self.render_step(
            self.mechanical_step("libavfilter/vf_\udcff.c"), target, "surrogateescape"
        )
        self.assertEqual(result[0], ordinary[0], result)
        text = target.read_bytes().decode("utf-8", errors="strict")
        expected = self.expected_report(self.mechanical_step(r"libavfilter/vf_\udcff.c"))
        self.assertEqual(text, expected)
        self.assertEqual(text, result[1])
        self.assertEqual(result[2], "")

    def test_arbitrary_step_fields_are_safe_at_output_boundary(self) -> None:
        """Future finding and step-name producers cannot bypass boundary escaping."""
        result = self.render_step(upstream_lint.Step("future\ud800", 0, "reason \udc01"))
        expected = self.expected_report(upstream_lint.Step(r"future\ud800", 0, r"reason \udc01"))
        self.assertEqual(result, (0, expected, ""))

    def test_report_oserror_with_byte_name_is_safe_on_strict_stderr(self) -> None:
        """A failed report write names its escaped path and returns the tool-error status."""
        with unittest.mock.patch.object(Path, "write_text", side_effect=OSError("blocked \udcff")):
            result = self.render_step(upstream_lint.Step("fixture", 0, "ok"), self.record_path)
        expected = self.expected_report(upstream_lint.Step("fixture", 0, "ok"))
        self.assertEqual(result, (2, expected, "report: blocked \\udcff\n"))

    def test_producer_errors_escape_byte_names_on_strict_stderr(self) -> None:
        """All producer failure classes preserve their exit code on strict stderr."""
        cases = (
            (revert_proof._PreflightError, 2),
            (revert_proof._HeadTestError, 3),
            (revert_proof._ReverseApplyError, 4),
            (revert_proof._ScratchRemovalError, 5),
        )
        argv = [
            "--tree",
            str(self.tree),
            "--commit",
            "HEAD",
            "--record",
            str(self.record_path),
            "--",
            sys.executable,
            "-c",
            "pass",
        ]
        for error_type, expected in cases[:4]:
            with self.subTest(error_type=error_type):
                buffer = io.BytesIO()
                with (
                    io.TextIOWrapper(buffer, encoding="utf-8", errors="strict") as errors,
                    unittest.mock.patch.object(sys, "stderr", errors),
                    unittest.mock.patch.object(
                        revert_proof, "run_proof", side_effect=error_type("bad \udcff")
                    ),
                ):
                    self.assertEqual(revert_proof.main(argv), expected)
                    errors.flush()
                    self.assertEqual(
                        buffer.getvalue().decode("utf-8"), "revert_proof: bad \\udcff\n"
                    )

    def test_both_parsers_escape_invalid_argument_diagnostics(self) -> None:
        """Argparse diagnostics also escape surrogate-bearing command-line arguments."""
        argv = ["--tree", str(self.tree), "--base", "base", "--bad\udcff"]
        producer = [
            "--tree",
            str(self.tree),
            "--commit",
            "HEAD",
            "--record",
            str(self.record_path),
            "--bad\udcff",
        ]
        cases = ((upstream_lint._parser(), argv), (revert_proof.build_parser(), producer))
        for parser, arguments in cases[:2]:
            with self.subTest(parser=parser):
                buffer = io.BytesIO()
                with (
                    io.TextIOWrapper(buffer, encoding="utf-8", errors="strict") as errors,
                    unittest.mock.patch.object(sys, "stderr", errors),
                ):
                    with self.assertRaises(SystemExit) as caught:
                        parser.parse_args(arguments)
                    errors.flush()
                    expected = (
                        parser.format_usage()
                        + f"{parser.prog}: error: unrecognized arguments: --bad\\udcff\n"
                    )
                    self.assertEqual(buffer.getvalue().decode("utf-8"), expected)
                    self.assertEqual(caught.exception.code, 2)

    def test_report_encoding_failure_is_a_tool_error(self) -> None:
        """A residual report encoder failure is handled rather than escaping as a traceback."""
        error = UnicodeEncodeError("utf-8", "\udcff", 0, 1, "planted report failure")
        with unittest.mock.patch.object(Path, "write_text", side_effect=error):
            result = self.render_step(upstream_lint.Step("fixture", 0, "ok"), self.record_path)
        expected = self.expected_report(upstream_lint.Step("fixture", 0, "ok"))
        self.assertEqual(result, (2, expected, f"report: {error}\n"))

    def test_stdout_encoding_fallback_preserves_passing_status(self) -> None:
        """An incompatible stdout codec escapes text and retains a passing step's status."""
        result = self.render_step(
            upstream_lint.Step("fixture", 0, "caf\u00e9"), stdout_encoding="ascii"
        )
        self.assertEqual(
            result,
            (0, self.expected_report(upstream_lint.Step("fixture", 0, r"caf\xe9")), ""),
        )

    def test_producer_record_encoding_failure_is_a_tool_error(self) -> None:
        """Residual record-write encoding errors produce a message and exit two."""
        fix = self.make_fix()
        error = UnicodeEncodeError("utf-8", "\udcff", 0, 1, "planted record failure")
        with (
            unittest.mock.patch.object(json, "dumps", side_effect=error),
            unittest.mock.patch.object(revert_proof, "measure_scratch", return_value=0),
        ):
            buffer = io.StringIO()
            argv = [
                "--tree",
                str(self.tree),
                "--commit",
                fix,
                "--record",
                str(self.record_path),
                "--",
                sys.executable,
                "-c",
                "pass",
            ]
            with unittest.mock.patch.object(sys, "stderr", buffer):
                self.assertEqual(revert_proof.main(argv), 2)
        self.assertEqual(buffer.getvalue(), f"revert_proof: {error}\n")

        self.assertFalse(self.record_path.exists())
        self.assertEqual(list(self.root.glob(".revert-proof-*.tmp")), [])

    def test_base_file_preserves_non_utf8_blob_bytes(self) -> None:
        """Base scripts keep every original byte for consumers such as bash that read bytes."""
        content = b"#!/bin/sh\n# invalid UTF-8: \xff; valid UTF-8: \xc3\xa9\nexit 0\n"
        path = self.write("tools/check.sh", "")
        path.write_bytes(content)
        commit = self.commit("tools: add byte script")
        scratch = self.root / "extracted"
        scratch.mkdir()
        target = upstream_lint._base_file(self.tree, commit, "tools/check.sh", scratch)
        self.assertEqual(target.read_bytes(), content)
        result = upstream_lint._run(["bash", str(target)], self.tree)
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_base_file_git_failure_is_a_tool_error(self) -> None:
        """A failed base extraction keeps Git failure as a tool error and writes no script."""
        scratch = self.root / "extracted"
        scratch.mkdir()
        step = upstream_lint._collect(
            "base", lambda: upstream_lint._base_file(self.tree, self.base, "missing.sh", scratch)
        )
        self.assertEqual(step.rc, 2, step.output)
        self.assertIn("git show", step.output)
        self.assertFalse((scratch / "missing.sh").exists())


class RevertProofTest(RevertProofFixture):
    """Check revert-proof core behavior using isolated Git fixtures."""

    def test_git_machine_reads_use_portable_advice_configuration(self) -> None:
        """Producer and history scans work when Git rejects the newer global advice flag."""
        fix = self.make_fix()
        real_run = upstream_lint._run

        def legacy_git(argv: list[str], cwd: Path, *args: object, **kwargs: object):
            """Reject the unsupported option while running every portable Git invocation."""
            if "--no-advice" in argv:
                return upstream_lint.RunResult(tuple(argv), 129, "unsupported global option")
            return real_run(argv, cwd, *args, **kwargs)

        with unittest.mock.patch.object(upstream_lint, "_run", side_effect=legacy_git):
            self.assertEqual(revert_proof.git_out(self.tree, "rev-parse", "HEAD").strip(), fix)
            self.assertIsNone(upstream_lint._commit_history_error(self.tree, fix))
            fixes = upstream_lint._fix_commits(self.tree, self.base, fix)
            self.assertEqual([item[0] for item in fixes], [fix])

    @unittest.skipUnless(os.name == "posix", "fixture requires executable file modes")
    def test_reverted_launch_failure_never_writes_a_record(self) -> None:
        """Removed executables and cleared execute bits cannot masquerade as a failing test."""
        for existed in (False, True):
            with self.subTest(existed=existed):
                path = self.write("libavfilter/command.c", "#!/bin/sh\nexit 0\n")
                if existed:
                    path.chmod(0o644)
                    self.commit("avfilter: add inactive command")
                path.chmod(0o755)
                fix = self.commit("avfilter: fix command availability")
                rc, _, error = self.run_cli(fix, ["./libavfilter/command.c"])
                self.assertEqual(rc, 3, error)
                self.assertIn("launch-error", error)
                self.assertFalse(self.record_path.exists())
                path.unlink()
                self.commit("avfilter: remove command fixture")

    def test_reader_rejects_launch_error_as_no_verdict(self) -> None:
        """A forged launch-error result is rejected with the same clarity as timeout."""
        fix = self.make_fix()
        self.record_path.write_text(
            json.dumps(self.valid_record(fix, "launch-error")), encoding="utf-8"
        )
        step = upstream_lint._revert_proof(self.tree, self.base, fix, (self.record_path,))
        self.assertEqual(step.rc, 1, step.output)
        self.assertIn("rc_reverted is launch-error (no verdict)", step.output)

    def test_reverse_patch_preserves_non_utf8_context_bytes(self) -> None:
        """A Latin-1 byte on an unchanged context line survives reverse application."""
        path = self.write("libavfilter/vf_x.c", BASE_SOURCE)
        path.write_bytes(b"/* latin-1: \xff */\n" + BASE_SOURCE.encode())
        self.commit("avfilter/vf_x: add Latin-1 context")
        path.write_bytes(b"/* latin-1: \xff */\n" + FIXED_SOURCE.encode())
        fix = self.commit("avfilter/vf_x: fix value")
        script = (
            "from pathlib import Path; import sys; "
            "data = Path('libavfilter/vf_x.c').read_bytes(); "
            "assert bytes([255]) in data; sys.exit(0 if b'return 2;' in data else 17)"
        )
        rc, _, error = self.run_cli(fix, [sys.executable, "-c", script])
        self.assertEqual(rc, 0, error)
        record = json.loads(self.record_path.read_text(encoding="utf-8"))
        self.assertEqual(record["rc_reverted"], 17)

    def test_reverse_patch_preserves_non_utf8_filename_and_sibling(self) -> None:
        """Reverse the byte-name source, leaving its replacement-character sibling fixed."""
        source, sibling_fix, fix = self.named_source_fix(b"libavfilter/vf_\xff.c")
        script = (
            "from pathlib import Path; import sys; "
            "assert 'return 2;' in Path('libavfilter/vf_\\ufffd.c').read_text(); "
            f"sys.exit(0 if 'return 2;' in Path({source!r}).read_text() else 17)"
        )
        rc, _, error = self.run_cli(fix, [sys.executable, "-c", script])
        self.assertEqual(rc, 0, error)
        text = self.record_path.read_bytes().decode("utf-8", errors="strict")
        self.assertIn("\\udcff", text)
        record = json.loads(text)
        self.assertEqual(record["reverted_paths"], [source])
        self.assertEqual(os.fsencode(record["reverted_paths"][0]), b"libavfilter/vf_\xff.c")
        self.assertEqual(record["rc_reverted"], 17)
        step = upstream_lint._revert_proof(self.tree, sibling_fix, fix, (self.record_path,))
        self.assertEqual(step.rc, 0, step.output)

    def test_git_text_input_preserves_decoded_filename_bytes(self) -> None:
        """Feeding decoded Git names back through stdin preserves their original bytes."""
        source, _, fix = self.named_source_fix(b"libavfilter/vf_\xff.c")
        names = revert_proof.git_out(
            self.tree, "diff-tree", "--no-commit-id", "--name-only", "-r", "-z", fix
        )
        self.assertEqual(names, source + "\0")
        command = ["git", "hash-object", "--stdin"]
        text_result = upstream_lint._run(command, self.tree, data=names)
        byte_result = upstream_lint._run(command, self.tree, data=os.fsencode(names))
        self.assertEqual(text_result.returncode, 0, text_result.stdout)
        self.assertEqual(byte_result.returncode, 0, byte_result.stdout)
        self.assertEqual(text_result.stdout, byte_result.stdout)

    def test_consumer_binds_non_utf8_record_to_exact_commit_paths(self) -> None:
        """Accept byte-name evidence and reject rebinding it to the sibling-only commit."""
        source, sibling_fix, fix = self.named_source_fix(b"libavfilter/vf_\xff.c")
        record = self.valid_record(fix)
        record["reverted_paths"] = [source]
        self.record_path.write_text(json.dumps(record), encoding="utf-8")
        step = upstream_lint._revert_proof(self.tree, sibling_fix, fix, (self.record_path,))
        self.assertEqual(step.rc, 0, step.output)
        record["commit"] = sibling_fix
        self.record_path.write_text(json.dumps(record), encoding="utf-8")
        step = upstream_lint._revert_proof(self.tree, self.base, fix, (self.record_path,))
        self.assertEqual(step.rc, 1, step.output)
        self.assertIn(sibling_fix[:12], step.output)
        self.assertIn("reverted_paths differ from fix source paths", step.output)

    def test_consumer_refuses_replacement_sibling_for_non_utf8_commit(self) -> None:
        """A replacement-character path cannot stand in for the byte-name source fix."""
        _, sibling_fix, fix = self.named_source_fix(b"libavfilter/vf_\xff.c")
        record = self.valid_record(fix)
        record["reverted_paths"] = ["libavfilter/vf_\ufffd.c"]
        self.record_path.write_text(json.dumps(record), encoding="utf-8")
        step = upstream_lint._revert_proof(self.tree, sibling_fix, fix, (self.record_path,))
        self.assertEqual(step.rc, 1, step.output)
        self.assertIn("reverted_paths differ from fix source paths", step.output)

    def test_reverse_patch_preserves_multibyte_utf8_filename(self) -> None:
        """A valid multibyte filename retains its spelling, measured reversal and binding."""
        source, sibling_fix, fix = self.named_source_fix(b"libavfilter/vf_\xc3\xa9.c")
        self.assertEqual(source, "libavfilter/vf_\u00e9.c")
        script = (
            "from pathlib import Path; import sys; "
            f"sys.exit(0 if 'return 2;' in Path({source!r}).read_text() else 17)"
        )
        rc, _, error = self.run_cli(fix, [sys.executable, "-c", script])
        self.assertEqual(rc, 0, error)
        record = json.loads(self.record_path.read_text(encoding="utf-8"))
        self.assertEqual(record["reverted_paths"], [source])
        self.assertEqual(record["rc_reverted"], 17)
        step = upstream_lint._revert_proof(self.tree, sibling_fix, fix, (self.record_path,))
        self.assertEqual(step.rc, 0, step.output)

    @unittest.skipUnless(os.name == "posix", "fixture requires a literal star in a path")
    def test_reverse_patch_matches_source_path_literally(self) -> None:
        """A glob character in a source path cannot pull a matching test sibling into reversal."""
        source = "libavfilter/*/vf_x.c"
        sibling = "libavfilter/tests/vf_x.c"
        self.write(source, BASE_SOURCE)
        self.write(sibling, BASE_SOURCE)
        self.commit("avfilter: add source and test fixture")
        self.write(source, FIXED_SOURCE)
        self.write(sibling, FIXED_SOURCE)
        fix = self.commit("avfilter: fix literal source and its fixture")
        script = (
            "from pathlib import Path; import sys; "
            f"assert 'return 2;' in Path({sibling!r}).read_text(); "
            f"sys.exit(0 if 'return 2;' in Path({source!r}).read_text() else 17)"
        )
        rc, _, error = self.run_cli(fix, [sys.executable, "-c", script])
        self.assertEqual(rc, 0, error)
        record = json.loads(self.record_path.read_text(encoding="utf-8"))
        self.assertEqual(record["reverted_paths"], [source])
        self.assertEqual(record["rc_reverted"], 17)

    def test_empty_argument_after_executable_round_trips(self) -> None:
        """A measured command keeps its empty trailing argument and remains consumer-valid."""
        fix = self.make_fix()
        script = (
            "from pathlib import Path; import sys; assert sys.argv[1] == ''; "
            "sys.exit(0 if 'return 2;' in Path('libavfilter/vf_x.c').read_text() else 17)"
        )
        command = [sys.executable, "-c", script, ""]
        rc, _, error = self.run_cli(fix, command)
        self.assertEqual(rc, 0, error)
        record = json.loads(self.record_path.read_text(encoding="utf-8"))
        self.assertEqual(record["command"], command)
        step = upstream_lint._revert_proof(self.tree, self.base, fix, (self.record_path,))
        self.assertEqual(step.rc, 0, step.output)

    def test_command_reader_keeps_executable_and_path_validation_strict(self) -> None:
        """Empty programs, non-string arguments and empty source paths remain invalid."""
        fix = self.make_fix()
        for command in ([], [""], ["", "arg"], [sys.executable, 1]):
            with self.subTest(command=command):
                record = self.valid_record(fix)
                record["command"] = command
                self.assertEqual(
                    upstream_lint._revert_proof_type_error(record), "wrong value type for command"
                )
        record = self.valid_record(fix)
        record["reverted_paths"] = [""]
        self.assertEqual(
            upstream_lint._revert_proof_type_error(record), "wrong value type for reverted_paths"
        )

    def test_nested_json_is_per_file_error_and_later_records_are_loaded(self) -> None:
        """Deep JSON names its malformed file without preventing a later valid record load."""
        fix = self.make_fix()
        depth = sys.getrecursionlimit() + 100
        self.record_path.write_text("[" * depth + "0" + "]" * depth, encoding="utf-8")
        good = self.root / "good.json"
        good.write_text(json.dumps(self.valid_record(fix)), encoding="utf-8")
        real_loads = upstream_lint.json.loads

        def reject_nested(text: str, **kwargs: object):
            """Raise the decoder depth error deterministically across Python versions."""
            if text.startswith("["):
                raise RecursionError("fixture decoder nesting limit")
            return real_loads(text, **kwargs)

        with unittest.mock.patch.object(upstream_lint.json, "loads", side_effect=reject_nested):
            payload, commit, error = upstream_lint._read_revert_proof(self.record_path)
            self.assertIsNone(payload)
            self.assertIsNone(commit)
            self.assertEqual(error, f"{self.record_path}: not valid JSON")
            loaded, by_commit, unmatched = upstream_lint._load_revert_proofs(
                (self.record_path, good), frozenset({fix})
            )
            self.assertEqual(loaded[fix], [self.valid_record(fix)])
            self.assertEqual(by_commit, {})
            self.assertEqual(unmatched, [error])
            step = upstream_lint._revert_proof(self.tree, self.base, fix, (self.record_path, good))
            self.assertEqual(step.rc, 1, step.output)
            self.assertIn(f"{self.record_path}: not valid JSON", step.output)
            self.assertNotIn("no revert-proof record", step.output)

    def test_git_runner_keeps_successful_stdout_separate_from_advice(self) -> None:
        """A raw Git read returns only the hash even with real graft advice enabled."""
        self.write(".git/info/grafts", f"{self.base}\n")
        result = upstream_lint._run(
            ["git", "-c", "advice.graftFileDeprecated=true", "rev-parse", "HEAD^{commit}"],
            self.tree,
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout, f"{self.base}\n")

    def test_git_failure_retains_stderr_diagnostic(self) -> None:
        """Both Git wrappers retain the real fatal diagnostic for an invalid revision."""
        with self.assertRaises(RuntimeError) as consumer:
            upstream_lint._git(self.tree, "rev-parse", "--verify", "missing-fixture-ref")
        status_line, separator, diagnostic = str(consumer.exception).partition("\n")
        self.assertEqual(status_line, "git rev-parse --verify missing-fixture-ref: rc=128")
        self.assertEqual(separator, "\n")
        diagnostic_line = diagnostic.strip()
        self.assertTrue(diagnostic_line)
        self.assertNotEqual(diagnostic_line, "rc=128")
        with self.assertRaises(revert_proof._PreflightError) as producer:
            revert_proof.git_out(self.tree, "rev-parse", "--verify", "missing-fixture-ref")
        self.assertEqual(
            str(producer.exception),
            f"git rev-parse --verify missing-fixture-ref: {diagnostic_line}",
        )

    def test_git_timeout_retains_stderr_through_bounded_reap(self) -> None:
        """Git timeout diagnostics survive both a completed reap and an exhausted drain."""
        argv = ["git", "--version"]
        for exhausted in (False, True):
            with self.subTest(exhausted=exhausted):
                process = unittest.mock.MagicMock(pid=123, returncode=-signal.SIGKILL)
                first = upstream_lint.subprocess.TimeoutExpired(
                    argv, 1, output=b"partial stdout", stderr=b"git timeout diagnostic"
                )
                second = (
                    upstream_lint.subprocess.TimeoutExpired(argv, 10)
                    if exhausted
                    else (b"complete stdout", b"git timeout diagnostic")
                )
                process.communicate.side_effect = [first, second]
                with (
                    unittest.mock.patch.object(upstream_lint.subprocess, "Popen") as popen,
                    unittest.mock.patch.object(upstream_lint.os, "killpg"),
                ):
                    popen.return_value.__enter__.return_value = process
                    result = upstream_lint._run(argv, self.tree, timeout=1)
                self.assertEqual(result.returncode, 124)
                self.assertTrue(result.timed_out)
                self.assertIn("git timeout diagnostic", result.stdout)
                self.assertIn("partial stdout" if exhausted else "complete stdout", result.stdout)

    def test_non_git_command_keeps_successful_stderr(self) -> None:
        """Successful test commands still expose both output streams in their result."""
        result = upstream_lint._run(
            [sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr)"],
            self.tree,
            allow_any_executable=True,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(sorted(result.stdout.splitlines()), ["err", "out"])

    def test_command_at_consumer_argument_cap_is_accepted(self) -> None:
        """Exactly the shared argument cap produces a consumer-valid command record."""
        self.write("libavfilter/vf_x.c", FIXED_SOURCE)
        commit = self.commit("avfilter/vf_x: fix return value")
        command = [
            sys.executable,
            "-c",
            (
                "from pathlib import Path; import sys; sys.exit(0 if 'return 2;' in "
                "Path('libavfilter/vf_x.c').read_text(encoding='utf-8') else 1)"
            ),
        ] + ["arg"] * (upstream_lint.MAX_COMMAND_ARGS - 3)
        rc, _, err = self.run_cli(commit, command)
        self.assertEqual(rc, 0, err)
        record = json.loads(self.record_path.read_text(encoding="utf-8"))
        self.assertEqual(len(record["command"]), upstream_lint.MAX_COMMAND_ARGS)
        self.assertEqual(record["command"], command)
        self.assertIsNone(upstream_lint._revert_proof_schema_error(record))
        step = upstream_lint._revert_proof(self.tree, self.base, commit, (self.record_path,))
        self.assertEqual(step.rc, 0, step.output)

    def test_planted_weak_test_passes_with_and_without_fix(self) -> None:
        """A planted weak test that exits 0 with and without the fix records rc_reverted 0."""
        self.write("libavfilter/vf_x.c", FIXED_SOURCE)
        fix_sha = self.commit("avfilter/vf_x: fix return value")
        cmd = [sys.executable, "-c", "import sys; sys.exit(0)"]

        rc, _, err = self.run_cli(fix_sha, cmd)
        self.assertEqual(rc, 0, err)
        self.assert_clean_tree(FIXED_SOURCE)
        record = json.loads(self.record_path.read_text(encoding="utf-8"))
        self.assertEqual(record["rc_head"], 0)
        self.assertEqual(record["rc_reverted"], 0)

    def test_refuse_commit_without_source_paths(self) -> None:
        """A commit changing only test or doc paths is refused with exit 2 and no record."""
        self.write("tests/ref/fate/filter-x", "0\n")
        doc_sha = self.commit("tests/fate: add filter-x ref")

        rc, _, err = self.run_cli(doc_sha, [sys.executable, "-c", "pass"], expect_scratch=False)
        self.assertEqual(rc, 2)
        self.assertIn("changes no source path", err)
        self.assertFalse(self.record_path.exists())

    def test_refuse_empty_command(self) -> None:
        """An empty test command is refused with exit 2 and no record."""
        self.write("libavfilter/vf_x.c", FIXED_SOURCE)
        fix_sha = self.commit("avfilter/vf_x: fix return value")

        rc, _, err = self.run_cli(fix_sha, [], expect_scratch=False)
        self.assertEqual(rc, 2)
        self.assertIn("test command is empty", err)
        self.assertFalse(self.record_path.exists())

    def test_refuse_test_failing_at_head(self) -> None:
        """A test command that exits nonzero at HEAD fails with exit 3 and writes no record."""
        self.write("libavfilter/vf_x.c", FIXED_SOURCE)
        fix_sha = self.commit("avfilter/vf_x: fix return value")
        cmd = [sys.executable, "-c", "import sys; sys.exit(1)"]

        rc, _, err = self.run_cli(fix_sha, cmd)
        self.assertEqual(rc, 3)
        self.assertIn("test fails at head", err)
        self.assertFalse(self.record_path.exists())
        self.assert_clean_tree(FIXED_SOURCE)

    def test_boundary_timeout_above_upper_bound_refused(self) -> None:
        """A timeout value above the 7200 second upper bound is refused with exit 2."""
        self.write("libavfilter/vf_x.c", FIXED_SOURCE)
        fix_sha = self.commit("avfilter/vf_x: fix return value")

        rc, _, err = self.run_cli(
            fix_sha, [sys.executable, "-c", "pass"], timeout="7201", expect_scratch=False
        )
        self.assertEqual(rc, 2)
        self.assertIn("7200", err)
        self.assertFalse(self.record_path.exists())
