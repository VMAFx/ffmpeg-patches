# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Revert-proof scratch regression tests using temporary Git repositories."""

from __future__ import annotations

import json
import os
import shutil
import subprocess  # nosec B404 - bounded fixture Git initialization errors only.
import sys
import unittest.mock
from pathlib import Path

from scripts import revert_proof, upstream_lint
from tests.helpers import capture_main, run_git
from tests.revert_proof_helpers import BASE_SOURCE, RevertProofFixture


class RevertProofScratchCleanupTest(RevertProofFixture):
    """Check caller preservation, scratch cleanup and record refusals."""

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
            f"assert not Path({str(self.record_path)!r}).exists(); raise SystemExit(7)"
        )
        rc, _, err = self.run_cli(fix, [sys.executable, "-c", script])
        self.assertEqual(rc, 3, err)
        self.assertIn("rc=7", err)
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
        """Fresh-process temp selection refuses caller storage without even a probe write."""
        fix = self.make_fix()
        self.record_path.write_bytes(b"old proof\n")
        temporary = self.tree / "empty-tmp"
        temporary.mkdir()
        before = temporary.stat().st_mtime_ns
        environment = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        environment.update(
            GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", TMPDIR=str(temporary)
        )
        result = subprocess.run(  # nosec B603 - fixed Python and fixture paths, no shell.
            [
                sys.executable,
                "-B",
                str(upstream_lint.REPO_ROOT / "scripts/revert_proof.py"),
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
            ],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("temporary directory must be outside", result.stderr)
        self.assertEqual(self.record_path.read_bytes(), b"old proof\n")
        self.assertEqual(list(temporary.iterdir()), [])
        self.assertEqual(temporary.stat().st_mtime_ns, before)

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


class ScratchReviewRegressionTest(RevertProofFixture):
    """Exercise hook, sparse, replace-object and cleanup review regressions."""

    def test_locked_scratch_registration_is_removed(self) -> None:
        """Cleanup removes exactly its owned locked registration and scratch directory."""
        fix = self.make_fix()
        directory = self.root / "scratch"
        directory.mkdir()
        scratch = directory / "tree"
        run_git(self.tree, "worktree", "add", "--detach", str(scratch), fix, preserve_config=True)
        run_git(self.tree, "worktree", "lock", str(scratch), preserve_config=True)
        revert_proof.remove_scratch(self.tree, scratch)
        listing = run_git(self.tree, "worktree", "list", "--porcelain", preserve_config=True)
        self.assertNotIn(str(scratch), listing)
        self.assertFalse(directory.exists())

    def test_unregistered_scratch_skips_git_removal(self) -> None:
        """An unregistered owned directory is removed without a Git removal command."""
        directory = self.root / "unregistered"
        scratch = directory / "tree"
        scratch.mkdir(parents=True)
        with unittest.mock.patch.object(revert_proof, "cleanup_git") as cleanup:
            revert_proof.remove_scratch(self.tree, scratch)
        cleanup.assert_not_called()
        self.assertFalse(directory.exists())

    def test_scratch_creation_does_not_run_setup_hooks(self) -> None:
        """Scratch setup suppresses checkout and reference hooks; test commands keep hooks."""
        fix = self.make_fix()
        marker = self.root / "hook-marker"
        reference_marker = self.root / "reference-hook-marker"
        hooks = self.root / "hooks"
        hooks.mkdir()
        hook = hooks / "post-checkout"
        hook.write_text(f"#!/bin/sh\nprintf hook >> '{marker}'\n", encoding="ascii")
        hook.chmod(0o755)
        reference_hook = hooks / "reference-transaction"
        reference_hook.write_text(
            f"#!/bin/sh\nprintf reference >> '{reference_marker}'\n", encoding="ascii"
        )
        reference_hook.chmod(0o755)
        run_git(self.tree, "config", "core.hooksPath", str(hooks), preserve_config=True)
        rc, _, error = self.run_cli(fix, [sys.executable, "-c", "pass"])
        self.assertEqual(rc, 0, error)
        self.assertTrue(self.record_path.is_file())
        self.assertFalse(marker.exists())
        self.assertFalse(reference_marker.exists())
        rc, _, error = self.run_cli(fix, ["git", "checkout", "--detach", "HEAD"])
        self.assertEqual(rc, 0, error)
        self.assertEqual(marker.read_text(encoding="ascii"), "hookhook")

    def test_sparse_caller_gets_full_scratch_without_config_changes(self) -> None:
        """Per-command sparse overrides populate excluded files without caller writes."""
        self.write("excluded/data.txt", "committed data\n")
        fix = self.make_fix()
        run_git(self.tree, "sparse-checkout", "set", "libavfilter", preserve_config=True)
        self.assertFalse((self.tree / "excluded/data.txt").exists())
        patterns = self.tree / ".git/info/sparse-checkout"
        before_patterns = patterns.read_bytes()
        before_config = run_git(
            self.tree, "config", "--list", "--show-origin", preserve_config=True
        )
        program = (
            "from pathlib import Path; import sys; "
            "assert Path('excluded/data.txt').read_text() == 'committed data\\n'; "
            "sys.exit(0 if 'return 2;' in Path('libavfilter/vf_x.c').read_text() else 7)"
        )
        rc, _, error = self.run_cli(fix, [sys.executable, "-c", program])
        self.assertEqual(rc, 0, error)
        self.assertTrue(self.record_path.is_file())
        payload = json.loads(self.record_path.read_text(encoding="utf-8"))
        self.assertEqual((payload["rc_head"], payload["rc_reverted"]), (0, 7))
        self.assertEqual(patterns.read_bytes(), before_patterns)
        self.assertEqual(
            run_git(self.tree, "config", "--list", "--show-origin", preserve_config=True),
            before_config,
        )

    def test_blob_replacements_cannot_change_measurement_bytes(self) -> None:
        """HEAD and reverse patches use stored blob bytes while record IDs remain exact."""
        self.write("fixture.txt", "committed fixture\n")
        self.write("replacement.txt", "replacement fixture\n")
        fix = self.make_fix()
        original = run_git(self.tree, "rev-parse", "HEAD:fixture.txt", preserve_config=True)
        replacement = run_git(self.tree, "rev-parse", "HEAD:replacement.txt", preserve_config=True)
        source = run_git(self.tree, "rev-parse", "HEAD:libavfilter/vf_x.c", preserve_config=True)
        run_git(self.tree, "replace", original, replacement, preserve_config=True)
        run_git(self.tree, "replace", source, replacement, preserve_config=True)
        program = (
            "from pathlib import Path; import sys; "
            "assert Path('fixture.txt').read_text() == 'committed fixture\\n'; "
            "text = Path('libavfilter/vf_x.c').read_text(); "
            "assert 'replacement' not in text; sys.exit(0 if 'return 2;' in text else 7)"
        )
        rc, _, error = self.run_cli(fix, [sys.executable, "-c", program])
        self.assertEqual(rc, 0, error)
        payload = json.loads(self.record_path.read_text(encoding="utf-8"))
        self.assertEqual((payload["rc_head"], payload["rc_reverted"]), (0, 7))
        self.assertEqual(payload["commit"], fix)
        self.assertEqual(payload["head"], fix)
        self.assertEqual(
            payload["head_tree"],
            run_git(
                self.tree, "--no-replace-objects", "rev-parse", "HEAD^{tree}", preserve_config=True
            ),
        )

    def test_unusable_temp_candidates_name_every_candidate(self) -> None:
        """Unusable environment and fallback directories produce a bounded diagnostic."""
        with (
            unittest.mock.patch.dict(
                os.environ, {"TMPDIR": "first", "TEMP": "second", "TMP": "third"}
            ),
            unittest.mock.patch.object(Path, "is_dir", return_value=False),
            self.assertRaises(revert_proof._PreflightError) as caught,
        ):
            revert_proof.scratch_parent(self.tree)
        for candidate in ("first", "second", "third", "/tmp", "/var/tmp", "/usr/tmp"):  # nosec B108 - expected diagnostic text; nothing created here.
            self.assertIn(candidate, str(caught.exception))
