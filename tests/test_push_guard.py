# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Offline pre-push checks, including closed PRs and unavailable API responses."""

from __future__ import annotations

import io
import json
import subprocess
import unittest
import unittest.mock

from scripts import push_guard
from tests.helpers import capture_main

REPOSITORY = "VMAFx/ffmpeg-patches"
REMOTE = f"https://github.com/{REPOSITORY}.git"
BRANCH = "fix/example"
ZERO = "0" * 40
HEAD = "a" * 40
LOCAL = "b" * 40
OTHER = "c" * 40


def pull_request(state: str = "closed", merged: bool = True) -> str:
    return json.dumps(
        [
            {
                "number": 42,
                "state": state,
                "head": {"sha": HEAD},
                "merged_at": "2026-11-01T12:00:00Z" if merged else None,
            }
        ]
    )


def update(remote_sha: str = ZERO, local_sha: str = LOCAL, ref: str = BRANCH) -> str:
    return f"refs/heads/local {local_sha} refs/heads/{ref} {remote_sha}\n"


def guard(reader: push_guard.APIReader, lines: str, remote: str = REMOTE) -> tuple[int, str, str]:
    with unittest.mock.patch("sys.stdin", io.StringIO(lines)):
        return capture_main(lambda argv: push_guard.main(argv, reader), "origin", remote)


class PushGuardTest(unittest.TestCase):
    def test_merged_pr_absent_or_unchanged_remote_is_refused(self) -> None:
        for remote_sha in (ZERO, HEAD):
            with self.subTest(remote_sha=remote_sha):
                reader = unittest.mock.Mock(return_value=pull_request())
                status, out, err = guard(reader, update(remote_sha))
                self.assertEqual((status, out), (1, ""))
                for detail in (BRANCH, "#42", "closed", "merged"):
                    self.assertIn(detail, err)
                reader.assert_called_once_with(REPOSITORY, BRANCH)

    def test_closed_unmerged_pr_absent_remote_is_refused(self) -> None:
        reader = unittest.mock.Mock(return_value=pull_request(merged=False))
        status, _, err = guard(reader, update())
        self.assertEqual(status, 1)
        self.assertIn("closed-unmerged", err)

    def test_fresh_branch_passes(self) -> None:
        reader = unittest.mock.Mock(return_value="[]")
        self.assertEqual(guard(reader, update()), (0, "", ""))

    def test_open_pr_passes_even_when_remote_absent_or_unchanged(self) -> None:
        for remote_sha in (ZERO, HEAD):
            with self.subTest(remote_sha=remote_sha):
                reader = unittest.mock.Mock(return_value=pull_request("open", False))
                self.assertEqual(guard(reader, update(remote_sha)), (0, "", ""))

    def test_reused_branch_with_different_remote_head_passes(self) -> None:
        reader = unittest.mock.Mock(return_value=pull_request())
        self.assertEqual(guard(reader, update(OTHER)), (0, "", ""))

    def test_deletions_and_tags_do_not_query_api(self) -> None:
        reader = unittest.mock.Mock(side_effect=AssertionError("unexpected API call"))
        lines = update(HEAD, ZERO) + f"refs/tags/v1 {LOCAL} refs/tags/v1 {ZERO}\n"
        self.assertEqual(guard(reader, lines), (0, "", ""))
        reader.assert_not_called()

    def test_empty_push_does_not_query_api(self) -> None:
        reader = unittest.mock.Mock()
        self.assertEqual(guard(reader, ""), (0, "", ""))
        reader.assert_not_called()

    def test_later_ref_is_checked(self) -> None:
        reader = unittest.mock.Mock(side_effect=["[]", pull_request()])
        status, _, err = guard(reader, update(ref="fresh") + update())
        self.assertEqual(status, 1)
        self.assertIn(BRANCH, err)
        self.assertEqual(reader.call_count, 2)

    def test_reader_errors_fail_closed(self) -> None:
        for error in (subprocess.TimeoutExpired("gh", 20), FileNotFoundError("gh missing")):
            with self.subTest(error=error):
                reader = unittest.mock.Mock(side_effect=error)
                status, _, err = guard(reader, update())
                self.assertEqual(status, 2)
                self.assertIn(f"push_guard: cannot read pull requests for {BRANCH}:", err)
                self.assertIn("; refusing (fail closed)", err)

    def test_bad_json_and_invalid_pr_data_fail_closed(self) -> None:
        for raw in ("bad JSON", "null", "{}", "[null]", '[{"state": "closed"}]'):
            with self.subTest(raw=raw):
                status, _, err = guard(unittest.mock.Mock(return_value=raw), update())
                self.assertEqual(status, 2)
                self.assertIn(f"cannot read pull requests for {BRANCH}:", err)
                self.assertIn("refusing (fail closed)", err)

    def test_malformed_updates_fail_closed(self) -> None:
        reader = unittest.mock.Mock()
        for line in ("incomplete\n", update(local_sha="invalid")):
            with self.subTest(line=line):
                self.assertEqual(guard(reader, line)[0], 2)
        reader.assert_not_called()

    def test_update_limit_boundary(self) -> None:
        reader = unittest.mock.Mock()
        deletion = update(HEAD, ZERO)
        self.assertEqual(guard(reader, deletion * push_guard.MAX_UPDATES), (0, "", ""))
        status, _, err = guard(reader, deletion * (push_guard.MAX_UPDATES + 1))
        self.assertEqual(status, 2)
        self.assertIn("more than 1000 pre-push updates", err)
        reader.assert_not_called()

    def test_sha256_zero_is_supported(self) -> None:
        reader = unittest.mock.Mock(return_value=pull_request())
        self.assertEqual(guard(reader, update("0" * 64, "b" * 64))[0], 1)


class RemoteTest(unittest.TestCase):
    def test_github_urls(self) -> None:
        for remote in (
            f"https://github.com/{REPOSITORY}",
            REMOTE,
            f"git@github.com:{REPOSITORY}.git",
            f"ssh://git@github.com/{REPOSITORY}.git",
            f"github.com:{REPOSITORY}",
            f"user@github.com:{REPOSITORY}/",
            f"git@GitHub.com:{REPOSITORY}.git/",
            f"www.github.com:{REPOSITORY}.git/",
            f"https://x-access-token:abc@github.com/{REPOSITORY}.git",
            f"ssh://git@github.com:22/{REPOSITORY}.git",
            f"ssh://github.com/{REPOSITORY}",
            f"git://github.com/{REPOSITORY}.git",
            f"http://github.com/{REPOSITORY}",
            f"git+ssh://git@github.com/{REPOSITORY}.git",
            f"ssh+git://github.com/{REPOSITORY}.git/",
            f"https://GitHub.com/{REPOSITORY}/",
            f"https://www.GitHub.com/{REPOSITORY}.git/",
            f"https://user@github.com:443/{REPOSITORY}",
            f"ssh://user:token@github.com:22/{REPOSITORY}.git/",
        ):
            with self.subTest(remote=remote):
                self.assertEqual(push_guard.github_repository(remote), REPOSITORY)
                reader = unittest.mock.Mock(return_value=pull_request())
                self.assertEqual(guard(reader, update(), remote)[0], 1)
                reader.assert_called_once_with(REPOSITORY, BRANCH)

    def test_non_github_urls_skip(self) -> None:
        for remote in (
            f"https://gitlab.com/{REPOSITORY}.git",
            f"git@example.org:{REPOSITORY}.git",
            f"https://code.ffmpeg.org/{REPOSITORY}.git",
            f"git://code.ffmpeg.org/{REPOSITORY}",
            "https://gitlab.com/github/repository.git",
            "github-user@gitlab.com:owner/repository.git",
            "/local/path",
            "/local/repository.git",
            "/local/github.com:repository.git",
        ):
            with self.subTest(remote=remote):
                self.assertIsNone(push_guard.github_repository(remote))
                reader = unittest.mock.Mock()
                status, _, err = guard(reader, update(), remote)
                self.assertEqual(status, 0)
                self.assertIn("non-GitHub remote; skipping", err)
                reader.assert_not_called()

    def test_invalid_repository_paths(self) -> None:
        for path in ("owner", "owner/", "owner/repo/extra", "owner/repo//", "owner/.git"):
            for prefix in ("https://github.com/", "git@github.com:"):
                with self.subTest(path=path, prefix=prefix):
                    self.assertIsNone(push_guard.github_repository(prefix + path))

    def test_ambiguous_github_hosts_fail_closed(self) -> None:
        for remote in (
            f"git@github.com-work:{REPOSITORY}.git",
            f"GitHub-work:{REPOSITORY}",
            f"https://github.com.example.org/{REPOSITORY}",
            f"ssh://git@GitHub.com-work:22/{REPOSITORY}.git",
            f"https://www.github.com.example.org/{REPOSITORY}",
        ):
            with self.subTest(remote=remote):
                self.assertIsNone(push_guard.github_repository(remote))
                reader = unittest.mock.Mock()
                status, out, err = guard(reader, update(), remote)
                self.assertEqual((status, out), (2, ""))
                self.assertEqual(
                    err,
                    f"push_guard: cannot tell whether {remote.replace('git@', '')} is GitHub; "
                    "refusing (fail closed)\n",
                )
                reader.assert_not_called()

    def test_ambiguous_remote_userinfo_is_redacted(self) -> None:
        for remote, redacted in (
            (
                "https://user:secret@github.example.com/o/r.git",
                "https://github.example.com/o/r.git",
            ),
            (
                "ssh://user:secret@github.example.com:22/o/r.git",
                "ssh://github.example.com:22/o/r.git",
            ),
            ("secret@github.example.com:o/r.git", "github.example.com:o/r.git"),
        ):
            with self.subTest(remote=remote):
                reader = unittest.mock.Mock()
                status, out, err = guard(reader, update(), remote)
                self.assertEqual((status, out), (2, ""))
                self.assertNotIn("secret", err)
                self.assertEqual(
                    err,
                    f"push_guard: cannot tell whether {redacted} is GitHub; "
                    "refusing (fail closed)\n",
                )
                reader.assert_not_called()


class APIReaderTest(unittest.TestCase):
    def test_newest_head_query_and_timeout(self) -> None:
        done = subprocess.CompletedProcess([], 0, stdout="[]", stderr="")
        with unittest.mock.patch("scripts.push_guard.subprocess.run", return_value=done) as run:
            self.assertEqual(push_guard.read_pull_requests(REPOSITORY, BRANCH), "[]")
        run.assert_called_once_with(
            [
                "gh",
                "api",
                f"repos/{REPOSITORY}/pulls?head=VMAFx:{BRANCH}"
                "&state=all&sort=created&direction=desc&per_page=1",
            ],
            timeout=20,
            check=False,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
        )

    def test_branch_query_characters_are_encoded(self) -> None:
        done = subprocess.CompletedProcess([], 0, stdout="[]", stderr="")
        with unittest.mock.patch("scripts.push_guard.subprocess.run", return_value=done) as run:
            push_guard.read_pull_requests(REPOSITORY, "fix/a&state=open#b+c")
        endpoint = run.call_args.args[0][2]
        self.assertIn("head=VMAFx:fix/a%26state%3Dopen%23b%2Bc&state=all", endpoint)

    def test_api_failure_fails_closed(self) -> None:
        for stderr in ("API unavailable", ""):
            with self.subTest(stderr=stderr):
                done = subprocess.CompletedProcess([], 1, stdout="[]", stderr=stderr)
                with unittest.mock.patch("scripts.push_guard.subprocess.run", return_value=done):
                    status, _, err = guard(push_guard.read_pull_requests, update())
                self.assertEqual(status, 2)
                self.assertIn(stderr or "gh api exited 1", err)
                self.assertIn("refusing (fail closed)", err)


if __name__ == "__main__":
    unittest.main()
