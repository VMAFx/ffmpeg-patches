# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Exercise clone marking and lane worktree isolation with planted repositories."""

from __future__ import annotations

import contextlib
import io
import os
import selectors
import subprocess  # nosec B404 - fixture-controlled Git results and timeout exceptions.
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import lane_isolation
from scripts.process_group import kill_process_group
from tests.helpers import run_git


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
        """Create a temporary directory for fixtures and register cleanup."""
        directory = tempfile.TemporaryDirectory()
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
                "--no-includes",
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
            with self.assertRaisesRegex(lane_isolation.IsolationError, "ref count exceeds cap 1$"):
                lane_isolation.private_refs(lane)
            rc, _, error = self.run_cli("check", lane)
        self.assertEqual(rc, 2)
        self.assertIn("ref count exceeds cap 1", error)

    def test_ref_listing_count_bound(self) -> None:
        """Bound Git reference output to one entry past the configured cap."""
        with (
            patch.object(lane_isolation, "MAX_REFS", 1),
            patch.object(
                lane_isolation, "run_git", return_value=subprocess.CompletedProcess([], 0, "", "")
            ) as query,
        ):
            self.assertEqual(lane_isolation.private_refs(self.root), [])
        self.assertIn("--count=2", query.call_args.args[0])

    def test_included_marker_never_counts(self) -> None:
        """Reject an included marker despite an explicit include-enabled control."""
        lane = repo(self.root / "lane")
        included = self.root / "inc.gitconfig"
        included.write_text("[lane]\n\tclone = true\n", encoding="utf-8")
        config = lane / ".git" / "config"
        run_git(
            lane,
            "config",
            "--file",
            str(config),
            "include.path",
            str(included),
            preserve_config=True,
        )
        self.assertEqual(
            run_git(
                lane,
                "config",
                "--file",
                str(config),
                "--includes",
                "--type=bool",
                "--get",
                lane_isolation.MARKER,
                preserve_config=True,
            ).strip(),
            "true",
        )
        self.assertFalse(lane_isolation.is_lane_clone(lane_isolation.common_dir(lane)))
        rc, _, error = self.run_cli("check", lane)
        self.assertEqual(rc, 1)
        self.assertIn("is not a lane clone", error)

    def test_bare_clone_refused(self) -> None:
        """Refuse a marked bare clone because hosted runners need a worktree."""
        origin = repo(self.root / "origin")
        bare = self.root / "bare.git"
        run_git(self.root, "clone", "-q", "--bare", str(origin), str(bare), preserve_config=True)
        self.assertEqual(lane_isolation.mark(bare), [])
        rc, _, error = self.run_cli("check", bare)
        self.assertEqual(rc, 1)
        self.assertIn("not inside a work tree", error)

    def test_linked_worktree_of_bare_clone_passes(self) -> None:
        """Allow a linked worktree belonging to a marked bare clone."""
        origin = repo(self.root / "origin")
        bare = self.root / "bare.git"
        run_git(self.root, "clone", "-q", "--bare", str(origin), str(bare), preserve_config=True)
        self.assertEqual(lane_isolation.mark(bare), [])
        worktree = linked(bare, self.root / "wt")
        rc, _, error = self.run_cli("check", worktree)
        self.assertEqual((rc, error), (0, ""))

    def test_undecodable_private_ref_refused(self) -> None:
        """Refuse private reference bytes that cannot be decoded as UTF-8."""
        lane = repo(self.root / "lane")
        self.assertEqual(lane_isolation.mark(lane), [])
        sha = run_git(lane, "rev-parse", "HEAD", preserve_config=True).strip()
        (lane / ".git" / "packed-refs").write_bytes(
            sha.encode() + b" refs/heads/x\xff" + lane_isolation.PRIVATE_WORDS[-1].encode() + b"\n"
        )
        rc, _, error = self.run_cli("check", lane)
        self.assertEqual(rc, 1)
        self.assertIn(lane_isolation.PRIVATE_WORDS[-1], error)
        self.assertNotIn("Traceback", error)

    @unittest.skipUnless(os.name == "posix", "trailing-space directory names are not portable")
    def test_trailing_space_common_dir(self) -> None:
        """Keep a trailing space in the common directory when writing its marker."""
        bare = self.root / "lane "
        run_git(self.root, "init", "-q", "--bare", str(bare), preserve_config=True)
        self.assertEqual(lane_isolation.mark(bare), [])
        self.assertEqual(
            run_git(
                self.root,
                "config",
                "--file",
                str(bare / "config"),
                "--type=bool",
                "--get",
                lane_isolation.MARKER,
                preserve_config=True,
            ).strip(),
            "true",
        )
        self.assertFalse((self.root / "lane").exists())

    def test_git_queries_avoid_path_format(self) -> None:
        """Keep marking and refusal queries compatible with older Git versions."""
        lane = repo(self.root / "lane")
        real_run_git = lane_isolation.run_git
        with patch.object(lane_isolation, "run_git", side_effect=real_run_git) as query:
            self.assertEqual(lane_isolation.mark(lane), [])
            self.assertEqual(lane_isolation.refusals(lane), [])
        self.assertFalse(
            any(
                argument.startswith("--path-format")
                for call in query.call_args_list
                for argument in call.args[0]
            ),
            query.call_args_list,
        )

    def test_common_dir_relative_output(self) -> None:
        """Resolve relative Git common-directory output against the queried path."""
        with patch.object(
            lane_isolation,
            "run_git",
            return_value=subprocess.CompletedProcess([], 0, "../.git\n", ""),
        ):
            self.assertEqual(
                lane_isolation.common_dir(self.root / "a" / "b"),
                Path(os.path.realpath(self.root / "a" / ".git")),
            )

    def test_common_dir_from_subdirectory(self) -> None:
        """Resolve nested worktree paths to the same canonical common directory."""
        lane = repo(self.root / "lane")
        subdirectory = lane / "sub" / "deep"
        subdirectory.mkdir(parents=True)
        self.assertEqual(lane_isolation.common_dir(subdirectory), lane_isolation.common_dir(lane))
        self.assertEqual(lane_isolation.common_dir(lane), Path(os.path.realpath(lane / ".git")))

    @unittest.skipUnless(os.name == "posix", "directory symlinks need POSIX")
    def test_common_dir_through_symlink(self) -> None:
        """Resolve and mark a symlinked clone through its canonical common directory."""
        lane = repo(self.root / "lane")
        link = self.root / "link"
        link.symlink_to(lane, target_is_directory=True)
        self.assertEqual(lane_isolation.common_dir(link), Path(os.path.realpath(lane / ".git")))
        self.assertEqual(lane_isolation.mark(link), [])
        self.assertEqual(
            run_git(
                lane,
                "config",
                "--file",
                str(lane / ".git" / "config"),
                "--type=bool",
                "--get",
                lane_isolation.MARKER,
                preserve_config=True,
            ),
            "true",
        )

    @unittest.skipUnless(os.name == "posix", "directory symlinks need POSIX")
    def test_mark_compares_real_git_dir(self) -> None:
        """Mark a clone when its Git directory uses a symlinked spelling."""
        lane = repo(self.root / "lane")
        link = self.root / "link"
        link.symlink_to(lane, target_is_directory=True)
        real_run_git = lane_isolation.run_git

        def symlinked_git_dir(args: list[str]) -> subprocess.CompletedProcess[str]:
            """Return the symlinked Git directory while preserving other Git calls."""
            if "--absolute-git-dir" in args:
                return subprocess.CompletedProcess(args, 0, f"{link / '.git'}\n", "")
            return real_run_git(args)

        with patch.object(lane_isolation, "run_git", side_effect=symlinked_git_dir):
            self.assertEqual(lane_isolation.mark(lane), [])
        self.assertEqual(
            run_git(
                lane,
                "config",
                "--file",
                str(lane / ".git" / "config"),
                "--type=bool",
                "--get",
                lane_isolation.MARKER,
                preserve_config=True,
            ),
            "true",
        )

    def test_mark_locked_already_marked(self) -> None:
        """Accept an existing true marker while another writer holds the lock."""
        lane = repo(self.root / "lane")
        self.assertEqual(lane_isolation.mark(lane), [])
        lock = lane / ".git" / "config.lock"
        lock.write_text("", encoding="utf-8")
        self.assertEqual(lane_isolation.mark(lane), [])
        self.assertTrue(lock.exists())

    def test_mark_locked_unmarked_fails(self) -> None:
        """Refuse a locked unmarked clone without setting its marker."""
        lane = repo(self.root / "lane")
        (lane / ".git" / "config.lock").write_text("", encoding="utf-8")
        with self.assertRaisesRegex(lane_isolation.IsolationError, "cannot set"):
            lane_isolation.mark(lane)
        self.assertFalse(lane_isolation.is_lane_clone(lane / ".git"))

    def test_timeout(self) -> None:
        """Convert a Git timeout into a diagnostic and input error status."""
        with patch.object(
            lane_isolation.subprocess, "Popen", side_effect=subprocess.TimeoutExpired("git", 30)
        ):
            rc, _, error = self.run_cli("check", self.root)
        self.assertEqual(rc, 2)
        self.assertIn("timed out after 30 seconds", error)

    def test_non_posix_host_refused_before_git(self) -> None:
        """Reject check and mark on non-POSIX hosts before starting Git."""
        lane = repo(self.root / "lane")
        with (
            patch.object(lane_isolation, "POSIX_HOST", False),
            patch.object(lane_isolation.subprocess, "Popen") as popen,
        ):
            for command in ("check", "mark"):
                with self.subTest(command=command):
                    rc, output, error = self.run_cli(command, lane)
                    self.assertEqual(rc, 2)
                    self.assertIn("needs POSIX", error)
                    self.assertNotIn("lane_isolation: pass", output)
            popen.assert_not_called()

    def test_posix_host_flag_matches_os(self) -> None:
        """Pin POSIX support detection to the operating system name."""
        self.assertEqual(lane_isolation.POSIX_HOST, os.name == "posix")

    def test_git_output_limit_boundary(self) -> None:
        """Accept the exact combined byte limit and reject one byte less."""
        lane = repo(self.root / "lane")
        argv = ["-C", str(lane), "rev-parse", "--git-common-dir"]
        first = lane_isolation.run_git(argv)
        n = len(first.stdout.encode()) + len(first.stderr.encode())
        with patch.object(lane_isolation, "GIT_OUTPUT_LIMIT", n):
            self.assertEqual(lane_isolation.run_git(argv).stdout, first.stdout)
        with (
            patch.object(lane_isolation, "GIT_OUTPUT_LIMIT", n - 1),
            self.assertRaisesRegex(lane_isolation.IsolationError, "output exceeds"),
        ):
            lane_isolation.run_git(argv)

    def test_git_stderr_counts_toward_limit(self) -> None:
        """Reject Git stderr alone when it exceeds the combined byte limit."""
        (self.root / "none").mkdir()
        with (
            patch.object(lane_isolation, "GIT_OUTPUT_LIMIT", 8),
            self.assertRaisesRegex(lane_isolation.IsolationError, "output exceeds"),
        ):
            lane_isolation.run_git(["-C", str(self.root / "none"), "rev-parse", "--git-common-dir"])

    def test_long_ref_name_over_limit_refused(self) -> None:
        """Refuse a marked clone whose reference name exceeds the output byte limit."""
        lane = self.clean_clone()
        self.assertEqual(self.run_cli("mark", lane)[0], 0)
        sha = run_git(lane, "rev-parse", "HEAD", preserve_config=True)
        (lane / ".git" / "packed-refs").write_text(
            f"{sha} refs/heads/{'a' * 300}\n", encoding="utf-8"
        )
        with patch.object(lane_isolation, "GIT_OUTPUT_LIMIT", 256):
            rc, output, error = self.run_cli("check", lane)
        self.assertEqual(rc, 2)
        self.assertIn("output exceeds", error)
        self.assertNotIn("lane_isolation: pass", output)

    @unittest.skipUnless(os.name == "posix", "process groups need POSIX")
    def test_git_timeout_kills_process(self) -> None:
        """Prove the whole Git process group is gone after its deadline."""
        real_popen = subprocess.Popen
        processes: list[subprocess.Popen[bytes]] = []
        read_end, write_end = os.pipe()
        self.addCleanup(os.close, read_end)

        def kill_sleeper() -> None:
            """Kill any surviving process group after a failed assertion."""
            with contextlib.suppress(PermissionError):
                kill_process_group(processes[0])

        def start_sleeper(args: list[str], **kwargs: object) -> subprocess.Popen[bytes]:
            """Launch a sleeping grandchild holding the pipe with the Git process options."""
            process = real_popen(["sh", "-c", "sleep 30 & wait"], pass_fds=(write_end,), **kwargs)
            os.close(write_end)
            processes.append(process)
            self.addCleanup(kill_sleeper)
            return process

        started = time.monotonic()
        with (
            patch.object(lane_isolation, "GIT_TIMEOUT", 1),
            patch.object(lane_isolation.subprocess, "Popen", side_effect=start_sleeper),
            self.assertRaisesRegex(lane_isolation.IsolationError, "timed out after 1 seconds"),
        ):
            lane_isolation.run_git(["status"])
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(len(processes), 1)
        with self.assertRaises(ProcessLookupError):
            os.kill(processes[0].pid, 0)
        with selectors.DefaultSelector() as selector:
            selector.register(read_end, selectors.EVENT_READ)
            self.assertTrue(selector.select(timeout=5), "process group still holds the pipe")
            self.assertEqual(os.read(read_end, 1), b"")

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
            subprocess.CompletedProcess([], 1, "", ""),
        ]
        with patch.object(lane_isolation, "run_git", side_effect=replies):
            rc, _, error = self.run_cli("mark", self.root)
        self.assertEqual(rc, 2)
        self.assertIn(str(common / "config"), error)
        self.assertIn("demo error", error)

    def test_git_unavailable(self) -> None:
        """Report an unavailable Git executable as a CLI input error."""
        with patch.object(lane_isolation.subprocess, "Popen", side_effect=OSError("demo error")):
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


class AmbientConfigTest(unittest.TestCase):
    """Reject ambient markers while proving each configuration source is active."""

    def setUp(self) -> None:
        """Create an unmarked repository and global config before patching the environment."""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.unmarked = repo(self.root / "unmarked")
        (self.root / "global.gitconfig").write_text("[lane]\n\tclone = true\n", encoding="utf-8")

    def test_ambient_marker_never_counts(self) -> None:
        """Refuse ambient global, counted, and parameter markers despite successful controls."""
        sources = (
            ("global", {"GIT_CONFIG_GLOBAL": str(self.root / "global.gitconfig")}),
            (
                "count",
                {
                    "GIT_CONFIG_COUNT": "1",
                    "GIT_CONFIG_KEY_0": lane_isolation.MARKER,
                    "GIT_CONFIG_VALUE_0": "true",
                },
            ),
            ("parameters", {"GIT_CONFIG_PARAMETERS": f"'{lane_isolation.MARKER}'='true'"}),
        )
        for name, mapping in sources:
            with self.subTest(source=name), patch.dict(os.environ, mapping):
                self.assertEqual(
                    run_git(
                        self.unmarked,
                        "config",
                        "--type=bool",
                        "--get",
                        lane_isolation.MARKER,
                        preserve_config=True,
                    ).strip(),
                    "true",
                )
                self.assertFalse(
                    lane_isolation.is_lane_clone(lane_isolation.common_dir(self.unmarked))
                )
                stdout, stderr = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    rc = lane_isolation.main(["check", str(self.unmarked)])
                self.assertEqual(rc, 1)
                self.assertIn("is not a lane clone", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
