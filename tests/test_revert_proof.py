# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Regression tests for scripts/revert_proof.py using temporary Git repositories."""

from __future__ import annotations

import json
import sys
import tempfile
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

    def run_cli(
        self, commit: str, command: list[str], *, timeout: str | None = None
    ) -> tuple[int, str, str]:
        """Invoke revert_proof.main with the fixture tree, commit, record path and command."""
        argv = [
            "--tree",
            str(self.tree),
            "--commit",
            commit,
            "--record",
            str(self.record_path),
        ]
        if timeout is not None:
            argv.extend(["--timeout", timeout])
        if command:
            argv.extend(["--", *command])
        return capture_main(revert_proof.main, *argv)

    def assert_clean_tree(self, expected_source: str = FIXED_SOURCE) -> None:
        """Assert tracked git status is empty and the source file holds expected_source."""
        status = run_git(
            self.tree, "status", "--porcelain", "--untracked-files=no", preserve_config=True
        )
        self.assertEqual(status, "")
        self.assertEqual(
            (self.tree / "libavfilter/vf_x.c").read_text(encoding="utf-8"), expected_source
        )

    def test_accept_fix_commit_fails_when_reverted_and_restores_tree(self) -> None:
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

        rc, _, err = self.run_cli(fix_sha, [sys.executable, "-c", "pass"])
        self.assertEqual(rc, 2)
        self.assertIn("uncommitted changes to tracked files", err)
        self.assertFalse(self.record_path.exists())

    def test_refuse_commit_not_ancestor(self) -> None:
        """A commit that is not an ancestor of HEAD is refused with exit 2 and no record."""
        self.write("libavfilter/vf_x.c", FIXED_SOURCE)
        other_sha = self.commit("avfilter/vf_x: fix on side branch")
        run_git(self.tree, "reset", "--hard", self.base, preserve_config=True)

        rc, _, err = self.run_cli(other_sha, [sys.executable, "-c", "pass"])
        self.assertEqual(rc, 2)
        self.assertIn("not an ancestor of HEAD", err)
        self.assertFalse(self.record_path.exists())

    def test_refuse_commit_without_source_paths(self) -> None:
        """A commit changing only test or doc paths is refused with exit 2 and no record."""
        self.write("tests/ref/fate/filter-x", "0\n")
        doc_sha = self.commit("tests/fate: add filter-x ref")

        rc, _, err = self.run_cli(doc_sha, [sys.executable, "-c", "pass"])
        self.assertEqual(rc, 2)
        self.assertIn("changes no source path", err)
        self.assertFalse(self.record_path.exists())

    def test_refuse_empty_command(self) -> None:
        """An empty test command is refused with exit 2 and no record."""
        self.write("libavfilter/vf_x.c", FIXED_SOURCE)
        fix_sha = self.commit("avfilter/vf_x: fix return value")

        rc, _, err = self.run_cli(fix_sha, [])
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

    def test_refuse_reverse_apply_conflict_restores_tree(self) -> None:
        """A reverse apply that conflicts exits 4, restores the tree and writes no record."""
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

    def test_boundary_timeout_records_timeout_and_restores_tree(self) -> None:
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

        rc, _, err = self.run_cli(fix_sha, [sys.executable, "-c", "pass"], timeout="7201")
        self.assertEqual(rc, 2)
        self.assertIn("7200", err)
        self.assertFalse(self.record_path.exists())

    def test_restore_failure_exits_five_and_names_tree(self) -> None:
        """A failed restore check exits 5 with the tree path named and writes no record."""
        self.write("libavfilter/vf_x.c", FIXED_SOURCE)
        fix_sha = self.commit("avfilter/vf_x: fix return value")
        real_run = upstream_lint._run

        def dirty_on_reset(
            argv: list[str],
            cwd: Path,
            data: str | None = None,
            timeout: float = 60,
            *,
            allow_any_executable: bool = False,
        ) -> upstream_lint.RunResult:
            """Simulate a failed git reset so tracked status remains dirty."""
            if argv[:3] == ["git", "reset", "--hard"]:
                return upstream_lint.RunResult(tuple(argv), 1, "reset failed\n")
            return real_run(argv, cwd, data, timeout, allow_any_executable=allow_any_executable)

        with unittest.mock.patch.object(upstream_lint, "_run", side_effect=dirty_on_reset):
            rc, _, err = self.run_cli(fix_sha, [sys.executable, "-c", "pass"])
        run_git(self.tree, "reset", "--hard", "HEAD", preserve_config=True)
        self.assertEqual(rc, 5)
        self.assertIn(str(self.tree), err)
        self.assertFalse(self.record_path.exists())

    def test_keyboard_interrupt_during_reverted_run_restores_tree(self) -> None:
        """A KeyboardInterrupt during run 2 still restores the work tree to HEAD."""
        self.write("libavfilter/vf_x.c", FIXED_SOURCE)
        fix_sha = self.commit("avfilter/vf_x: fix return value")
        calls = 0

        def interrupt_on_second_run(command: list[str], tree: Path, timeout: float) -> int | str:
            """Pass at HEAD on call 1 and raise KeyboardInterrupt on call 2."""
            nonlocal calls
            calls += 1
            if calls == 2:
                raise KeyboardInterrupt
            return 0

        with (
            unittest.mock.patch.object(
                revert_proof, "run_test_command", side_effect=interrupt_on_second_run
            ),
            self.assertRaises(KeyboardInterrupt),
        ):
            self.run_cli(fix_sha, [sys.executable, "-c", "pass"])
        self.assert_clean_tree(FIXED_SOURCE)
        self.assertFalse(self.record_path.exists())


if __name__ == "__main__":
    unittest.main()
