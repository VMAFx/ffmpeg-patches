# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Revert-proof core regression tests using temporary Git repositories."""

from __future__ import annotations

import io
import json
import os
import signal
import sys
import unittest
import unittest.mock
from pathlib import Path

from scripts import revert_proof, upstream_lint
from tests.revert_proof_helpers import BASE_SOURCE, FIXED_SOURCE, RevertProofFixture


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

    def render_step(
        self,
        step: upstream_lint.Step,
        report: Path | None = None,
        stdout_errors: str = "strict",
        stdout_encoding: str = "utf-8",
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
            io.TextIOWrapper(error_bytes, encoding="utf-8", errors="strict") as errors,
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
                error_bytes.getvalue().decode("utf-8"),
            )

    def mechanical_step(self, name: str) -> upstream_lint.Step:
        """Produce a real TAB finding for a supplied filename without filesystem dependence."""
        count, findings = upstream_lint._file_rules(name, "\treturn 1;\n", {1})
        self.assertEqual(count, 1)
        return upstream_lint.Step("developer.texi", 1, "\n".join(findings))

    def test_byte_name_finding_completes_through_strict_stdout(self) -> None:
        """Byte-name and ordinary-name findings both finish with exit one on strict stdout."""
        ordinary = self.render_step(self.mechanical_step("libavfilter/vf_x.c"))
        escaped = self.render_step(self.mechanical_step("libavfilter/vf_\udcff.c"))
        self.assertEqual(ordinary[0], 1, ordinary)
        self.assertEqual(escaped[0], ordinary[0], escaped)
        self.assertIn(r"libavfilter/vf_\udcff.c:1: TAB character", escaped[1])
        self.assertEqual(escaped[2], "")

    def test_byte_name_finding_completes_through_utf8_report(self) -> None:
        """The strict UTF-8 report retains the finding independently of stdout encoding."""
        ordinary = self.render_step(self.mechanical_step("libavfilter/vf_x.c"))
        target = self.root / "report.txt"
        result = self.render_step(
            self.mechanical_step("libavfilter/vf_\udcff.c"), target, "surrogateescape"
        )
        self.assertEqual(result[0], ordinary[0], result)
        text = target.read_bytes().decode("utf-8", errors="strict")
        self.assertIn(r"libavfilter/vf_\udcff.c:1: TAB character", text)
        self.assertEqual(text, result[1])
        self.assertEqual(result[2], "")

    def test_arbitrary_step_fields_are_safe_at_output_boundary(self) -> None:
        """Future finding and step-name producers cannot bypass boundary escaping."""
        result = self.render_step(upstream_lint.Step("future\ud800", 0, "reason \udc01"))
        self.assertEqual(result[0], 0, result)
        self.assertIn(r"step: future\ud800 rc: 0", result[1])
        self.assertIn(r"reason \udc01", result[1])

    def test_report_oserror_with_byte_name_is_safe_on_strict_stderr(self) -> None:
        """A failed report write names its escaped path and returns the tool-error status."""
        with unittest.mock.patch.object(Path, "write_text", side_effect=OSError("blocked \udcff")):
            result = self.render_step(upstream_lint.Step("fixture", 0, "ok"), self.record_path)
        self.assertEqual(result[0], 2, result)
        self.assertIn(r"report: blocked \udcff", result[2])

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
                    self.assertIn(r"revert_proof: bad \udcff", buffer.getvalue().decode("utf-8"))

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
                    self.assertIn(r"--bad\udcff", buffer.getvalue().decode("utf-8"))
                    self.assertEqual(caught.exception.code, 2)

    def test_report_encoding_failure_is_a_tool_error(self) -> None:
        """A residual report encoder failure is handled rather than escaping as a traceback."""
        error = UnicodeEncodeError("utf-8", "\udcff", 0, 1, "planted report failure")
        with unittest.mock.patch.object(Path, "write_text", side_effect=error):
            result = self.render_step(upstream_lint.Step("fixture", 0, "ok"), self.record_path)
        self.assertEqual(result[0], 2, result)
        self.assertIn("report:", result[2])
        self.assertIn("planted report failure", result[2])

    def test_stdout_encoding_failure_is_a_tool_error(self) -> None:
        """An incompatible stdout codec returns two with a diagnostic even for a passing step."""
        result = self.render_step(
            upstream_lint.Step("fixture", 0, "caf\u00e9"), stdout_encoding="ascii"
        )
        self.assertEqual(result[0], 2, result)
        self.assertIn("report:", result[2])
        self.assertIn("ascii", result[2])

    def test_producer_record_encoding_failure_is_a_tool_error(self) -> None:
        """Residual record-write encoding errors produce a message and exit two."""
        fix = self.make_fix()
        error = UnicodeEncodeError("utf-8", "\udcff", 0, 1, "planted record failure")
        with (
            unittest.mock.patch.object(Path, "write_text", side_effect=error),
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
        self.assertIn("revert_proof:", buffer.getvalue())
        self.assertIn("planted record failure", buffer.getvalue())

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
