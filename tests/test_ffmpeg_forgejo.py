# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Tests for scripts/ffmpeg-forgejo against a stub Forgejo API."""

from __future__ import annotations

import contextlib
import http.server
import json
import os
import shutil
import subprocess  # nosec B404 - fixed git and helper argv in temporary test repositories.
import tempfile
import threading
import unittest
import unittest.mock
import urllib.parse
from collections.abc import Iterator
from pathlib import Path

from tests.helpers import isolated_git_environment

ROOT = Path(__file__).resolve().parent.parent
HELPER = ROOT / "scripts" / "ffmpeg-forgejo"
A = "a" * 40
B = "b" * 40
LIVE = "77c2f4b1c12b" + "0" * 28
STALE = "c1f8e9c2a0"
BODY = "new body\nline two\n"
NOW = "2026-11-06T09:00:00Z"
OVERLAP_FILE = "libavfilter/vf_find_rect.c"
HELPER_SUBCOMMANDS = frozenset({"comment", "edit-body", "issue", "pr", "push", "fairy-reviews"})


def label(status: str, n: int = 0) -> dict:
    """Build an Autolabel status payload."""

    return {
        "context": f"Autolabel / Labeler {n} (pull_request_target)",
        "status": status,
        "updated_at": "2026-11-06T08:00:00Z",
    }


def other_status(state: str, updated_at: str, n: int = 0) -> dict:
    """Build a non-Autolabel commit status with a fixed timestamp."""

    return {"context": f"Test / job {n}", "status": state, "updated_at": updated_at}


def open_pull(number: int = 25612, login: str = "tester") -> dict:
    """Build one open pull request list item."""

    return {"number": number, "state": "open", "user": {"login": login}}


def fairy_review(
    state: str | None = "APPROVED",
    submitted_at: str = "2026-11-03T12:00:00Z",
    login: str = "Forgejo_Fairy",
    commit_id: str = A,
) -> dict:
    """Build one submitted or requested review payload."""

    return {
        "user": {"login": login},
        "state": state,
        "submitted_at": submitted_at,
        "commit_id": commit_id,
    }


def git(repo: Path, *args: str) -> None:
    """Run Git with fixed argv in a temporary test repository."""

    subprocess.run(  # nosec B603 B607 - fixed git argv, temporary test repository, test literals
        ["git", "-c", "core.hooksPath=/dev/null", *args],
        cwd=repo,
        env=isolated_git_environment(preserve_config=True),
        check=True,
        timeout=120,
    )


def git_output(repo: Path, *args: str) -> str:
    """Return the stripped output of Git with fixed argv in a temporary test repository."""

    return subprocess.run(  # nosec B603 B607 - fixed git argv, temporary test repository, test literals
        ["git", "-c", "core.hooksPath=/dev/null", *args],
        cwd=repo,
        env=isolated_git_environment(preserve_config=True),
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    ).stdout.strip()


OTHER = other_status("pending", "2026-11-06T08:00:00Z")
ChangedFile = str | dict[str, str]


class GitEnvironmentTest(unittest.TestCase):
    def test_git_ignores_checkout_environment(self) -> None:
        """Fixture Git commands do not inherit the caller's repository paths."""

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            bogus_git_dir = root / "bogus-git-dir"
            bogus_index = root / "bogus-index"
            with unittest.mock.patch.dict(
                os.environ,
                {"GIT_DIR": str(bogus_git_dir), "GIT_INDEX_FILE": str(bogus_index)},
            ):
                git(repo, "init", "-q", "-b", "master")
            self.assertTrue(repo.joinpath(".git").is_dir())
            self.assertFalse(bogus_git_dir.exists())
            self.assertFalse(bogus_index.exists())

    def test_git_ignores_global_hooks(self) -> None:
        """Fixture Git explicitly bypasses hooks without changing credential settings."""

        with unittest.mock.patch("subprocess.run") as run:
            git(Path("fixture"), "init", "-q")
        self.assertEqual(run.call_args.args[0][:3], ["git", "-c", "core.hooksPath=/dev/null"])
        self.assertEqual(
            run.call_args.kwargs["env"], isolated_git_environment(preserve_config=True)
        )


class Stub:
    """Forgejo API stub.

    heads: head sha returned by successive GET /pulls/1 (the last one repeats).
    polls: sha -> list of status lists, one per poll (page 1 starts a poll; the last repeats).
    fail_status: answer the status endpoint with HTTP 500.
    pull_status: answer GET /pulls/1 with this HTTP status instead of a pull.
    """

    def __init__(
        self,
        heads: list[object] | None = None,
        polls: dict[str, list[list[dict]]] | None = None,
        pull_status: int = 200,
        reviewers: list[list[str]] | None = None,
        **kwargs: object,
    ):
        """Initialize scripted responses and persistent requested team reviewers."""

        heads = heads or [A]
        polls = polls or {}
        self.heads, self.polls = heads, polls
        self.fail_status = bool(kwargs.get("fail_status", False))
        self.pull_status = pull_status
        self.reviewers = reviewers or [[]]
        self.login = str(kwargs.get("login", "tester"))
        self.max_response_items = kwargs.get("max_response_items", 50)
        self.open_pulls: list[dict] = []
        self.pull_files: dict[int, list[ChangedFile]] = {}
        self.fail_pulls = False
        self.fail_files: set[int] = set()
        self.reviews: dict[int, list[dict]] = {}
        self.fail_reviews: set[int] = set()
        self.comments: dict[int, list[dict]] = {}
        self.fail_comments: set[int] = set()
        self.head_reads = 0
        self.reviewer_reads = 0
        self.poll_index = {sha: -1 for sha in polls}
        self.requests: list[tuple[str, str, str]] = []
        self.pull_headers: list[dict[str, str]] = []
        self.requested_users: list[str] = []
        self.requested_teams = [
            name.removeprefix("team:") for name in self.reviewers[0] if name.startswith("team:")
        ]
        self.drop_reviewers = False
        self.request_status = 201
        self.branch_head = kwargs.get("branch_head")
        self.branch_status = int(kwargs.get("branch_status", 200))

    def head(self) -> object:
        """Return the next scripted pull head, repeating the last."""

        sha = self.heads[min(self.head_reads, len(self.heads) - 1)]
        self.head_reads += 1
        return sha

    def statuses(self, sha: str, query: dict[str, list[str]]) -> list[dict]:
        """Return a server-capped page from the current scripted status poll."""

        page = int(query.get("page", ["1"])[0])
        limit = self.page_limit(query)
        if page == 1:
            self.poll_index[sha] += 1
        seq = self.polls.get(sha, [[]])
        full = seq[min(max(self.poll_index.get(sha, 0), 0), len(seq) - 1)]
        return full[(page - 1) * limit : page * limit]

    def pull(self) -> dict:
        """Return scripted user reviewers and the persisted requested team state."""

        reviewers = self.reviewers[min(self.reviewer_reads, len(self.reviewers) - 1)]
        self.reviewer_reads += 1
        return {
            "number": 1,
            "head": {"sha": self.head()},
            "html_url": "stub/pulls/1",
            "requested_reviewers": [
                {"login": login}
                for login in dict.fromkeys(reviewers + self.requested_users)
                if not login.startswith("team:")
            ],
            "requested_reviewers_teams": [{"name": name} for name in self.requested_teams],
        }

    def pulls(self, query: dict[str, list[str]]) -> list[dict]:
        """Return one filtered, server-capped page of open pull requests."""

        poster = query.get("poster", [None])[0]
        rows = [row for row in self.open_pulls if poster is None or row["user"]["login"] == poster]
        page = int(query.get("page", ["1"])[0])
        limit = self.page_limit(query)
        return rows[(page - 1) * limit : page * limit]

    def files(self, number: int, query: dict[str, list[str]]) -> list[dict] | None:
        """Return one server-capped page of changed files for a pull request."""

        page = int(query.get("page", ["1"])[0])
        limit = self.page_limit(query)
        rows = self.pull_files.get(number, [])
        result = [
            row if isinstance(row, dict) else {"filename": row}
            for row in rows[(page - 1) * limit : page * limit]
        ]
        return None if page > 1 and not result else result

    def review_page(self, number: int, query: dict[str, list[str]]) -> list[dict]:
        """Return one server-capped page of reviews for a pull request."""

        page = int(query.get("page", ["1"])[0])
        limit = self.page_limit(query)
        return self.reviews.get(number, [])[(page - 1) * limit : page * limit]

    def comment_page(self, number: int, query: dict[str, list[str]]) -> list[dict]:
        """Return one server-capped page of issue comments."""

        page = int(query.get("page", ["1"])[0])
        limit = self.page_limit(query)
        return self.comments.get(number, [])[(page - 1) * limit : page * limit]

    def page_limit(self, query: dict[str, list[str]]) -> int:
        """Cap the requested page size at the configured server maximum."""

        return min(int(query.get("limit", ["30"])[0]), self.max_response_items)


def overlap_stub(
    open_pulls: list[dict] | None = None,
    pull_files: dict[int, list[ChangedFile]] | None = None,
    reviewers: list[list[str]] | None = None,
    fail_pulls: bool = False,
    fail_files: set[int] | None = None,
) -> Stub:
    """Build a stub configured for pull-overlap probes."""

    stub = Stub(reviewers=reviewers)
    stub.open_pulls = open_pulls or []
    stub.pull_files = pull_files or {}
    stub.fail_pulls = fail_pulls
    stub.fail_files = fail_files or set()
    return stub


class Handler(http.server.BaseHTTPRequestHandler):
    def stub(self) -> Stub:
        """Return the stub attached to this test server."""

        return self.server.stub  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        """Keep request logging out of the test output."""

    def reply(self, code: int, obj: object) -> None:
        """Send one JSON response from the stub server."""

        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _route_user(self, stub: Stub, url: urllib.parse.SplitResult) -> bool:
        """Handle user read requests."""

        if url.path == "/api/v1/user":
            self.reply(200, {"login": stub.login})
            return True
        return False

    def _route_settings(self, stub: Stub, url: urllib.parse.SplitResult) -> bool:
        """Handle public API pagination settings requests."""

        if url.path == "/api/v1/settings/api":
            self.reply(200, {"max_response_items": stub.max_response_items})
            return True
        return False

    def _route_pull(self, stub: Stub, url: urllib.parse.SplitResult) -> bool:
        """Handle pull list and single pull read requests."""

        if url.path.endswith("/pulls"):
            if stub.fail_pulls:
                self.reply(500, {"message": "pull list failed"})
            else:
                self.reply(200, stub.pulls(urllib.parse.parse_qs(url.query)))
            return True
        if url.path.endswith("/pulls/1"):
            stub.pull_headers.append(dict(self.headers.items()))
            if stub.pull_status == 200:
                self.reply(200, stub.pull())
            else:
                self.reply(stub.pull_status, {"message": "pull read failed"})
            return True
        return False

    def _route_pull_files(
        self, stub: Stub, parts: list[str], url: urllib.parse.SplitResult
    ) -> bool:
        """Handle pull changed files requests."""

        if len(parts) > 3 and parts[-3] == "pulls" and parts[-1] == "files":
            number = int(parts[-2])
            if number in stub.fail_files:
                self.reply(500, {"message": "pull files failed"})
            else:
                self.reply(200, stub.files(number, urllib.parse.parse_qs(url.query)))
            return True
        return False

    def _route_reviews(self, stub: Stub, parts: list[str], url: urllib.parse.SplitResult) -> bool:
        """Handle pull reviews requests."""

        if len(parts) > 3 and parts[-3] == "pulls" and parts[-1] == "reviews":
            number = int(parts[-2])
            if number in stub.fail_reviews:
                self.reply(500, {"message": "reviews failed"})
            else:
                self.reply(200, stub.review_page(number, urllib.parse.parse_qs(url.query)))
            return True
        return False

    def _route_comments(self, stub: Stub, parts: list[str], url: urllib.parse.SplitResult) -> bool:
        """Handle paged issue comment reads."""

        if len(parts) > 3 and parts[-3] == "issues" and parts[-1] == "comments":
            number = int(parts[-2])
            if number in stub.fail_comments:
                self.reply(500, {"message": "comments failed"})
            else:
                self.reply(200, stub.comment_page(number, urllib.parse.parse_qs(url.query)))
            return True
        return False

    def _route_status(self, stub: Stub, parts: list[str], url: urllib.parse.SplitResult) -> bool:
        """Handle commit status requests."""

        if len(parts) > 3 and parts[-3] == "commits" and parts[-1] == "status":
            if stub.fail_status:
                self.reply(500, {"message": "boom"})
            else:
                st = stub.statuses(parts[-2], urllib.parse.parse_qs(url.query))
                self.reply(200, {"statuses": st, "total_count": len(st)})
            return True
        return False

    def _route_branch(self, stub: Stub, parts: list[str]) -> bool:
        """Return the scripted fork branch commit or a failed read."""

        if len(parts) > 3 and parts[-2] == "branches":
            self.reply(stub.branch_status, {"commit": {"id": stub.branch_head}})
            return True
        return False

    def _dispatch_get(self, stub: Stub, url: urllib.parse.SplitResult, parts: list[str]) -> bool:
        """Route settings and repository GET requests to the matching handler."""

        routes = (
            lambda: self._route_settings(stub, url),
            lambda: self._route_user(stub, url),
            lambda: self._route_pull(stub, url),
            lambda: self._route_pull_files(stub, parts, url),
            lambda: self._route_reviews(stub, parts, url),
            lambda: self._route_comments(stub, parts, url),
            lambda: self._route_branch(stub, parts),
            lambda: self._route_status(stub, parts, url),
        )
        return any(route() for route in routes)

    def do_GET(self) -> None:
        """Serve scripted user, pull, and commit-status reads."""

        stub = self.stub()
        url = urllib.parse.urlsplit(self.path)
        stub.requests.append(("GET", self.path, ""))
        parts = url.path.split("/")
        if not self._dispatch_get(stub, url, parts):
            self.reply(404, {})

    def do_POST(self) -> None:
        """Record writes and persist additive reviewer requests as Forgejo does."""

        data = self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode()
        self.stub().requests.append(("POST", self.path, data))
        if self.path.endswith("/pulls"):
            self.reply(201, {"number": 1, "html_url": "stub/pulls/1"})
        elif self.path.endswith("/pulls/1/requested_reviewers"):
            stub = self.stub()
            if not stub.drop_reviewers and stub.request_status == 201:
                payload = json.loads(data)
                stub.requested_users = list(
                    dict.fromkeys(stub.requested_users + payload.get("reviewers", []))
                )
                stub.requested_teams = list(
                    dict.fromkeys(stub.requested_teams + payload.get("team_reviewers", []))
                )
            self.reply(stub.request_status, {})
        elif self.path.endswith("/issues/1/comments"):
            self.reply(201, {"html_url": "stub/issues/1#issuecomment-1"})
        else:
            self.reply(404, {})

    def do_PATCH(self) -> None:
        """Record and acknowledge pull-body edits."""

        data = self.rfile.read(int(self.headers["Content-Length"])).decode()
        self.stub().requests.append(("PATCH", self.path, data))
        self.reply(200, {"html_url": "stub/pulls/1"})


def serve(stub: Stub) -> http.server.HTTPServer:
    """Start a local Forgejo API stub in a background thread."""

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    server.stub = stub  # type: ignore[attr-defined]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class ForgejoTest(unittest.TestCase):
    helper: Path = HELPER

    def setUp(self) -> None:
        """Create an isolated helper environment with a fake keyring command."""

        self.tmp = Path(tempfile.mkdtemp())
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        tool = bin_dir / "secret-tool"
        tool.write_text("#!/bin/sh\necho testtoken\n", encoding="utf-8")
        tool.chmod(0o755)
        self.bin_dir = bin_dir
        (self.tmp / "body.md").write_text(BODY, encoding="utf-8")
        self.repo = self.tmp / "ffmpeg"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "master")
        candidate = self.repo / OVERLAP_FILE
        candidate.parent.mkdir(parents=True)
        candidate.write_text("base\n", encoding="utf-8")
        git(self.repo, "add", OVERLAP_FILE)
        commit = [
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "commit.gpgSign=false",
            "commit",
            "-qm",
        ]
        git(self.repo, *commit, "base")
        self.upstream = self.tmp / "upstream.git"
        git(self.tmp, "clone", "-q", "--bare", str(self.repo), str(self.upstream))
        git(self.repo, "checkout", "-qb", "topic")
        candidate.write_text("topic\n", encoding="utf-8")
        git(self.repo, "add", OVERLAP_FILE)
        git(self.repo, *commit, "topic")
        self.sweep = self.tmp / "sweep.md"
        sha = git_output(self.repo, "rev-parse", "topic")
        self.sweep.write_text(f"class: unchecked-return\nhead: {sha}\nno hits\n", encoding="utf-8")

    def run_command(
        self,
        stub: Stub,
        *args: str,
        path_first: Path | None = None,
        env_updates: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run one allow-listed helper subcommand against the stub API."""

        if not args or args[0] not in HELPER_SUBCOMMANDS:
            raise ValueError(f"unsupported helper subcommand: {args[0] if args else '<missing>'}")
        if args[0] == "pr" and stub.branch_head is None:
            stub.branch_head = git_output(self.repo, "rev-parse", "topic")
        server = serve(stub)
        try:
            env = isolated_git_environment(preserve_config=True)
            dirs = [str(p) for p in (path_first, self.bin_dir) if p]
            env.update(
                PATH=os.pathsep.join(dirs + [env.get("PATH", "")]),
                FFMPEG_FORGEJO_API=f"http://127.0.0.1:{server.server_port}/api/v1",
                FFMPEG_FORGEJO_GIT=str(self.upstream),
                FFMPEG_FORGEJO_POLL="0",
            )
            env.update(env_updates or {})
            return subprocess.run(  # nosec B603 - fixed interpreter/helper, allow-list, test literals.
                ["bash", str(self.helper), *args],
                env=env,
                cwd=self.repo,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        finally:
            server.shutdown()
            server.server_close()

    def sent_bodies(self, stub: Stub, method: str) -> list[str]:
        """Return decoded body values sent with one HTTP method."""

        return [json.loads(data)["body"] for got, _, data in stub.requests if got == method]


class RunCommandTest(ForgejoTest):
    def test_unknown_subcommand_is_rejected_before_any_request(self) -> None:
        """An unknown helper subcommand raises before the stub receives a request."""

        stub = Stub()
        with self.assertRaises(ValueError):
            self.run_command(stub, "unknown")
        self.assertEqual(stub.requests, [])


class FairyReviewsTest(ForgejoTest):
    def assert_summary(self, rows: list[dict], expected: str) -> None:
        """Assert the exact tab-separated review summary for one pull."""

        stub = Stub()
        stub.reviews = {1: rows}
        proc = self.run_command(stub, "fairy-reviews", "1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, f"1\t{expected}\n")
        self.assertTrue(all(method == "GET" for method, _, _ in stub.requests))
        self.assertFalse(any(path.endswith("/user") for _, path, _ in stub.requests))

    def test_submitted_approval_counts(self) -> None:
        """A submitted Fairy approval counts and reports its abbreviated commit."""

        self.assert_summary([fairy_review()], f"1\tAPPROVED\t{A[:12]}")

    def test_request_review_is_not_submitted(self) -> None:
        """A requested Fairy reviewer row cannot count as a submitted review."""

        self.assert_summary([fairy_review("REQUEST_REVIEW", "")], "0\t-\t-")

    def test_pending_is_not_submitted(self) -> None:
        """A pending Fairy row stays excluded even with a timestamp."""

        self.assert_summary([fairy_review("PENDING")], "0\t-\t-")

    def test_request_with_timestamp_is_not_submitted(self) -> None:
        """A request state stays excluded even with a timestamp."""

        self.assert_summary([fairy_review("REQUEST_REVIEW")], "0\t-\t-")

    def test_missing_submission_time_is_not_submitted(self) -> None:
        """An approval without a submission timestamp stays excluded."""

        self.assert_summary([fairy_review(submitted_at="")], "0\t-\t-")

    def test_state_null_is_not_submitted(self) -> None:
        """A review row with null state cannot count as a submitted review."""

        self.assert_summary([fairy_review(state=None)], "0\t-\t-")

    def test_state_empty_is_not_submitted(self) -> None:
        """A review row with empty state cannot count as a submitted review."""

        self.assert_summary([fairy_review(state="")], "0\t-\t-")

    def test_unknown_state_is_not_submitted(self) -> None:
        """An unknown review state cannot count as a submitted review."""

        self.assert_summary([fairy_review(state="FOO")], "0\t-\t-")

    def test_each_allowed_state_counts(self) -> None:
        """Each valid Forgejo review state counts when submitted."""

        for state in ("APPROVED", "COMMENT", "REQUEST_CHANGES"):
            with self.subTest(state=state):
                self.assert_summary([fairy_review(state=state)], f"1\t{state}\t{A[:12]}")

    def test_another_reviewer_is_not_fairy(self) -> None:
        """Another users submitted approval cannot count as a Fairy review."""

        self.assert_summary([fairy_review(login="other")], "0\t-\t-")

    def test_empty_reviews(self) -> None:
        """An empty review list produces explicit zero and missing fields."""

        self.assert_summary([], "0\t-\t-")

    def test_newest_submission_ignores_list_order(self) -> None:
        """Mixed reviews select the newest submitted instant across timezones."""

        rows = [
            fairy_review("COMMENT", "2026-11-03T12:30:00Z", commit_id=B),
            fairy_review("REQUEST_REVIEW", ""),
            fairy_review("PENDING", "2026-11-03T15:00:00Z"),
            fairy_review(login="other"),
            fairy_review("APPROVED", "2026-11-03T13:00:00+01:00"),
        ]
        self.assert_summary(rows, f"2\tCOMMENT\t{B[:12]}")

    def test_reviews_on_later_page_count(self) -> None:
        """A submitted Fairy review after a full page of requests is seen."""

        self.assert_summary(
            [fairy_review("REQUEST_REVIEW", "") for _ in range(50)] + [fairy_review()],
            f"1\tAPPROVED\t{A[:12]}",
        )

    def test_server_page_cap_counts_all_reviews(self) -> None:
        """A thirty-row server cap cannot truncate a forty-five-review listing."""

        stub = Stub(max_response_items=30)
        stub.reviews = {1: [fairy_review()] * 45}
        proc = self.run_command(stub, "fairy-reviews", "1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, f"1\t45\tAPPROVED\t{A[:12]}\n")

    def test_invalid_server_page_cap_fails_without_output(self) -> None:
        """Noninteger and nonpositive settings fail before listing reviews."""

        for maximum in ("x", 0, -1, True, 1.5, None):
            with self.subTest(max_response_items=maximum):
                stub = Stub(max_response_items=maximum)
                with unittest.mock.patch.object(stub, "review_page", return_value=[]):
                    proc = self.run_command(stub, "fairy-reviews", "1")
                self.assertNotEqual(proc.returncode, 0)
                self.assertEqual(proc.stdout, "")
                self.assertIn("invalid max_response_items", proc.stderr)
                self.assertEqual(stub.requests, [("GET", "/api/v1/settings/api", "")])

    def test_server_page_cap_is_cached_across_default_listings(self) -> None:
        """One settings read supplies capped pull and review listings for the process."""

        stub = Stub(max_response_items=1)
        stub.open_pulls = [open_pull(1), open_pull(2)]
        stub.reviews = {1: [fairy_review()] * 2}
        proc = self.run_command(stub, "fairy-reviews")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, f"1\t2\tAPPROVED\t{A[:12]}\n2\t0\t-\t-\n")
        self.assertEqual(stub.requests.count(("GET", "/api/v1/settings/api", "")), 1)
        paths = [path for _, path, _ in stub.requests if "page=" in path]
        self.assertTrue(all("limit=1" in path for path in paths))

    def test_server_page_cap_above_fifty_uses_fifty(self) -> None:
        """A larger server maximum preserves the client fifty-row limit."""

        stub = Stub(max_response_items=100)
        stub.reviews = {1: [fairy_review()] * 51}
        proc = self.run_command(stub, "fairy-reviews", "1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, f"1\t51\tAPPROVED\t{A[:12]}\n")
        paths = [path for _, path, _ in stub.requests if "page=" in path]
        self.assertEqual(len(paths), 2)
        self.assertTrue(all("limit=50" in path for path in paths))

    def test_capped_page_twenty_boundary(self) -> None:
        """A capped twentieth page succeeds only when shorter than the server cap."""

        for count in (599, 600):
            with self.subTest(count=count):
                stub = Stub(max_response_items=30)
                stub.reviews = {1: [fairy_review()] * count}
                proc = self.run_command(stub, "fairy-reviews", "1")
                if count == 599:
                    self.assertEqual(proc.returncode, 0, proc.stderr)
                    self.assertEqual(proc.stdout, f"1\t599\tAPPROVED\t{A[:12]}\n")
                else:
                    self.assertNotEqual(proc.returncode, 0)
                    self.assertEqual(proc.stdout, "")
                    self.assertIn("pagination limit reached", proc.stderr)
                paths = [path for _, path, _ in stub.requests if "page=" in path]
                self.assertEqual(len(paths), 20)

    def test_explicit_numbers_preserve_order(self) -> None:
        """Explicit pull numbers each receive their own summary in argument order."""

        stub = Stub()
        stub.reviews = {2: [fairy_review()]}
        proc = self.run_command(stub, "fairy-reviews", "2", "1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, f"2\t1\tAPPROVED\t{A[:12]}\n1\t0\t-\t-\n")

    def test_default_lists_only_own_open_pulls_across_pages(self) -> None:
        """Default selection uses the poster filter and paginates open pulls."""

        stub = Stub(login="someone-else")
        stub.open_pulls = [open_pull(n, "someone-else") for n in range(1, 52)] + [
            open_pull(99, "other")
        ]
        proc = self.run_command(stub, "fairy-reviews")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.splitlines(), [f"{n}\t0\t-\t-" for n in range(1, 52)])
        paths = [path for _, path, _ in stub.requests if "/pulls?" in path]
        self.assertTrue(any("page=2" in path for path in paths))
        self.assertTrue(
            all("state=open" in path and "poster=someone-else" in path for path in paths)
        )
        review_pulls = [
            int(path.split("/pulls/")[1].split("/reviews")[0])
            for _, path, _ in stub.requests
            if "/reviews?" in path
        ]
        self.assertEqual(review_pulls, list(range(1, 52)))

    def test_second_pull_failure_emits_no_partial_output(self) -> None:
        """A failure reading reviews for a later pull emits no partial output on stdout."""

        stub = Stub()
        stub.reviews = {1: [fairy_review()]}
        stub.fail_reviews = {2}
        proc = self.run_command(stub, "fairy-reviews", "1", "2")
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")

    def test_reviews_at_page_twenty_short_page_succeeds(self) -> None:
        """A short twentieth page completes the listing without hitting pagination limit."""

        stub = Stub()
        stub.reviews = {1: [fairy_review()] * 999}
        proc = self.run_command(stub, "fairy-reviews", "1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, f"1\t999\tAPPROVED\t{A[:12]}\n")

    def test_reviews_at_page_twenty_full_page_dies(self) -> None:
        """A full twentieth page reaches the pagination limit and fails closed."""

        stub = Stub()
        stub.reviews = {1: [fairy_review()] * 1000}
        proc = self.run_command(stub, "fairy-reviews", "1")
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")
        paths = [path for _, path, _ in stub.requests if "page=" in path]
        self.assertEqual(len(paths), 20)

    def test_no_own_open_pulls(self) -> None:
        """Default selection with no own open pulls prints nothing."""

        proc = self.run_command(Stub(), "fairy-reviews")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "")

    def test_api_failures_are_errors(self) -> None:
        """Failed pull or review API reads cannot print a successful zero count."""

        for explicit in (False, True):
            stub = Stub()
            stub.fail_pulls = True
            stub.fail_reviews = {1}
            with self.subTest(explicit=explicit):
                proc = self.run_command(stub, "fairy-reviews", *(("1",) if explicit else ()))
                self.assertNotEqual(proc.returncode, 0)
                self.assertEqual(proc.stdout, "")

    def test_pagination_limit_fails_without_partial_count(self) -> None:
        """Neither review nor pull listings can exceed twenty page requests."""

        for explicit in (False, True):
            stub = Stub()
            stub.reviews = {1: [fairy_review()] * 1001}
            stub.open_pulls = [open_pull(n) for n in range(1, 1002)]
            with self.subTest(explicit=explicit):
                proc = self.run_command(stub, "fairy-reviews", *(("1",) if explicit else ()))
                self.assertNotEqual(proc.returncode, 0)
                self.assertEqual(proc.stdout, "")
                paths = [path for _, path, _ in stub.requests if "page=" in path]
                self.assertEqual(len(paths), 20)

    def test_invalid_number_is_rejected_before_api_reads(self) -> None:
        """Invalid pull arguments fail usage validation before any API request."""

        stub = Stub()
        proc = self.run_command(stub, "fairy-reviews", "1", "invalid")
        self.assertEqual(proc.returncode, 64)
        self.assertEqual(stub.requests, [])

    def test_from_json_uses_the_same_classifier(self) -> None:
        """Offline JSON classification needs no API or keyring request."""

        path = self.tmp / "reviews.json"
        path.write_text(
            json.dumps([fairy_review(), fairy_review("REQUEST_REVIEW", "")]), encoding="utf-8"
        )
        stub = Stub()
        proc = self.run_command(stub, "fairy-reviews", "--from-json", str(path))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, f"1\tAPPROVED\t{A[:12]}\n")
        self.assertEqual(stub.requests, [])

    def test_invalid_json_payloads_fail(self) -> None:
        """Invalid review shapes and commit hashes fail offline classification."""

        path = self.tmp / "reviews.json"
        for rows in ({}, [fairy_review(commit_id="invalid")]):
            path.write_text(json.dumps(rows), encoding="utf-8")
            with self.subTest(rows=rows):
                proc = self.run_command(Stub(), "fairy-reviews", "--from-json", str(path))
                self.assertNotEqual(proc.returncode, 0)
                self.assertEqual(proc.stdout, "")

    def test_malformed_review_rows_fail(self) -> None:
        """Malformed review rows fail classification with exit non-zero and no stdout."""

        fairy_no_state = {
            "user": {"login": "Forgejo_Fairy"},
            "submitted_at": "2026-11-03T12:00:00Z",
        }
        cases: list[tuple[str, list[object]]] = [
            ("empty_dict_row", [{}]),
            ("fairy_row_without_state", [fairy_no_state]),
            ("row_not_a_dict", ["not-a-dict"]),
            ("state_not_str_or_null", [{"state": 123}]),
            ("user_not_dict_or_null", [{"state": "APPROVED", "user": "not-a-dict"}]),
            ("submitted_at_not_str_or_null", [{"state": "APPROVED", "submitted_at": 123}]),
            ("commit_id_not_str_or_null", [{"state": "APPROVED", "commit_id": 123}]),
        ]
        for name, rows in cases:
            with self.subTest(case=name):
                stub = Stub()
                stub.reviews = {1: rows}  # type: ignore[dict-item]
                proc = self.run_command(stub, "fairy-reviews", "1")
                self.assertNotEqual(proc.returncode, 0)
                self.assertEqual(proc.stdout, "")


class EditBodyTest(ForgejoTest):
    """Exercise body-edit decisions against scripted commit statuses."""

    def run_helper(
        self,
        stub: Stub,
        *args: str,
        path_first: Path | None = None,
        env_updates: dict[str, str] | None = None,
    ) -> int:
        """Run an unclaimed body edit and return its status."""

        proc = self.run_command(
            stub,
            "edit-body",
            *args,
            "--no-test-claims",
            "1",
            str(self.tmp / "body.md"),
            path_first=path_first,
            env_updates=env_updates,
        )
        return proc.returncode

    def patches(self, stub: Stub) -> list[str]:
        """Return bodies sent by pull-body PATCH requests."""

        return self.sent_bodies(stub, "PATCH")

    def assert_refused(self, stub: Stub, *args: str) -> None:
        """Prove an unsafe edit exits 75 without a PATCH."""

        self.assertEqual(self.run_helper(stub, *args), 75)
        self.assertEqual(self.patches(stub), [])

    def assert_edited(self, stub: Stub, *args: str) -> None:
        """Prove a safe edit succeeds with the expected PATCH body."""

        self.assertEqual(self.run_helper(stub, *args), 0)
        self.assertEqual(self.patches(stub), [BODY])

    def test_pending_and_none_are_refused(self) -> None:
        """Pending Autolabel and fresh non-Autolabel statuses refuse an edit."""

        self.assert_refused(Stub([A], {A: [[label("pending"), OTHER]]}))
        stub = Stub([A], {A: [[other_status("success", NOW)]]})
        self.assertEqual(self.run_helper(stub, env_updates={"FFMPEG_FORGEJO_NOW": NOW}), 75)
        self.assertEqual(self.patches(stub), [])

    def test_fresh_head_without_autolabel_is_refused(self) -> None:
        """A successful status only five minutes old cannot clear the edit."""

        stub = Stub([A], {A: [[other_status("success", "2026-11-06T08:55:00Z")]]})
        self.assertEqual(self.run_helper(stub, env_updates={"FFMPEG_FORGEJO_NOW": NOW}), 75)
        self.assertEqual(self.patches(stub), [])

    def test_aged_head_without_autolabel_is_edited(self) -> None:
        """Ten settled statuses permit an edit when the newest is 31 minutes old."""

        rows = [other_status("success", "2026-11-06T08:29:00Z", n) for n in range(10)]
        stub = Stub([A], {A: [rows]})
        self.assertEqual(self.run_helper(stub, env_updates={"FFMPEG_FORGEJO_NOW": NOW}), 0)
        self.assertEqual(self.patches(stub), [BODY])

    def test_absent_autolabel_uses_created_at_and_offset(self) -> None:
        """A missing update time falls back to a created time with an offset."""

        row = {
            "context": "Test / created",
            "status": "warning",
            "created_at": "2026-11-06T09:29:00+01:00",
        }
        stub = Stub([A], {A: [[row]]})
        self.assertEqual(self.run_helper(stub, env_updates={"FFMPEG_FORGEJO_NOW": NOW}), 0)
        self.assertEqual(self.patches(stub), [BODY])

    def test_absent_autolabel_age_boundary(self) -> None:
        """Exactly 1800 seconds qualifies while 1799 seconds does not."""

        for timestamp, expected in (("08:30:00Z", 0), ("08:30:01Z", 75)):
            with self.subTest(timestamp=timestamp):
                stub = Stub([A], {A: [[other_status("success", f"2026-11-06T{timestamp}")]]})
                self.assertEqual(
                    self.run_helper(stub, env_updates={"FFMPEG_FORGEJO_NOW": NOW}), expected
                )
                self.assertEqual(self.patches(stub), [BODY] if expected == 0 else [])

    def test_zero_statuses_without_autolabel_are_refused(self) -> None:
        """No statuses cannot prove an absent Autolabel run has settled."""

        stub = Stub([A], {A: [[]]})
        self.assertEqual(self.run_helper(stub, env_updates={"FFMPEG_FORGEJO_NOW": NOW}), 75)
        self.assertEqual(self.patches(stub), [])

    def test_old_pending_status_without_autolabel_is_refused(self) -> None:
        """An old pending status still blocks a body edit."""

        stub = Stub([A], {A: [[other_status("pending", "2026-11-06T07:00:00Z")]]})
        self.assertEqual(self.run_helper(stub, env_updates={"FFMPEG_FORGEJO_NOW": NOW}), 75)
        self.assertEqual(self.patches(stub), [])

    def test_unparseable_status_time_without_autolabel_is_refused(self) -> None:
        """An invalid update time fails closed."""

        stub = Stub([A], {A: [[other_status("success", "yesterday")]]})
        self.assertEqual(self.run_helper(stub, env_updates={"FFMPEG_FORGEJO_NOW": NOW}), 75)
        self.assertEqual(self.patches(stub), [])

    def test_fresh_status_on_second_page_without_autolabel_is_refused(self) -> None:
        """The newest status on page two determines the age of the head."""

        rows = [other_status("success", "2026-11-06T07:00:00Z", n) for n in range(50)]
        rows.append(other_status("success", "2026-11-06T08:55:00Z", 50))
        stub = Stub([A], {A: [rows]})
        self.assertEqual(self.run_helper(stub, env_updates={"FFMPEG_FORGEJO_NOW": NOW}), 75)
        self.assertEqual(self.patches(stub), [])
        self.assertTrue(any("page=2" in path for _, path, _ in stub.requests))

    def test_twenty_full_status_pages_without_autolabel_are_refused(self) -> None:
        """Twenty full status pages leave Autolabel unknown and forbid an edit."""

        rows = [other_status("success", "2026-11-06T07:00:00Z", n) for n in range(1000)]
        stub = Stub([A], {A: [rows]})
        self.assertEqual(self.run_helper(stub, env_updates={"FFMPEG_FORGEJO_NOW": NOW}), 75)
        self.assertEqual(self.patches(stub), [])
        pages = [
            path for method, path, _ in stub.requests if method == "GET" and "/status?" in path
        ]
        self.assertEqual(len(pages), 20)
        self.assertTrue(pages[-1].endswith("page=20"))
        self.assertFalse(any("page=21" in path for path in pages))

    def test_empty_twentieth_status_page_without_autolabel_allows_edit(self) -> None:
        """An empty twentieth page proves aged terminal statuses are complete."""

        rows = [other_status("success", "2026-11-06T07:00:00Z", n) for n in range(950)]
        stub = Stub([A], {A: [rows]})
        self.assertEqual(self.run_helper(stub, env_updates={"FFMPEG_FORGEJO_NOW": NOW}), 0)
        self.assertEqual(self.patches(stub), [BODY])
        pages = [
            path for method, path, _ in stub.requests if method == "GET" and "/status?" in path
        ]
        self.assertEqual(len(pages), 20)
        self.assertTrue(pages[-1].endswith("page=20"))

    def test_absent_autolabel_age_override_and_invalid_value(self) -> None:
        """A 60-second override permits the edit and a noninteger value fails."""

        row = other_status("success", "2026-11-06T08:59:00Z")
        for age, expected in (("60", 0), ("sixty", 1)):
            with self.subTest(age=age):
                stub = Stub([A], {A: [[row]]})
                env = {"FFMPEG_FORGEJO_NOW": NOW, "FFMPEG_FORGEJO_AUTOLABEL_ABSENT_AGE": age}
                self.assertEqual(self.run_helper(stub, env_updates=env), expected)
                self.assertEqual(self.patches(stub), [BODY] if expected == 0 else [])

    def test_every_terminal_state_allows_the_edit(self) -> None:
        """Every Forgejo terminal status permits a body edit."""

        for status in ("success", "error", "failure", "warning", "skipped"):
            with self.subTest(status=status):
                self.assert_edited(Stub([A], {A: [[label(status), OTHER]]}))

    def test_unknown_state_is_refused(self) -> None:
        """An unrecognized status refuses a body edit."""

        self.assert_refused(Stub([A], {A: [[label("running")]]}))

    def test_any_pending_context_refuses_in_either_order(self) -> None:
        """Any pending Autolabel context wins regardless of response order."""

        self.assert_refused(Stub([A], {A: [[label("success", 0), label("pending", 1)]]}))
        self.assert_refused(Stub([A], {A: [[label("pending", 0), label("success", 1)]]}))

    def test_pending_context_on_a_later_page_is_seen(self) -> None:
        """Pagination cannot hide a pending Autolabel context."""

        filler = [{"context": f"Test / job {n}", "status": "success"} for n in range(50)]
        stub = Stub([A], {A: [[label("success")] + filler + [label("pending", 1)]]})
        self.assert_refused(stub)
        self.assertTrue(any("page=2" in p for _, p, _ in stub.requests))

    def test_wait_polls_until_autolabel_finishes(self) -> None:
        """Wait mode polls until Autolabel reaches a terminal status."""

        stub = Stub([A], {A: [[], [label("pending")], [label("success")]]})
        self.assert_edited(stub, "--wait")
        self.assertGreaterEqual(stub.poll_index[A], 2)

    def test_wait_gives_up_after_80_checks(self) -> None:
        """Wait mode stops after its bounded 80-check poll."""

        stub = Stub([A], {A: [[label("pending")]]})
        self.assert_refused(stub, "--wait")
        self.assertEqual(stub.poll_index[A], 79)

    def test_a_push_during_the_check_is_not_cleared_by_the_old_head(self) -> None:
        """A new push forces Autolabel validation against the new head."""

        polls = {A: [[label("success")]], B: [[label("pending")]]}
        self.assert_refused(Stub([A, B], polls))
        stub = Stub([A, B], {A: [[label("success")]], B: [[label("pending")], [label("success")]]})
        self.assert_edited(stub, "--wait")
        self.assertGreaterEqual(stub.poll_index[B], 1)

    def test_api_failure_never_edits(self) -> None:
        """A status API failure cannot produce a body edit."""

        stub = Stub([A], {A: [[label("success")]]}, fail_status=True)
        self.assertNotEqual(self.run_helper(stub), 0)
        self.assertEqual(self.patches(stub), [])

    def test_a_failing_seq_does_not_skip_the_guard(self) -> None:
        """A broken external seq command cannot bypass the bounded guard."""

        broken = self.tmp / "broken"
        broken.mkdir()
        seq = broken / "seq"
        seq.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        seq.chmod(0o755)
        stub = Stub([A], {A: [[label("pending")]]})
        self.assertEqual(self.run_helper(stub, path_first=broken), 75)
        self.assertEqual(self.patches(stub), [])


class TestClaimHeadTest(ForgejoTest):
    """Validate evidence before accepting claims about a tested head."""

    def evidence(self, head: str = A, content: str | None = None) -> str:
        """Write evidence for a tested head."""

        path = self.tmp / "evidence.md"
        path.write_text(
            content if content is not None else f"- test: fate tree=/w head={head}\n",
            encoding="utf-8",
        )
        return str(path)

    def comment(self, stub: Stub, *args: str) -> subprocess.CompletedProcess[str]:
        """Run a comment command with the shared issue and body arguments."""

        return self.run_command(stub, "comment", *args, "1", str(self.tmp / "body.md"))

    def comment_body(
        self,
        stub: Stub,
        *,
        path_first: Path | None = None,
        env_updates: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run a tested-head comment with the shared issue and body arguments."""

        return self.run_command(
            stub,
            "comment",
            "--tested-head",
            A,
            "--evidence",
            self.evidence(),
            "1",
            str(self.tmp / "body.md"),
            path_first=path_first,
            env_updates=env_updates,
        )

    def edit_body(self, stub: Stub, *args: str) -> subprocess.CompletedProcess[str]:
        """Run an edit-body command with the shared pull and body arguments."""

        return self.run_command(stub, "edit-body", *args, "1", str(self.tmp / "body.md"))

    def posts(self, stub: Stub) -> list[str]:
        """Return bodies sent by issue-comment POST requests."""

        return self.sent_bodies(stub, "POST")

    def patches(self, stub: Stub) -> list[str]:
        """Return bodies sent by pull-body PATCH requests."""

        return self.sent_bodies(stub, "PATCH")

    def test_stale_evidence_is_refused_before_network(self) -> None:
        """A test on another head cannot authorize either write."""

        for command in (self.comment, self.edit_body):
            with self.subTest(command=command.__name__):
                stub = Stub([A], {A: [[label("success")]]})
                evidence = self.evidence(
                    content=f"- test: fate-source rc 0 tree=/x/.lane/build head={STALE}\n"
                )
                proc = command(stub, "--tested-head", A, "--evidence", evidence)
                self.assertEqual(proc.returncode, 76, proc.stderr)
                self.assertIn(f"{evidence}:1", proc.stderr)
                self.assertEqual(stub.requests, [])

    def test_evidence_validation(self) -> None:
        """Invalid or absent evidence cannot reach the API."""

        cases = (
            (f"- test: fate head={A}\n", "tree"),
            ("test: fate tree=/w\n", "head"),
            ("- control: red run\n", "test"),
            ("- test: fate tree=/w head=aaaaaa\n", "head"),
        )
        for content, reason in cases:
            with self.subTest(content=content):
                stub = Stub()
                evidence = self.evidence(content=content)
                proc = self.comment(stub, "--tested-head", A, "--evidence", evidence)
                self.assertEqual(proc.returncode, 76, proc.stderr)
                self.assertIn(evidence, proc.stderr)
                self.assertIn(reason, proc.stderr)
                self.assertEqual(stub.requests, [])

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFOs unavailable")
    def test_fifo_evidence_is_refused(self) -> None:
        """A FIFO is refused without waiting for a writer or contacting the API."""

        path = self.tmp / "fifo"
        os.mkfifo(path)
        stub = Stub()
        proc = self.comment(stub, "--tested-head", A, "--evidence", str(path))
        self.assertEqual(proc.returncode, 76, proc.stderr)
        self.assertIn(f"{path}: evidence is not a regular file", proc.stderr)
        self.assertEqual(stub.requests, [])

    def test_evidence_size_boundary(self) -> None:
        """Evidence at one MiB is accepted and larger evidence is refused."""

        claim = f"- test: fate tree=/w head={A}\n"
        padding = "#" * 200 + "\n"
        remainder = 1048576 - len(claim)
        content = claim + padding * (remainder // len(padding)) + "#" * (remainder % len(padding))
        for extra in ("", "x"):
            with self.subTest(extra=extra):
                stub = Stub([A])
                evidence = self.evidence(content=content + extra)
                proc = self.comment(stub, "--tested-head", A, "--evidence", evidence)
                self.assertEqual(proc.returncode, 76 if extra else 0, proc.stderr)
                if extra:
                    self.assertIn(f"{evidence}: evidence larger than 1 MiB", proc.stderr)
                    self.assertEqual(stub.requests, [])
                else:
                    self.assertEqual(self.posts(stub), [BODY])

    def test_directory_evidence_is_refused(self) -> None:
        """A directory is refused without contacting the API."""

        stub = Stub()
        proc = self.comment(stub, "--tested-head", A, "--evidence", str(self.tmp))
        self.assertEqual(proc.returncode, 76, proc.stderr)
        self.assertIn(f"{self.tmp}: evidence is not a regular file", proc.stderr)
        self.assertEqual(stub.requests, [])

    def test_evidence_line_count_boundary(self) -> None:
        """Ten thousand lines are accepted and an extra line is refused."""

        claim = f"- test: fate tree=/w head={A}\n"
        for count in (10000, 10001):
            with self.subTest(count=count):
                stub = Stub([A])
                evidence = self.evidence(content=claim + "#\n" * (count - 1))
                proc = self.comment(stub, "--tested-head", A, "--evidence", evidence)
                self.assertEqual(proc.returncode, 0 if count == 10000 else 76, proc.stderr)
                if count == 10001:
                    self.assertIn(f"{evidence}:10001: too many lines", proc.stderr)
                    self.assertEqual(stub.requests, [])
                else:
                    self.assertEqual(self.posts(stub), [BODY])

    def test_evidence_line_length_boundary(self) -> None:
        """A 65536-byte line is accepted and a longer line is refused."""

        claim = f"- test: fate tree=/w head={A}\n"
        for length in (65536, 65537):
            with self.subTest(length=length):
                stub = Stub([A])
                evidence = self.evidence(content=claim + "#" * (length - 1) + "\n")
                proc = self.comment(stub, "--tested-head", A, "--evidence", evidence)
                self.assertEqual(proc.returncode, 0 if length == 65536 else 76, proc.stderr)
                if length == 65537:
                    self.assertIn(f"{evidence}:2: line longer than 65536 bytes", proc.stderr)
                    self.assertEqual(stub.requests, [])
                else:
                    self.assertEqual(self.posts(stub), [BODY])

    def test_missing_evidence_file_is_refused(self) -> None:
        """A missing evidence file exits 1 without any request."""

        stub = Stub()
        path = str(self.tmp / "missing")
        proc = self.comment(stub, "--tested-head", A, "--evidence", path)
        self.assertEqual(proc.returncode, 1)
        self.assertIn(path, proc.stderr)
        self.assertEqual(stub.requests, [])

    def test_evidence_prefix_and_control_are_accepted(self) -> None:
        """A matching short head with a control line on another head posts."""

        stub = Stub([A])
        evidence = self.evidence(
            content=f"- control: red run head={STALE}\n  test: fate tree=/w head={A[:12].upper()}\n"
        )
        proc = self.comment(stub, "--tested-head", A, "--evidence", evidence)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.posts(stub), [BODY])

    def test_tested_head_without_evidence_is_usage_error(self) -> None:
        """--tested-head without --evidence is a usage error without any request."""

        for command in (self.comment, self.edit_body):
            with self.subTest(command=command.__name__):
                stub = Stub()
                proc = command(stub, "--tested-head", A)
                self.assertEqual(proc.returncode, 64, proc.stderr)
                self.assertEqual(stub.requests, [])

    def test_stale_tested_head_is_refused_without_post(self) -> None:
        """A stale tested head exits 76 without posting a comment."""

        stub = Stub([LIVE])
        proc = self.comment(stub, "--tested-head", STALE, "--evidence", self.evidence(STALE))
        self.assertEqual(proc.returncode, 76)
        self.assertIn(STALE, proc.stderr)
        self.assertIn(LIVE, proc.stderr)
        self.assertEqual(self.posts(stub), [])

    def test_comment_pull_server_failure_exits_one_without_post(self) -> None:
        """A pull HTTP 500 exits 1 with its status and never posts a comment."""

        stub = Stub(pull_status=500)
        proc = self.comment(stub, "--tested-head", A, "--evidence", self.evidence(A))
        self.assertEqual(proc.returncode, 1)
        self.assertIn(
            f"cannot read the head of #1 (HTTP 500); tested head {A} not checked",
            proc.stderr,
        )
        self.assertEqual(self.posts(stub), [])

    def test_edit_body_pull_auth_failure_exits_one_without_patch(self) -> None:
        """A pull HTTP 401 exits 1 with its status and never patches the body."""

        stub = Stub(pull_status=401)
        proc = self.edit_body(stub, "--tested-head", A, "--evidence", self.evidence(A))
        self.assertEqual(proc.returncode, 1)
        self.assertIn(
            f"cannot read the head of #1 (HTTP 401); tested head {A} not checked",
            proc.stderr,
        )
        self.assertEqual(self.patches(stub), [])

    def test_curlrc_fail_keeps_a_missing_pull_as_exit_76(self) -> None:
        """A curlrc fail option cannot turn a missing pull into a curl failure."""

        (self.tmp / ".curlrc").write_text("fail\n", encoding="utf-8")
        stub = Stub(pull_status=404)
        proc = self.comment_body(
            stub, env_updates={"HOME": str(self.tmp), "CURL_HOME": str(self.tmp)}
        )
        self.assertEqual(proc.returncode, 76, proc.stderr)
        self.assertIn("not a pull request", proc.stderr)
        self.assertEqual(self.posts(stub), [])
        self.assertEqual(self.patches(stub), [])

    def test_curl_transport_failure_exits_one_without_post(self) -> None:
        """A curl transport failure reports its status without attempting a POST."""

        fake_bin = self.tmp / "fake-bin"
        fake_bin.mkdir()
        log = self.tmp / "curl.log"
        curl = fake_bin / "curl"
        curl.write_text(
            '#!/bin/sh\nprintf \'%s\\n\' "$*" >>"$CURL_LOG"\nexit 7\n', encoding="utf-8"
        )
        curl.chmod(0o755)
        stub = Stub()
        proc = self.comment_body(
            stub,
            path_first=fake_bin,
            env_updates={"CURL_LOG": str(log)},
        )
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("curl rc 7", proc.stderr)
        invocations = log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(invocations), 1)
        self.assertIn("-X GET", invocations[0])

    def test_curlrc_header_reaches_the_pull_read(self) -> None:
        """The tested-head pull read preserves headers configured in curlrc."""

        (self.tmp / ".curlrc").write_text('header = "X-Curlrc: 1"\n', encoding="utf-8")
        stub = Stub()
        proc = self.comment_body(
            stub, env_updates={"HOME": str(self.tmp), "CURL_HOME": str(self.tmp)}
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(stub.pull_headers[0].get("X-Curlrc"), "1")

    def assert_unreadable_head(self, live: str | None) -> None:
        """Assert that an unreadable live head prevents a comment."""

        stub = Stub([live])
        proc = self.comment(stub, "--tested-head", A, "--evidence", self.evidence(A))
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("cannot read the head", proc.stderr)
        self.assertEqual(self.posts(stub), [])

    def test_null_live_head_exits_one_without_post(self) -> None:
        """A null live head exits 1 as unreadable without posting a comment."""

        self.assert_unreadable_head(None)

    def test_short_live_head_exits_one_without_post(self) -> None:
        """A short live head exits 1 as unreadable without posting a comment."""

        self.assert_unreadable_head("abc")

    def test_matching_seven_character_live_head_exits_one_without_post(self) -> None:
        """A matching seven-character live head cannot authorize a comment."""

        short = LIVE[:7]
        stub = Stub([short])
        proc = self.comment(stub, "--tested-head", short, "--evidence", self.evidence(short))
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("cannot read the head", proc.stderr)
        self.assertEqual(self.posts(stub), [])

    def test_matching_prefix_and_full_head_are_posted(self) -> None:
        """Both matching head prefixes and full hashes authorize a comment."""

        for tested in (LIVE[:12], LIVE):
            with self.subTest(tested=tested):
                stub = Stub([LIVE])
                proc = self.comment(
                    stub, "--tested-head", tested, "--evidence", self.evidence(tested)
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(self.posts(stub), [BODY])

    def test_short_tested_head_checks_evidence_against_live_head(self) -> None:
        """Both writes reject a different full head sharing the tested prefix."""

        other = LIVE[:7] + ("1" if LIVE[7] != "1" else "2") + LIVE[8:]
        for command, method in ((self.comment, "POST"), (self.edit_body, "PATCH")):
            for evidence_head, expected in ((other, 76), (LIVE, 0), (LIVE[:7], 0)):
                with self.subTest(command=command.__name__, evidence_head=evidence_head):
                    stub = Stub([LIVE], {LIVE: [[label("success")]]})
                    proc = command(
                        stub, "--tested-head", LIVE[:7], "--evidence", self.evidence(evidence_head)
                    )
                    self.assertEqual(proc.returncode, expected, proc.stderr)
                    self.assertEqual(self.sent_bodies(stub, method), [] if expected else [BODY])

    def test_edit_body_rechecks_evidence_after_autolabel(self) -> None:
        """A new head with the same tested prefix cannot authorize a body edit."""

        other = LIVE[:7] + ("1" if LIVE[7] != "1" else "2") + LIVE[8:]
        stub = Stub([LIVE, LIVE, LIVE, other], {LIVE: [[label("success")]]})
        proc = self.edit_body(stub, "--tested-head", LIVE[:7], "--evidence", self.evidence(LIVE))
        self.assertEqual(proc.returncode, 76, proc.stderr)
        self.assertEqual(self.patches(stub), [])

    def test_invalid_tested_heads_are_usage_errors(self) -> None:
        """Short and nonhex tested heads are usage errors without writes."""

        for tested in (LIVE[:6], "not-hex"):
            with self.subTest(tested=tested):
                stub = Stub([LIVE])
                proc = self.comment(
                    stub, "--tested-head", tested, "--evidence", self.evidence(tested)
                )
                self.assertEqual(proc.returncode, 64)
                self.assertIn("usage: comment", proc.stderr)
                self.assertEqual(self.posts(stub), [])

    def test_missing_or_conflicting_claim_options_are_usage_errors(self) -> None:
        """Missing or conflicting claim modes are rejected without writes."""

        for args in (
            (),
            ("--tested-head", LIVE, "--evidence", self.evidence(LIVE), "--no-test-claims"),
        ):
            with self.subTest(args=args):
                stub = Stub([LIVE])
                proc = self.comment(stub, *args)
                self.assertEqual(proc.returncode, 64)
                self.assertIn("usage: comment", proc.stderr)
                self.assertEqual(self.posts(stub), [])

    def test_no_test_claims_posts_without_reading_the_head(self) -> None:
        """A no-test claim posts without reading a pull head."""

        stub = Stub([LIVE])
        proc = self.comment(stub, "--no-test-claims")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(stub.head_reads, 0)
        self.assertEqual(self.posts(stub), [BODY])

    def test_edit_body_uses_head_read_after_autolabel(self) -> None:
        """The final tested-head check rejects a push made during Autolabel checks."""

        stub = Stub([A, A, A, B], {A: [[label("success")]]})
        proc = self.edit_body(stub, "--tested-head", A, "--evidence", self.evidence(A))
        self.assertEqual(proc.returncode, 76)
        self.assertIn(A, proc.stderr)
        self.assertIn(B, proc.stderr)
        self.assertEqual(self.patches(stub), [])

    def test_edit_body_matching_tested_head_is_patched(self) -> None:
        """A matching tested-head prefix authorizes the body PATCH."""

        stub = Stub([A], {A: [[label("success")]]})
        proc = self.edit_body(stub, "--tested-head", A[:12], "--evidence", self.evidence(A[:12]))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.patches(stub), [BODY])

    def test_edit_body_requires_exactly_one_claim_option(self) -> None:
        """Body edits require exactly one claim mode and make no invalid PATCH."""

        for args in ((), ("--tested-head", A, "--evidence", self.evidence(A), "--no-test-claims")):
            with self.subTest(args=args):
                stub = Stub([A], {A: [[label("success")]]})
                proc = self.edit_body(stub, *args)
                self.assertEqual(proc.returncode, 64)
                self.assertIn("usage: edit-body", proc.stderr)
                self.assertEqual(self.patches(stub), [])

    def test_issue_refuses_tested_head_and_accepts_no_test_claims(self) -> None:
        """Issue comments reject tested-head claims but accept no-test claims."""

        stub = Stub(pull_status=404)
        proc = self.comment(stub, "--tested-head", A, "--evidence", self.evidence(A))
        self.assertEqual(proc.returncode, 76)
        self.assertIn("not a pull request", proc.stderr)
        self.assertEqual(self.posts(stub), [])

        stub = Stub(pull_status=404)
        proc = self.comment(stub, "--no-test-claims")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.posts(stub), [BODY])

    def test_issue_edit_body_refuses_tested_head_before_autolabel(self) -> None:
        """An issue body edit exits 76 before status reads and never PATCHes."""

        stub = Stub(pull_status=404)
        proc = self.edit_body(stub, "--tested-head", A, "--evidence", self.evidence(A))
        self.assertEqual(proc.returncode, 76)
        self.assertIn(f"#1 is not a pull request; tested head {A} cannot be checked", proc.stderr)
        self.assertEqual(self.patches(stub), [])


FAIRY_FIXTURE_HEAD = "f" * 40


class FairyRequestTestBase(unittest.TestCase):
    """Use the existing HTTP stub with inert Git and keyring executables."""

    helper: Path = HELPER

    def setUp(self) -> None:
        """Create fixture executables without reading or changing credential settings."""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.bin_dir = self.tmp / "bin"
        self.bin_dir.mkdir()
        for name, body in {
            "secret-tool": "printf 'fixture-token\\n'",
            "git": (
                'case "$1" in fetch|diff) exit 0;; '
                f"rev-parse) echo {FAIRY_FIXTURE_HEAD};; *) exit 97;; esac"
            ),
        }.items():
            tool = self.bin_dir / name
            tool.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
            tool.chmod(0o755)
        (self.tmp / "body.md").write_text(BODY, encoding="utf-8")
        self.sweep = self.tmp / "sweep.md"
        self.sweep.write_text(
            f"class: unchecked-return\nhead: {FAIRY_FIXTURE_HEAD}\nno hits\n", encoding="utf-8"
        )

    def run_command(
        self, stub: Stub, *args: str, env_updates: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        """Run only request commands against localhost with the inherited environment."""
        if not args or args[0] not in {"pr", "fairy-requests"}:
            raise ValueError("unsupported fixture command")
        if args[0] == "pr" and "--sweep" not in args:
            args = ("pr", "--sweep", str(self.sweep), *args[1:])
        if args[0] == "pr" and stub.branch_head is None:
            stub.branch_head = FAIRY_FIXTURE_HEAD
        server = serve(stub)
        try:
            environment = os.environ.copy()
            environment.update(
                PATH=os.pathsep.join([str(self.bin_dir), environment.get("PATH", "")]),
                FFMPEG_FORGEJO_API=f"http://127.0.0.1:{server.server_port}/api/v1",
                FFMPEG_FORGEJO_GIT="fixture-upstream",
                FFMPEG_FORGEJO_POLL="0",
                FFMPEG_FORGEJO_BOT_REVIEWER="Forgejo_Fairy",
                FFMPEG_FORGEJO_OFFLINE="0",
            )
            environment.update(env_updates or {})
            return subprocess.run(  # nosec B603 B607 - fixed bash helper argv, temporary test directory, test literals
                ["bash", str(self.helper), *args],
                env=environment,
                cwd=self.tmp,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        finally:
            server.shutdown()
            server.server_close()


class PullRequestReviewerTest(FairyRequestTestBase):
    def test_pr_refuses_unswept_fork_head(self) -> None:
        """A different fork head cannot create a pull for a validated local commit."""

        stub = Stub(reviewers=[["alice"]], branch_head=A)
        proc = self.pr(stub)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn(FAIRY_FIXTURE_HEAD, proc.stderr)
        self.assertIn(A, proc.stderr)
        self.assertFalse(any(method == "POST" for method, _, _ in stub.requests))

    def test_pr_refuses_unreadable_swept_fork_branch(self) -> None:
        """A missing or unavailable fork branch fails before any POST."""

        for status in (404, 500):
            with self.subTest(status=status):
                stub = Stub(reviewers=[["alice"]], branch_status=status)
                proc = self.pr(stub)
                self.assertEqual(proc.returncode, 1, proc.stderr)
                self.assertIn("fork branch", proc.stderr)
                self.assertIn(FAIRY_FIXTURE_HEAD, proc.stderr)
                self.assertFalse(any(method == "POST" for method, _, _ in stub.requests))

    def test_pr_refuses_missing_fork_commit(self) -> None:
        """A successful read without a commit ID still refuses creation."""

        stub = Stub(reviewers=[["alice"]], branch_head="")
        proc = self.pr(stub)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn(FAIRY_FIXTURE_HEAD, proc.stderr)
        self.assertFalse(any(method == "POST" for method, _, _ in stub.requests))

    def test_pr_accepts_matching_swept_fork_head(self) -> None:
        """The encoded branch in the fork must match before pull creation."""

        stub = Stub(reviewers=[["alice"]])
        proc = self.run_command(stub, "pr", "fix/swept-head", "title", str(self.tmp / "body.md"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        branch_read = ("GET", "/api/v1/repos/tester/FFmpeg/branches/fix%2Fswept-head", "")
        self.assertIn(branch_read, stub.requests)
        post_index = next(
            index
            for index, (method, path, _) in enumerate(stub.requests)
            if method == "POST" and path.endswith("/pulls")
        )
        self.assertLess(stub.requests.index(branch_read), post_index)
        self.assertEqual(json.loads(stub.requests[post_index][2])["head"], "tester:fix/swept-head")

    def pr(self, stub: Stub) -> subprocess.CompletedProcess[str]:
        """Create a pull request and read its requested reviewers back."""

        return self.run_command(
            stub,
            "pr",
            "--sweep",
            str(self.sweep),
            "topic",
            "title",
            str(self.tmp / "body.md"),
        )

    def test_only_fairy_readback_requires_human_reviewer(self) -> None:
        """Fairy alone cannot satisfy the human-reviewer requirement."""

        stub = Stub(reviewers=[["Forgejo_Fairy"]])
        proc = self.pr(stub)
        self.assertEqual(proc.returncode, 3, proc.stderr)
        self.assertIn("has no requested human reviewer", proc.stderr)
        self.assertIn(
            "pick from MAINTAINERS and git log on the touched files, then: "
            "ffmpeg-forgejo reviewers 1 <login>...",
            proc.stderr,
        )
        self.assertNotIn("missing requested reviewer Forgejo_Fairy", proc.stderr)
        self.assertEqual(stub.reviewer_reads, 5)

    def test_human_reviewer_can_appear_on_later_poll(self) -> None:
        """Human-reviewer polling allows delayed CODEOWNERS requests."""

        stub = Stub(reviewers=[[], [], [], ["alice"]])
        proc = self.pr(stub)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(stub.reviewer_reads, 4)

    def test_only_configured_bot_requires_human_reviewer(self) -> None:
        """An overridden bot login cannot count as a human reviewer."""

        stub = Stub(reviewers=[["Other_Bot"]])
        proc = self.run_command(
            stub,
            "pr",
            "topic",
            "title",
            str(self.tmp / "body.md"),
            env_updates={"FFMPEG_FORGEJO_BOT_REVIEWER": "Other_Bot"},
        )
        self.assertEqual(proc.returncode, 3, proc.stderr)
        self.assertIn("has no requested human reviewer", proc.stderr)
        self.assertIn("ffmpeg-forgejo reviewers 1 <login>...", proc.stderr)

    def test_empty_reviewers_three_times_exits_three(self) -> None:
        """Three empty user and team reviewer reads exit 3."""

        stub = Stub(reviewers=[[], [], []])
        stub.drop_reviewers = True
        proc = self.pr(stub)
        self.assertEqual(proc.returncode, 3)
        self.assertEqual(proc.stdout, "stub/pulls/1\n")
        self.assertIn("Forgejo_Fairy", proc.stderr)
        reads = [
            path for method, path, _ in stub.requests if method == "GET" and "/pulls/1" in path
        ]
        self.assertEqual(len(reads), 4)

    def test_requested_reviewers_are_printed(self) -> None:
        """Requested user reviewers are printed after pull creation."""

        stub = Stub(reviewers=[["alice", "bob"]])
        proc = self.pr(stub)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            proc.stdout,
            "stub/pulls/1\n#1 requested reviewers: alice bob Forgejo_Fairy\n",
        )

    def test_requested_team_reviewers_are_printed(self) -> None:
        """A user-only POST preserves persisted CODEOWNERS teams in later GETs."""

        stub = Stub(reviewers=[["team:codec"], ["team:codec"], ["team:codec"]])
        proc = self.pr(stub)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            proc.stdout,
            "stub/pulls/1\n#1 requested reviewers: Forgejo_Fairy team:codec\n",
        )
        self.assertEqual(stub.requested_teams, ["codec"])
        self.assertEqual(stub.pull()["requested_reviewers_teams"], [{"name": "codec"}])
        requests = [
            json.loads(data)
            for method, path, data in stub.requests
            if method == "POST" and path.endswith("requested_reviewers")
        ]
        self.assertEqual(requests, [{"reviewers": ["Forgejo_Fairy"]}])

    def test_dropped_team_is_not_restored_by_readback(self) -> None:
        """A planted POST team loss remains visible to the human-reviewer check."""

        stub = Stub(reviewers=[["team:codec"], ["team:codec"], ["team:codec"]])
        original_post = Handler.do_POST

        def drop_team(handler: Handler) -> None:
            """Plant team loss after a successful review-request POST."""

            original_post(handler)
            if handler.path.endswith("requested_reviewers"):
                handler.stub().requested_teams.clear()

        with unittest.mock.patch.object(Handler, "do_POST", drop_team):
            proc = self.pr(stub)
        self.assertEqual(proc.returncode, 3, proc.stderr)
        self.assertIn("has no requested human reviewer", proc.stderr)
        self.assertNotIn("team:codec", proc.stdout)
        self.assertEqual(stub.requested_teams, [])
        self.assertEqual(stub.pull()["requested_reviewers_teams"], [])

    def test_invalid_bot_login_is_rejected_before_pull_creation(self) -> None:
        """Invalid bot logins cannot create a pull or send a reviewer request."""

        for login in ("bad/login", "bad login", "bad@login"):
            with self.subTest(login=login):
                stub = Stub(reviewers=[["alice"]])
                proc = self.run_command(
                    stub,
                    "pr",
                    "topic",
                    "title",
                    str(self.tmp / "body.md"),
                    env_updates={"FFMPEG_FORGEJO_BOT_REVIEWER": login},
                )
                self.assertEqual(proc.returncode, 1, proc.stderr)
                self.assertIn(f"not a login: {login}", proc.stderr)
                self.assertEqual(stub.requests, [])

    def test_missing_fairy_readback_is_refused(self) -> None:
        """A successful POST dropping Fairy cannot pass on a human reviewer."""
        stub = Stub(reviewers=[["alice"]])
        stub.drop_reviewers = True
        proc = self.pr(stub)
        self.assertEqual(proc.returncode, 3, proc.stderr)
        self.assertIn("missing requested reviewer Forgejo_Fairy after POST", proc.stderr)
        self.assertNotIn("pick from MAINTAINERS", proc.stderr)

    def test_fairy_is_posted_with_codeowners(self) -> None:
        """The explicit request merges Fairy and existing CODEOWNERS users."""
        stub = Stub(reviewers=[["alice", "Forgejo_Fairy"]])
        proc = self.pr(stub)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        requests = [
            json.loads(data)
            for method, path, data in stub.requests
            if method == "POST" and path.endswith("requested_reviewers")
        ]
        self.assertEqual(requests, [{"reviewers": ["alice", "Forgejo_Fairy"]}])

    def test_bot_login_override(self) -> None:
        """The configured bot replaces the default in both request and read-back."""
        stub = Stub(reviewers=[["alice"]])
        proc = self.run_command(
            stub,
            "pr",
            "topic",
            "title",
            str(self.tmp / "body.md"),
            env_updates={"FFMPEG_FORGEJO_BOT_REVIEWER": "Other_Bot"},
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(stub.requested_users, ["alice", "Other_Bot"])

    def test_request_http_failure_is_refused(self) -> None:
        """A rejected review request fails PR creation's follow-up step."""
        stub = Stub(reviewers=[["alice"]])
        stub.request_status = 500
        proc = self.pr(stub)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("500", proc.stderr)


class FairyRequestsTest(FairyRequestTestBase):
    def test_pending_fairy_review_keeps_candidate_listed(self) -> None:
        """A pending Fairy review without a submission cannot clear an overdue pull."""

        stub = Stub()
        pull = open_pull(number=2)
        pull.update(created_at="2000-01-01T00:00:00Z", title="fixture", requested_reviewers=[])
        stub.open_pulls = [pull]
        stub.reviews[2] = [fairy_review("PENDING", submitted_at="")]
        proc = self.run_command(stub, "fairy-requests")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        number, age, title = proc.stdout.strip().split("\t")
        self.assertEqual(number, "2")
        self.assertGreater(int(age), 15)
        self.assertEqual(title, "fixture")

    def test_submitted_configured_bot_reviews_clear_candidate(self) -> None:
        """Multiple submitted reviews from the configured bot clear an overdue pull."""

        stub = Stub()
        pull = open_pull(number=2)
        pull.update(created_at="2000-01-01T00:00:00Z", title="fixture", requested_reviewers=[])
        stub.open_pulls = [pull]
        stub.reviews[2] = [fairy_review(login="Other_Bot")] * 2
        proc = self.run_command(
            stub,
            "fairy-requests",
            env_updates={"FFMPEG_FORGEJO_BOT_REVIEWER": "Other_Bot"},
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "")

    def test_configured_repository_is_used_for_pulls_and_reviews(self) -> None:
        """The audit lists pulls and their reviews only in the configured repository."""

        stub = Stub()
        pull = open_pull(number=2)
        pull.update(created_at="2000-01-01T00:00:00Z", title="fixture", requested_reviewers=[])
        stub.open_pulls = [pull]
        stub.reviews[2] = [fairy_review()]
        proc = self.run_command(
            stub, "fairy-requests", env_updates={"FFMPEG_FORGEJO_REPO": "example/custom"}
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        repository_paths = [
            urllib.parse.urlsplit(path).path
            for _, path, _ in stub.requests
            if path.startswith("/api/v1/repos/")
        ]
        for endpoint in ("pulls", "pulls/2/reviews"):
            with self.subTest(endpoint=endpoint):
                self.assertEqual(
                    [path for path in repository_paths if path.endswith(f"/{endpoint}")],
                    [f"/api/v1/repos/example/custom/{endpoint}"],
                )


class PullRequestOverlapTest(ForgejoTest):
    def pr(self, stub: Stub, *options: str) -> subprocess.CompletedProcess[str]:
        """Attempt to create the shared topic pull request."""

        return self.run_command(
            stub,
            "pr",
            "--sweep",
            str(self.sweep),
            *options,
            "topic",
            "title",
            str(self.tmp / "body.md"),
        )

    def pull_posts(self, stub: Stub) -> list[str]:
        """Return pull creation request bodies."""

        return [
            data
            for method, path, data in stub.requests
            if method == "POST" and path.endswith("/pulls")
        ]

    def test_own_open_pull_with_shared_file_is_refused(self) -> None:
        """An own open pull touching the candidate file exits 77 without a POST."""

        stub = overlap_stub(open_pulls=[open_pull()], pull_files={25612: [OVERLAP_FILE]})
        proc = self.pr(stub)
        self.assertEqual(proc.returncode, 77, proc.stderr)
        self.assertIn("#25612", proc.stderr)
        self.assertIn(OVERLAP_FILE, proc.stderr)
        self.assertEqual(self.pull_posts(stub), [])

    def test_own_open_pull_renaming_shared_file_is_refused(self) -> None:
        """An own open pull renaming the candidate file exits 77 without a POST."""

        changed_file = {
            "filename": "libavfilter/vf_new.c",
            "previous_filename": OVERLAP_FILE,
        }
        stub = overlap_stub(open_pulls=[open_pull()], pull_files={25612: [changed_file]})
        proc = self.pr(stub)
        self.assertEqual(proc.returncode, 77, proc.stderr)
        self.assertIn("#25612", proc.stderr)
        self.assertEqual(self.pull_posts(stub), [])

    def test_own_open_pull_bare_rename_fails_closed(self) -> None:
        """A renamed file without its previous name exits one without a POST."""

        changed_file = {"filename": "libavfilter/vf_new.c", "status": "renamed"}
        stub = overlap_stub(open_pulls=[open_pull()], pull_files={25612: [changed_file]})
        proc = self.pr(stub)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("cannot parse files", proc.stderr)
        self.assertEqual(self.pull_posts(stub), [])

    def test_nonoverlapping_open_pull_allows_creation(self) -> None:
        """An own open pull touching another file does not block creation."""

        stub = overlap_stub(
            reviewers=[["alice"]],
            open_pulls=[open_pull()],
            pull_files={25612: ["libavfilter/vf_other.c"]},
        )
        proc = self.pr(stub)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(self.pull_posts(stub)), 1)

    def test_full_file_page_followed_by_null_allows_creation(self) -> None:
        """A full first file page followed by a null page ends the listing and allows creation."""

        files = [f"libavfilter/other_{number}.c" for number in range(50)]
        stub = overlap_stub(
            reviewers=[["alice"]], open_pulls=[open_pull()], pull_files={25612: files}
        )
        proc = self.pr(stub)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(self.pull_posts(stub)), 1)

    def test_null_first_file_page_fails_closed(self) -> None:
        """A null first file page exits one as unparsable without a POST."""

        stub = overlap_stub(open_pulls=[open_pull()])
        with unittest.mock.patch.object(stub, "files", return_value=None):
            proc = self.pr(stub)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("cannot parse files", proc.stderr)
        self.assertEqual(self.pull_posts(stub), [])

    def test_upstream_base_avoids_stale_local_master(self) -> None:
        """The guard diffs against the fetched upstream base, not a stale local master."""

        upstream_work = self.tmp / "upstream-work"
        git(self.tmp, "clone", "-q", str(self.upstream), str(upstream_work))
        upstream_work.joinpath("upstream-file").write_text("upstream\n", encoding="utf-8")
        git(upstream_work, "add", "upstream-file")
        git(
            upstream_work,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "commit.gpgSign=false",
            "commit",
            "-qm",
            "advance upstream",
        )
        git(upstream_work, "push", "-q", "origin", "master")
        master = git_output(self.repo, "rev-parse", "master")
        git(self.repo, "fetch", "-q", str(self.upstream), "refs/heads/master")
        git(self.repo, "reset", "--hard", "-q", "FETCH_HEAD")
        self.repo.joinpath(OVERLAP_FILE).write_text("topic on upstream\n", encoding="utf-8")
        git(self.repo, "add", OVERLAP_FILE)
        git(
            self.repo,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "commit.gpgSign=false",
            "commit",
            "-qm",
            "new topic",
        )
        sha = git_output(self.repo, "rev-parse", "topic")
        self.sweep.write_text(f"class: unchecked-return\nhead: {sha}\nno hits\n", encoding="utf-8")
        stub = overlap_stub(
            reviewers=[["alice"]], open_pulls=[open_pull()], pull_files={25612: ["upstream-file"]}
        )
        proc = self.pr(stub)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(self.pull_posts(stub)), 1)
        after = git_output(self.repo, "rev-parse", "master")
        self.assertEqual(after, master)

    def test_unreachable_upstream_fails_closed(self) -> None:
        """An unreachable upstream base exits one without a POST."""

        stub = overlap_stub(reviewers=[["alice"]])
        missing = str(self.tmp / "missing.git")
        proc = self.run_command(
            stub,
            "pr",
            "--sweep",
            str(self.sweep),
            "topic",
            "title",
            str(self.tmp / "body.md"),
            env_updates={"FFMPEG_FORGEJO_GIT": missing},
        )
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn(f"cannot fetch master from {missing}; overlap not proven absent", proc.stderr)
        self.assertEqual(self.pull_posts(stub), [])

    def test_other_login_overlap_allows_creation(self) -> None:
        """The poster filter excludes an overlapping pull from another login."""

        stub = overlap_stub(
            reviewers=[["alice"]],
            open_pulls=[open_pull(login="other")],
            pull_files={25612: [OVERLAP_FILE]},
        )
        proc = self.pr(stub)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(self.pull_posts(stub)), 1)
        list_paths = [
            path for method, path, _ in stub.requests if method == "GET" and "/pulls?" in path
        ]
        self.assertTrue(
            any("state=open" in path and "poster=tester" in path for path in list_paths)
        )

    def test_exact_overlap_override_allows_creation(self) -> None:
        """Only an overlap with the explicitly allowed pull is accepted."""

        allowed = overlap_stub(
            reviewers=[["alice"]],
            open_pulls=[open_pull()],
            pull_files={25612: [OVERLAP_FILE]},
        )
        self.assertEqual(self.pr(allowed, "--overlap-ok", "25612").returncode, 0)
        refused = overlap_stub(open_pulls=[open_pull()], pull_files={25612: [OVERLAP_FILE]})
        proc = self.pr(refused, "--overlap-ok", "999")
        self.assertEqual(proc.returncode, 77, proc.stderr)
        self.assertEqual(self.pull_posts(refused), [])

    def test_probe_failures_never_create_a_pull(self) -> None:
        """Pull-list and pull-file probe failures exit 1 without a POST."""

        cases = (
            overlap_stub(fail_pulls=True),
            overlap_stub(open_pulls=[open_pull()], fail_files={25612}),
        )
        for stub in cases:
            with self.subTest(stub=stub):
                proc = self.pr(stub)
                self.assertEqual(proc.returncode, 1, proc.stderr)
                self.assertEqual(self.pull_posts(stub), [])

    def test_overlap_on_second_file_page_is_refused(self) -> None:
        """An overlap on the second changed-file page cannot evade the guard."""

        files = [f"libavfilter/filler_{number}.c" for number in range(50)] + [OVERLAP_FILE]
        stub = overlap_stub(open_pulls=[open_pull()], pull_files={25612: files})
        proc = self.pr(stub)
        self.assertEqual(proc.returncode, 77, proc.stderr)
        self.assertTrue(
            any("/pulls/25612/files?" in path and "page=2" in path for _, path, _ in stub.requests)
        )
        self.assertEqual(self.pull_posts(stub), [])


class SweepTest(ForgejoTest):
    """Require a reviewed sweep before either outbound operation."""

    helper = HELPER

    def test_sweep_ignores_cwd_stdlib_decoys(self) -> None:
        """Valid sweeps ignore checkout modules shadowing the Python standard library."""

        marker = self.repo / "stdlib-decoy-executed"
        decoy = f"open({str(marker)!r}, 'w').write('executed')\nraise SystemExit(99)\n"
        for name in ("pathlib.py", "importlib.py"):
            (self.repo / name).write_text(decoy, encoding="utf-8")
        result = self.sweep_command("push", "--sweep", str(self.sweep))
        self.assertFalse(marker.exists(), "checkout-level standard library decoy executed")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("stub push", result.stdout)

    def test_push_uses_swept_sha_refspec(self) -> None:
        """Push uses the validated commit ID and preserves its explicit lease."""

        sha = git_output(self.repo, "rev-parse", "topic")
        result = self.sweep_command("push", "--lease", A, "--sweep", str(self.sweep))
        self.assertEqual(result.returncode, 0, result.stderr)
        argv = json.loads((self.tmp / "push-argv.json").read_text(encoding="utf-8"))
        self.assertEqual(argv[-1], f"{sha}:refs/heads/topic")
        self.assertIn(f"--force-with-lease=refs/heads/topic:{A}", argv)

    def test_sweep_accepts_helper_symlink(self) -> None:
        """A helper symlink outside scripts still imports the sweep validator."""

        self.helper = self.bin_dir / "ffmpeg-forgejo"
        self.helper.symlink_to(HELPER)
        result = self.sweep_command("push", "--sweep", str(self.sweep))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("stub push", result.stdout)

    def test_sweep_names_missing_module(self) -> None:
        """A helper missing its validator reports the module and refuses the push."""

        self.helper = self.bin_dir / "ffmpeg-forgejo"
        shutil.copyfile(HELPER, self.helper)
        result = self.sweep_command("push", "--sweep", str(self.sweep))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(
            f"sweep: missing validator: {self.bin_dir / 'variant_sweep.py'}", result.stderr
        )
        self.assertEqual(self.sweep_requests, [])
        self.assertNotIn("stub push", result.stdout)

    def test_sweep_missing_sibling_rejects_cwd_decoy(self) -> None:
        """A missing sibling fails by name without executing a checkout-level decoy."""

        self.helper = self.bin_dir / "ffmpeg-forgejo"
        shutil.copyfile(HELPER, self.helper)
        marker = self.repo / "decoy-executed"
        (self.repo / "variant_sweep.py").write_text(
            "from pathlib import Path\n"
            f"Path({str(marker)!r}).write_text('executed')\n"
            "def validate_report(path, head):\n"
            '    """Pretend every report is valid after recording decoy execution."""\n'
            "    return True\n",
            encoding="utf-8",
        )
        for command in ("push", "pr"):
            with self.subTest(command=command):
                result = self.sweep_command(command, "--sweep", str(self.sweep))
                self.assertFalse(marker.exists(), "checkout-level decoy executed")
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn(
                    f"sweep: missing validator: {self.bin_dir / 'variant_sweep.py'}", result.stderr
                )
                self.assertEqual(self.sweep_requests, [])
                self.assertNotIn("stub push", result.stdout)

    def test_sweep_rejects_non_table_findings(self) -> None:
        """Malformed findings and unrecognized text cannot disappear during validation."""

        original = self.sweep.read_text(encoding="utf-8")
        for line in (
            "sibling.c:4 | play | discarded | no | TODO |",
            "unexpected text",
            "no hits trailing junk",
            "| sibling.c:4 | play | discarded | no | TODO",
        ):
            with self.subTest(line=line):
                self.sweep.write_text(original + line + "\n", encoding="utf-8")
                self.assert_sweep_refused("--sweep", str(self.sweep))

    def test_sweep_rejects_metadata_only_prefix(self) -> None:
        """A metadata-only prefix is incomplete rather than a reviewed empty report."""

        self.sweep.write_text(
            self.sweep.read_text(encoding="utf-8").replace("no hits\n", ""), encoding="utf-8"
        )
        self.assert_sweep_refused("--sweep", str(self.sweep))

    def test_sweep_report_size_limit(self) -> None:
        """An oversized report is refused by name before outbound operations."""

        self.sweep.write_text(
            self.sweep.read_text(encoding="utf-8") + " " * (4 * 1024 * 1024), encoding="utf-8"
        )
        self.assert_sweep_refused("--sweep", str(self.sweep))
        result = self.sweep_command("push", "--sweep", str(self.sweep))
        self.assertIn("report size limit", result.stderr)

    def sweep_command(
        self, command: str, *options: str, stub: Stub | None = None
    ) -> subprocess.CompletedProcess[str]:
        """Attempt an outbound operation with API and Git push stubbed."""

        real_git = shutil.which("git")
        if real_git is None:
            raise RuntimeError("git missing")
        fake = self.bin_dir / "git"
        fake.write_text(
            "#!/usr/bin/env python3\nimport json, os, sys\n"
            'if "push" in sys.argv:\n'
            f"    with open({str(self.tmp / 'push-argv.json')!r}, 'w') as record:\n"
            "        json.dump(sys.argv[1:], record)\n"
            '    print("stub push")\n    sys.exit(0)\n'
            f"os.execv({real_git!r}, [{real_git!r}, *sys.argv[1:]])\n",
            encoding="utf-8",
        )
        fake.chmod(0o755)
        stub = stub or Stub(reviewers=[["alice"]])
        positional = ["topic"]
        if command == "pr":
            positional += ["title", str(self.tmp / "body.md")]
        result = self.run_command(stub, command, *options, *positional)
        self.sweep_requests = stub.requests
        return result

    def test_push_refuses_unrecorded_own_next_push_promise(self) -> None:
        """An own next-push promise blocks a push without a promise file."""

        stub = Stub()
        base = git_output(self.repo, "rev-parse", "master")
        stub.open_pulls = [
            {
                "number": 25619,
                "user": {"login": "tester"},
                "head": {"ref": "topic"},
                "merge_base": base,
                "body": "Unrelated body.",
            }
        ]
        stub.comments[25619] = [
            {
                "id": 81815,
                "user": {"login": "tester"},
                "body": "I will qualify the close errors in the commit message with the next push.",
                "html_url": "https://example.invalid/comments/81815",
            }
        ]
        result = self.sweep_command("push", "--sweep", str(self.sweep), stub=stub)
        self.assertEqual(result.returncode, 79, result.stderr)
        self.assertIn("81815", result.stderr)
        self.assertFalse((self.tmp / "push-argv.json").exists())

    def promise_stub(
        self,
        *,
        body: str = "Unrelated body.",
        comment: str = "I will qualify the close errors in the commit message with the next push.",
        login: str = "tester",
        max_response_items: int = 50,
    ) -> Stub:
        """Build an open pull and a scripted next-push comment."""

        stub = Stub(max_response_items=max_response_items)
        stub.open_pulls = [
            {
                "number": 25619,
                "user": {"login": "tester"},
                "head": {"ref": "topic"},
                "merge_base": git_output(self.repo, "rev-parse", "master"),
                "body": body,
            }
        ]
        stub.comments[25619] = [
            {
                "id": 81815,
                "user": {"login": login},
                "body": comment,
                "html_url": "https://example.invalid/comments/81815",
            }
        ]
        return stub

    def promise_file(self, content: str = "81815: qualify the close errors\n") -> Path:
        """Write a promise-row fixture in the temporary test directory."""

        path = self.tmp / "promises.txt"
        path.write_text(content, encoding="utf-8")
        return path

    def promise_push(
        self, stub: Stub, path: Path | None = None
    ) -> subprocess.CompletedProcess[str]:
        """Attempt a stubbed push with an optional promise-row file."""

        options = ["--sweep", str(self.sweep)]
        if path is not None:
            options += ["--promises", str(path)]
        return self.sweep_command("push", *options, stub=stub)

    def assert_promise_refused(self, stub: Stub, path: Path | None, detail: str) -> None:
        """Assert promise rejection precedes the stubbed Git push."""

        result = self.promise_push(stub, path)
        self.assertEqual(result.returncode, 79, result.stderr)
        self.assertIn(detail, result.stderr)
        self.assertFalse((self.tmp / "push-argv.json").exists())

    def test_push_refuses_fragment_absent_from_messages_and_body(self) -> None:
        """A row without literal evidence in either location blocks the push."""

        self.assert_promise_refused(self.promise_stub(), self.promise_file(), "fragment absent")

    def test_push_accepts_fragment_in_commit_message(self) -> None:
        """A normalized fragment in one pushed commit message satisfies a promise."""

        git(
            self.repo,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "commit.gpgSign=false",
            "commit",
            "--allow-empty",
            "-qm",
            "qualify  the close\nerrors",
        )
        sha = git_output(self.repo, "rev-parse", "topic")
        self.sweep.write_text(f"class: unchecked-return\nhead: {sha}\nno hits\n", encoding="utf-8")
        result = self.promise_push(self.promise_stub(), self.promise_file())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.tmp / "push-argv.json").exists())

    def test_push_accepts_fragment_only_in_pr_body(self) -> None:
        """The PR body can supply literal promise evidence."""

        result = self.promise_push(
            self.promise_stub(body="We will qualify the close errors in this patch."),
            self.promise_file(),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.tmp / "push-argv.json").exists())

    def test_push_ignores_other_login_promise(self) -> None:
        """A promise from another login imposes no row requirement."""

        result = self.promise_push(self.promise_stub(login="reviewer"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.tmp / "push-argv.json").exists())

    def test_push_ignores_own_non_promise_comment(self) -> None:
        """An own comment without next-push language permits a push."""

        result = self.promise_push(self.promise_stub(comment="I will push"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.tmp / "push-argv.json").exists())

    def test_push_refuses_row_for_non_promise_comment(self) -> None:
        """A row naming an own comment without a promise is refused."""

        stub = self.promise_stub(body="qualify the close errors", comment="I will push")
        stub.comments[25619].append(
            {
                "id": 81816,
                "user": {"login": "tester"},
                "body": "I'll qualify it in my next push.",
                "html_url": "https://example.invalid/comments/81816",
            }
        )
        self.assert_promise_refused(stub, self.promise_file(), "not an own next-push promise")

    def test_push_enforces_promise_on_second_comment_page(self) -> None:
        """A promise after one full comment page still blocks an unrecorded push."""

        stub = self.promise_stub(max_response_items=1)
        stub.comments[25619].insert(
            0,
            {
                "id": 81814,
                "user": {"login": "tester"},
                "body": "No promise here.",
            },
        )
        self.assert_promise_refused(stub, None, "81815")
        self.assertTrue(any("page=2" in path for _, path, _ in stub.requests))

    def test_push_fragment_length_boundary_and_contraction(self) -> None:
        """Seven non-space characters fail; eight satisfy an I'll promise."""

        stub = self.promise_stub(body="abcdefgh", comment="I'll qualify it in my next push.")
        path = self.promise_file("81815: abcdefg\n")
        self.assert_promise_refused(stub, path, "malformed row")
        path.write_text("81815: abcdefgh\n", encoding="utf-8")
        result = self.promise_push(stub, path)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_push_comment_read_failure_stops_push(self) -> None:
        """A failed comment API read never reaches Git push."""

        stub = self.promise_stub()
        stub.fail_comments.add(25619)
        result = self.promise_push(stub)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.tmp / "push-argv.json").exists())

    def test_push_without_open_pull_proceeds(self) -> None:
        """A branch without an open pull carries no promise check."""

        result = self.promise_push(Stub())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.tmp / "push-argv.json").exists())

    def test_push_refuses_malformed_open_pull_rows(self) -> None:
        """Every malformed pull row fails closed before comment selection."""

        for row in (
            {"head": {}},
            {"head": {"ref": ""}, "user": {"login": "tester"}},
            {"head": {"ref": "other"}, "user": {}},
            "bad row",
        ):
            with self.subTest(row=row):
                stub = self.promise_stub()
                stub.open_pulls.insert(0, row)
                self.assert_promise_refused(stub, None, "malformed")

    def test_push_refuses_fifo_and_oversize_promise_file(self) -> None:
        """Promise input must be a regular file of at most 64 KiB."""

        stub = self.promise_stub()
        fifo = self.tmp / "promise.fifo"
        os.mkfifo(fifo)
        self.assert_promise_refused(stub, fifo, "not a regular file")
        path = self.promise_file("#" + "x" * 65536)
        self.assert_promise_refused(stub, path, "above 64 KiB")

    def test_push_refuses_fragment_split_across_commit_messages(self) -> None:
        """Adjacent commit messages cannot combine to satisfy one fragment."""

        for message in ("errors now", "qualify close"):
            git(
                self.repo,
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "-c",
                "commit.gpgSign=false",
                "commit",
                "--allow-empty",
                "-qm",
                message,
            )
        sha = git_output(self.repo, "rev-parse", "topic")
        self.sweep.write_text(f"class: unchecked-return\nhead: {sha}\nno hits\n", encoding="utf-8")
        self.assert_promise_refused(
            self.promise_stub(),
            self.promise_file("81815: qualify close errors\n"),
            "fragment absent",
        )

    def test_push_refuses_missing_row_and_invalid_file_rows(self) -> None:
        """Missing, malformed, and unrelated rows all block an own promise."""

        for content, detail in (
            ("# empty\n", "missing row"),
            ("not a row\n", "malformed row"),
            ("81816: unrelated content\n", "not an own next-push promise"),
        ):
            with self.subTest(content=content):
                self.assert_promise_refused(self.promise_stub(), self.promise_file(content), detail)

    def test_push_refuses_invalid_utf8_and_too_many_rows(self) -> None:
        """Invalid UTF-8 and more than 200 rows fail before the push."""

        path = self.promise_file()
        path.write_bytes(b"\xff")
        self.assert_promise_refused(self.promise_stub(), path, str(path))
        path.write_text("".join(f"{n}: eightchars\n" for n in range(1, 202)), encoding="utf-8")
        self.assert_promise_refused(self.promise_stub(), path, "above 200 rows")

    def test_push_refuses_nonlocal_merge_base(self) -> None:
        """A PR merge base absent from local Git cannot satisfy a promise."""

        stub = self.promise_stub(body="qualify the close errors")
        stub.open_pulls[0]["merge_base"] = B
        self.assert_promise_refused(stub, self.promise_file(), "not a local commit")

    def test_push_refuses_multiple_own_pulls(self) -> None:
        """Ambiguous own open pull selection blocks a push."""

        stub = self.promise_stub()
        stub.open_pulls.append(dict(stub.open_pulls[0], number=25620))
        self.assert_promise_refused(stub, None, "ambiguous")

    def assert_sweep_refused(self, *options: str) -> None:
        """Both operations fail locally with a sweep-specific error."""

        for command in ("pr", "push"):
            with self.subTest(command=command):
                result = self.sweep_command(command, *options)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("sweep", result.stderr)
                self.assertEqual(self.sweep_requests, [])
                self.assertNotIn("stub push", result.stdout)

    def test_sweep_required(self) -> None:
        """Missing sweep refuses both operations before API or push."""

        self.assert_sweep_refused()

    def test_sweep_missing_file(self) -> None:
        """An absent report is never accepted."""

        self.assert_sweep_refused("--sweep", str(self.tmp / "missing.md"))

    def test_sweep_head_mismatch(self) -> None:
        """A reviewed report for another commit is stale."""

        self.sweep.write_text(f"class: unchecked-return\nhead: {A}\nno hits\n", encoding="utf-8")
        self.assert_sweep_refused("--sweep", str(self.sweep))

    def test_sweep_missing_class(self) -> None:
        """A report without its bug class is refused."""

        self.sweep.write_text(
            self.sweep.read_text(encoding="utf-8").replace("class: unchecked-return\n", ""),
            encoding="utf-8",
        )
        self.assert_sweep_refused("--sweep", str(self.sweep))

    def test_sweep_todo(self) -> None:
        """An unresolved table disposition blocks outbound operations."""

        self.add_sweep_rows("TODO")
        self.assert_sweep_refused("--sweep", str(self.sweep))

    def test_sweep_bad_disposition(self) -> None:
        """Only the two named disposition forms are accepted."""

        for value in ("ignore", "", "follow-up:", "fold later", "TODO follow-up: x"):
            with self.subTest(value=value):
                self.sweep.write_text(
                    self.sweep.read_text(encoding="utf-8").split("|", 1)[0], encoding="utf-8"
                )
                self.add_sweep_rows(value)
                self.assert_sweep_refused("--sweep", str(self.sweep))

    def add_sweep_rows(self, *values: str) -> None:
        """Append table headers and planted disposition rows."""

        with self.sweep.open("a") as report:
            report.write("| file:line | callee | kind | same file as fix | disposition |\n")
            report.write("| --- | --- | --- | --- | --- |\n")
            for value in values:
                report.write(f"| sibling.c:4 | play | discarded | no | {value} |\n")

    def test_sweep_reviewed(self) -> None:
        """Reviewed fold and follow-up rows allow PR and leased push."""

        self.add_sweep_rows("fold", "follow-up: evidence/ancestor-reproducer.py")
        for command in ("pr", "push"):
            with self.subTest(command=command):
                options = ["--sweep", str(self.sweep)]
                if command == "push":
                    options = ["--lease", A, *options]
                result = self.sweep_command(command, *options)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(self.sweep_requests)


MUTANTS = {
    "guard removed": (
        'if [ "$state" = "done" ]; then',
        "if true; then",
        "test_pending_and_none_are_refused",
    ),
    "head not rechecked": (
        '[ "$(head_sha "$1")" = "$sha" ] && break',
        "break",
        "test_a_push_during_the_check_is_not_cleared_by_the_old_head",
    ),
    "first page only": (
        "page <= 20; page++",
        "page <= 1; page++",
        "test_pending_context_on_a_later_page_is_seen",
    ),
    "terminal set narrowed": (
        '"warning", "skipped"}',
        "}",
        "test_every_terminal_state_allows_the_edit",
    ),
    "wait ignored": (
        'if [ "$wait" = 0 ] || [ "$i" = 80 ]; then',
        "if true; then",
        "test_wait_polls_until_autolabel_finishes",
    ),
    "seq loop": (
        "for ((i = 1; i <= 80; i++)); do",
        "for i in $(seq 1 80); do",
        "test_a_failing_seq_does_not_skip_the_guard",
    ),
}


@contextlib.contextmanager
def mutant_case(
    source: str,
    old: str,
    new: str,
    base: type[unittest.TestCase],
    name: str,
) -> Iterator[type[unittest.TestCase]]:
    """Write one mutated helper and yield its temporary test case class."""

    with tempfile.TemporaryDirectory() as tmp:
        mutant = Path(tmp) / "ffmpeg-forgejo"
        mutant.write_text(source.replace(old, new), encoding="utf-8")
        yield type(name, (base,), {"helper": mutant})


class MutantTest(unittest.TestCase):
    """Each planted defect in the guard must make at least one EditBodyTest case fail."""

    def test_every_mutant_is_caught(self) -> None:
        """Every planted guard defect is killed by its selected edit-body case."""

        source = HELPER.read_text(encoding="utf-8")
        for name, (old, new, test_name) in MUTANTS.items():
            with self.subTest(mutant=name):
                self.assertEqual(source.count(old), 1, "mutation site missing")
                with mutant_case(source, old, new, EditBodyTest, "Mutated") as case:
                    result = unittest.TestResult()
                    case(test_name).run(result)
                    self.assertTrue(result.failures or result.errors, f"{name} survived")


class FairyReviewsMutantTest(unittest.TestCase):
    def test_naive_fairy_row_counter_is_caught(self) -> None:
        """The requested-only negative kills a counter accepting every Fairy row."""

        old = '        and row["state"] in ALLOWED_STATES\n        and row.get("submitted_at")'
        source = HELPER.read_text(encoding="utf-8")
        self.assertEqual(source.count(old), 1, "mutation site missing")
        source = source.replace(
            'key=lambda row: datetime.fromisoformat(row["submitted_at"])',
            'key=lambda row: row["submitted_at"]',
        )
        with mutant_case(source, old, "", FairyReviewsTest, "NaiveFairyCounter") as case:
            result = unittest.TestResult()
            case("test_request_review_is_not_submitted").run(result)
            self.assertEqual(len(result.failures), 1, "naive Fairy counter survived")
            self.assertEqual(result.errors, [])
            self.assertIn("AssertionError: '1\\t1\\tREQUEST_REVIEW", result.failures[0][1])


class PullOverlapMutantTest(unittest.TestCase):
    """Check that pagination and upstream regressions are detected."""

    def test_overlap_mutants_are_caught(self) -> None:
        """Each planted pagination or base defect fails at least one overlap case."""

        source = HELPER.read_text(encoding="utf-8")
        mutations = (
            (
                "if rows is None and int(sys.argv[2]) >= 2: rows = []",
                "",
                (
                    "test_nonoverlapping_open_pull_allows_creation",
                    "test_full_file_page_followed_by_null_allows_creation",
                ),
            ),
            (
                "if rows is None and int(sys.argv[2]) >= 2: rows = []",
                "if rows is None: rows = []",
                ("test_null_first_file_page_fails_closed",),
            ),
            (
                '"FETCH_HEAD...$branch"',
                '"$BASE...$branch"',
                ("test_upstream_base_avoids_stale_local_master",),
            ),
        )
        for old, new, cases in mutations:
            self.assertEqual(source.count(old), 1, "mutation site missing")
            with mutant_case(source, old, new, PullRequestOverlapTest, "MutatedOverlap") as case:
                for name in cases:
                    with self.subTest(test=name):
                        result = unittest.TestResult()
                        case(name).run(result)
                        self.assertTrue(result.failures or result.errors, f"{name} survived")


if __name__ == "__main__":
    unittest.main()
