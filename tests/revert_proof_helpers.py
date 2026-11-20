# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Shared fixtures for revert-proof command, history and scratch tests."""

from __future__ import annotations

import argparse
import contextlib
import io
import os
import select
import shutil
import subprocess  # nosec B404 - fixture-controlled Python argv, no shell.
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


def clone_shallow_side_fix(
    tree: Path, clone: Path, path: str, subject: str = "side: fix value"
) -> tuple[str, str]:
    """Clone a merged side fix at depth two with a visible base and hidden side parent."""
    branch = run_git(tree, "symbolic-ref", "--short", "HEAD", preserve_config=True)
    parent = run_git(tree, "rev-parse", "HEAD", preserve_config=True)
    run_git(tree, "commit", "--allow-empty", "-qm", "main: advance base", preserve_config=True)
    base = run_git(tree, "rev-parse", "HEAD", preserve_config=True)
    run_git(tree, "checkout", "-qb", "side", parent, preserve_config=True)
    target = tree / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(FIXED_SOURCE, encoding="utf-8")
    run_git(tree, "add", ".", preserve_config=True)
    run_git(tree, "commit", "-qm", subject, preserve_config=True)
    commit = run_git(tree, "rev-parse", "HEAD", preserve_config=True)
    run_git(tree, "checkout", "-q", branch, preserve_config=True)
    run_git(tree, "merge", "--no-ff", "-qm", "merge side history", "side", preserve_config=True)
    run_git(
        tree.parent, "clone", "-q", "--depth=2", tree.as_uri(), str(clone), preserve_config=True
    )
    run_git(clone, "merge-base", "--is-ancestor", base, "HEAD", preserve_config=True)
    return base, commit


def configure_ssh_signing(tree: Path, directory: Path) -> None:
    """Configure a disposable SSH signing key or skip when ssh-keygen is unavailable."""
    keygen = shutil.which("ssh-keygen")
    if keygen is None:
        raise unittest.SkipTest(
            "ssh-keygen is not installed; SSH signed-commit fixture unavailable"
        )
    key = directory / "signing-key"
    subprocess.run(  # nosec B603 - discovered keygen and disposable key path, no shell.
        [keygen, "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
        check=True,
        capture_output=True,
        timeout=30,
    )
    run_git(tree, "config", "gpg.format", "ssh", preserve_config=True)
    run_git(tree, "config", "user.signingkey", str(key), preserve_config=True)


def process_exited(pid: int) -> bool:
    """Return whether a process has terminated, including an unreaped child."""
    try:
        descriptor = os.pidfd_open(pid)
        try:
            return bool(select.select([descriptor], [], [], 0)[0])
        finally:
            os.close(descriptor)
    except ProcessLookupError:
        return True


def count_worktrees(listing: str) -> int:
    """Count only worktree fields in NUL-delimited Git porcelain output."""
    return sum(field.startswith("worktree ") for field in listing.split("\0"))


class RevertProofFixture(unittest.TestCase):
    """Shared isolated repositories and assertions for the revert-proof test modules."""

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
        """Require removed scratch files and count exact NUL-delimited worktree fields."""
        for path in self.scratch_paths[:2]:
            self.assertFalse(path.exists(), str(path))
        entries = run_git(self.tree, "worktree", "list", "--porcelain", "-z", preserve_config=True)
        self.assertEqual(count_worktrees(entries), getattr(self, "expected_entries", 1), entries)

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
                result = capture_main(revert_proof.main, *argv)
                if result[0] == 0:
                    self.assertEqual(len(self.scratch_paths), 2)
                return result
        finally:
            self.assert_caller_unchanged(before, head_may_move)
            self.assert_no_scratch()
            self.assertIn(len(self.scratch_paths), (1, 2) if expect_scratch else (0,))

    def assert_clean_tree(self, expected_source: str = FIXED_SOURCE) -> None:
        """Assert tracked git status is empty and the source file holds expected_source."""
        status = run_git(
            self.tree, "status", "--porcelain", "--untracked-files=no", preserve_config=True
        )
        self.assertEqual(status, "")
        self.assertEqual(
            (self.tree / "libavfilter/vf_x.c").read_text(encoding="utf-8"), expected_source
        )

    def valid_record(self, fix: str, rc_reverted: int | str = 17) -> dict[str, object]:
        """Return a record bound to this fixture's current commit and source fix."""
        return {
            "commit": fix,
            "head": run_git(self.tree, "rev-parse", "HEAD", preserve_config=True),
            "head_tree": run_git(self.tree, "rev-parse", "HEAD^{tree}", preserve_config=True),
            "command": [sys.executable, "-c", "pass"],
            "rc_head": 0,
            "rc_reverted": rc_reverted,
            "reverted_paths": ["libavfilter/vf_x.c"],
        }

    def lint_cli(self, tree: Path, base: str) -> tuple[int, str, str]:
        """Run real lint history steps while stubbing unrelated external tool steps."""
        with contextlib.ExitStack() as stubs:
            for name in ("_precommit", "_commit_messages", "_patcheck", "_c_rules", "_docstrings"):
                stubs.enter_context(
                    unittest.mock.patch.object(
                        upstream_lint, name, return_value=upstream_lint.Step(name, 0, "fixture")
                    )
                )
            return capture_main(
                upstream_lint.main, "--tree", str(tree), "--base", base, "--head", "HEAD"
            )

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

    def assert_parent_override_refused(self, commit: str) -> None:
        """Require a hidden-parent finding and preflight refusal with old record retention."""
        step = upstream_lint._revert_proof(self.tree, self.base, commit)
        self.assertEqual(step.rc, 1, step.output)
        self.assertIn(commit, step.output)
        self.assertIn("history is incomplete", step.output)
        self.assertNotIn("no revert-proof record", step.output)
        self.record_path.write_bytes(b"old override proof\n")
        rc, _, error = self.run_cli(commit, [sys.executable, "-c", "pass"], expect_scratch=False)
        self.assertEqual(rc, 2, error)
        self.assertIn(commit, error)
        self.assertIn("history is incomplete", error)
        self.assertEqual(self.record_path.read_bytes(), b"old override proof\n")

    def make_fix(self) -> str:
        """Commit a source fix without adding caller-only fixture files."""
        self.write("libavfilter/vf_x.c", FIXED_SOURCE)
        return self.commit("avfilter/vf_x: fix return value")

    def named_source_fix(self, raw_name: bytes) -> tuple[str, str, str]:
        """Create distinct byte-name and replacement-name fixes, skipping refused names."""
        try:
            name = os.fsdecode(raw_name)
            self.write(name, BASE_SOURCE)
        except (OSError, UnicodeError) as error:
            self.skipTest(f"temporary filesystem refuses filename {raw_name!r}: {error}")
        self.assertEqual(os.fsencode(name), raw_name)
        self.write("libavfilter/vf_\ufffd.c", BASE_SOURCE)
        self.commit("avfilter: add filename fixtures")
        self.write("libavfilter/vf_\ufffd.c", FIXED_SOURCE)
        sibling_fix = self.commit("avfilter: fix replacement-name sibling")
        self.write(name, FIXED_SOURCE)
        fix = self.commit("avfilter: fix byte-name source")
        return name, sibling_fix, fix


class ProcessOutputFixture(unittest.TestCase):
    """Run executable output boundaries with deterministic text and real OS pipes."""

    def pipe_process(self, arguments: list[str], closed: str | None = None):
        """Close a pipe reader before spawn when requested and always reap the child."""
        inherited = os.environ.copy()
        self.assertLessEqual(len(inherited), upstream_lint.MAX_ENV_ENTRIES)
        environment = {
            name: value
            for name, value in list(inherited.items())[: upstream_lint.MAX_ENV_ENTRIES]
            if not name.startswith("GIT_")
        }
        environment.update(
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_CONFIG_NOSYSTEM="1",
            COLUMNS="80",
            LINES="24",
            LC_ALL="C",
            LANG="C",
            LANGUAGE="C",
            PYTHONUTF8="1",
            PYTHONIOENCODING="utf-8",
            TERM="xterm-256color",
            FORCE_COLOR="1",
            NO_COLOR="",
            PYTHON_COLORS="0",
        )
        destinations = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE}
        with contextlib.ExitStack() as resources:
            if closed is not None:
                reader, writer = os.pipe()
                os.close(reader)
                destinations[closed] = resources.enter_context(os.fdopen(writer, "wb", buffering=0))
            process = subprocess.Popen(  # nosec B603 - fixture-controlled Python argv, no shell.
                [sys.executable, "-B", *arguments],
                cwd=upstream_lint.REPO_ROOT,
                env=environment,
                stdin=subprocess.PIPE,
                **destinations,
            )
            try:
                output, errors = process.communicate(timeout=15)
                return process.returncode, output or b"", errors or b""
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=15)
                for stream in (process.stdin, process.stdout, process.stderr)[:3]:
                    if stream is not None:
                        stream.close()

    def parser_result(self, name: str, arguments: list[str]):
        """Capture complete plain parser text with the same fixed child formatting."""
        output, errors = io.StringIO(), io.StringIO()
        with (
            unittest.mock.patch.dict(os.environ, {"COLUMNS": "80", "LINES": "24"}),
            unittest.mock.patch.object(argparse, "_", lambda message: message),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(errors),
        ):
            parser = (
                upstream_lint._parser() if name == "upstream_lint" else revert_proof.build_parser()
            )
            parser.prog = name + ".py"
            with self.assertRaises(SystemExit) as caught:
                parser.parse_args(arguments)
        return caught.exception.code, output.getvalue().encode(), errors.getvalue().encode()

    def assert_pipe_result(self, arguments: list[str], expected, closed: str) -> None:
        """Require full open-pipe output and the same decided status after reader closure."""
        with self.subTest(reader="open", arguments=arguments):
            self.assertEqual(self.pipe_process(arguments), expected)
        status, output, errors = expected
        with self.subTest(reader="closed", arguments=arguments):
            self.assertEqual(
                self.pipe_process(arguments, closed),
                (
                    status,
                    b"" if closed == "stdout" else output,
                    b"" if closed == "stderr" else errors,
                ),
            )
