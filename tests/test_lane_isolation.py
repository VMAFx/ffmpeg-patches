# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Exercise clone marking and lane worktree isolation with planted repositories."""

from __future__ import annotations

import contextlib
import io
import os
import subprocess  # nosec B404 - fixture-controlled Git results and timeout exceptions.
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import lane_isolation
from tests.helpers import run_git

ROOT = Path(__file__).resolve().parents[1]


def repo(path: Path, *refs: str) -> Path:
    """Create a committed fixture repository with optional planted references."""
    if len(refs) > lane_isolation.MAX_REFS:
        raise ValueError("fixture ref count exceeds cap")
    run_git(path.parent, "init", "-q", "-b", "main", str(path), preserve_config=True)
    run_git(path, "commit", "-q", "--allow-empty", "-m", "init", preserve_config=True)
    for ref in refs[: lane_isolation.MAX_REFS]:
        run_git(path, "update-ref", ref, "HEAD", preserve_config=True)
    return path


def linked(main: Path, path: Path) -> Path:
    """Create a detached linked worktree of a fixture repository."""
    run_git(main, "worktree", "add", "--detach", str(path), preserve_config=True)
    return path


class LaneIsolationTest(unittest.TestCase):
    """Check independent marker and private-reference barriers and their limits."""

    def setUp(self) -> None:
        """Keep every fixture inside the lane worktree and register cleanup."""
        directory = tempfile.TemporaryDirectory(dir=ROOT / ".lane")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def run_cli(self, command: str, path: Path) -> tuple[int, str, str]:
        """Capture isolation CLI status and both output streams."""
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = lane_isolation.main([command, str(path)])
        return result, stdout.getvalue(), stderr.getvalue()

    def clean_clone(self) -> Path:
        """Clone a clean local origin without contacting a network remote."""
        origin = repo(self.root / "origin")
        lane = self.root / "lane"
        run_git(self.root, "clone", "-q", str(origin), str(lane), preserve_config=True)
        return lane

    def assert_private_refusal(self, path: Path, common: Path, ref: str) -> None:
        """Require both barriers to reject a planted main checkout or worktree."""
        lines = lane_isolation.refusals(path)
        self.assertEqual(len(lines), 2)
        self.assertIn("is not a lane clone", lines[0])
        self.assertIn(str(common), lines[0])
        self.assertIn(f"{ref} in {common}", lines[1])
        rc, _, error = self.run_cli("check", path)
        self.assertEqual(rc, 1)
        self.assertEqual(error.splitlines(), lines)

    def test_planted_main_checkout(self) -> None:
        """Refuse an unmarked main checkout carrying a planted private topic."""
        ref = f"refs/heads/local/{lane_isolation.PRIVATE_WORDS[0]}-topic"
        main = repo(self.root / "main", ref)
        self.assert_private_refusal(main, main / ".git", ref)

    def test_planted_linked_worktree(self) -> None:
        """Name the main common directory when rejecting its linked worktree."""
        ref = f"refs/heads/local/{lane_isolation.PRIVATE_WORDS[0]}-topic"
        main = repo(self.root / "main", ref)
        worktree = linked(main, self.root / "demo")
        self.assert_private_refusal(worktree, main / ".git", ref)
        own = run_git(worktree, "rev-parse", "--absolute-git-dir", preserve_config=True)
        self.assertNotIn(own, "\n".join(lane_isolation.refusals(worktree)))

    def test_marked_clone_and_linked_worktree(self) -> None:
        """Allow a marked clean clone and a linked worktree sharing its marker."""
        lane = self.clean_clone()
        rc, output, error = self.run_cli("mark", lane)
        self.assertEqual((rc, error), (0, ""))
        self.assertEqual(output, f"lane_isolation: marked {lane / '.git' / 'config'}\n")
        for path in (lane, linked(lane, self.root / "demo")):
            with self.subTest(path=path):
                rc, output, error = self.run_cli("check", path)
                self.assertEqual((rc, error), (0, ""))
                self.assertEqual(
                    output, f"lane_isolation: pass {path} (common dir {lane / '.git'})\n"
                )

    def test_marked_clone_private_ref(self) -> None:
        """Reject an uppercase private remote reference even after marking."""
        lane = self.clean_clone()
        self.assertEqual(self.run_cli("mark", lane)[0], 0)
        ref = f"refs/remotes/origin/{lane_isolation.PRIVATE_WORDS[1].upper()}-sei"
        run_git(lane, "update-ref", ref, "HEAD", preserve_config=True)
        rc, _, error = self.run_cli("check", lane)
        self.assertEqual(rc, 1)
        self.assertIn(ref, error)
        self.assertNotIn("is not a lane clone", error)

    def test_marker_boolean_boundaries(self) -> None:
        """Accept Git true forms and reject unset, false, and malformed markers."""
        lane = repo(self.root / "lane")
        config = lane / ".git" / "config"
        self.assertEqual(self.run_cli("check", lane)[0], 1)
        for value, expected in (("false", 1), ("true", 0), ("yes", 0), ("1", 0), ("maybe", 2)):
            with self.subTest(value=value):
                run_git(
                    lane,
                    "config",
                    "--file",
                    str(config),
                    lane_isolation.MARKER,
                    value,
                    preserve_config=True,
                )
                rc, _, error = self.run_cli("check", lane)
                self.assertEqual(rc, expected, error)
                if expected:
                    self.assertIn(str(config), error)

    def test_marker_query_is_file_scoped(self) -> None:
        """Require explicit clone configuration rather than an ambient marker lookup."""
        lane = repo(self.root / "lane")
        common = lane / ".git"
        with patch.object(
            lane_isolation, "run_git", return_value=subprocess.CompletedProcess([], 1, "", "")
        ) as query:
            self.assertFalse(lane_isolation.is_lane_clone(common))
        query.assert_called_once_with(
            [
                "config",
                "--file",
                str(common / "config"),
                "--type=bool",
                "--get",
                lane_isolation.MARKER,
            ]
        )

    def test_repository_location_overrides(self) -> None:
        """Ignore caller repository paths when checking the planted main checkout."""
        lane = self.clean_clone()
        self.assertEqual(self.run_cli("mark", lane)[0], 0)
        ref = f"refs/heads/local/{lane_isolation.PRIVATE_WORDS[0]}-topic"
        main = repo(self.root / "main", ref)
        with patch.dict(
            os.environ, {"GIT_DIR": str(lane / ".git"), "GIT_COMMON_DIR": str(lane / ".git")}
        ):
            self.assert_private_refusal(main, main / ".git", ref)

    def test_not_git_directory(self) -> None:
        """Report a missing directory as a Git input error."""
        rc, _, error = self.run_cli("check", self.root / "missing")
        self.assertEqual(rc, 2)
        self.assertIn("not a git worktree", error)

    def test_private_listing_boundary(self) -> None:
        """List exactly the allowed references and summarize overflow separately."""
        lane = repo(self.root / "lane")
        self.assertEqual(self.run_cli("mark", lane)[0], 0)
        for count in (1, 2, 3):
            ref = f"refs/heads/{lane_isolation.PRIVATE_WORDS[2]}-demo-{count}"
            run_git(lane, "update-ref", ref, "HEAD", preserve_config=True)
            with patch.object(lane_isolation, "MAX_LISTED", 2):
                lines = lane_isolation.refusals(lane)
            self.assertEqual(
                sum("names a private topic word" in line for line in lines), min(count, 2)
            )
            if count <= 2:
                self.assertFalse(any("more private refs" in line for line in lines))
            else:
                self.assertEqual(lines[-1], f"{lane}: 1 more private refs in {lane / '.git'}")

    def test_ref_cap(self) -> None:
        """Reject reference output exceeding the configured count cap."""
        lane = repo(self.root / "lane", "refs/heads/demo")
        with patch.object(lane_isolation, "MAX_REFS", 2):
            self.assertEqual(lane_isolation.private_refs(lane), [])
        with patch.object(lane_isolation, "MAX_REFS", 1):
            with self.assertRaisesRegex(lane_isolation.IsolationError, "1"):
                lane_isolation.private_refs(lane)
            rc, _, error = self.run_cli("check", lane)
        self.assertEqual(rc, 2)
        self.assertIn("1", error)

    def test_timeout(self) -> None:
        """Convert a Git timeout into a diagnostic and input error status."""
        with patch.object(
            lane_isolation.subprocess, "run", side_effect=subprocess.TimeoutExpired("git", 30)
        ):
            rc, _, error = self.run_cli("check", self.root)
        self.assertEqual(rc, 2)
        self.assertIn("timed out after 30 seconds", error)

    def test_failed_or_empty_git_queries(self) -> None:
        """Reject failed Git queries and successful directory queries with no answer."""
        common = self.root / ".git"
        for status in (0, 128):
            with (
                self.subTest(status=status),
                patch.object(
                    lane_isolation,
                    "run_git",
                    return_value=subprocess.CompletedProcess([], status, "", "demo error"),
                ),
            ):
                with self.assertRaisesRegex(lane_isolation.IsolationError, "not a git worktree"):
                    lane_isolation.common_dir(self.root)
                with patch.object(lane_isolation, "common_dir", return_value=common):
                    rc, _, error = self.run_cli("mark", self.root)
                self.assertEqual(rc, 2)
                self.assertIn("cannot read git directory", error)
        with (
            patch.object(
                lane_isolation,
                "run_git",
                return_value=subprocess.CompletedProcess([], 128, "", "demo error"),
            ),
            self.assertRaisesRegex(lane_isolation.IsolationError, "cannot list refs"),
        ):
            lane_isolation.private_refs(self.root)

    def test_mark_write_failure(self) -> None:
        """Report a failed marker write with its configuration path and Git error."""
        common = self.root / ".git"
        replies = [
            subprocess.CompletedProcess([], 0, str(common), ""),
            subprocess.CompletedProcess([], 0, str(common), ""),
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 128, "", "demo error"),
        ]
        with patch.object(lane_isolation, "run_git", side_effect=replies):
            rc, _, error = self.run_cli("mark", self.root)
        self.assertEqual(rc, 2)
        self.assertIn(str(common / "config"), error)
        self.assertIn("demo error", error)

    def test_git_unavailable(self) -> None:
        """Report an unavailable Git executable as a CLI input error."""
        with patch.object(lane_isolation.subprocess, "run", side_effect=OSError("demo error")):
            rc, _, error = self.run_cli("check", self.root)
        self.assertEqual(rc, 2)
        self.assertIn("lane_isolation: demo error", error)

    def test_mark_linked_refuses_without_write(self) -> None:
        """Refuse marking a linked worktree without changing the clone config."""
        lane = repo(self.root / "lane")
        worktree = linked(lane, self.root / "demo")
        config = lane / ".git" / "config"
        before = config.read_bytes()
        rc, _, error = self.run_cli("mark", worktree)
        self.assertEqual(rc, 1)
        self.assertIn("is a linked worktree; mark the clone itself", error)
        self.assertEqual(config.read_bytes(), before)
        self.assertFalse(lane_isolation.is_lane_clone(lane / ".git"))

    def test_mark_private_refuses_without_write(self) -> None:
        """Refuse marking private references with the same bounded listing policy."""
        refs = tuple(f"refs/heads/{lane_isolation.PRIVATE_WORDS[0]}-topic-{i}" for i in range(3))
        lane = repo(self.root / "lane", *refs)
        config = lane / ".git" / "config"
        before = config.read_bytes()
        with patch.object(lane_isolation, "MAX_LISTED", 2):
            rc, _, error = self.run_cli("mark", lane)
        self.assertEqual(rc, 1)
        self.assertIn(refs[0], error)
        self.assertIn(refs[1], error)
        self.assertIn("1 more private refs", error)
        self.assertEqual(config.read_bytes(), before)
        self.assertFalse(lane_isolation.is_lane_clone(lane / ".git"))


if __name__ == "__main__":
    unittest.main()
