# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Revert-proof history regression tests using temporary Git repositories."""

from __future__ import annotations

import contextlib
import json
import sys
import unittest.mock

from scripts import revert_proof, upstream_lint
from tests.helpers import capture_main, run_git
from tests.revert_proof_helpers import (
    CONFLICT_SOURCE,
    FIXED_SOURCE,
    RevertProofFixture,
    clone_shallow_side_fix,
)


class RevertProofHistoryTest(RevertProofFixture):
    """Check revert-proof history behavior using isolated Git fixtures."""

    def test_noop_graft_demands_and_accepts_measured_source_proof(self) -> None:
        """A no-op graft keeps the fix visible and measures both runs without moving HEAD."""
        self.write("libavfilter/vf_x.c", FIXED_SOURCE)
        commit = self.commit("avfilter/vf_x: fix value")
        self.write(".git/info/grafts", f"{self.base}\n")
        run_git(self.tree, "config", "advice.graftFileDeprecated", "true", preserve_config=True)
        self.assertEqual(
            run_git(self.tree, "rev-parse", "--is-shallow-repository", preserve_config=True),
            "false",
        )
        missing = upstream_lint._revert_proof(self.tree, self.base, commit)
        self.assertEqual(missing.rc, 1, missing.output)
        self.assertIn(commit[:12], missing.output)
        self.assertIn("no revert-proof record", missing.output)
        program = (
            "from pathlib import Path; import sys; "
            "sys.exit(0 if 'return 2;' in Path('libavfilter/vf_x.c').read_text() else 17)"
        )
        command = [
            sys.executable,
            "-c",
            program,
        ]
        rc, _, error = self.run_cli(commit, command)
        self.assertEqual(rc, 0, error)
        payload = json.loads(self.record_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["head"], commit)
        self.assertEqual(payload["rc_head"], 0)
        self.assertEqual(payload["rc_reverted"], 17)
        self.assertEqual(payload["reverted_paths"], ["libavfilter/vf_x.c"])
        self.assertEqual(run_git(self.tree, "rev-parse", "HEAD", preserve_config=True), commit)
        proved = upstream_lint._revert_proof(self.tree, self.base, commit, (self.record_path,))
        self.assertEqual(proved.rc, 0, proved.output)
        self.assertEqual(proved.output, "1 fix commit(s), 1 revert-proof record(s)")

    def test_noop_graft_producer_finishes_after_good_measurements(self) -> None:
        """Real graft advice cannot turn a successful producer run into a moved-HEAD error."""
        self.write("libavfilter/vf_x.c", FIXED_SOURCE)
        commit = self.commit("avfilter/vf_x: fix value")
        self.write(".git/info/grafts", f"{self.base}\n")
        run_git(self.tree, "config", "advice.graftFileDeprecated", "true", preserve_config=True)
        program = (
            "from pathlib import Path; import sys; "
            "sys.exit(0 if 'return 2;' in Path('libavfilter/vf_x.c').read_text() else 17)"
        )
        command = [
            sys.executable,
            "-c",
            program,
        ]
        rc, _, error = self.run_cli(commit, command)
        self.assertEqual(rc, 0, error)
        payload = json.loads(self.record_path.read_text(encoding="utf-8"))
        self.assertEqual((payload["rc_head"], payload["rc_reverted"]), (0, 17))
        self.assertEqual(payload["head"], commit)
        self.assertEqual(run_git(self.tree, "rev-parse", "HEAD", preserve_config=True), commit)

    def test_shallow_nonfix_docs_cli_reports_regression_success(self) -> None:
        """A shallow docs-only non-fix passes regression-test and retains the history finding."""
        clone = self.root / "shallow"
        base, commit = clone_shallow_side_fix(
            self.tree, clone, "doc/manual.txt", "doc: update wording"
        )
        rc, output, error = self.lint_cli(clone, base)
        self.assertEqual(rc, 1, output + error)
        self.assertIn("step: regression-test rc: 0", output)
        self.assertIn("step: revert-proof rc: 1", output)
        self.assertIn(commit, output)
        self.assertIn("history is incomplete", output)
        self.assertNotIn("step: regression-test rc: 2", output)
        self.assertEqual(error, "")

    def test_shallow_source_fix_cli_reports_regression_failure(self) -> None:
        """A hidden source fix reports a regression-test finding instead of raising."""
        clone = self.root / "shallow"
        base, commit = clone_shallow_side_fix(self.tree, clone, "libavfilter/vf_x.c")
        head = run_git(clone, "rev-parse", "HEAD", preserve_config=True)
        step = upstream_lint._regression_test(clone, base, head, "")
        self.assertEqual(step.rc, 1, step.output)
        self.assertIn(commit, step.output)
        self.assertIn("history is incomplete", step.output)
        rc, output, error = self.lint_cli(clone, base)
        self.assertEqual(rc, 1, output + error)
        self.assertIn("step: regression-test rc: 1", output)
        self.assertIn("step: revert-proof rc: 1", output)
        self.assertEqual(error, "")

    def test_hidden_graft_lint_cli_reaches_history_finding(self) -> None:
        """CLI preflight reaches hidden-parent findings with unrelated external tools stubbed."""
        branch = run_git(self.tree, "symbolic-ref", "--short", "HEAD", preserve_config=True)
        run_git(self.tree, "commit", "--allow-empty", "-qm", "main: advance", preserve_config=True)
        base = run_git(self.tree, "rev-parse", "HEAD", preserve_config=True)
        run_git(self.tree, "checkout", "-qb", "side", self.base, preserve_config=True)
        self.write("libavfilter/vf_x.c", FIXED_SOURCE)
        commit = self.commit("avfilter/vf_x: fix side value")
        run_git(self.tree, "checkout", "-q", branch, preserve_config=True)
        run_git(self.tree, "merge", "--no-ff", "-qm", "merge side", "side", preserve_config=True)
        self.write(".git/info/grafts", f"{commit}\n")
        run_git(self.tree, "config", "advice.graftFileDeprecated", "true", preserve_config=True)
        with contextlib.ExitStack() as stubs:
            for name in ("_precommit", "_commit_messages", "_patcheck", "_c_rules", "_docstrings"):
                stubs.enter_context(
                    unittest.mock.patch.object(
                        upstream_lint, name, return_value=upstream_lint.Step(name, 0, "fixture")
                    )
                )
            rc, output, error = capture_main(
                upstream_lint.main, "--tree", str(self.tree), "--base", base, "--head", "HEAD"
            )
        self.assertEqual(rc, 1, output + error)
        self.assertNotIn("step: preflight", output)
        self.assertIn("step: revert-proof rc: 1", output)
        self.assertIn(f"{commit[:12]}: avfilter/vf_x: fix side value: {commit}", output)
        self.assertIn("history is incomplete", output)
        self.assertIn("git fetch --unshallow", output)
        self.assertNotIn("no revert-proof record", output)

    def test_root_fix_reversal_removes_source_in_scratch(self) -> None:
        """An imported root fix yields a proof after its added source disappears."""
        commit = self.import_root_fix()
        head = run_git(self.tree, "rev-parse", "HEAD", preserve_config=True)
        missing = upstream_lint._revert_proof(self.tree, self.base, head, ())
        self.assertEqual(missing.rc, 1, missing.output)
        self.assertIn("no revert-proof record", missing.output)
        program = (
            "from pathlib import Path; import sys; "
            "sys.exit(0 if Path('libavfilter/vf_root.c').exists() else 17)"
        )
        command = [
            sys.executable,
            "-c",
            program,
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

    def test_shallow_docs_boundary_has_own_consumer_finding(self) -> None:
        """A docs-only side boundary reports incomplete history instead of inherited sources."""
        clone = self.root / "shallow"
        base, commit = clone_shallow_side_fix(self.tree, clone, "doc/manual.txt")
        head = run_git(clone, "rev-parse", "HEAD", preserve_config=True)
        step = upstream_lint._revert_proof(clone, base, head)
        self.assertEqual(step.rc, 1, step.output)
        self.assertEqual(len(step.output.splitlines()), 1, step.output)
        self.assertIn(commit, step.output)
        self.assertIn("history is incomplete", step.output)
        self.assertIn("fetch --unshallow", step.output)
        self.assertNotIn("no revert-proof record", step.output)
        self.assertNotIn("merge commit", step.output)

    def test_shallow_docs_boundary_refuses_before_scratch(self) -> None:
        """A docs boundary refuses before commands or scratch and preserves old record bytes."""
        clone = self.root / "shallow"
        _, commit = clone_shallow_side_fix(self.tree, clone, "doc/manual.txt")
        self.tree = clone
        old = b"old record\x00\xff\n"
        self.record_path.write_bytes(old)
        with unittest.mock.patch.object(revert_proof, "run_test_command") as command:
            rc, _, error = self.run_cli(
                commit, [sys.executable, "-c", "pass"], expect_scratch=False
            )
        self.assertEqual(rc, 2, error)
        self.assertIn(commit, error)
        self.assertIn("history is incomplete", error)
        self.assertIn("fetch --unshallow", error)
        command.assert_not_called()
        self.assertEqual(self.record_path.read_bytes(), old)

    def test_shallow_source_boundary_refuses_before_scratch(self) -> None:
        """A source boundary cannot measure a snapshot reversal or overwrite an older record."""
        clone = self.root / "shallow"
        _, commit = clone_shallow_side_fix(self.tree, clone, "libavfilter/vf_x.c")
        self.tree = clone
        self.record_path.write_bytes(b"old source proof\n")
        rc, _, error = self.run_cli(commit, [sys.executable, "-c", "pass"], expect_scratch=False)
        self.assertEqual(rc, 2, error)
        self.assertIn(commit, error)
        self.assertIn("history is incomplete", error)
        self.assertEqual(self.record_path.read_bytes(), b"old source proof\n")

    def test_shallow_source_record_is_refused_until_local_history_is_complete(self) -> None:
        """A matching source record fails at the boundary and passes after local unshallowing."""
        clone = self.root / "shallow"
        base, commit = clone_shallow_side_fix(self.tree, clone, "libavfilter/vf_x.c")
        head = run_git(clone, "rev-parse", "HEAD", preserve_config=True)
        payload = {
            "commit": commit,
            "head": head,
            "head_tree": run_git(clone, "rev-parse", "HEAD^{tree}", preserve_config=True),
            "command": [sys.executable, "-c", "pass"],
            "rc_head": 0,
            "rc_reverted": 17,
            "reverted_paths": ["libavfilter/vf_x.c"],
        }
        self.record_path.write_text(json.dumps(payload), encoding="utf-8")
        step = upstream_lint._revert_proof(clone, base, head, (self.record_path,))
        self.assertEqual(step.rc, 1, step.output)
        self.assertEqual(len(step.output.splitlines()), 1, step.output)
        self.assertIn(commit, step.output)
        self.assertIn("history is incomplete", step.output)
        self.assertNotIn("no revert-proof record", step.output)
        run_git(clone, "fetch", "-q", "--unshallow", "origin", preserve_config=True)
        complete = upstream_lint._revert_proof(clone, base, head, (self.record_path,))
        self.assertEqual(complete.rc, 0, complete.output)
        self.assertIn("1 fix commit(s), 1 revert-proof record(s)", complete.output)

    def test_neutral_shallow_boundary_is_reported(self) -> None:
        """A docs boundary without fix wording cannot bypass the history check."""
        clone = self.root / "shallow"
        base, commit = clone_shallow_side_fix(
            self.tree, clone, "doc/manual.txt", "doc: update wording"
        )
        head = run_git(clone, "rev-parse", "HEAD", preserve_config=True)
        step = upstream_lint._revert_proof(clone, base, head)
        self.assertEqual(step.rc, 1, step.output)
        self.assertEqual(len(step.output.splitlines()), 1, step.output)
        self.assertIn(commit, step.output)
        self.assertIn("history is incomplete", step.output)
        self.assertNotIn("no revert-proof record", step.output)

    def test_parentless_replace_reference_is_refused(self) -> None:
        """A real replacement commit cannot disguise a source fix as an imported root."""
        self.write("libavfilter/vf_x.c", FIXED_SOURCE)
        commit = self.commit("avfilter/vf_x: fix value")
        run_git(self.tree, "replace", "--graft", commit, preserve_config=True)
        self.assert_parent_override_refused(commit)

    def test_parentless_legacy_graft_is_refused(self) -> None:
        """A legacy graft hiding a stored parent produces the same explicit history refusal."""
        self.write("libavfilter/vf_x.c", FIXED_SOURCE)
        commit = self.commit("avfilter/vf_x: fix value")
        (self.tree / ".git/info/grafts").write_text(f"{commit}\n", encoding="utf-8")
        self.assert_parent_override_refused(commit)

    def test_true_root_in_shallow_clone_keeps_root_reversal(self) -> None:
        """A genuine imported root still reverses its added source in a depth-two clone."""
        commit = self.import_root_fix()
        clone = self.root / "shallow"
        run_git(
            self.root,
            "clone",
            "-q",
            "--depth=2",
            self.tree.as_uri(),
            str(clone),
            preserve_config=True,
        )
        self.tree = clone
        program = (
            "from pathlib import Path; import sys; "
            "sys.exit(0 if Path('libavfilter/vf_root.c').exists() else 17)"
        )
        command = [
            sys.executable,
            "-c",
            program,
        ]
        rc, _, error = self.run_cli(commit, command)
        self.assertEqual(rc, 0, error)
        payload = json.loads(self.record_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["rc_reverted"], 17)
        self.assertEqual(payload["reverted_paths"], ["libavfilter/vf_root.c"])
        head = run_git(self.tree, "rev-parse", "HEAD", preserve_config=True)
        step = upstream_lint._revert_proof(self.tree, self.base, head, (self.record_path,))
        self.assertEqual(step.rc, 0, step.output)
        self.assertIn("1 fix commit(s), 1 revert-proof record(s)", step.output)
