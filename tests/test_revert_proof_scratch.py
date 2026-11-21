# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Revert-proof scratch regression tests using temporary Git repositories."""

from __future__ import annotations

import json
import os
import stat
import subprocess  # nosec B404 - bounded fixture Git initialization errors only.
import sys
import unittest.mock

from scripts import revert_proof, upstream_lint
from tests import revert_proof_helpers
from tests.helpers import capture_main, run_git
from tests.revert_proof_helpers import BASE_SOURCE, RevertProofFixture


class RevertProofScratchTest(RevertProofFixture):
    """Check revert-proof scratch behavior using isolated Git fixtures."""

    def test_active_checkout_filter_refuses_before_measurement(self) -> None:
        """HEAD attributes using a configured smudge or process driver refuse publication."""
        self.write(".gitattributes", "fixture.txt filter=counter\n")
        self.write("fixture.txt", "fixture bytes\n")
        fix = self.make_fix()
        counter = self.root / "counter"
        program = (
            "from pathlib import Path; "
            f"p = Path({str(counter)!r}); "
            "n = int(p.read_text()) + 1 if p.exists() else 1; "
            "p.write_text(str(n)); print(n)"
        )
        command = f'{sys.executable} -c "{program}"'
        for kind in ("smudge", "process"):
            with self.subTest(kind=kind):
                run_git(
                    self.tree, "config", f"filter.counter.{kind}", command, preserve_config=True
                )
                with unittest.mock.patch.object(
                    revert_proof, "measure_scratch", wraps=revert_proof.measure_scratch
                ) as measure:
                    rc, _, error = capture_main(
                        revert_proof.main,
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
                    )
                self.assertEqual(rc, 2, error)
                self.assertIn("counter", error)
                self.assertIn("fixture.txt", error)
                self.assertFalse(self.record_path.exists())
                self.assertFalse(counter.exists())
                measure.assert_not_called()
                run_git(self.tree, "config", f"filter.counter.{kind}", "", preserve_config=True)

    def test_hidden_attribute_edit_cannot_hide_head_filter(self) -> None:
        """An ignored working-tree attribute edit cannot hide an active HEAD checkout filter."""
        self.write(".gitattributes", "fixture.txt filter=counter\n")
        self.write("fixture.txt", "fixture bytes\n")
        fix = self.make_fix()
        run_git(self.tree, "config", "filter.counter.smudge", "exit 99", preserve_config=True)
        run_git(
            self.tree, "update-index", "--assume-unchanged", ".gitattributes", preserve_config=True
        )
        self.write(".gitattributes", "fixture.txt !filter\n")
        rc, _, error = self.run_cli(fix, [sys.executable, "-c", "pass"], expect_scratch=False)
        self.assertEqual(rc, 2, error)
        self.assertIn("counter", error)
        self.assertIn("fixture.txt", error)
        self.assertFalse(self.record_path.exists())

    def test_head_filter_scan_path_bound(self) -> None:
        """The HEAD filter scan refuses overflow and accepts its exact scalar path bound."""
        fix = self.make_fix()
        for limit, expected in ((1, 2), (2, 0)):
            with (
                self.subTest(limit=limit),
                unittest.mock.patch.object(revert_proof, "MAX_HEAD_PATHS", limit, create=True),
            ):
                rc, _, error = self.run_cli(
                    fix, [sys.executable, "-c", "pass"], expect_scratch=expected == 0
                )
                self.assertEqual(rc, expected, error)
                if expected == 2:
                    self.assertIn("HEAD paths exceed MAX_HEAD_PATHS=1: 2", error)
                    self.assertFalse(self.record_path.exists())
                else:
                    self.assertTrue(self.record_path.is_file())

    def test_filter_without_configured_driver_publishes_record(self) -> None:
        """An attributed HEAD path without a configured checkout command remains measurable."""
        self.write(".gitattributes", "fixture.txt filter=counter\n")
        self.write("fixture.txt", "fixture bytes\n")
        fix = self.make_fix()
        rc, _, error = self.run_cli(fix, [sys.executable, "-c", "pass"])
        self.assertEqual(rc, 0, error)
        self.assertTrue(self.record_path.is_file())

    def test_unused_configured_filter_publishes_record(self) -> None:
        """A configured filter driver unused by every HEAD path permits publication."""
        fix = self.make_fix()
        run_git(self.tree, "config", "filter.counter.smudge", "exit 99", preserve_config=True)
        rc, _, error = self.run_cli(fix, [sys.executable, "-c", "pass"])
        self.assertEqual(rc, 0, error)
        self.assertTrue(self.record_path.is_file())

    def test_worktree_count_uses_nul_field_boundaries(self) -> None:
        """A worktree path containing the field marker and a newline counts once."""
        listing = "worktree /fixture/worktree name\nwith newline\0HEAD abc\0detached\0\0"
        self.assertEqual(revert_proof_helpers.count_worktrees(listing), 1)

    def test_environment_refusal_is_no_verdict(self) -> None:
        """An environment refusal in either phase cannot publish a test verdict."""
        fix = self.make_fix()
        real_command = revert_proof.run_test_command
        for refused_phase in (1, 2):
            calls = 0

            def refuse_environment(command, tree, timeout, phase=refused_phase):
                """Refuse only the selected test launch without affecting Git commands."""
                nonlocal calls
                calls += 1
                if calls == phase:
                    with unittest.mock.patch.object(
                        upstream_lint, "_command_environment", side_effect=ValueError("refused")
                    ):
                        result = real_command(command, tree, timeout)
                    self.assertEqual(result, "launch-error")
                    return result
                return real_command(command, tree, timeout)

            with unittest.mock.patch.object(
                revert_proof, "run_test_command", side_effect=refuse_environment
            ):
                rc, _, error = self.run_cli(fix, [sys.executable, "-c", "pass"])
            self.assertEqual(rc, 3, error)
            self.assertIn("launch-error", error)
            if refused_phase == 2:
                self.assertIn("no verdict", error)
            self.assertFalse(self.record_path.exists())

    def test_started_exit_two_is_a_verdict(self) -> None:
        """A real reverted child exiting two keeps its measured status in the record."""
        fix = self.make_fix()
        program = (
            "from pathlib import Path; "
            "raise SystemExit(0 if 'return 2;' in Path('libavfilter/vf_x.c').read_text() else 2)"
        )
        command = [
            sys.executable,
            "-c",
            program,
        ]
        rc, _, error = self.run_cli(fix, command)
        self.assertEqual(rc, 0, error)
        record = json.loads(self.record_path.read_text(encoding="utf-8"))
        self.assertEqual(record["rc_reverted"], 2)

    def test_record_write_is_owner_only_with_zero_umask(self) -> None:
        """Record publication stays owner-readable and owner-writable with umask zero."""
        fix = self.make_fix()
        previous_umask = os.umask(0)
        try:
            rc, _, error = self.run_cli(fix, [sys.executable, "-c", "pass"])
        finally:
            os.umask(previous_umask)
        self.assertEqual(rc, 0, error)
        self.assertEqual(stat.S_IMODE(os.stat(self.record_path).st_mode), 0o600)

    def test_record_parent_swap_before_removal_preserves_sentinel(self) -> None:
        """A parent swapped after the path check cannot redirect old-record removal."""
        fix = self.make_fix()
        parent, second = self.root / "records", self.root / "second"
        parent.mkdir()
        second.mkdir()
        sentinel = second / "record.json"
        sentinel.write_bytes(b"sentinel")
        self.record_path = parent / "record.json"
        real_check = revert_proof._check_record_location

        def swap_after_check(config, path):
            """Replace the requested parent immediately after its path location check."""
            real_check(config, path)
            if path == self.record_path and not parent.is_symlink():
                parent.rename(self.root / "held")
                parent.symlink_to(second, target_is_directory=True)

        with unittest.mock.patch.object(
            revert_proof, "_check_record_location", side_effect=swap_after_check
        ):
            rc, _, _ = self.run_cli(fix, [sys.executable, "-c", "pass"], expect_scratch=False)
        self.assertEqual(rc, 2)
        self.assertEqual(sentinel.read_bytes(), b"sentinel")

    def test_record_parent_swap_cannot_write_inside_caller(self) -> None:
        """Swapping the pinned parent to a caller symlink refuses publication with exit two."""
        fix = self.make_fix()
        parent, held = self.root / "records", self.root / "held-records"
        self.record_path = parent / "record.json"
        parent.mkdir()
        real_measure = revert_proof.measure_scratch

        def swap_after_head(config, head, commit=None, source_paths=None):
            """Move the checked directory after the first measurement and plant a symlink."""
            result = real_measure(config, head, commit, source_paths)
            if commit is None:
                parent.rename(held)
                parent.symlink_to(self.tree, target_is_directory=True)
            return result

        with unittest.mock.patch.object(
            revert_proof, "measure_scratch", side_effect=swap_after_head
        ):
            rc, _, error = self.run_cli(fix, [sys.executable, "-c", "pass"])
        self.assertEqual(rc, 2, error)
        self.assertIn(f"record directory changed: {parent}", error)
        self.assertFalse((self.tree / "record.json").exists())
        self.assertEqual(list(held.iterdir()), [])

    def test_record_parent_renamed_away_refuses_publication(self) -> None:
        """Renaming the pinned parent without replacement refuses publication everywhere."""
        fix = self.make_fix()
        parent, held = self.root / "records", self.root / "held-records"
        parent.mkdir()
        self.record_path = parent / "record.json"
        real_measure = revert_proof.measure_scratch

        def rename_after_head(config, head, commit=None, source_paths=None):
            """Rename the record parent after measuring HEAD without replacing its name."""
            result = real_measure(config, head, commit, source_paths)
            if commit is None:
                parent.rename(held)
            return result

        with unittest.mock.patch.object(
            revert_proof, "measure_scratch", side_effect=rename_after_head
        ):
            rc, _, error = self.run_cli(fix, [sys.executable, "-c", "pass"])
        self.assertEqual(rc, 2, error)
        self.assertIn(f"record directory changed: {parent}", error)
        self.assertFalse(parent.exists())
        self.assertEqual(list(held.iterdir()), [])
        self.assertEqual(list(self.root.rglob("record.json")), [])

    def test_reverse_patch_refuses_source_replaced_by_directory(self) -> None:
        """A literal source pathspec cannot reverse a non-source descendant file."""
        self.write("libavfilter/old.c", BASE_SOURCE)
        self.commit("add source fixture")
        (self.tree / "libavfilter/old.c").unlink()
        self.write("libavfilter/old.c/fixture.txt", "non-source sentinel\n")
        fix = self.commit("fix old source layout")
        rc, _, error = self.run_cli(fix, [sys.executable, "-c", "pass"])
        self.assertEqual(rc, 4, error)
        self.assertIn("old.c/fixture.txt", error)
        self.assertFalse(self.record_path.exists())
        self.assertEqual(
            (self.tree / "libavfilter/old.c/fixture.txt").read_text(encoding="utf-8"),
            "non-source sentinel\n",
        )

    def test_sha256_producer_record_passes_consumer(self) -> None:
        """SHA-256 Git object IDs survive real producer and consumer measurements."""
        self.tree = self.root / "sha256"
        self.tree.mkdir()
        try:
            run_git(self.tree, "init", "-q", "--object-format=sha256", preserve_config=True)
        except subprocess.CalledProcessError as error:
            refusal = ("unknown option", "unknown hash algorithm", "unsupported object format")
            if not any(marker in error.stderr.lower() for marker in refusal):
                raise
            self.skipTest(f"installed Git refuses git init --object-format=sha256: {error.stderr}")
        self.write("libavfilter/vf_x.c", BASE_SOURCE)
        self.base = self.commit("avfilter: base")
        fix = self.make_fix()
        program = (
            "from pathlib import Path; "
            "raise SystemExit(0 if 'return 2;' in Path('libavfilter/vf_x.c').read_text() else 17)"
        )
        rc, _, error = self.run_cli(fix, [sys.executable, "-c", program])
        self.assertEqual(rc, 0, error)
        payload = json.loads(self.record_path.read_text(encoding="utf-8"))
        self.assertEqual(len(payload["commit"]), 64)
        step = upstream_lint._revert_proof(self.tree, self.base, fix, (self.record_path,))
        self.assertEqual(step.rc, 0, step.output)
        self.assertEqual(step.output, "1 fix commit(s), 1 revert-proof record(s)")

    def test_unknown_object_format_preserves_old_record(self) -> None:
        """Unknown Git object formats fail preflight before removing an old record."""
        fix = self.make_fix()
        self.record_path.write_bytes(b"old record")
        real_out = revert_proof.git_out

        def unknown_format(tree, *args):
            """Plant an unsupported format while retaining real Git for other queries."""
            if args == ("rev-parse", "--show-object-format"):
                return "future-format\n"
            return real_out(tree, *args)

        with unittest.mock.patch.object(revert_proof, "git_out", side_effect=unknown_format):
            rc, _, error = self.run_cli(fix, [sys.executable, "-c", "pass"], expect_scratch=False)
        self.assertEqual(rc, 2, error)
        self.assertIn("unsupported Git object format: future-format", error)
        self.assertEqual(self.record_path.read_bytes(), b"old record")

    def test_oversized_payload_refuses_before_temporary_write(self) -> None:
        """An oversized encoded payload leaves neither a record nor a temporary file."""
        fix = self.make_fix()
        self.record_path = self.root / "records" / "record.json"
        with unittest.mock.patch.object(upstream_lint, "MAX_RECORD_BYTES", 1, create=True):
            rc, _, error = self.run_cli(fix, [sys.executable, "-c", "pass"])
        self.assertEqual(rc, 2, error)
        self.assertIn("record exceeds MAX_RECORD_BYTES=1", error)
        self.assertEqual(list(self.record_path.parent.iterdir()), [])

    def test_failed_record_replace_removes_temporary_file(self) -> None:
        """A failed atomic publication leaves no record or temporary file behind."""
        fix = self.make_fix()
        self.record_path = self.root / "records" / "record.json"
        with unittest.mock.patch.object(os, "replace", side_effect=OSError("publish failed")):
            rc, _, error = self.run_cli(fix, [sys.executable, "-c", "pass"])
        self.assertEqual(rc, 2, error)
        self.assertIn("publish failed", error)
        self.assertEqual(list(self.record_path.parent.iterdir()), [])

    def test_record_unlink_requires_directory_fd_support(self) -> None:
        """Missing unlink dir_fd support refuses removal before any measurements."""
        fix = self.make_fix()
        supported = os.supports_dir_fd - {os.unlink}
        with unittest.mock.patch.object(os, "supports_dir_fd", supported):
            rc, _, error = self.run_cli(fix, [sys.executable, "-c", "pass"], expect_scratch=False)
        self.assertEqual(rc, 2, error)
        self.assertIn("dir_fd support for os.open and os.replace and os.unlink", error)
        self.assertFalse(self.record_path.exists())

    def test_record_write_requires_directory_fd_support(self) -> None:
        """Unsupported directory-relative writes fail explicitly before measurements."""
        fix = self.make_fix()
        with unittest.mock.patch.object(os, "supports_dir_fd", set()):
            rc, _, error = self.run_cli(fix, [sys.executable, "-c", "pass"], expect_scratch=False)
        self.assertEqual(rc, 2, error)
        self.assertIn("dir_fd support for os.open and os.replace and os.unlink", error)
        self.assertFalse(self.record_path.exists())

    def test_reverse_apply_preserves_parent_whitespace_despite_git_config(self) -> None:
        """Reverse apply restores exact parent bytes despite apply.whitespace=fix."""
        parent_source = BASE_SOURCE.replace("return 1;", "return 1; ")
        self.write("libavfilter/vf_x.c", parent_source)
        parent = self.commit("avfilter/vf_x: initial whitespace fixture")
        fix = self.make_fix()
        run_git(self.tree, "config", "apply.whitespace", "fix", preserve_config=True)
        parent_blob = revert_proof.git_result(
            self.tree, "show", f"{parent}:libavfilter/vf_x.c"
        ).stdout_bytes
        program = (
            "from pathlib import Path; "
            "data = Path('libavfilter/vf_x.c').read_bytes(); "
            "fixed = b'return 2;' in data; "
            f"assert fixed or data == {parent_blob!r}; raise SystemExit(0 if fixed else 7)"
        )
        command = [
            sys.executable,
            "-c",
            program,
        ]
        rc, _, error = self.run_cli(fix, command)
        self.assertEqual(rc, 0, error)
        record = json.loads(self.record_path.read_text(encoding="utf-8"))
        self.assertEqual(record["rc_reverted"], 7)

    def test_worktree_root_preserves_trailing_space(self) -> None:
        """Only Git's terminating newline is removed from a caller path."""
        tree = self.root / "caller "
        tree.mkdir()
        with unittest.mock.patch.object(revert_proof, "git_out", return_value=f"{tree}\n"):
            self.assertEqual(revert_proof.worktree_root(tree), tree)

    def test_record_check_preserves_git_directory_trailing_space(self) -> None:
        """A Git administration path ending in a space remains excluded from record writes."""
        administration = self.root / "git admin "
        administration.mkdir()
        record = administration / "record.json"
        record.write_text("old record", encoding="utf-8")
        config = revert_proof.ProofConfig(self.tree, self.base, record, 1, ["unused"])
        with (
            unittest.mock.patch.object(revert_proof, "git_out", return_value=f"{administration}\n"),
            self.assertRaisesRegex(ValueError, "outside the caller tree"),
        ):
            revert_proof.clear_record(config)
        self.assertEqual(record.read_text(encoding="utf-8"), "old record")
