# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Exercise Git's trailer parser and bounded introduced-commit sign-off checks."""

from __future__ import annotations

import os
import subprocess  # nosec B404 - fixed local Git/Python argv in isolated fixture repositories.
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from scripts import signoff
from tests.helpers import capture_main

ROOT = Path(__file__).resolve().parents[1]
IDENTITY = "Signoff Fixture <signoff@example.invalid>"
TRAILER = f"Signed-off-by: {IDENTITY}"
ZERO = "0" * 40


class GitFixture(unittest.TestCase):
    """Provide disposable repositories with fixture-owned identity and isolated configuration."""

    def setUp(self) -> None:
        """Strip every inherited Git variable before configuring a temporary repository."""
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        self.env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
        isolation = unittest.mock.patch.dict(os.environ, self.env, clear=True)
        isolation.start()
        self.addCleanup(isolation.stop)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Signoff Fixture")
        self.git("config", "user.email", "signoff@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        self.git("config", "core.hooksPath", str(self.root / "empty-hooks"))

    def git(self, *args: str, text: str = "") -> str:
        """Run bounded local Git commands only against the isolated fixture."""
        return subprocess.run(  # nosec B603 B607 - local fixture argv, no shell, timeout set.
            ["git", *args],
            input=text,
            cwd=self.root,
            env=self.env,
            capture_output=True,
            encoding="utf-8",
            check=True,
            timeout=30,
        ).stdout.strip()

    def commit(self, subject: str = "fixture", signed: bool = True) -> str:
        """Create an empty commit with an optional sign-off using the fixture identity."""
        self.git("commit", "--allow-empty", "-qm", subject, *(["--signoff"] if signed else []))
        return self.git("rev-parse", "HEAD")

    def merge(self, signed: bool) -> str:
        """Plant a two-parent merge with signed parents and an optional merge sign-off."""
        self.commit("base")
        self.git("checkout", "-qb", "topic")
        self.commit("topic")
        self.git("checkout", "-q", "main")
        self.commit("main")
        self.git(
            "merge", "--no-ff", "-m", "merge fixture", "topic", *(["--signoff"] if signed else [])
        )
        return self.git("rev-parse", "HEAD")


class MessageTest(GitFixture):
    """Compare real Git trailer parsing with the configured fixture identity."""

    def test_one_correct_trailer_and_comments(self) -> None:
        """Accept an ordinary trailer with trailing commit-message comment lines."""
        for message in (f"Subject\n\n{TRAILER}\n", f"Subject\n\n{TRAILER}\n# comment\n"):
            with self.subTest(message=message):
                values = signoff.message_values(message, str(self.root))
                self.assertEqual(values, [IDENTITY])
                self.assertIsNone(signoff.judge(values, signoff.identity(str(self.root))))

    def test_invalid_message_cases(self) -> None:
        """Refuse missing, duplicate, foreign, body-only and comment-only sign-offs."""
        cases = (
            ("", "no Signed-off-by"),
            ("Subject\n", "no Signed-off-by"),
            (f"Subject\n\n{TRAILER}\n{TRAILER}\n", "more than one"),
            ("Subject\n\nSigned-off-by: Other <other@example.invalid>\n", "other identity"),
            (f"Subject\n\n{TRAILER}\n\nA non-trailer paragraph.\n", "no Signed-off-by"),
            (f"Subject\n\n# {TRAILER}\n", "no Signed-off-by"),
        )
        for message, expected in cases:
            with self.subTest(message=message):
                reason = signoff.judge(signoff.message_values(message, str(self.root)), IDENTITY)
                self.assertIsNotNone(reason)
                self.assertIn(expected, reason)

    def test_folded_value(self) -> None:
        """Unfold a continuation line before comparing the complete identity."""
        message = "Subject\n\nSigned-off-by: Signoff Fixture\n <signoff@example.invalid>\n"
        self.assertEqual(signoff.message_values(message, str(self.root)), [IDENTITY])

    def test_identity_unset_or_empty(self) -> None:
        """Refuse to decide without both configured identity fields."""
        for key in ("user.name", "user.email"):
            with self.subTest(key=key):
                self.git("config", key, "")
                with self.assertRaisesRegex(ValueError, "identity unset or empty"):
                    signoff.identity(str(self.root))
                self.git("config", "--unset", key)
                with self.assertRaisesRegex(ValueError, "identity unset or empty"):
                    signoff.identity(str(self.root))
                self.git("config", "user.name", "Signoff Fixture")
                self.git("config", "user.email", "signoff@example.invalid")

    def test_cli_pass_refusal_and_unreadable_file(self) -> None:
        """Exercise the actual script with valid, missing, empty and absent message files."""
        message = self.root / "message with spaces.txt"
        for text, expected in ((f"Subject\n\n{TRAILER}\n", 0), ("merge fixture\n", 1), ("", 1)):
            with self.subTest(expected=expected, text=text):
                message.write_text(text, encoding="utf-8")
                result = self.cli(message)
                self.assertEqual(result.returncode, expected, result.stderr)
                if expected:
                    self.assertIn("no Signed-off-by", result.stderr)
                    self.assertIn("amend with the sign-off option", result.stderr)
                    self.assertIn("merge with the sign-off option", result.stderr)
        message.unlink()
        self.assertEqual(self.cli(message).returncode, 2)

    def cli(self, message: Path) -> subprocess.CompletedProcess[str]:
        """Invoke the script from a fixture repository with its own Git configuration."""
        return subprocess.run(  # nosec B603 - fixed Python argv, fixture path, no shell.
            [
                sys.executable,
                "-B",
                str(ROOT / "scripts/signoff.py"),
                "--message-file",
                str(message),
            ],
            cwd=self.root,
            env=self.env,
            capture_output=True,
            encoding="utf-8",
            check=False,
            timeout=30,
        )

    def test_cli_identity_missing(self) -> None:
        """Return cannot-decide for a valid message when the Git identity is missing."""
        message = self.root / "message"
        message.write_text(f"Subject\n\n{TRAILER}\n", encoding="utf-8")
        self.git("config", "--unset", "user.name")
        result = self.cli(message)
        self.assertEqual(result.returncode, 2)
        self.assertIn("identity unset or empty", result.stderr)

    def test_git_missing_timeout_and_failure(self) -> None:
        """Convert missing Git, timeout and failed parser calls to cannot-decide."""
        message = self.root / "message"
        message.write_text("fixture\n", encoding="utf-8")
        for error in (FileNotFoundError(), subprocess.TimeoutExpired("git", 10), UnicodeError()):
            with (
                self.subTest(error=type(error).__name__),
                unittest.mock.patch.object(signoff.subprocess, "run", side_effect=error),
            ):
                status, _, err = capture_main(signoff.main, "--message-file", str(message))
                self.assertEqual(status, 2)
                self.assertIn("cannot decide", err)
        with unittest.mock.patch.object(signoff.subprocess, "run") as run:
            run.return_value.returncode = 128
            with self.assertRaisesRegex(ValueError, "exited 128"):
                signoff.message_values("fixture")


class CommitRangeTest(GitFixture):
    """Check ordinary and merge commits through real bounded Git history reads."""

    def test_signed_ordinary_commit(self) -> None:
        """Accept one correctly signed-off ordinary commit on a new branch."""
        sha = self.commit()
        self.assertIsNone(signoff.check_commits(sha, ZERO, str(self.root)))

    def test_merge_signoff_required(self) -> None:
        """Reject the planted unsigned-off merge even when both parents comply."""
        sha = self.merge(False)
        self.assertEqual(len(self.git("show", "-s", "--format=%P", sha).split()), 2)
        self.assertEqual(
            signoff.check_commits(sha, ZERO, str(self.root)),
            f"commit {sha}: no Signed-off-by trailer",
        )
        self.git("commit", "--amend", "--no-edit", "--signoff")
        self.assertIsNone(
            signoff.check_commits(self.git("rev-parse", "HEAD"), ZERO, str(self.root))
        )

    def test_merge_with_signoff_option(self) -> None:
        """Accept a two-parent merge created with the sign-off option."""
        self.assertIsNone(signoff.check_commits(self.merge(True), ZERO, str(self.root)))

    def test_commit_duplicate_and_foreign_trailers(self) -> None:
        """Preserve duplicate and foreign trailer values in the single Git log read."""
        for suffix, reason in (
            (f"{TRAILER}\n{TRAILER}", "more than one"),
            ("Signed-off-by: Other <other@example.invalid>", "other identity"),
        ):
            with self.subTest(reason=reason):
                sha = self.commit(f"fixture\n\n{suffix}", signed=False)
                self.assertIn(reason, signoff.check_commits(sha, ZERO, str(self.root)))

    def test_commit_folded_value(self) -> None:
        """Unfold commit trailers consistently with the message-file parser."""
        sha = self.commit(
            "Subject\n\nSigned-off-by: Signoff Fixture\n <signoff@example.invalid>", signed=False
        )
        self.assertIsNone(signoff.check_commits(sha, ZERO, str(self.root)))

    def test_remote_tracking_history_is_excluded(self) -> None:
        """Exclude already-published history even if it contains an invalid old sign-off."""
        old = self.commit("published without signoff", signed=False)
        self.git("update-ref", "refs/remotes/origin/main", old)
        self.assertEqual(signoff.introduced_commits(old, ZERO, str(self.root)), [])
        self.assertIsNone(signoff.check_commits(old, ZERO, str(self.root)))
        sha = self.commit("new signed work")
        self.assertEqual(signoff.introduced_commits(sha, ZERO, str(self.root)), [sha])
        self.assertIsNone(signoff.check_commits(sha, ZERO, str(self.root)))

    def test_remote_head_is_excluded_when_locally_known(self) -> None:
        """Exclude an advertised remote head even without any remote-tracking reference."""
        old = self.commit("published", signed=False)
        sha = self.commit("introduced")
        self.assertEqual(signoff.introduced_commits(sha, old, str(self.root)), [sha])
        self.assertIsNone(signoff.check_commits(sha, old, str(self.root)))

    def test_unknown_remote_head_does_not_fetch(self) -> None:
        """Check available local history when the remote's advertised object is absent."""
        sha = self.commit()
        self.assertEqual(signoff.introduced_commits(sha, "f" * 40, str(self.root)), [sha])

    def test_exact_cap_and_one_above(self) -> None:
        """Accept exactly MAX_COMMITS real commits and refuse one above the fixed cap."""
        tree = self.git("mktree")
        parent = []
        for index in range(signoff.MAX_COMMITS + 1):
            sha = self.git("commit-tree", tree, *parent, text=f"commit {index}\n\n{TRAILER}\n")
            parent = ["-p", sha]
            if index == signoff.MAX_COMMITS - 1:
                self.assertEqual(
                    len(signoff.introduced_commits(sha, ZERO, str(self.root))), signoff.MAX_COMMITS
                )
                self.assertIsNone(signoff.check_commits(sha, ZERO, str(self.root)))
        with self.assertRaisesRegex(ValueError, "more than 1000 introduced commits"):
            signoff.check_commits(sha, ZERO, str(self.root))

    def test_one_log_call_per_nonempty_update(self) -> None:
        """Read all selected commits in one log call and skip log reads for an empty range."""
        sha = self.commit()
        self.commit("second")
        with unittest.mock.patch.object(signoff, "git", wraps=signoff.git) as git:
            self.assertIsNone(
                signoff.check_commits(self.git("rev-parse", "HEAD"), ZERO, str(self.root))
            )
        logs = [call for call in git.call_args_list if call.args[0][0] == "log"]
        self.assertEqual(len(logs), 1)
        self.git("update-ref", "refs/remotes/origin/main", sha)
        with unittest.mock.patch.object(signoff, "git", wraps=signoff.git) as git:
            self.assertIsNone(signoff.check_commits(sha, ZERO, str(self.root)))
        self.assertFalse(any(call.args[0][0] == "log" for call in git.call_args_list))

    def test_malformed_log_fails_closed(self) -> None:
        """Refuse incomplete or unexpected commit records instead of trusting partial output."""
        sha = self.commit()
        for raw in ("", f"{sha}\0{IDENTITY}", f"{'f' * 40}\0{IDENTITY}\0"):
            with (
                self.subTest(raw=raw),
                unittest.mock.patch.object(
                    signoff,
                    "git",
                    side_effect=[f"{sha}\n", "Signoff Fixture\n", "signoff@example.invalid\n", raw],
                ),
                self.assertRaises(ValueError),
            ):
                signoff.check_commits(sha, ZERO, str(self.root))


if __name__ == "__main__":
    unittest.main()
