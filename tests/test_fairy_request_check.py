# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Stub-only coverage of the own-PR Fairy request check."""

import datetime
import socket
import unittest.mock

from tests import test_ffmpeg_forgejo as forgejo


def _closed_port() -> int:
    """Return an unbound local TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def pull(age=20, reviewers=(), login="tester", number=1):
    """Build an open PR relative to the current UTC time."""
    created = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=age)
    return {
        "number": number,
        "state": "open",
        "user": {"login": login},
        "created_at": created.isoformat(),
        "title": "planted missing Fairy",
        "requested_reviewers": [{"login": name} for name in reviewers],
    }


class FairyRequestCheckTest(forgejo.FairyRequestTestBase):
    """Exercise status, author, age, and review coverage without external I/O."""

    def check_pull(self, row, reviews=(), **settings):
        """Run the check on one scripted pull and its review authors."""
        stub = forgejo.Stub(**settings)
        stub.open_pulls = [row]
        stub.reviews[row["number"]] = list(reviews)
        return self.run_command(stub, "fairy-requests")

    def test_twenty_minutes_without_fairy_fails(self):
        """An overdue planted gap emits number, age in minutes, and title."""
        proc = self.check_pull(pull())
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertEqual(proc.stdout, "1\t20\tplanted missing Fairy\n")

    def test_fairy_requested_passes(self):
        """An outstanding Fairy request satisfies the check."""
        proc = self.check_pull(pull(reviewers=["Forgejo_Fairy"]))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "")

    def test_fairy_reviewed_only_passes(self):
        """A Fairy review satisfies the check after the request disappears."""
        proc = self.check_pull(pull(), [forgejo.fairy_review()])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "")

    def test_ten_minutes_without_fairy_passes(self):
        """A recent PR remains within the grace period."""
        proc = self.check_pull(pull(age=10))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "")

    def test_token_login_selects_author(self):
        """The authenticated login need not match any hardcoded operator name."""
        proc = self.check_pull(pull(login="different"), login="different")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("planted missing Fairy", proc.stdout)

    def test_another_author_is_ignored(self):
        """Other contributors' overdue PRs do not fail this own-PR check."""
        proc = self.check_pull(pull(login="other"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "")

    def test_reviews_on_second_capped_page(self):
        """A server page cap cannot hide an existing Fairy review."""
        proc = self.check_pull(
            pull(),
            [forgejo.fairy_review(login="alice"), forgejo.fairy_review()],
            max_response_items=1,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_review_failure_fails_closed(self):
        """Unavailable review evidence cannot produce a clean check."""
        stub = forgejo.Stub()
        stub.open_pulls = [pull()]
        stub.fail_reviews = {1}
        proc = self.run_command(stub, "fairy-requests")
        self.assertEqual(proc.returncode, 1, proc.stderr)

    def test_explicit_offline_skips_loudly(self):
        """The optional target can skip offline without contacting the API."""
        stub = forgejo.Stub()
        proc = self.run_command(
            stub,
            "fairy-requests",
            "--skip-unavailable",
            env_updates={"FFMPEG_FORGEJO_OFFLINE": "1"},
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("SKIPPED", proc.stderr)
        self.assertEqual(stub.requests, [])

    def test_offline_api_with_login_skips_loudly(self):
        """A closed API port with preset login skips loudly when requested."""
        port = _closed_port()
        stub = forgejo.Stub()
        proc = self.run_command(
            stub,
            "fairy-requests",
            "--skip-unavailable",
            env_updates={
                "FFMPEG_FORGEJO_LOGIN": "tester",
                "FFMPEG_FORGEJO_API": f"http://127.0.0.1:{port}/api/v1",
            },
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("SKIPPED", proc.stderr)
        self.assertIn("curl rc 7", proc.stderr)

    def test_offline_api_with_login_fails_without_skip(self):
        """A closed API port with preset login fails without the skip flag."""
        port = _closed_port()
        stub = forgejo.Stub()
        proc = self.run_command(
            stub,
            "fairy-requests",
            env_updates={
                "FFMPEG_FORGEJO_LOGIN": "tester",
                "FFMPEG_FORGEJO_API": f"http://127.0.0.1:{port}/api/v1",
            },
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn("SKIPPED", proc.stderr)

    def test_online_api_with_login_runs_full_check(self):
        """An online API with preset login probes settings and audits pulls."""
        stub = forgejo.Stub()
        stub.open_pulls = [pull()]
        proc = self.run_command(
            stub,
            "fairy-requests",
            "--skip-unavailable",
            env_updates={"FFMPEG_FORGEJO_LOGIN": "tester"},
        )
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn(("GET", "/api/v1/settings/api", ""), stub.requests)

    def test_skip_unavailable_probes_settings_once(self):
        """Skip-unavailable reuses the settings probe response without duplicate requests."""
        stub = forgejo.Stub()
        stub.open_pulls = [pull(reviewers=["Forgejo_Fairy"])]
        served = 0
        original = forgejo.Handler._route_settings

        def single_settings(handler, s, url):
            nonlocal served
            if url.path == "/api/v1/settings/api":
                served += 1
                if served > 1:
                    handler.reply(500, {"message": "duplicate settings request"})
                    return True
            return original(handler, s, url)

        with unittest.mock.patch.object(forgejo.Handler, "_route_settings", single_settings):
            proc = self.run_command(
                stub,
                "fairy-requests",
                "--skip-unavailable",
                env_updates={"FFMPEG_FORGEJO_LOGIN": "tester"},
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        settings_reqs = [r for r in stub.requests if r[1] == "/api/v1/settings/api"]
        self.assertEqual(len(settings_reqs), 1)

    def test_login_validation_positive_and_boundary(self):
        """Valid and single-character logins are accepted without rejection."""
        for user in ("custom_user", "a.b-c_d", "a"):
            with self.subTest(login=user):
                stub = forgejo.Stub()
                proc = self.run_command(
                    stub,
                    "fairy-requests",
                    env_updates={"FFMPEG_FORGEJO_LOGIN": user},
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(proc.stdout, "")

    def test_login_validation_negative_rejects_and_avoids_api(self):
        """Malformed and path traversal logins die naming the variable without API I/O."""
        for bad in ("x/../FFmpeg", "..", "-x", "a b"):
            with self.subTest(login=bad):
                stub = forgejo.Stub()
                proc = self.run_command(
                    stub,
                    "fairy-requests",
                    env_updates={"FFMPEG_FORGEJO_LOGIN": bad},
                )
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn(f"invalid FFMPEG_FORGEJO_LOGIN: {bad}", proc.stderr)
                self.assertEqual(stub.requests, [])

    def test_login_negative_rejects_in_pr_without_api(self):
        """Malformed logins in PR commands die naming the variable without API I/O."""
        for bad in ("x/../FFmpeg", "..", "-x", "a b"):
            with self.subTest(login=bad):
                stub = forgejo.Stub()
                proc = self.run_command(
                    stub,
                    "pr",
                    "topic",
                    "title",
                    str(self.tmp / "body.md"),
                    env_updates={"FFMPEG_FORGEJO_LOGIN": bad},
                )
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn(f"invalid FFMPEG_FORGEJO_LOGIN: {bad}", proc.stderr)
                self.assertEqual(stub.requests, [])

    def test_bot_reviewer_traversal_refused_without_api(self):
        """Path traversal in FFMPEG_FORGEJO_BOT_REVIEWER is refused without API I/O."""
        stub = forgejo.Stub()
        proc = self.run_command(
            stub,
            "pr",
            "topic",
            "title",
            str(self.tmp / "body.md"),
            env_updates={"FFMPEG_FORGEJO_BOT_REVIEWER": ".."},
        )
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("not a login: ..", proc.stderr)
        self.assertEqual(stub.requests, [])
