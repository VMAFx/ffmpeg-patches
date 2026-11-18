# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Regression tests for scripts/revert_proof.py using temporary Git repositories."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import sys
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

from scripts import revert_proof, upstream_lint
from tests.helpers import capture_main, run_git

BASE_SOURCE = "int ff_value(void)\n{\n    return 1;\n}\n"
FIXED_SOURCE = "int ff_value(void)\n{\n    return 2;\n}\n"
CONFLICT_SOURCE = "int ff_value(void)\n{\n    return 3;\n}\n"


class RevertProofTest(unittest.TestCase):
    """Test scripts/revert_proof.py acceptance, planted weak test, refusals and boundaries."""

    def setUp(self) -> None:
        """Create an isolated temporary Git repository with one base source commit."""
        self._scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self._scratch.cleanup)
        self.root = Path(self._scratch.name)
        self.tree = self.root / "tree"
        self.tree.mkdir()
        self.record_path = self.root / "record.json"
        run_git(self.tree, "init", "-q", preserve_config=True)
        self.write(".gitignore", "ignored-user.txt\n")
        self.write("libavfilter/vf_x.c", BASE_SOURCE)
        self.base = self.commit("avfilter/vf_x: base commit")

    def write(self, relpath: str, content: str) -> Path:
        """Write UTF-8 text to a relative path inside the fixture work tree."""
        target = self.tree / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return target

    def commit(self, subject: str) -> str:
        """Stage all fixture changes, create a commit and return its full SHA."""
        run_git(self.tree, "add", ".", preserve_config=True)
        run_git(self.tree, "commit", "-qm", subject, preserve_config=True)
        return run_git(self.tree, "rev-parse", "HEAD", preserve_config=True)

    def caller_snapshot(self) -> tuple[str, bytes, str]:
        """Capture caller status, index bytes and branch without refreshing the index."""
        status = run_git(
            self.tree,
            "--no-optional-locks",
            "status",
            "--porcelain",
            "--untracked-files=all",
            "--ignored",
            preserve_config=True,
        )
        index = (self.tree / ".git/index").read_bytes()
        head = run_git(self.tree, "rev-parse", "HEAD", preserve_config=True)
        return status, index, head

    def assert_caller_unchanged(self, before: tuple[str, bytes, str], moved: bool) -> None:
        """Preserve caller bytes, allowing porcelain to reflect an external ref move."""
        after = self.caller_snapshot()
        self.assertEqual(after[1] if moved else after, before[1] if moved else before)
        self.assertEqual((self.tree / "user.txt").read_bytes(), b"user untracked\n")
        self.assertEqual((self.tree / "ignored-user.txt").read_bytes(), b"user ignored\n")

    def assert_no_scratch(self) -> None:
        """Require removed scratch files and the expected number of Git entries."""
        for path in self.scratch_paths[:2]:
            self.assertFalse(path.exists(), str(path))
        entries = run_git(self.tree, "worktree", "list", "--porcelain", preserve_config=True)
        self.assertEqual(entries.count("worktree "), getattr(self, "expected_entries", 1), entries)

    def run_cli(
        self,
        commit: str,
        command: list[str],
        *,
        timeout: str | None = None,
        expect_scratch: bool = True,
        head_may_move: bool = False,
    ) -> tuple[int, str, str]:
        """Run the CLI and require caller preservation and scratch cleanup on every outcome."""
        self.write("user.txt", "user untracked\n")
        self.write("ignored-user.txt", "user ignored\n")
        before = self.caller_snapshot()
        self.scratch_paths: list[Path] = []
        real_mkdtemp = tempfile.mkdtemp

        def capture_directory(*args: object, **kwargs: object) -> str:
            """Record fresh private scratch directories without replacing their creation."""
            directory = real_mkdtemp(*args, **kwargs)
            path = Path(directory)
            self.scratch_paths.append(path)
            self.assertEqual(path.stat().st_mode & 0o777, 0o700)
            self.assertFalse(path.is_relative_to(self.tree))
            return directory

        argv = ["--tree", str(self.tree), "--commit", commit, "--record", str(self.record_path)]
        if timeout is not None:
            argv.extend(["--timeout", timeout])
        if command:
            argv.extend(["--", *command])
        try:
            with unittest.mock.patch.object(tempfile, "mkdtemp", side_effect=capture_directory):
                return capture_main(revert_proof.main, *argv)
        finally:
            self.assert_caller_unchanged(before, head_may_move)
            self.assert_no_scratch()
            self.assertEqual(len(self.scratch_paths), int(expect_scratch))

    def assert_clean_tree(self, expected_source: str = FIXED_SOURCE) -> None:
        """Assert tracked git status is empty and the source file holds expected_source."""
        status = run_git(
            self.tree, "status", "--porcelain", "--untracked-files=no", preserve_config=True
        )
        self.assertEqual(status, "")
        self.assertEqual(
            (self.tree / "libavfilter/vf_x.c").read_text(encoding="utf-8"), expected_source
        )

    def test_accept_fix_commit_fails_when_reverted_in_scratch(self) -> None:
        """A fix commit whose test fails after reverse apply records nonzero rc_reverted."""
        self.write("libavfilter/vf_x.c", FIXED_SOURCE)
        self.write(
            "tests/check_x.py",
            "from pathlib import Path\n"
            "import sys\n"
            "text = Path('libavfilter/vf_x.c').read_text(encoding='utf-8')\n"
            "sys.exit(0 if 'return 2;' in text else 1)\n",
        )
        fix_sha = self.commit("avfilter/vf_x: fix return value")
        head_tree = run_git(self.tree, "rev-parse", "HEAD^{tree}", preserve_config=True)
        cmd = [sys.executable, "tests/check_x.py"]

        rc, _, err = self.run_cli(fix_sha, cmd)
        self.assertEqual(rc, 0, err)
        self.assert_clean_tree(FIXED_SOURCE)
        record = json.loads(self.record_path.read_text(encoding="utf-8"))
        self.assertEqual(
            set(record.keys()),
            {
                "commit",
                "head",
                "head_tree",
                "command",
                "rc_head",
                "rc_reverted",
                "reverted_paths",
            },
        )
        self.assertEqual(record["commit"], fix_sha)
        self.assertEqual(record["head"], fix_sha)
        self.assertEqual(record["head_tree"], head_tree)
        self.assertEqual(record["command"], cmd)
        self.assertEqual(record["rc_head"], 0)
        self.assertEqual(record["rc_reverted"], 1)
        self.assertEqual(record["reverted_paths"], ["libavfilter/vf_x.c"])

    def import_root_fix(self) -> str:
        """Merge an unrelated source-fix root into the caller's existing history."""
        branch = run_git(self.tree, "symbolic-ref", "--short", "HEAD", preserve_config=True)
        run_git(self.tree, "checkout", "--orphan", "imported-root", preserve_config=True)
        run_git(self.tree, "rm", "-rf", ".", preserve_config=True)
        self.write("libavfilter/vf_root.c", FIXED_SOURCE)
        commit = self.commit("avfilter/vf_root: fix imported value")
        run_git(self.tree, "checkout", "-q", branch, preserve_config=True)
        run_git(
            self.tree,
            "merge",
            "--allow-unrelated-histories",
            "--no-ff",
            "-qm",
            "merge imported history",
            "imported-root",
            preserve_config=True,
        )
        return commit

    def test_root_fix_reversal_removes_source_in_scratch(self) -> None:
        """An imported root fix yields a proof after its added source disappears."""
        commit = self.import_root_fix()
        head = run_git(self.tree, "rev-parse", "HEAD", preserve_config=True)
        missing = upstream_lint._revert_proof(self.tree, self.base, head, ())
        self.assertEqual(missing.rc, 1, missing.output)
        self.assertIn("no revert-proof record", missing.output)
        command = [
            sys.executable,
            "-c",
            "from pathlib import Path; import sys; "
            "sys.exit(0 if Path('libavfilter/vf_root.c').exists() else 17)",
        ]
        rc, _, err = self.run_cli(commit, command)
        self.assertEqual(rc, 0, err)
        record = json.loads(self.record_path.read_text(encoding="utf-8"))
        self.assertEqual(record["commit"], commit)
        self.assertEqual(record["rc_head"], 0)
        self.assertEqual(record["rc_reverted"], 17)
        self.assertEqual(record["reverted_paths"], ["libavfilter/vf_root.c"])
        self.assertTrue((self.tree / "libavfilter/vf_root.c").exists())
        proved = upstream_lint._revert_proof(self.tree, self.base, head, (self.record_path,))
        self.assertEqual(proved.rc, 0, proved.output)
        self.assertIn("1 fix commit(s), 1 revert-proof record(s)", proved.output)

    def test_root_fix_reverse_apply_conflict_is_refused(self) -> None:
        """A changed root-added source refuses reverse apply with the existing exit 4."""
        commit = self.import_root_fix()
        self.write("libavfilter/vf_root.c", CONFLICT_SOURCE)
        self.commit("avfilter/vf_root: update imported value")
        rc, _, err = self.run_cli(commit, [sys.executable, "-c", "pass"])
        self.assertEqual(rc, 4, err)
        self.assertIn(f"reverse apply failed for {commit}", err)
        self.assertFalse(self.record_path.exists())

    def test_command_at_consumer_argument_cap_is_accepted(self) -> None:
        """Exactly the shared argument cap produces a consumer-valid command record."""
        self.write("libavfilter/vf_x.c", FIXED_SOURCE)
        commit = self.commit("avfilter/vf_x: fix return value")
        command = [
            sys.executable,
            "-c",
            "from pathlib import Path; import sys; sys.exit(0 if 'return 2;' in "
            "Path('libavfilter/vf_x.c').read_text(encoding='utf-8') else 1)",
        ] + ["arg"] * (upstream_lint.MAX_COMMAND_ARGS - 3)
        rc, _, err = self.run_cli(commit, command)
        self.assertEqual(rc, 0, err)
        record = json.loads(self.record_path.read_text(encoding="utf-8"))
        self.assertEqual(len(record["command"]), upstream_lint.MAX_COMMAND_ARGS)
        self.assertEqual(record["command"], command)
        self.assertIsNone(upstream_lint._revert_proof_schema_error(record))
        step = upstream_lint._revert_proof(self.tree, self.base, commit, (self.record_path,))
        self.assertEqual(step.rc, 0, step.output)

    def test_command_above_consumer_argument_cap_is_refused_before_scratch(self) -> None:
        """One argument beyond the shared cap refuses before scratch or command work."""
        self.write("libavfilter/vf_x.c", FIXED_SOURCE)
        commit = self.commit("avfilter/vf_x: fix return value")
        self.record_path.write_text("old record\n", encoding="utf-8")
        count = upstream_lint.MAX_COMMAND_ARGS + 1
        command = [sys.executable, "-c", "pass"] + ["arg"] * (count - 3)
        with unittest.mock.patch.object(revert_proof, "run_test_command") as run_command:
            rc, _, err = self.run_cli(commit, command, expect_scratch=False)
        self.assertEqual(rc, 2, err)
        self.assertIn(f"command arguments exceed MAX_COMMAND_ARGS={count - 1}: {count}", err)
        run_command.assert_not_called()
        self.assertEqual(self.record_path.read_text(encoding="utf-8"), "old record\n")

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

    def test_refuse_dirty_tree(self) -> None:
        """An uncommitted change to a tracked file is refused with exit 2 and no record."""
        self.write("libavfilter/vf_x.c", FIXED_SOURCE)
        fix_sha = self.commit("avfilter/vf_x: fix return value")
        self.write("libavfilter/vf_x.c", CONFLICT_SOURCE)

        rc, _, err = self.run_cli(fix_sha, [sys.executable, "-c", "pass"], expect_scratch=False)
        self.assertEqual(rc, 2)
        self.assertIn("uncommitted changes to tracked files", err)
        self.assertFalse(self.record_path.exists())

    def test_refuse_commit_not_ancestor(self) -> None:
        """A commit that is not an ancestor of HEAD is refused with exit 2 and no record."""
        self.write("libavfilter/vf_x.c", FIXED_SOURCE)
        other_sha = self.commit("avfilter/vf_x: fix on side branch")
        run_git(self.tree, "reset", "--hard", self.base, preserve_config=True)

        rc, _, err = self.run_cli(other_sha, [sys.executable, "-c", "pass"], expect_scratch=False)
        self.assertEqual(rc, 2)
        self.assertIn("not an ancestor of HEAD", err)
        self.assertFalse(self.record_path.exists())

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

    def test_refuse_reverse_apply_conflict_removes_scratch(self) -> None:
        """A conflicting reverse apply exits 4, removes scratch and preserves caller files."""
        self.write("libavfilter/vf_x.c", FIXED_SOURCE)
        fix_sha = self.commit("avfilter/vf_x: fix return value")
        self.write("libavfilter/vf_x.c", CONFLICT_SOURCE)
        self.commit("avfilter/vf_x: overwrite return value")
        cmd = [sys.executable, "-c", "import sys; sys.exit(0)"]

        rc, _, err = self.run_cli(fix_sha, cmd)
        self.assertEqual(rc, 4)
        self.assertIn("reverse apply", err)
        self.assertFalse(self.record_path.exists())
        self.assert_clean_tree(CONFLICT_SOURCE)

    def test_boundary_timeout_records_timeout_and_removes_scratch(self) -> None:
        """A test sleeping past a 1 second timeout on the reverted tree records timeout."""
        self.write("libavfilter/vf_x.c", FIXED_SOURCE)
        fix_sha = self.commit("avfilter/vf_x: fix hang")
        script = (
            "from pathlib import Path\n"
            "import sys, time\n"
            "text = Path('libavfilter/vf_x.c').read_text(encoding='utf-8')\n"
            "if 'return 2;' not in text:\n"
            "    time.sleep(5)\n"
            "sys.exit(0)\n"
        )
        cmd = [sys.executable, "-c", script]

        rc, _, err = self.run_cli(fix_sha, cmd, timeout="1")
        self.assertEqual(rc, 0, err)
        self.assert_clean_tree(FIXED_SOURCE)
        record = json.loads(self.record_path.read_text(encoding="utf-8"))
        self.assertEqual(record["rc_head"], 0)
        self.assertEqual(record["rc_reverted"], "timeout")

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

    def test_keyboard_interrupt_reaps_real_child_process_group_and_removes_scratch(self) -> None:
        """An interrupt kills the real child process group and removes its scratch checkout."""
        self.write("libavfilter/vf_x.c", FIXED_SOURCE)
        fix_sha = self.commit("avfilter/vf_x: fix return value")
        pid_file = self.root / "pids.txt"
        script = (
            "from pathlib import Path\n"
            "import os, signal, subprocess, sys, time\n"
            "Path('libavfilter/vf_x.c').write_text('interrupted edit\\n', encoding='utf-8')\n"
            "grandchild = subprocess.Popen(\n"
            "    [sys.executable, '-c', 'import time; time.sleep(5)'],\n"
            "    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,\n"
            ")\n"
            f"Path({str(pid_file)!r}).write_text(\n"
            "    f'{os.getpid()}\\n{grandchild.pid}\\n', encoding='utf-8'\n"
            ")\n"
            "os.kill(os.getppid(), signal.SIGINT)\n"
            "time.sleep(2)\n"
        )
        previous = signal.signal(signal.SIGINT, signal.default_int_handler)
        try:
            with self.assertRaises(KeyboardInterrupt):
                self.run_cli(fix_sha, [sys.executable, "-c", script], timeout="3")
        finally:
            signal.signal(signal.SIGINT, previous)
        pids = [int(line) for line in pid_file.read_text(encoding="utf-8").splitlines()[:2] if line]
        self.assertEqual(len(pids), 2)
        for pid in pids[:2]:
            for _ in range(25):
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.02)
            try:
                with self.assertRaises(ProcessLookupError):
                    os.kill(pid, 0)
            finally:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)
        self.assert_clean_tree(FIXED_SOURCE)
        self.assertFalse(self.record_path.exists())

    def make_fix(self) -> str:
        """Commit a source fix without adding caller-only fixture files."""
        self.write("libavfilter/vf_x.c", FIXED_SOURCE)
        return self.commit("avfilter/vf_x: fix return value")

    def test_scratch_staging_cannot_delete_untracked_caller_file(self) -> None:
        """Staging a user-named file in the scratch index cannot delete caller data."""
        fix = self.make_fix()
        script = (
            "from pathlib import Path; import subprocess\n"
            "Path('user.txt').write_text('scratch copy\\n', encoding='utf-8')\n"
            "subprocess.run(['git', 'add', 'user.txt'], check=True)\n"
        )
        rc, _, err = self.run_cli(fix, [sys.executable, "-c", script])
        self.assertEqual(rc, 3, err)
        self.assertFalse(self.record_path.exists())

    def test_refuse_staged_new_file_as_dirty(self) -> None:
        """A staged new caller file is refused without changing its index or bytes."""
        fix = self.make_fix()
        self.write("staged.txt", "staged user data\n")
        run_git(self.tree, "add", "staged.txt", preserve_config=True)
        rc, _, err = self.run_cli(fix, [sys.executable, "-c", "pass"], expect_scratch=False)
        self.assertEqual(rc, 2, err)
        self.assertIn("proof is about the commit", err)
        self.assertEqual((self.tree / "staged.txt").read_bytes(), b"staged user data\n")

    def test_hidden_caller_content_is_not_used_as_committed_source(self) -> None:
        """Caller visibility flags cannot substitute hidden edits for committed source."""
        fix = self.make_fix()
        for flag in ("--assume-unchanged", "--skip-worktree"):
            with self.subTest(flag=flag):
                run_git(self.tree, "update-index", flag, "libavfilter/vf_x.c", preserve_config=True)
                self.write("libavfilter/vf_x.c", CONFLICT_SOURCE)
                rc, _, err = self.run_cli(
                    fix,
                    [
                        sys.executable,
                        "-c",
                        "from pathlib import Path; import sys; "
                        "text = Path('libavfilter/vf_x.c').read_text(encoding='utf-8'); "
                        "sys.exit(0 if 'return 2;' in text else 1)",
                    ],
                )
                self.assertEqual(rc, 0, err)
                payload = json.loads(self.record_path.read_text(encoding="utf-8"))
                self.assertEqual((payload["rc_head"], payload["rc_reverted"]), (0, 1))
                self.assertEqual(
                    (self.tree / "libavfilter/vf_x.c").read_bytes(), CONFLICT_SOURCE.encode()
                )
                self.write("libavfilter/vf_x.c", FIXED_SOURCE)
                run_git(
                    self.tree,
                    "update-index",
                    "--no-assume-unchanged",
                    "--no-skip-worktree",
                    "libavfilter/vf_x.c",
                    preserve_config=True,
                )

    def test_untracked_reverse_apply_target_in_caller_is_irrelevant(self) -> None:
        """A deleted source path is recreated only in scratch despite caller collisions."""
        self.write("libavfilter/old.c", BASE_SOURCE)
        self.commit("avfilter/old: add legacy file")
        run_git(self.tree, "rm", "-q", "libavfilter/old.c", preserve_config=True)
        fix = self.make_fix()
        target = self.write("libavfilter/old.c", "user old source\n")
        rc, _, err = self.run_cli(fix, [sys.executable, "-c", "pass"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(target.read_bytes(), b"user old source\n")
        payload = json.loads(self.record_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["reverted_paths"], ["libavfilter/old.c", "libavfilter/vf_x.c"])

    def test_keyboard_interrupt_in_either_run_removes_scratch(self) -> None:
        """Either command interrupt removes scratch and preserves caller user files."""
        fix = self.make_fix()
        for results in ((KeyboardInterrupt(),), (0, KeyboardInterrupt())):
            with self.subTest(run=len(results)):
                with (
                    unittest.mock.patch.object(
                        revert_proof, "run_test_command", side_effect=results
                    ),
                    self.assertRaises(KeyboardInterrupt),
                ):
                    self.run_cli(fix, [sys.executable, "-c", "pass"])
                self.assertFalse(self.record_path.exists())

    def test_interrupt_during_reverse_apply_removes_created_scratch_paths(self) -> None:
        """An interrupt after reverse apply recreates a source only in removed scratch."""
        self.write("libavfilter/old.c", BASE_SOURCE)
        self.commit("avfilter/old: add legacy file")
        run_git(self.tree, "rm", "-q", "libavfilter/old.c", preserve_config=True)
        fix = self.make_fix()
        real_run = upstream_lint._run

        def interrupt_apply(argv: list[str], cwd: Path, *args: object, **kwargs: object):
            """Apply the actual patch before raising an interrupt at the return boundary."""
            result = real_run(argv, cwd, *args, **kwargs)
            if argv[:3] == ["git", "apply", "-R"]:
                self.assertEqual(result.returncode, 0)
                self.assertTrue((cwd / "libavfilter/old.c").exists())
                raise KeyboardInterrupt
            return result

        with (
            unittest.mock.patch.object(upstream_lint, "_run", side_effect=interrupt_apply),
            self.assertRaises(KeyboardInterrupt),
        ):
            self.run_cli(fix, [sys.executable, "-c", "pass"])
        self.assertFalse((self.tree / "libavfilter/old.c").exists())
        self.assertFalse(self.record_path.exists())

    def test_removal_failure_and_spawn_error_exit_five_with_scratch_path(self) -> None:
        """Nonzero removal and cleanup spawn errors both exit 5 and name scratch."""
        fix = self.make_fix()
        real_run = upstream_lint._run
        for failure in ("returncode", "spawn"):
            with self.subTest(failure=failure):

                def fail_remove(
                    argv: list[str],
                    cwd: Path,
                    *args: object,
                    failure_kind: str = failure,
                    **kwargs: object,
                ):
                    """Fail only named scratch removal without affecting unrelated Git commands."""
                    if argv[:3] == ["git", "worktree", "remove"]:
                        if failure_kind == "spawn":
                            raise OSError("planted cleanup spawn failure")
                        return upstream_lint.RunResult(tuple(argv), 1, "planted removal failure")
                    return real_run(argv, cwd, *args, **kwargs)

                with unittest.mock.patch.object(upstream_lint, "_run", side_effect=fail_remove):
                    self.expected_entries = 2
                    rc, _, err = self.run_cli(fix, [sys.executable, "-c", "pass"])
                self.assertEqual(rc, 5, err)
                self.assertIn(str(self.scratch_paths[0] / "tree"), err)
                self.assertIn("scratch removal failed", err)
                self.assertFalse(self.record_path.exists())
                run_git(
                    self.tree,
                    "worktree",
                    "remove",
                    "--force",
                    str(self.scratch_paths[0] / "tree"),
                    preserve_config=True,
                )

    def test_failed_scratch_creation_still_cleans_partial_checkout(self) -> None:
        """A creation failure after Git registers scratch still removes its entry."""
        fix = self.make_fix()
        real_run = upstream_lint._run

        def fail_after_add(argv: list[str], cwd: Path, *args: object, **kwargs: object):
            """Create the real worktree then report a planted creation failure."""
            result = real_run(argv, cwd, *args, **kwargs)
            if argv[:3] == ["git", "worktree", "add"]:
                self.assertEqual(result.returncode, 0)
                return upstream_lint.RunResult(tuple(argv), 1, "planted checkout failure")
            return result

        with unittest.mock.patch.object(upstream_lint, "_run", side_effect=fail_after_add):
            rc, _, err = self.run_cli(fix, [sys.executable, "-c", "pass"])
        self.assertEqual(rc, 2, err)
        self.assertIn("planted checkout failure", err)
        self.assertFalse(self.record_path.exists())

    def test_moved_caller_head_exits_three_without_record(self) -> None:
        """Moving the caller branch during either run refuses proof with exit 3."""
        fix = self.make_fix()
        real_command = revert_proof.run_test_command
        for phase in (1, 2):
            run_git(self.tree, "update-ref", "HEAD", fix, preserve_config=True)
            calls = 0

            def move_caller(
                command: list[str], tree: Path, timeout: float, at_phase: int = phase
            ) -> int | str:
                """Move only the caller ref at the selected command boundary."""
                nonlocal calls
                calls += 1
                if calls == at_phase:
                    run_git(self.tree, "update-ref", "HEAD", self.base, preserve_config=True)
                return real_command(command, tree, timeout)

            with unittest.mock.patch.object(
                revert_proof, "run_test_command", side_effect=move_caller
            ):
                rc, _, err = self.run_cli(fix, [sys.executable, "-c", "pass"], head_may_move=True)
            self.assertEqual(rc, 3, err)
            self.assertIn("HEAD moved", err)
            self.assertFalse(self.record_path.exists())
            self.assertEqual(
                run_git(self.tree, "rev-parse", "HEAD", preserve_config=True), self.base
            )

    def test_old_record_is_removed_before_failing_head_run(self) -> None:
        """A failed HEAD command cannot leave a previously successful record usable."""
        fix = self.make_fix()
        self.record_path.write_text("old proof\n", encoding="utf-8")
        script = (
            "from pathlib import Path; "
            f"assert not Path({str(self.record_path)!r}).exists(); raise SystemExit(1)"
        )
        rc, _, err = self.run_cli(fix, [sys.executable, "-c", script])
        self.assertEqual(rc, 3, err)
        self.assertFalse(self.record_path.exists())

    def test_unremovable_old_record_refuses_before_runs(self) -> None:
        """An unremovable record path refuses with exit 2 before scratch creation."""
        fix = self.make_fix()
        self.record_path.mkdir()
        rc, _, err = self.run_cli(fix, [sys.executable, "-c", "pass"], expect_scratch=False)
        self.assertEqual(rc, 2, err)
        self.assertIn("record", err)
        self.assertTrue(self.record_path.is_dir())

    def test_caller_only_build_file_fails_clean_head_run(self) -> None:
        """A command requiring caller-only ignored build data fails at clean HEAD."""
        fix = self.make_fix()
        script = "from pathlib import Path; Path('ignored-user.txt').read_bytes()"
        rc, _, err = self.run_cli(fix, [sys.executable, "-c", script])
        self.assertEqual(rc, 3, err)
        self.assertIn("test fails at head", err)
        self.assertFalse(self.record_path.exists())

    def test_command_pwd_matches_detached_scratch_checkout(self) -> None:
        """Command PWD names the clean detached scratch checkout on both runs."""
        fix = self.make_fix()
        script = (
            "from pathlib import Path; import os, subprocess\n"
            "assert os.environ['PWD'] == str(Path.cwd())\n"
            f"assert Path.cwd() != Path({str(self.tree)!r})\n"
            "assert not Path('user.txt').exists()\n"
            "assert subprocess.run(['git', 'symbolic-ref', '-q', 'HEAD']).returncode == 1\n"
        )
        rc, _, err = self.run_cli(fix, [sys.executable, "-c", script])
        self.assertEqual(rc, 0, err)
        payload = json.loads(self.record_path.read_text(encoding="utf-8"))
        self.assertEqual((payload["rc_head"], payload["rc_reverted"]), (0, 0))

    def test_tempdir_inside_caller_is_refused_before_record_removal(self) -> None:
        """A caller-contained temporary root is refused before old output is removed."""
        fix = self.make_fix()
        self.record_path.write_text("old proof\n", encoding="utf-8")
        with unittest.mock.patch.object(tempfile, "gettempdir", return_value=str(self.tree)):
            rc, _, err = self.run_cli(fix, [sys.executable, "-c", "pass"], expect_scratch=False)
        self.assertEqual(rc, 2, err)
        self.assertIn("temporary directory must be outside", err)
        self.assertEqual(self.record_path.read_text(encoding="utf-8"), "old proof\n")

    def test_record_path_inside_caller_is_refused_without_touching_it(self) -> None:
        """An output path inside the caller cannot break the read-only caller contract."""
        fix = self.make_fix()
        self.record_path = self.write("old-record.json", "caller record\n")
        rc, _, err = self.run_cli(fix, [sys.executable, "-c", "pass"], expect_scratch=False)
        self.assertEqual(rc, 2, err)
        self.assertIn("outside the caller", err)
        self.assertEqual(self.record_path.read_bytes(), b"caller record\n")

    def test_help_explains_moved_head_cleanup_and_clean_checkout_cost(self) -> None:
        """Help states the changed exit classes and clean-checkout command limitation."""
        help_text = revert_proof.build_parser().format_help()
        self.assertIn("exit 2", help_text)
        self.assertIn("exit 4", help_text)
        self.assertIn("rights of the user", help_text)
        self.assertIn("not provenance", help_text)
        self.assertIn("outcome the module survives", help_text)
        self.assertIn("exit 3", help_text)
        self.assertIn("exit 5", help_text)
        self.assertIn("clean checkout", help_text)
        self.assertIn("absolute paths", help_text)

    def test_failing_or_timed_out_head_command_only_edits_scratch(self) -> None:
        """Failed and timed-out HEAD edits leave caller bytes and index untouched."""
        fix = self.make_fix()
        for tail in ("raise SystemExit(1)", "import time; time.sleep(5)"):
            script = (
                "from pathlib import Path\n"
                "Path('libavfilter/vf_x.c').write_text('scratch edit\\n', encoding='utf-8')\n"
                + tail
            )
            rc, _, err = self.run_cli(fix, [sys.executable, "-c", script], timeout="0.2")
            self.assertEqual(rc, 3, err)
            self.assertIn("test fails at head", err)
            self.assertFalse(self.record_path.exists())
            self.assert_clean_tree()

    def test_successful_head_edit_is_refused_before_reverse_apply(self) -> None:
        """A successful command that edits scratch source cannot prove committed HEAD."""
        fix = self.make_fix()
        script = (
            "from pathlib import Path\n"
            "Path('libavfilter/vf_x.c').write_text('scratch edit\\n', encoding='utf-8')\n"
        )
        rc, _, err = self.run_cli(fix, [sys.executable, "-c", script])
        self.assertEqual(rc, 3, err)
        self.assertIn("modified tracked files at head", err)
        self.assertFalse(self.record_path.exists())
        self.assert_clean_tree()

    def test_head_failure_cannot_hide_scratch_removal_failure(self) -> None:
        """Scratch cleanup status 5 takes precedence over a failing HEAD command."""
        fix = self.make_fix()
        real_run = upstream_lint._run

        def fail_remove(argv: list[str], cwd: Path, *args: object, **kwargs: object):
            """Fail only the Git removal step after a real failing HEAD command."""
            if argv[:3] == ["git", "worktree", "remove"]:
                return upstream_lint.RunResult(tuple(argv), 1, "planted removal failure")
            return real_run(argv, cwd, *args, **kwargs)

        with unittest.mock.patch.object(upstream_lint, "_run", side_effect=fail_remove):
            self.expected_entries = 2
            rc, _, err = self.run_cli(fix, [sys.executable, "-c", "raise SystemExit(1)"])
        self.assertEqual(rc, 5, err)
        self.assertIn(str(self.scratch_paths[0] / "tree"), err)
        self.assertFalse(self.record_path.exists())

    def test_missing_recorded_branch_refuses_even_if_new_head_has_same_commit(self) -> None:
        """Deleting the recorded branch cannot hide behind a replacement HEAD ref."""
        fix = self.make_fix()
        reference = run_git(self.tree, "symbolic-ref", "HEAD", preserve_config=True)
        real_command = revert_proof.run_test_command

        def replace_branch(command: list[str], tree: Path, timeout: float) -> int | str:
            """Replace the caller HEAD ref without changing its index or file bytes."""
            run_git(self.tree, "update-ref", "refs/heads/replacement", fix, preserve_config=True)
            run_git(
                self.tree, "symbolic-ref", "HEAD", "refs/heads/replacement", preserve_config=True
            )
            run_git(self.tree, "update-ref", "-d", reference, preserve_config=True)
            return real_command(command, tree, timeout)

        with unittest.mock.patch.object(
            revert_proof, "run_test_command", side_effect=replace_branch
        ):
            rc, _, err = self.run_cli(fix, [sys.executable, "-c", "pass"])
        self.assertEqual(rc, 3, err)
        self.assertIn("HEAD moved", err)
        self.assertFalse(self.record_path.exists())
        self.assertEqual(run_git(self.tree, "rev-parse", "HEAD", preserve_config=True), fix)

    def test_subdirectory_tree_preserves_record_elsewhere_in_caller(self) -> None:
        """A repository subdirectory cannot bypass caller record containment."""
        fix = self.make_fix()
        record = self.write("user-record.json", "caller bytes\n")
        with unittest.mock.patch.object(revert_proof, "run_test_command") as command:
            rc, _, err = capture_main(
                revert_proof.main,
                "--tree",
                str(self.tree / "libavfilter"),
                "--commit",
                fix,
                "--record",
                str(record),
                "--",
                sys.executable,
                "-c",
                "pass",
            )
        self.assertEqual(rc, 2, err)
        self.assertIn("work tree root", err)
        self.assertEqual(record.read_bytes(), b"caller bytes\n")
        command.assert_not_called()

    def test_linked_caller_git_administration_record_paths_are_refused(self) -> None:
        """Linked indexes, common paths and symlink aliases survive record refusal."""
        fix = self.make_fix()
        linked = self.root / "linked"
        run_git(self.tree, "worktree", "add", "--detach", str(linked), fix, preserve_config=True)
        gitdir = Path(run_git(linked, "rev-parse", "--absolute-git-dir", preserve_config=True))
        common = (
            linked / run_git(linked, "rev-parse", "--git-common-dir", preserve_config=True)
        ).resolve()
        index = gitdir / "index"
        admin = common / "user-record"
        admin.write_bytes(b"admin bytes\n")
        alias = self.root / "alias"
        alias.symlink_to(admin)
        before_index = index.read_bytes()
        for record in (index, admin, alias):
            with self.subTest(record=record):
                with unittest.mock.patch.object(revert_proof, "run_test_command") as command:
                    rc, _, err = capture_main(
                        revert_proof.main,
                        "--tree",
                        str(linked),
                        "--commit",
                        fix,
                        "--record",
                        str(record),
                        "--",
                        sys.executable,
                        "-c",
                        "pass",
                    )
                self.assertEqual(rc, 2, err)
                self.assertIn("Git administration", err)
                self.assertEqual(index.read_bytes(), before_index)
                self.assertEqual(admin.read_bytes(), b"admin bytes\n")
                self.assertTrue(alias.is_symlink())
                command.assert_not_called()

    def test_unrelated_missing_worktree_survives_pass_fail_and_timeout(self) -> None:
        """Cleanup preserves unrelated unavailable worktrees after each command outcome."""
        fix = self.make_fix()
        missing = self.root / "unrelated"
        run_git(self.tree, "worktree", "add", "--detach", str(missing), fix, preserve_config=True)
        shutil.rmtree(missing)
        for script, expected in (
            ("pass", 0),
            ("raise SystemExit(1)", 3),
            ("import time; time.sleep(5)", 3),
        ):
            with self.subTest(script=script):
                self.expected_entries = 2
                rc, _, err = self.run_cli(fix, [sys.executable, "-c", script], timeout="0.2")
                self.assertEqual(rc, expected, err)
                entries = run_git(
                    self.tree, "worktree", "list", "--porcelain", preserve_config=True
                )
                self.assertIn(f"worktree {missing}\n", entries)

    def test_named_removal_cleans_an_already_missing_owned_scratch(self) -> None:
        """Named Git removal handles a missing owned checkout without global pruning."""
        fix = self.make_fix()
        directory = self.root / "owned"
        directory.mkdir()
        scratch = directory / "tree"
        run_git(self.tree, "worktree", "add", "--detach", str(scratch), fix, preserve_config=True)
        shutil.rmtree(scratch)
        revert_proof.remove_scratch(self.tree, scratch)
        self.assertFalse(directory.exists())
        entries = run_git(self.tree, "worktree", "list", "--porcelain", preserve_config=True)
        self.assertEqual(entries.count("worktree "), 1, entries)

    def test_successful_head_edit_of_unrelated_tracked_file_is_refused(self) -> None:
        """Editing an unrelated tracked scratch file cannot prove committed HEAD."""
        self.write("libavfilter/other.c", BASE_SOURCE)
        self.commit("avfilter/other: add independent source")
        fix = self.make_fix()
        script = (
            "from pathlib import Path; Path('libavfilter/other.c').write_text('scratch edit\\n')"
        )
        rc, _, err = self.run_cli(fix, [sys.executable, "-c", script])
        self.assertEqual(rc, 3, err)
        self.assertIn("modified tracked files at head", err)
        self.assertFalse(self.record_path.exists())
        self.assertEqual(
            (self.tree / "libavfilter/other.c").read_text(encoding="utf-8"), BASE_SOURCE
        )


if __name__ == "__main__":
    unittest.main()
