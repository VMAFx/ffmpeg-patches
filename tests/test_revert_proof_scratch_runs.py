# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Revert-proof scratch regression tests using temporary Git repositories."""

from __future__ import annotations

import contextlib
import json
import os
import signal
import sys
import time
import unittest
import unittest.mock
from pathlib import Path

from scripts import revert_proof, upstream_lint
from tests.helpers import run_git
from tests.revert_proof_helpers import (
    BASE_SOURCE,
    CONFLICT_SOURCE,
    FIXED_SOURCE,
    RevertProofFixture,
    process_exited,
)


class RevertProofScratchRunsTest(RevertProofFixture):
    """Check reverted runs, refusals, interrupts and scratch creation."""

    def test_reverted_run_starts_without_head_build_artifacts(self) -> None:
        """Both runs start fresh even when HEAD creates ignored and untracked build files."""
        self.write(".gitignore", "build/\nignored-user.txt\n")
        fix = self.make_fix()
        for expected in (0, 17):
            with self.subTest(expected=expected):
                script = (
                    "from pathlib import Path; import sys; "
                    "Path('build').mkdir(); Path('artifact').write_text('head build'); "
                    f"sys.exit(0 if 'return 2;' in Path('libavfilter/vf_x.c').read_text() "
                    f"else {expected})"
                )
                rc, _, error = self.run_cli(fix, [sys.executable, "-c", script])
                self.assertEqual(rc, 0, error)
                record = json.loads(self.record_path.read_text(encoding="utf-8"))
                self.assertEqual(record["rc_reverted"], expected)
                self.assertEqual(len(self.scratch_paths), 2)
                self.assertNotEqual(self.scratch_paths[0], self.scratch_paths[1])

    def test_reverted_checkout_setup_failure_writes_no_record(self) -> None:
        """A failure to create the second checkout cannot become regression evidence."""
        fix = self.make_fix()
        real_run = upstream_lint._run
        additions = 0

        def fail_second_add(argv: list[str], cwd: Path, *args: object, **kwargs: object):
            """Fail reverted registration after any per-command Git options."""
            nonlocal additions
            if "worktree" in argv and argv[argv.index("worktree") + 1] == "add":
                additions += 1
                if additions == 2:
                    return upstream_lint.RunResult(tuple(argv), 1, "reverted setup failed")
            return real_run(argv, cwd, *args, **kwargs)

        with unittest.mock.patch.object(upstream_lint, "_run", side_effect=fail_second_add):
            rc, _, error = self.run_cli(fix, [sys.executable, "-c", "pass"])
        self.assertEqual(rc, 2, error)
        self.assertIn("reverted setup failed", error)
        self.assertFalse(self.record_path.exists())

    def test_head_launch_failure_is_no_verdict(self) -> None:
        """A command that never starts at HEAD returns the launch-error sentinel."""
        self.assertEqual(
            revert_proof.run_test_command(["./missing-program"], self.tree, 1), "launch-error"
        )
        fix = self.make_fix()
        rc, _, error = self.run_cli(fix, ["./missing-program"])
        self.assertEqual(rc, 3, error)
        self.assertIn("launch-error", error)
        self.assertFalse(self.record_path.exists())

    def test_started_process_exit_127_remains_a_verdict(self) -> None:
        """An actual process exit 127 stays numeric and can demonstrate a regression."""
        fix = self.make_fix()
        script = (
            "from pathlib import Path; import sys; "
            "sys.exit(0 if 'return 2;' in Path('libavfilter/vf_x.c').read_text() else 127)"
        )
        rc, _, error = self.run_cli(fix, [sys.executable, "-c", script])
        self.assertEqual(rc, 0, error)
        record = json.loads(self.record_path.read_text(encoding="utf-8"))
        self.assertEqual(record["rc_reverted"], 127)
        step = upstream_lint._revert_proof(self.tree, self.base, fix, (self.record_path,))
        self.assertEqual(step.rc, 0, step.output)

    def test_accept_fix_commit_fails_when_reverted_in_scratch(self) -> None:
        """A fix commit whose test fails after reverse apply records nonzero rc_reverted."""
        self.write("libavfilter/vf_x.c", FIXED_SOURCE)
        program = (
            "from pathlib import Path\n"
            "import sys\n"
            "text = Path('libavfilter/vf_x.c').read_text(encoding='utf-8')\n"
            "sys.exit(0 if 'return 2;' in text else 1)\n"
        )
        self.write(
            "tests/check_x.py",
            program,
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

    @unittest.skipUnless(hasattr(os, "pidfd_open"), "requires kernel process descriptors")
    def test_process_exit_check_distinguishes_live_zombie_and_missing(self) -> None:
        """A live child fails the exit check while an unreaped or reaped child passes."""
        with upstream_lint.subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"]
        ) as child:
            self.assertFalse(process_exited(child.pid))
            child.kill()
            status = None
            for _ in range(50):
                status = os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG)
                if status is not None:
                    break
                time.sleep(0.01)
            self.assertIsNotNone(status)
            os.kill(child.pid, 0)
            self.assertTrue(process_exited(child.pid))
        self.assertTrue(process_exited(child.pid))

    @unittest.skipUnless(hasattr(os, "pidfd_open"), "requires kernel process descriptors")
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
            try:
                for _ in range(25):
                    if process_exited(pid):
                        break
                    time.sleep(0.02)
                self.assertTrue(process_exited(pid), f"child {pid} can still execute")
            finally:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)
        self.assert_clean_tree(FIXED_SOURCE)
        self.assertFalse(self.record_path.exists())

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
                program = (
                    "from pathlib import Path; import sys; "
                    "text = Path('libavfilter/vf_x.c').read_text(encoding='utf-8'); "
                    "sys.exit(0 if 'return 2;' in text else 1)"
                )
                rc, _, err = self.run_cli(
                    fix,
                    [
                        sys.executable,
                        "-c",
                        program,
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
            if argv[:2] == ["git", "apply"] and "-R" in argv:
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

    def test_failed_scratch_creation_preserves_original_error(self) -> None:
        """Creation before registration retains its error and never attempts named removal."""
        fix = self.make_fix()
        real_run = upstream_lint._run
        removals = []

        def fail_before_add(argv: list[str], cwd: Path, *args: object, **kwargs: object):
            """Reject registration after any per-command Git options."""
            if "worktree" in argv and argv[argv.index("worktree") + 1] == "add":
                return upstream_lint.RunResult(tuple(argv), 1, "original creation failure")
            if argv[:3] == ["git", "worktree", "remove"]:
                removals.append(argv)
            return real_run(argv, cwd, *args, **kwargs)

        with unittest.mock.patch.object(upstream_lint, "_run", side_effect=fail_before_add):
            rc, _, error = self.run_cli(fix, [sys.executable, "-c", "pass"])
        self.assertEqual(rc, 2, error)
        self.assertIn("original creation failure", error)
        self.assertEqual(removals, [])
        self.assertFalse(self.record_path.exists())

    def test_failed_scratch_creation_still_cleans_partial_checkout(self) -> None:
        """A creation failure after Git registers scratch still removes its entry."""
        fix = self.make_fix()
        real_run = upstream_lint._run

        def fail_after_add(argv: list[str], cwd: Path, *args: object, **kwargs: object):
            """Report a planted failure after registration with per-command Git options."""
            result = real_run(argv, cwd, *args, **kwargs)
            if "worktree" in argv and argv[argv.index("worktree") + 1] == "add":
                self.assertEqual(result.returncode, 0)
                return upstream_lint.RunResult(tuple(argv), 1, "planted checkout failure")
            return result

        with unittest.mock.patch.object(upstream_lint, "_run", side_effect=fail_after_add):
            rc, _, err = self.run_cli(fix, [sys.executable, "-c", "pass"])
        self.assertEqual(rc, 2, err)
        self.assertIn("planted checkout failure", err)
        self.assertFalse(self.record_path.exists())
