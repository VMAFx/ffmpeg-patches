# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Offline pre-push checks, including closed PRs and unavailable API responses."""

from __future__ import annotations

import io
import json
import subprocess  # nosec B404 - mocked queries and local temporary Git repositories.
import tempfile
import threading
import time
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
REWRITE_RULES = ("insteadOf", "pushInsteadOf")


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


def config_guard(
    reader: push_guard.APIReader, config_reader: push_guard.ConfigReader, lines: str = ""
) -> tuple[int, str, str]:
    with unittest.mock.patch("sys.stdin", io.StringIO(lines)):
        return capture_main(
            lambda argv: push_guard.main(argv, reader, config_reader), "--from-config"
        )


class ConfigModeTest(unittest.TestCase):
    def test_merged_and_fresh_heads(self) -> None:
        for response, expected in ((pull_request(), 1), ("[]", 0)):
            with self.subTest(expected=expected):
                reader = unittest.mock.Mock(return_value=response)
                config_reader = unittest.mock.Mock(return_value={"origin": [REMOTE]})
                self.assertEqual(config_guard(reader, config_reader, update())[0], expected)
                reader.assert_called_once_with(REPOSITORY, BRANCH)
                config_reader.assert_called_once_with()

    def test_second_repository_and_later_update_are_checked(self) -> None:
        config = {"origin": [REMOTE], "fork": ["https://github.com/o/r"]}
        reader = unittest.mock.Mock(side_effect=["[]", "[]", "[]", pull_request()])
        with unittest.mock.patch.object(
            push_guard, "read_stdin", return_value=io.StringIO(update(ref="fresh") + update())
        ) as stdin:
            self.assertEqual(config_guard(reader, lambda: config)[0], 1)
        stdin.assert_called_once_with()
        self.assertEqual(
            reader.call_args_list,
            [
                unittest.mock.call(REPOSITORY, "fresh"),
                unittest.mock.call("o/r", "fresh"),
                unittest.mock.call(REPOSITORY, BRANCH),
                unittest.mock.call("o/r", BRANCH),
            ],
        )

    def test_only_non_github_remotes_skip(self) -> None:
        reader = unittest.mock.Mock()
        config = {"origin": ["https://gitlab.com/o/r"], "local": ["/local/repo"]}
        status, _, err = config_guard(reader, lambda: config, "invalid updates")
        self.assertEqual(status, 0)
        self.assertIn("non-GitHub remote; skipping", err)
        reader.assert_not_called()

    def test_unparsable_github_host_fails_closed(self) -> None:
        for remote in ("https://github.com/owner", "git@github.work:o/r"):
            with self.subTest(remote=remote):
                reader = unittest.mock.Mock()
                config = {"origin": [REMOTE], "bad": [remote]}
                status, _, err = config_guard(
                    reader, unittest.mock.Mock(return_value=config), update()
                )
                self.assertEqual(status, 2)
                self.assertIn("cannot tell whether", err)
                reader.assert_not_called()

    def test_config_reader_errors_fail_closed(self) -> None:
        for error in (
            OSError("git missing"),
            ValueError("git remote exited 1"),
            subprocess.TimeoutExpired("git", 10),
        ):
            with self.subTest(error=error):
                reader = unittest.mock.Mock()
                config_reader = unittest.mock.Mock(side_effect=error)
                status, _, err = config_guard(reader, config_reader, update())
                self.assertEqual(status, 2)
                self.assertIn("cannot read remote configuration", err)
                self.assertIn("refusing (fail closed)", err)
                reader.assert_not_called()

    def test_repository_count_boundary(self) -> None:
        for count, expected in (
            (push_guard.MAX_REPOSITORIES, 0),
            (push_guard.MAX_REPOSITORIES + 1, 2),
        ):
            with self.subTest(count=count):
                config = {"origin": [f"https://github.com/o/r{index}" for index in range(count)]}
                reader = unittest.mock.Mock(return_value="[]")
                status, _, err = config_guard(
                    reader, unittest.mock.Mock(return_value=config), update()
                )
                self.assertEqual(status, expected)
                self.assertEqual(reader.call_count, 0 if expected else count)
                if expected:
                    self.assertIn("repositories", err)

    def test_duplicate_repositories_are_checked_once(self) -> None:
        config = {"origin": [REMOTE, f"git@github.com:{REPOSITORY}"], "fork": [REMOTE]}
        reader = unittest.mock.Mock(return_value="[]")
        self.assertEqual(config_guard(reader, lambda: config, update())[0], 0)
        reader.assert_called_once_with(REPOSITORY, BRANCH)

    def test_shell_metacharacters_in_remote_name_are_data(self) -> None:
        config = {"x';true;#": [REMOTE]}
        reader = unittest.mock.Mock(return_value=pull_request())
        self.assertEqual(config_guard(reader, lambda: config, update())[0], 1)
        reader.assert_called_once_with(REPOSITORY, BRANCH)

    def test_first_nonzero_status_wins(self) -> None:
        config = {"origin": [REMOTE], "fork": ["https://github.com/o/r"]}
        for response, expected in ((pull_request(), 1), ("bad JSON", 2)):
            with self.subTest(expected=expected):
                reader = unittest.mock.Mock(return_value=response)
                self.assertEqual(config_guard(reader, lambda: config, update())[0], expected)
                reader.assert_called_once_with(REPOSITORY, BRANCH)

    def test_config_mode_excludes_positionals_and_requires_a_complete_mode(self) -> None:
        for argv in (
            [],
            ["origin"],
            ["--from-config", "origin"],
            ["--from-config", "origin", REMOTE],
        ):
            with self.subTest(argv=argv), unittest.mock.patch("sys.stderr", io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    push_guard.main(argv, unittest.mock.Mock(), unittest.mock.Mock())
                self.assertEqual(raised.exception.code, 2)

    def test_config_entry_count_boundary(self) -> None:
        for count, expected in (
            (push_guard.MAX_CONFIG_ENTRIES, 0),
            (push_guard.MAX_CONFIG_ENTRIES + 1, 2),
        ):
            with self.subTest(count=count):
                config = {"origin": [REMOTE] * count}
                reader = unittest.mock.Mock(return_value="[]")
                status, _, err = config_guard(
                    reader, unittest.mock.Mock(return_value=config), update()
                )
                self.assertEqual(status, expected)
                self.assertEqual(reader.call_count, 0 if expected else 1)
                if expected:
                    self.assertIn("remote push URLs", err)

    def test_no_remotes_skip(self) -> None:
        reader = unittest.mock.Mock()
        self.assertEqual(config_guard(reader, lambda: {}, "invalid updates")[0], 0)
        reader.assert_not_called()

    def test_expanded_rewritten_url_checks_merged_head(self) -> None:
        reader = unittest.mock.Mock(return_value=pull_request())
        expanded = {"origin": [f"https://github.com/{REPOSITORY}"]}
        self.assertEqual(config_guard(reader, lambda: expanded, update())[0], 1)
        reader.assert_called_once_with(REPOSITORY, BRANCH)

    def test_remote_count_boundary(self) -> None:
        for count, expected in (
            (push_guard.MAX_REMOTES, 0),
            (push_guard.MAX_REMOTES + 1, 2),
        ):
            with self.subTest(count=count):
                remotes = {f"r{index}": [REMOTE] for index in range(count)}
                reader = unittest.mock.Mock(return_value="[]")
                status, _, err = config_guard(
                    reader, unittest.mock.Mock(return_value=remotes), update()
                )
                self.assertEqual(status, expected)
                self.assertEqual(reader.call_count, 0 if expected else 1)
                if expected:
                    self.assertIn("remotes", err)


class ConfigReaderTest(unittest.TestCase):
    def test_push_url_command_has_argv_data_and_deadline(self) -> None:
        name = "x';true;#"
        outputs = [
            subprocess.CompletedProcess([], 0, stdout=f"{name}\n", stderr=""),
            subprocess.CompletedProcess(
                [], 0, stdout=f"{REMOTE}\nhttps://github.com/o/r\n", stderr=""
            ),
        ]
        with unittest.mock.patch("scripts.push_guard.subprocess.run", side_effect=outputs) as run:
            self.assertEqual(
                push_guard.read_remote_config(cwd="/fixture"),
                {name: [REMOTE, "https://github.com/o/r"]},
            )
        options = dict(
            cwd="/fixture",
            timeout=push_guard.CONFIG_DEADLINE,
            check=False,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
        )
        self.assertEqual(
            run.call_args_list,
            [
                unittest.mock.call(["git", "remote"], **options),
                unittest.mock.call(
                    ["git", "remote", "get-url", "--push", "--all", name], **options
                ),
            ],
        )

    def test_git_errors_and_timeouts_name_the_failed_query(self) -> None:
        listed = subprocess.CompletedProcess([], 0, stdout="origin\n", stderr="")
        failures = (
            subprocess.CompletedProcess([], 1, stdout="", stderr="error"),
            subprocess.CompletedProcess([], 128, stdout="", stderr="error"),
            subprocess.TimeoutExpired("git", push_guard.CONFIG_DEADLINE),
        )
        for failure in failures:
            for prefix, reason in (
                ([], "git remote"),
                ([listed], "git remote get-url for 'origin'"),
            ):
                with (
                    self.subTest(failure=failure, reason=reason),
                    unittest.mock.patch(
                        "scripts.push_guard.subprocess.run", side_effect=[*prefix, failure]
                    ),
                ):
                    reader = unittest.mock.Mock()
                    status, _, err = config_guard(reader, push_guard.read_remote_config, update())
                    self.assertEqual(status, 2)
                    self.assertIn(reason, err)
                    self.assertIn(
                        "TimeoutExpired" if isinstance(failure, Exception) else "exited", err
                    )
                    reader.assert_not_called()

    def test_no_remotes_needs_only_one_query(self) -> None:
        done = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with unittest.mock.patch("scripts.push_guard.subprocess.run", return_value=done) as run:
            self.assertEqual(
                config_guard(unittest.mock.Mock(), push_guard.read_remote_config)[0], 0
            )
        self.assertEqual(run.call_count, 1)

    def test_remote_count_over_bound_stops_before_url_query(self) -> None:
        names = "origin\n" * (push_guard.MAX_REMOTES + 1)
        done = subprocess.CompletedProcess([], 0, stdout=names, stderr="")
        with unittest.mock.patch("scripts.push_guard.subprocess.run", return_value=done) as run:
            status, _, err = config_guard(unittest.mock.Mock(), push_guard.read_remote_config)
        self.assertEqual(status, 2)
        self.assertIn("remotes", err)
        self.assertEqual(run.call_count, 1)

    def test_push_url_count_over_bound_fails_closed(self) -> None:
        outputs = [
            subprocess.CompletedProcess([], 0, stdout="origin\n", stderr=""),
            subprocess.CompletedProcess(
                [], 0, stdout=f"{REMOTE}\n" * (push_guard.MAX_CONFIG_ENTRIES + 1), stderr=""
            ),
        ]
        with unittest.mock.patch("scripts.push_guard.subprocess.run", side_effect=outputs):
            status, _, err = config_guard(unittest.mock.Mock(), push_guard.read_remote_config)
        self.assertEqual(status, 2)
        self.assertIn("remote push URLs", err)

    def test_real_reader_expands_rewritten_push_urls(self) -> None:
        for rule in REWRITE_RULES:
            with self.subTest(rule=rule), tempfile.TemporaryDirectory() as directory:
                fixture_git(directory, "init")
                fixture_git(
                    directory, "config", "set", "--local", f"url.https://github.com/.{rule}", "gh:"
                )
                fixture_git(directory, "remote", "add", "origin", f"gh:{REPOSITORY}")
                urls = push_guard.read_remote_config(cwd=directory)["origin"]
                self.assertEqual(len(urls), 1)
                self.assertEqual(push_guard.github_repository(urls[0]), REPOSITORY)

    def test_planted_raw_config_reader_is_refused(self) -> None:
        result = unittest.TestResult()
        with unittest.mock.patch.object(push_guard, "read_remote_config", raw_config_reader):
            ConfigReaderTest("test_real_reader_expands_rewritten_push_urls").run(result)
        self.assertEqual(len(result.failures), len(REWRITE_RULES))
        self.assertFalse(result.errors)

    def test_real_reader_checks_push_url_instead_of_fetch_url(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture_git(directory, "init")
            fixture_git(directory, "remote", "add", "origin", "https://gitlab.com/o/r")
            fixture_git(directory, "remote", "set-url", "--push", "origin", REMOTE)
            reader = unittest.mock.Mock(return_value=pull_request())
            remotes = push_guard.read_remote_config(cwd=directory)
            self.assertEqual(remotes, {"origin": [REMOTE]})
            self.assertEqual(config_guard(reader, lambda: remotes, update())[0], 1)
            reader.assert_called_once_with(REPOSITORY, BRANCH)


def fixture_git(directory: str, *args: str) -> str:
    """Run local Git commands only in a disposable integration fixture."""
    return subprocess.run(  # nosec B603 B607 - fixture argv, no network or shell.
        ["git", *args],
        cwd=directory,
        check=True,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=push_guard.CONFIG_DEADLINE,
    ).stdout


def raw_config_reader(cwd: str) -> dict[str, list[str]]:
    """Plant the old raw-config behavior against the temporary origin fixture."""
    raw = fixture_git(cwd, "config", "--get-regexp", r"^remote\.origin\.url$")
    return {"origin": [raw.rstrip("\n").partition(" ")[2]]}


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
        malformed = ["bad JSON", "null", "{}", "[null]", '[{"state": "closed"}]']
        for number in (True, False):
            payload = json.loads(pull_request())
            payload[0]["number"] = number
            malformed.append(json.dumps(payload))
        for raw in malformed:
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    push_guard.newest_pull_request(raw)
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


class StdinTest(unittest.TestCase):
    def test_stdin_deadline_fails_closed(self) -> None:
        release = threading.Event()
        finished = threading.Event()

        def blocked_read(_size: int) -> str:
            # Independent timeout keeps a missing join timeout from hanging the test.
            release.wait(timeout=1)
            finished.set()
            return ""

        stdin = unittest.mock.Mock(read=blocked_read)
        reader = unittest.mock.Mock(return_value="[]")
        with (
            unittest.mock.patch("sys.stdin", stdin),
            unittest.mock.patch.object(push_guard, "STDIN_DEADLINE", 0.1),
        ):
            start = time.monotonic()
            try:
                status, _, err = capture_main(
                    lambda argv: push_guard.main(argv, reader), "origin", REMOTE
                )
                elapsed = time.monotonic() - start
            finally:
                release.set()
                self.assertTrue(finished.wait(timeout=2))
        self.assertEqual(status, 2)
        self.assertIn("stdin deadline", err)
        self.assertIn("refusing (fail closed)", err)
        self.assertLess(elapsed, 0.75)
        reader.assert_not_called()

    def test_stdin_byte_cap_boundary(self) -> None:
        for ref in (BRANCH, "fix/é"):
            line = update(ref=ref)
            size = len(line.encode("utf-8"))
            for cap, expected in ((size - 1, 2), (size, 0), (size + 1, 0)):
                with (
                    self.subTest(ref=ref, cap=cap),
                    unittest.mock.patch.object(push_guard, "MAX_STDIN_BYTES", cap),
                ):
                    reader = unittest.mock.Mock(return_value="[]")
                    status, _, err = guard(reader, line)
                    self.assertEqual(status, expected)
                    if expected:
                        self.assertIn("byte cap", err)
                        self.assertIn("refusing (fail closed)", err)
                        reader.assert_not_called()

    def test_stdin_read_errors_fail_closed(self) -> None:
        for error in (OSError("stdin failed"), ValueError("stdin closed")):
            with (
                self.subTest(error=error),
                unittest.mock.patch(
                    "sys.stdin", unittest.mock.Mock(read=unittest.mock.Mock(side_effect=error))
                ),
            ):
                reader = unittest.mock.Mock()
                status, _, err = capture_main(
                    lambda argv, reader=reader: push_guard.main(argv, reader), "origin", REMOTE
                )
                self.assertEqual(status, 2)
                self.assertIn(str(error), err)
                self.assertIn("refusing (fail closed)", err)
                reader.assert_not_called()

    def test_short_reads_preserve_update_lines(self) -> None:
        stdin = unittest.mock.Mock()
        stdin.read.side_effect = [*update(ref="fix/é"), ""]
        reader = unittest.mock.Mock(return_value="[]")
        with unittest.mock.patch("sys.stdin", stdin):
            self.assertEqual(
                capture_main(lambda argv: push_guard.main(argv, reader), "origin", REMOTE),
                (0, "", ""),
            )
        reader.assert_called_once_with(REPOSITORY, "fix/é")


class RemoteTest(unittest.TestCase):
    def test_github_urls(self) -> None:
        for remote in (
            f"https://github.com/{REPOSITORY}",
            f"https://GitHub.Com/{REPOSITORY}",
            f"ssh://git@GITHUB.COM/{REPOSITORY}.git",
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
    def test_newest_head_query_hostname_and_timeout(self) -> None:
        done = subprocess.CompletedProcess([], 0, stdout="[]", stderr="")
        with unittest.mock.patch("scripts.push_guard.subprocess.run", return_value=done) as run:
            self.assertEqual(push_guard.read_pull_requests(REPOSITORY, BRANCH), "[]")
        run.assert_called_once_with(
            [
                "gh",
                "api",
                f"repos/{REPOSITORY}/pulls?head=VMAFx:{BRANCH}"
                "&state=all&sort=created&direction=desc&per_page=1",
                "--hostname",
                "github.com",
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
