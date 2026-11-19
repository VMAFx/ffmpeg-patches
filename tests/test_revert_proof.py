# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Revert-proof core regression tests using temporary Git repositories."""

from __future__ import annotations

import json
import os
import signal
import sys
import unittest
import unittest.mock
from pathlib import Path

from scripts import revert_proof, upstream_lint
from tests.revert_proof_helpers import BASE_SOURCE, FIXED_SOURCE, RevertProofFixture


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
