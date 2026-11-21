# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Tests for scripts/ffmpeg-forgejo against a stub Forgejo API."""

from __future__ import annotations

import contextlib
import copy
import http.server
import json
import os
import re
import shlex
import shutil
import subprocess  # nosec B404 - fixed git and helper argv in temporary test repositories.
import tempfile
import threading
import unittest
import unittest.mock
import urllib.parse
from collections.abc import Iterator
from pathlib import Path

from scripts import review_classes, unit_test_budget
from tests.helpers import full_sweep_report, isolated_git_environment

ROOT = Path(__file__).resolve().parent.parent
HELPER = ROOT / "scripts" / "ffmpeg-forgejo"
A = "a" * 40
B = "b" * 40
MERGE_BASE = "c" * 40
LIVE = "77c2f4b1c12b" + "0" * 28
STALE = "c1f8e9c2a0"
BODY = "new body\nline two\n"
NOW = "2026-11-06T09:00:00Z"
OVERLAP_FILE = "libavfilter/vf_find_rect.c"
HELPER_SUBCOMMANDS = frozenset(
    {"close", "comment", "edit-body", "issue", "pr", "push", "fairy-reviews", "overlap", "reply"}
)


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


def open_pull(
    number: int = 25612,
    login: str = "tester",
    head_sha: str | None = None,
    head_ref: str | None = None,
) -> dict:
    """Build one open pull request list item with an optional head ref and label."""

    sha = head_sha if head_sha is not None else f"{number:040x}"
    head = {"sha": sha}
    if head_ref is not None:
        head["ref"] = head_ref
        head["label"] = f"{login}:{head_ref}"
    return {
        "number": number,
        "state": "open",
        "user": {"login": login},
        "head": head,
    }


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


def copy_sweep_validator(scripts: Path) -> None:
    """Copy the validator, class reader, YAML reader, and canonical class data together."""
    for name in ("variant_sweep.py", "review_classes.py", "lane_brief.py"):
        shutil.copyfile(HELPER.parent / name, scripts / name)
    config = scripts.parent / ".config"
    config.mkdir(exist_ok=True)
    shutil.copyfile(review_classes.CLASS_FILE, config / review_classes.CLASS_FILE.name)


def advance_upstream(tmp: Path, upstream: Path, path: str, text: str) -> str:
    """Clone upstream under tmp, commit text at path on master, and push."""

    work = Path(tempfile.mkdtemp(prefix="upstream-work-", dir=tmp))
    git(tmp, "clone", "-q", str(upstream), str(work))
    target = work / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    git(work, "add", path)
    git(
        work,
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
    git(work, "push", "-q", "origin", "master")
    return git_output(work, "rev-parse", "HEAD")


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
    paginate_large_comments: paginate comment rows normally even when there are more than 1000.
    on_pull: optional callback run on GET /pulls/1 before building the reply.
    pull_author: login of the pull author (default: the token login).
    pull_state: state of the pull ("open" by default); a PATCH with state closed
        sets it unless ignore_close.
    ignore_close: acknowledge a close PATCH without changing the state.
    review_comments: review id -> rows served by GET pulls/1/reviews/<id>/comments;
        a POST there appends a row.
    drop_review_replies: acknowledge a review-comment POST without storing it.
    review_reply_extra_lines: store and return a review-comment POST with this
        extra_lines_count instead of the posted one.
    on_review_listing: optional callback run on GET pulls/1/reviews/<id>/comments
        before building the reply.
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
        self.merge_base = MERGE_BASE
        self.pull_body = ""
        self.on_pull = None
        self.fail_status = bool(kwargs.get("fail_status", False))
        self.pull_status = pull_status
        self.reviewers = reviewers or [[]]
        self.login = str(kwargs.get("login", "tester"))
        self.pull_author = str(kwargs.get("pull_author", self.login))
        self.pull_state = str(kwargs.get("pull_state", "open"))
        self.ignore_close = bool(kwargs.get("ignore_close", False))
        self.max_response_items = kwargs.get("max_response_items", 50)
        self.open_pulls: list[dict] = []
        self.pull_files: dict[int, list[ChangedFile]] = {}
        self.fail_pulls = False
        self.fail_files: set[int] = set()
        self.reviews: dict[int, list[dict]] = {}
        self.fail_reviews: set[int] = set()
        self.comments: dict[int, list[dict]] = {}
        self.review_comments: dict[int, list[dict]] = {}
        self.drop_review_replies = bool(kwargs.get("drop_review_replies", False))
        self.review_reply_extra_lines = kwargs.get("review_reply_extra_lines")
        self.on_review_listing = None
        self.fail_comments: set[int] = set()
        self.paginate_large_comments = bool(kwargs.get("paginate_large_comments", False))
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
            "merge_base": self.merge_base,
            "body": self.pull_body,
            "html_url": "stub/pulls/1",
            "user": {"login": self.pull_author},
            "state": self.pull_state,
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
        rows = [
            row
            for row in self.open_pulls
            if poster is None
            or not isinstance(row, dict)
            or not isinstance(row.get("user"), dict)
            or "login" not in row["user"]
            or row["user"].get("login") == poster
        ]
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
        rows = self.comments.get(number, [])
        if len(rows) > 1000 and not self.paginate_large_comments:
            return rows if page == 1 else []
        return rows[(page - 1) * limit : page * limit]

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
            if stub.on_pull is not None:
                stub.on_pull()
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

    def _route_review_comments(self, stub: Stub, parts: list[str]) -> bool:
        """Serve the comments of one pull review."""

        if (
            len(parts) > 5
            and parts[-5] == "pulls"
            and parts[-3] == "reviews"
            and parts[-1] == "comments"
        ):
            if stub.on_review_listing is not None:
                stub.on_review_listing()
            self.reply(200, stub.review_comments.get(int(parts[-2]), []))
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
            lambda: self._route_review_comments(stub, parts),
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

    def _post_review_comment(self, stub: Stub, data: str) -> bool:
        """Persist and return a posted pull-review comment when the path matches."""

        parts = urllib.parse.urlsplit(self.path).path.split("/")
        if not (
            len(parts) > 5
            and parts[-5] == "pulls"
            and parts[-3] == "reviews"
            and parts[-1] == "comments"
        ):
            return False
        review_id = int(parts[-2])
        n = len(stub.review_comments.get(review_id, []))
        payload = json.loads(data)
        row = {
            "id": 5000 + n,
            "body": payload["body"],
            "user": {"login": stub.login},
            "path": payload["path"],
            "position": payload["new_position"],
            "original_position": payload["old_position"],
            "extra_lines_count": payload["extra_lines_count"],
            "html_url": f"stub/pulls/1#issuecomment-{5000 + n}",
        }
        if stub.review_reply_extra_lines is not None:
            row["extra_lines_count"] = stub.review_reply_extra_lines
        if not stub.drop_review_replies:
            stub.review_comments.setdefault(review_id, []).append(row)
        self.reply(200, row)
        return True

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
        elif self._post_review_comment(self.stub(), data):
            return
        else:
            self.reply(404, {})

    def do_PATCH(self) -> None:
        """Record pull edits, apply a close unless the stub ignores it, and acknowledge."""

        data = self.rfile.read(int(self.headers["Content-Length"])).decode()
        stub = self.stub()
        stub.requests.append(("PATCH", self.path, data))
        if (
            self.path.endswith("/pulls/1")
            and json.loads(data).get("state") == "closed"
            and not stub.ignore_close
        ):
            stub.pull_state = "closed"
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

        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.tmp = Path(scratch.name)
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
        self.sweep.write_text(full_sweep_report(sha), encoding="utf-8")

    def command_environment(
        self,
        server: http.server.HTTPServer,
        path_first: Path | None = None,
        env_updates: dict[str, str] | None = None,
    ) -> dict[str, str]:
        """Build the isolated execution environment for helper runs."""

        env = isolated_git_environment(preserve_config=True)
        dirs = [str(p) for p in (path_first, self.bin_dir) if p]
        env.update(
            PATH=os.pathsep.join(dirs + [env.get("PATH", "")]),
            FFMPEG_FORGEJO_API=f"http://127.0.0.1:{server.server_port}/api/v1",
            FFMPEG_FORGEJO_GIT=str(self.upstream),
            FFMPEG_FORGEJO_POLL="0",
        )
        env.update(env_updates or {})
        return env

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
            env = self.command_environment(server, path_first=path_first, env_updates=env_updates)
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


class FixtureCleanupTest(unittest.TestCase):
    """Verify that Forgejo test fixtures clean up their temporary directories."""

    def test_fixture_root_removed_after_cleanup(self) -> None:
        """The temporary fixture root created in setUp is removed by cleanups."""
        case = RunCommandTest("test_unknown_subcommand_is_rejected_before_any_request")
        case.setUp()
        root = case.tmp
        self.assertTrue(root.is_dir())
        try:
            case.doCleanups()
            self.assertFalse(root.exists())
        finally:
            if root.exists():
                shutil.rmtree(root, ignore_errors=True)


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

    def test_edit_body_filler_is_refused_before_any_request(self) -> None:
        """A filler body exits 81 before any head, status or token request."""
        (self.tmp / "body.md").write_text("Would you like me to add a test?\n", encoding="utf-8")
        stub = Stub()
        self.assertEqual(self.run_helper(stub), 81)
        self.assertEqual(stub.requests, [])

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

    def test_edit_body_stale_base_claim_exits_80(self) -> None:
        """A stale base claim in the new PR body exits 80 without a PATCH."""

        (self.tmp / "body.md").write_text("Base: FFmpeg master 46f23be8b197.\n", encoding="utf-8")
        stub = Stub([A], {A: [[label("success")]]})
        proc = self.run_command(
            stub, "edit-body", "--no-test-claims", "1", str(self.tmp / "body.md")
        )
        self.assertEqual(proc.returncode, 80, proc.stderr)
        self.assertIn("46f23be8b197", proc.stderr)
        self.assertEqual(self.patches(stub), [])

    def test_edit_body_current_base_claim_is_patched(self) -> None:
        """A new PR body naming the live merge base is patched."""

        body = f"Base: FFmpeg master {MERGE_BASE[:12]}.\n"
        (self.tmp / "body.md").write_text(body, encoding="utf-8")
        stub = Stub([A], {A: [[label("success")]]})
        self.assertEqual(self.run_helper(stub), 0)
        self.assertEqual(self.patches(stub), [body])

    def test_edit_body_patches_the_checked_snapshot(self) -> None:
        """Rewriting the body file during pull reads cannot alter the patched snapshot."""

        body_path = self.tmp / "body.md"
        original = f"Base: FFmpeg master {MERGE_BASE[:12]}.\n"
        body_path.write_text(original, encoding="utf-8")
        stub = Stub([A], {A: [[label("success")]]})
        stub.on_pull = lambda: body_path.write_text(
            "Base: FFmpeg master 46f23be8b197.\n", encoding="utf-8"
        )
        proc = self.run_command(stub, "edit-body", "--no-test-claims", "1", str(body_path))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.patches(stub), [original])

    def test_edit_body_base_read_is_the_head_reread(self) -> None:
        """The Autolabel head re-read performs the base check without a later pull read."""

        stub = Stub([A, A, B], {A: [[label("success")]]})
        proc = self.run_command(
            stub, "edit-body", "--no-test-claims", "1", str(self.tmp / "body.md")
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.patches(stub), [BODY])
        self.assertEqual(stub.head_reads, 2)

    def test_edit_body_head_change_in_base_read_exits_75(self) -> None:
        """A head change seen by the base-check pull read exits 75 without a PATCH."""

        stub = Stub([A, B], {A: [[label("success")]]})
        proc = self.run_command(
            stub, "edit-body", "--no-test-claims", "1", str(self.tmp / "body.md")
        )
        self.assertEqual(proc.returncode, 75, proc.stderr)
        self.assertIn("superseded by a new push", proc.stderr)
        self.assertEqual(self.patches(stub), [])


class CloseTest(ForgejoTest):
    """The close verb comments on and closes only an own open pull request."""

    def test_close_filler_is_refused_before_any_request(self) -> None:
        """A filler closing comment exits 81 before any token or pull request."""
        body = self.tmp / "body.md"
        body.write_text("Would you like me to add a test?\n", encoding="utf-8")
        stub = Stub()
        proc = self.run_command(stub, "close", "1", str(body))
        self.assertEqual(proc.returncode, 81, proc.stderr)
        self.assertEqual(stub.requests, [])

    def writes(self, stub: Stub) -> list[tuple[str, str]]:
        """Return the method and path of every POST and PATCH the stub received."""

        return [(m, p) for m, p, _ in stub.requests if m in {"POST", "PATCH"}]

    def test_own_open_pull_is_commented_then_closed(self) -> None:
        """An own open pull is commented on, closed, and verified closed."""

        stub = Stub()
        proc = self.run_command(stub, "close", "1", str(self.tmp / "body.md"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            self.writes(stub),
            [
                ("POST", "/api/v1/repos/FFmpeg/FFmpeg/issues/1/comments"),
                ("PATCH", "/api/v1/repos/FFmpeg/FFmpeg/pulls/1"),
            ],
        )
        self.assertEqual(self.sent_bodies(stub, "POST"), [BODY])
        patches = [json.loads(data) for method, _, data in stub.requests if method == "PATCH"]
        self.assertEqual(patches, [{"state": "closed"}])
        self.assertIn("closed stub/pulls/1", proc.stdout)

    def test_foreign_pull_is_refused_before_any_write(self) -> None:
        """A pull authored by another login exits 80 before any write."""

        stub = Stub(pull_author="someone-else")
        proc = self.run_command(stub, "close", "1", str(self.tmp / "body.md"))
        self.assertEqual(proc.returncode, 80, proc.stderr)
        self.assertEqual(self.writes(stub), [])
        self.assertIn("authored by someone-else, not tester", proc.stderr)

    def test_closed_pull_is_refused_before_any_write(self) -> None:
        """An already-closed pull exits 1 before any write."""

        stub = Stub(pull_state="closed")
        proc = self.run_command(stub, "close", "1", str(self.tmp / "body.md"))
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertEqual(self.writes(stub), [])
        self.assertIn("is closed; not closed", proc.stderr)

    def test_missing_comment_file_is_refused_before_any_request(self) -> None:
        """A missing comment file exits 1 before contacting the API."""

        stub = Stub()
        proc = self.run_command(stub, "close", "1", str(self.tmp / "missing.md"))
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertEqual(stub.requests, [])
        self.assertIn("no comment file", proc.stderr)

    def test_blank_comment_file_is_refused_before_any_request(self) -> None:
        """A whitespace-only comment file exits 1 before contacting the API."""

        blank = self.tmp / "blank.md"
        blank.write_text(" \n\t\n", encoding="utf-8")
        stub = Stub()
        proc = self.run_command(stub, "close", "1", str(blank))
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertEqual(stub.requests, [])
        self.assertIn("empty comment file", proc.stderr)

    def test_duplicate_comment_is_refused_before_any_write(self) -> None:
        """An own matching comment exits 78 before posting or closing."""

        stub = Stub()
        stub.comments[1] = [
            {
                "id": 7,
                "html_url": "stub/issues/1#issuecomment-7",
                "body": BODY,
                "user": {"login": "tester"},
            }
        ]
        proc = self.run_command(stub, "close", "1", str(self.tmp / "body.md"))
        self.assertEqual(proc.returncode, 78, proc.stderr)
        self.assertEqual(self.writes(stub), [])

    def test_close_not_read_back_fails(self) -> None:
        """A close PATCH whose read-back stays open exits 1."""

        stub = Stub(ignore_close=True)
        proc = self.run_command(stub, "close", "1", str(self.tmp / "body.md"))
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertEqual(
            self.writes(stub),
            [
                ("POST", "/api/v1/repos/FFmpeg/FFmpeg/issues/1/comments"),
                ("PATCH", "/api/v1/repos/FFmpeg/FFmpeg/pulls/1"),
            ],
        )
        self.assertIn("is still open after the close request", proc.stderr)

    def test_wrong_argument_count_is_a_usage_error(self) -> None:
        """Missing the comment file argument exits 64 before any request."""

        stub = Stub()
        proc = self.run_command(stub, "close", "1")
        self.assertEqual(proc.returncode, 64, proc.stderr)
        self.assertEqual(stub.requests, [])

    def test_non_numeric_pull_is_refused(self) -> None:
        """A non-numeric pull identifier exits 1 before any request."""

        stub = Stub()
        body = str(self.tmp / "body.md")
        proc = self.run_command(stub, "close", "x", body)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertEqual(stub.requests, [])


THREAD = [
    {
        "id": 85943,
        "body": "first",
        "user": {"login": "reviewer"},
        "path": "other.c",
        "position": 5,
        "original_position": 0,
        "extra_lines_count": 0,
        "html_url": "stub/pulls/1#issuecomment-85943",
    },
    {
        "id": 85995,
        "body": "question",
        "user": {"login": "reviewer"},
        "path": "tests/checkasm/vvc_alf.c",
        "position": 79,
        "original_position": 0,
        "extra_lines_count": 3,
        "html_url": "stub/pulls/1#issuecomment-85995",
    },
]


class ReplyTest(ForgejoTest):
    """The reply verb answers a review comment inside its thread."""

    def test_reply_filler_is_refused_before_any_request(self) -> None:
        """A filler reply exits 81 before any token or review-comment request."""
        body = self.tmp / "body.md"
        body.write_text("Would you like me to add a test?\n", encoding="utf-8")
        stub = self.stub()
        proc = self.reply(stub, "85995", str(body))
        self.assertEqual(proc.returncode, 81, proc.stderr)
        self.assertEqual(stub.requests, [])

    def stub(self, *, heads: list[object] | None = None, **kwargs: object) -> Stub:
        """Return a stub serving the planted thread under review 7."""

        stub = Stub(heads=heads, **kwargs)
        stub.review_comments[7] = copy.deepcopy(THREAD)
        return stub

    def reply(self, stub: Stub, *args: str) -> subprocess.CompletedProcess[str]:
        """Run a reply with no test claims on pull 1, review 7."""

        return self.run_command(stub, "reply", "--no-test-claims", "1", "7", *args)

    def payloads(self, stub: Stub) -> list[dict]:
        """Return the JSON bodies of every review-comment POST."""

        return [
            json.loads(data)
            for method, path, data in stub.requests
            if method == "POST" and "/reviews/" in path and path.endswith("/comments")
        ]

    def test_reply_copies_the_anchor_of_the_answered_comment(self) -> None:
        """Replying to a review comment posts at its file, line, and range."""

        stub = self.stub()
        body = str(self.tmp / "body.md")
        proc = self.reply(stub, "85995", body)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            self.payloads(stub),
            [
                {
                    "body": BODY,
                    "path": "tests/checkasm/vvc_alf.c",
                    "new_position": 79,
                    "old_position": 0,
                    "extra_lines_count": 3,
                }
            ],
        )
        posts = [path for method, path, _ in stub.requests if method == "POST"]
        self.assertEqual(posts, ["/api/v1/repos/FFmpeg/FFmpeg/pulls/1/reviews/7/comments"])
        self.assertIn("replied stub/pulls/1#issuecomment-5002", proc.stdout)

    def test_old_side_comment_keeps_its_old_position(self) -> None:
        """An old-side review comment keeps position zero and its original line."""

        stub = self.stub()
        stub.review_comments[7][1]["position"] = 0
        stub.review_comments[7][1]["original_position"] = 12
        proc = self.reply(stub, "85995", str(self.tmp / "body.md"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = self.payloads(stub)[0]
        self.assertEqual(payload["new_position"], 0)
        self.assertEqual(payload["old_position"], 12)

    def test_unknown_comment_is_refused_before_any_write(self) -> None:
        """A comment missing from the named review exits 1 before posting."""

        stub = self.stub()
        proc = self.reply(stub, "99", str(self.tmp / "body.md"))
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertFalse(any(method == "POST" for method, _, _ in stub.requests))
        self.assertIn("comment 99 is not in review 7", proc.stderr)

    def test_duplicate_reply_is_refused_before_any_write(self) -> None:
        """An own matching comment in the review exits 78 before posting."""

        stub = self.stub()
        stub.review_comments[7].append(
            {
                "id": 6000,
                "body": BODY,
                "user": {"login": "tester"},
                "path": "tests/checkasm/vvc_alf.c",
                "position": 79,
                "original_position": 0,
                "extra_lines_count": 3,
                "html_url": "stub/pulls/1#issuecomment-6000",
            }
        )
        proc = self.reply(stub, "85995", str(self.tmp / "body.md"))
        self.assertEqual(proc.returncode, 78, proc.stderr)
        self.assertFalse(any(method == "POST" for method, _, _ in stub.requests))

    def test_reply_not_read_back_fails(self) -> None:
        """A dropped review-comment POST fails read-back with exit 1."""

        stub = self.stub(drop_review_replies=True)
        proc = self.reply(stub, "85995", str(self.tmp / "body.md"))
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertEqual(len([method for method, _, _ in stub.requests if method == "POST"]), 1)
        self.assertIn("was not read back in review 7", proc.stderr)

    def test_reply_stored_with_another_range_fails_read_back(self) -> None:
        """A reply stored with another line range than the answered comment fails read-back."""

        stub = self.stub(review_reply_extra_lines=0)
        proc = self.reply(stub, "85995", str(self.tmp / "body.md"))
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertEqual(len([method for method, _, _ in stub.requests if method == "POST"]), 1)
        self.assertIn("was not read back in review 7", proc.stderr)

    def test_target_without_range_field_reads_back_with_range_zero(self) -> None:
        """A target row without extra_lines_count posts and reads back with range zero."""

        stub = self.stub()
        del stub.review_comments[7][1]["extra_lines_count"]
        proc = self.reply(stub, "85995", str(self.tmp / "body.md"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.payloads(stub)[0]["extra_lines_count"], 0)
        self.assertIn("replied stub/pulls/1#issuecomment-5002", proc.stdout)

    def test_missing_body_file_is_refused_before_any_request(self) -> None:
        """A missing body file exits 1 before contacting the API."""

        stub = self.stub()
        proc = self.reply(stub, "85995", str(self.tmp / "missing.md"))
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertEqual(stub.requests, [])

    def test_blank_body_file_is_refused_before_any_request(self) -> None:
        """A whitespace-only body file exits 1 before contacting the API."""

        blank = self.tmp / "blank.md"
        blank.write_text(" \n\t\n", encoding="utf-8")
        stub = self.stub()
        proc = self.reply(stub, "85995", str(blank))
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertEqual(stub.requests, [])

    def test_body_rewritten_after_listing_posts_the_snapshot(self) -> None:
        """A body file rewritten after the run starts is neither checked, posted nor read back."""

        stub = self.stub()
        body = self.tmp / "body.md"
        stub.review_comments[7].append(
            {
                "id": 6000,
                "body": "changed text",
                "user": {"login": "tester"},
                "path": "tests/checkasm/vvc_alf.c",
                "position": 79,
                "original_position": 0,
                "extra_lines_count": 3,
                "html_url": "stub/pulls/1#issuecomment-6000",
            }
        )
        stub.on_review_listing = lambda: body.write_text("changed text\n", encoding="utf-8")
        proc = self.reply(stub, "85995", str(body))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            self.payloads(stub),
            [
                {
                    "body": BODY,
                    "path": "tests/checkasm/vvc_alf.c",
                    "new_position": 79,
                    "old_position": 0,
                    "extra_lines_count": 3,
                }
            ],
        )
        self.assertIn("replied stub/pulls/1#issuecomment-5003", proc.stdout)

    def test_wrong_argument_count_is_a_usage_error(self) -> None:
        """Missing the body file argument exits 64 before any request."""

        stub = self.stub()
        proc = self.run_command(stub, "reply", "--no-test-claims", "1", "7", "85995")
        self.assertEqual(proc.returncode, 64, proc.stderr)
        self.assertEqual(stub.requests, [])

    def test_non_numeric_review_is_refused(self) -> None:
        """A non-numeric review identifier exits 1 before any request."""

        stub = self.stub()
        body = str(self.tmp / "body.md")
        proc = self.run_command(stub, "reply", "--no-test-claims", "1", "x", "85995", body)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertEqual(stub.requests, [])

    def test_tested_head_is_rechecked_after_review_listing(self) -> None:
        """A PR head changing during review listing exits 76 without posting."""

        stub = self.stub(heads=[A, B])
        evidence = self.tmp / "evidence.md"
        evidence.write_text(f"- test: fate tree=/w head={A}\n", encoding="utf-8")
        body = str(self.tmp / "body.md")
        proc = self.run_command(
            stub,
            "reply",
            "--tested-head",
            A,
            "--evidence",
            str(evidence),
            "1",
            "7",
            "85995",
            body,
        )
        self.assertEqual(proc.returncode, 76, proc.stderr)
        self.assertIn("does not match live head", proc.stderr)
        self.assertFalse(any(method == "POST" for method, _, _ in stub.requests))

    def test_moved_head_exit_removes_the_reply_payload(self) -> None:
        """A moved head that exits 76 after the payload file exists leaves no tmp file behind."""

        stub = self.stub(heads=[A, B])
        evidence = self.tmp / "evidence.md"
        evidence.write_text(f"- test: fate tree=/w head={A}\n", encoding="utf-8")
        body = str(self.tmp / "body.md")
        clean_tmp = self.tmp / "reply-tmp"
        clean_tmp.mkdir()
        proc = self.run_command(
            stub,
            "reply",
            "--tested-head",
            A,
            "--evidence",
            str(evidence),
            "1",
            "7",
            "85995",
            body,
            env_updates={"TMPDIR": str(clean_tmp)},
        )
        self.assertEqual(proc.returncode, 76, proc.stderr)
        self.assertFalse(any(method == "POST" for method, _, _ in stub.requests))
        self.assertEqual(list(clean_tmp.iterdir()), [], "reply payload tmp file leaked on exit 76")


class TestClaimHeadTest(ForgejoTest):
    """Validate evidence before accepting claims about a tested head."""

    def test_comment_filler_is_refused_before_any_request(self) -> None:
        """A filler comment exits 81 before any token, head or comment request."""
        (self.tmp / "body.md").write_text("Would you like me to add a test?\n", encoding="utf-8")
        stub = Stub()
        proc = self.comment(stub, "--no-test-claims")
        self.assertEqual(proc.returncode, 81, proc.stderr)
        self.assertEqual(stub.requests, [])

    def test_issue_filler_is_refused_before_any_request(self) -> None:
        """A filler issue body exits 81 before any token or issue request."""
        body = self.tmp / "body.md"
        body.write_text("Would you like me to add a test?\n", encoding="utf-8")
        stub = Stub()
        proc = self.run_command(stub, "issue", "title", str(body))
        self.assertEqual(proc.returncode, 81, proc.stderr)
        self.assertEqual(stub.requests, [])

    def evidence(self, head: str = A, content: str | None = None) -> str:
        """Write evidence for a tested head."""

        path = self.tmp / "evidence.md"
        path.write_text(
            content if content is not None else f"- test: fate tree=/w head={head}\n",
            encoding="utf-8",
        )
        return str(path)

    def comment(
        self, stub: Stub, *args: str, env_updates: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        """Run a comment command with the shared issue and body arguments."""

        return self.run_command(
            stub,
            "comment",
            *args,
            "1",
            str(self.tmp / "body.md"),
            env_updates=env_updates,
        )

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

    def test_own_duplicate_comment_is_refused(self) -> None:
        """A whitespace-equivalent own reply exits 78 without a POST."""

        stub = Stub()
        stub.comments = {
            1: [
                {
                    "user": {"login": "tester"},
                    "id": 81918,
                    "html_url": "stub/issues/1#issuecomment-81918",
                    "body": "  new   body\tline two \n\n",
                }
            ]
        }
        proc = self.comment(stub, "--no-test-claims")
        self.assertEqual(proc.returncode, 78, proc.stderr)
        self.assertIn("own comment 81918", proc.stderr)
        self.assertEqual(self.posts(stub), [])

    def test_comment_duplicate_uses_authenticated_token_login(self) -> None:
        """A configured login override cannot hide a duplicate by the token owner."""

        stub = Stub(login="tester")
        stub.comments = {
            1: [
                {
                    "user": {"login": "tester"},
                    "id": 81918,
                    "html_url": "stub/issues/1#issuecomment-81918",
                    "body": BODY,
                }
            ]
        }
        proc = self.comment(stub, "--no-test-claims", env_updates={"FFMPEG_FORGEJO_LOGIN": "other"})
        self.assertEqual(proc.returncode, 78, proc.stderr)
        self.assertEqual(self.posts(stub), [])

    def test_comment_duplicate_on_page_twenty_one_is_refused(self) -> None:
        """Comment duplicate detection continues through twenty-one full pages."""

        stub = Stub(paginate_large_comments=True)
        stub.comments = {
            1: [
                {"user": {"login": "other"}, "id": 10000 + n, "body": "different"}
                for n in range(1049)
            ]
            + [
                {
                    "user": {"login": "tester"},
                    "id": 11049,
                    "html_url": "stub/issues/1#issuecomment-11049",
                    "body": BODY,
                }
            ],
        }
        proc = self.comment(stub, "--no-test-claims")
        self.assertEqual(proc.returncode, 78, proc.stderr)
        self.assertIn("own comment 11049", proc.stderr)
        self.assertEqual(self.posts(stub), [])

    def test_comment_listing_hits_two_hundred_page_bound(self) -> None:
        """A comment listing at the explicit bound fails closed without posting."""

        stub = Stub(paginate_large_comments=True)
        stub.comments = {
            1: [
                {"user": {"login": "other"}, "id": 10000 + n, "body": "different"}
                for n in range(10000)
            ]
        }
        proc = self.comment(stub, "--no-test-claims")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("pagination limit reached", proc.stderr)
        self.assertEqual(self.posts(stub), [])

    def test_tested_head_is_rechecked_after_comment_listing(self) -> None:
        """A PR head changing during comment listing prevents the comment POST."""

        stub = Stub([A, B])
        proc = self.comment_body(stub)
        self.assertEqual(proc.returncode, 76, proc.stderr)
        self.assertIn("does not match live head", proc.stderr)
        paths = [
            urllib.parse.urlsplit(path).path for method, path, _ in stub.requests if method == "GET"
        ]
        pull_reads = [i for i, p in enumerate(paths) if p.endswith("/pulls/1")]
        listed = [i for i, p in enumerate(paths) if p.endswith("/issues/1/comments")]
        self.assertTrue(listed)
        self.assertGreater(pull_reads[-1], listed[-1])
        self.assertEqual(self.posts(stub), [])

    def test_comment_distinct_or_other_author_is_posted(self) -> None:
        """Distinct bodies, other authors, and thread boundaries authorize a comment POST."""

        cases = (
            (
                "distinct_body",
                [
                    {
                        "user": {"login": "tester"},
                        "id": 100,
                        "html_url": "stub/issues/1#issuecomment-100",
                        "body": "completely different text",
                    }
                ],
            ),
            (
                "other_login",
                [
                    {
                        "user": {"login": "someone_else"},
                        "id": 101,
                        "html_url": "stub/issues/1#issuecomment-101",
                        "body": BODY,
                    }
                ],
            ),
            (
                "one_char_difference",
                [
                    {
                        "user": {"login": "tester"},
                        "id": 102,
                        "html_url": "stub/issues/1#issuecomment-102",
                        "body": BODY.strip() + "!",
                    }
                ],
            ),
            (
                "empty_thread",
                [],
            ),
        )
        for case_name, comments in cases:
            with self.subTest(case=case_name):
                stub = Stub()
                stub.comments = {1: comments}
                proc = self.comment(stub, "--no-test-claims")
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(self.posts(stub), [BODY])

    def test_own_duplicate_comment_on_page_two_is_refused(self) -> None:
        """An own duplicate comment spanning onto page two exits 78 without a POST."""

        stub = Stub(max_response_items=1)
        stub.comments = {
            1: [
                {
                    "user": {"login": "tester"},
                    "id": 81910,
                    "html_url": "stub/issues/1#issuecomment-81910",
                    "body": "first comment",
                },
                {
                    "user": {"login": "tester"},
                    "id": 81918,
                    "html_url": "stub/issues/1#issuecomment-81918",
                    "body": "  new   body\tline two \n\n",
                },
            ]
        }
        proc = self.comment(stub, "--no-test-claims")
        self.assertEqual(proc.returncode, 78, proc.stderr)
        self.assertIn("own comment 81918", proc.stderr)
        self.assertEqual(self.posts(stub), [])

    def test_comment_list_failure_exits_nonzero_without_post(self) -> None:
        """A failure reading existing comments fails closed before posting."""

        stub = Stub()
        stub.fail_comments.add(1)
        proc = self.comment(stub, "--no-test-claims")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertEqual(self.posts(stub), [])

    def test_non_utf8_body_file_exits_nonzero_without_post(self) -> None:
        """A body file containing invalid UTF-8 fails closed without posting."""

        stub = Stub()
        (self.tmp / "body.md").write_bytes(b"\xff\xfe\x00\x00invalid")
        proc = self.comment(stub, "--no-test-claims")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertEqual(self.posts(stub), [])

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

    def test_comment_stale_live_base_exits_80(self) -> None:
        """A stale base claim in the live PR body exits 80 without posting a comment."""

        stub = Stub([LIVE])
        stub.pull_body = "Base: FFmpeg master 46f23be8b197.\n"
        proc = self.comment(stub, "--tested-head", LIVE, "--evidence", self.evidence(LIVE))
        self.assertEqual(proc.returncode, 80, proc.stderr)
        self.assertIn("live PR body", proc.stderr)
        self.assertEqual(self.posts(stub), [])

    def test_comment_current_live_base_is_posted(self) -> None:
        """A live PR body naming the current merge base authorizes the comment POST."""

        stub = Stub([LIVE])
        stub.pull_body = f"Base: FFmpeg master {MERGE_BASE[:12]}.\n"
        proc = self.comment(stub, "--tested-head", LIVE, "--evidence", self.evidence(LIVE))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.posts(stub), [BODY])

    def test_comment_posts_the_checked_snapshot(self) -> None:
        """Rewriting the comment body file during pull reads cannot alter the posted text."""

        body_path = self.tmp / "body.md"
        stub = Stub([LIVE])
        stub.pull_body = f"Base: FFmpeg master {MERGE_BASE[:12]}.\n"
        stub.on_pull = lambda: body_path.write_text("changed\n", encoding="utf-8")
        proc = self.comment(stub, "--tested-head", LIVE, "--evidence", self.evidence(LIVE))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.posts(stub), [BODY])

    def test_comment_head_change_in_base_read_exits_76(self) -> None:
        """A head change seen by the base-check pull read exits 76 without posting."""

        stub = Stub([LIVE, LIVE, B])
        stub.pull_body = f"Base: FFmpeg master {MERGE_BASE[:12]}.\n"
        proc = self.comment(stub, "--tested-head", LIVE, "--evidence", self.evidence(LIVE))
        self.assertEqual(proc.returncode, 76, proc.stderr)
        self.assertIn("base check", proc.stderr)
        self.assertEqual(self.posts(stub), [])

    def test_comment_prefix_head_change_in_base_read_exits_76(self) -> None:
        """A base-check read of a different head sharing the tested prefix exits 76
        without posting."""

        other = LIVE[:7] + ("1" if LIVE[7] != "1" else "2") + LIVE[8:]
        stub = Stub([LIVE, LIVE, other])
        stub.pull_body = f"Base: FFmpeg master {MERGE_BASE[:12]}.\n"
        proc = self.comment(stub, "--tested-head", LIVE[:7], "--evidence", self.evidence(LIVE))
        self.assertEqual(proc.returncode, 76, proc.stderr)
        self.assertIn("base check", proc.stderr)
        self.assertEqual(self.posts(stub), [])

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

    def test_edit_body_abbreviated_evidence_rechecks_full_head(self) -> None:
        """Abbreviated evidence cannot carry a body edit across a push to a same-prefix head."""

        other = LIVE[:7] + ("1" if LIVE[7] != "1" else "2") + LIVE[8:]
        stub = Stub([LIVE, LIVE, LIVE, other], {LIVE: [[label("success")]]})
        proc = self.edit_body(
            stub, "--tested-head", LIVE[:7], "--evidence", self.evidence(LIVE[:7])
        )
        self.assertEqual(proc.returncode, 76, proc.stderr)
        self.assertEqual(self.patches(stub), [])
        self.assertIn(other, proc.stderr)

    def test_edit_body_abbreviated_evidence_same_head_edits(self) -> None:
        """Abbreviated evidence still edits when every read returns the same head."""

        stub = Stub([LIVE, LIVE, LIVE, LIVE], {LIVE: [[label("success")]]})
        proc = self.edit_body(
            stub, "--tested-head", LIVE[:7], "--evidence", self.evidence(LIVE[:7])
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(self.patches(stub)), 1)

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
                "config) exit 1;; "
                "hash-object) echo 4b825dc642cb6eb9a060e54bf8d69288fbee4904;; "
                "merge-tree) echo 4b825dc642cb6eb9a060e54bf8d69288fbee4904; exit 0;; "
                '-c) case " $* " in *" merge-tree "*)'
                " echo 4b825dc642cb6eb9a060e54bf8d69288fbee4904; exit 0;; esac; exit 97;; "
                "update-ref) exit 0;; "
                'rev-parse) if [ "$2" = --git-path ]; then echo "$PWD/fixture-git-dir/$3"; '
                f"else echo {FAIRY_FIXTURE_HEAD}; fi;; *) exit 97;; esac"
            ),
        }.items():
            tool = self.bin_dir / name
            tool.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
            tool.chmod(0o755)
        (self.tmp / "body.md").write_text(BODY, encoding="utf-8")
        self.sweep = self.tmp / "sweep.md"
        self.sweep.write_text(full_sweep_report(FAIRY_FIXTURE_HEAD), encoding="utf-8")

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
    def test_pr_filler_is_refused_before_any_request(self) -> None:
        """A filler PR body exits 81 before any fetch, token or API request."""
        (self.tmp / "body.md").write_text("Would you like me to add a test?\n", encoding="utf-8")
        stub = Stub(reviewers=[["alice"]])
        proc = self.pr(stub)
        self.assertEqual(proc.returncode, 81, proc.stderr)
        self.assertEqual(stub.requests, [])

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

    def assert_refused_before_post(
        self, stub: Stub, proc: subprocess.CompletedProcess[str]
    ) -> None:
        """Assert exit 77 naming pull #25612 and OVERLAP_FILE, with no pull-creation POST."""

        self.assertEqual(proc.returncode, 77, proc.stderr)
        self.assertIn("#25612", proc.stderr)
        self.assertIn(OVERLAP_FILE, proc.stderr)
        self.assertEqual(self.pull_posts(stub), [])

    def test_own_open_pull_with_shared_file_is_refused(self) -> None:
        """An own open pull touching the candidate file exits 77 without a POST."""

        stub = overlap_stub(open_pulls=[open_pull()], pull_files={25612: [OVERLAP_FILE]})
        proc = self.pr(stub)
        self.assert_refused_before_post(stub, proc)

    def test_own_open_pull_from_the_same_branch_is_refused(self) -> None:
        """An own open pull from the branch being opened (a lost create response)
        exits 77 without a POST.
        """

        topic_head = git_output(self.repo, "rev-parse", "topic")
        stub = overlap_stub(
            open_pulls=[open_pull(head_sha=topic_head, head_ref="topic")],
            pull_files={25612: [OVERLAP_FILE]},
        )
        proc = self.pr(stub)
        self.assert_refused_before_post(stub, proc)

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
        self.sweep.write_text(full_sweep_report(sha), encoding="utf-8")
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
        self.assertIn(
            f"cannot fetch master from {missing}; merge with the live base not proven",
            proc.stderr,
        )
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


class OverlapVerbTest(ForgejoTest):
    """Verify that the overlap verb reports own open pull requests."""

    def overlap(self, stub: Stub, *options: str) -> subprocess.CompletedProcess[str]:
        """Run the overlap verb against the topic branch."""

        return self.run_command(stub, "overlap", *options, "topic")

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

    def test_fate_list_overlap_is_listed(self) -> None:
        """A shared fate makefile is detected as an overlap."""

        git(self.repo, "checkout", "-q", "master")
        mak = self.repo / "tests" / "fate" / "filter-video.mak"
        mak.parent.mkdir(parents=True, exist_ok=True)
        mak.write_text("fate base\n", encoding="utf-8")
        git(self.repo, "add", "tests/fate/filter-video.mak")
        identity = [
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "commit.gpgSign=false",
        ]
        commit = [*identity, "commit", "-qm"]
        git(self.repo, *commit, "fate base")
        git(self.repo, "push", "-q", str(self.upstream), "master")
        git(self.repo, "checkout", "-q", "topic")
        git(self.repo, *identity, "rebase", "-q", "master")
        mak.write_text("fate topic\n", encoding="utf-8")
        git(self.repo, "add", "tests/fate/filter-video.mak")
        git(self.repo, *commit, "fate topic")

        stub = overlap_stub(
            open_pulls=[
                open_pull(number=25612),
                open_pull(number=25613),
            ],
            pull_files={
                25612: ["tests/fate/filter-video.mak"],
                25613: ["libavfilter/vf_other.c"],
            },
        )
        proc = self.overlap(stub)
        self.assertEqual(proc.returncode, 77, proc.stderr)
        self.assertIn('#25612 ["tests/fate/filter-video.mak"]', proc.stdout)
        lines = proc.stdout.splitlines()
        self.assertEqual(lines[-1], "own-open: 25612")
        self.assertNotIn("25613", proc.stdout)
        self.assertEqual(
            [method for method, _, _ in stub.requests if method in {"POST", "PATCH", "PUT"}],
            [],
        )

    def test_every_overlapping_pull_is_listed(self) -> None:
        """Every overlapping own open pull is reported in ascending order."""

        stub = overlap_stub(
            open_pulls=[open_pull(number=25700), open_pull(number=25612)],
            pull_files={25612: [OVERLAP_FILE], 25700: [OVERLAP_FILE]},
        )
        proc = self.overlap(stub)
        self.assertEqual(proc.returncode, 77, proc.stderr)
        self.assertIn('#25612 ["libavfilter/vf_find_rect.c"]', proc.stdout)
        self.assertIn('#25700 ["libavfilter/vf_find_rect.c"]', proc.stdout)
        lines = proc.stdout.splitlines()
        self.assertEqual(lines[-1], "own-open: 25612 25700")

    def test_no_overlap_reports_none(self) -> None:
        """A pull touching unrelated files reports no overlap and exits zero."""

        stub = overlap_stub(
            open_pulls=[open_pull(number=25612)],
            pull_files={25612: ["libavfilter/vf_other.c"]},
        )
        proc = self.overlap(stub)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = proc.stdout.splitlines()
        self.assertEqual(lines[-1], "own-open: none")

    def test_overlap_ok_skips_exact_pull(self) -> None:
        """Passing --overlap-ok exempts specific pulls from overlap checks."""

        stub1 = overlap_stub(
            open_pulls=[open_pull(number=25612), open_pull(number=25700)],
            pull_files={25612: [OVERLAP_FILE], 25700: [OVERLAP_FILE]},
        )
        proc1 = self.overlap(stub1, "--overlap-ok", "25612")
        self.assertEqual(proc1.returncode, 77, proc1.stderr)
        lines1 = proc1.stdout.splitlines()
        self.assertEqual(lines1[-1], "own-open: 25700")

        stub2 = overlap_stub(
            open_pulls=[open_pull(number=25612), open_pull(number=25700)],
            pull_files={25612: [OVERLAP_FILE], 25700: [OVERLAP_FILE]},
        )
        proc2 = self.overlap(stub2, "--overlap-ok", "25612", "--overlap-ok", "25700")
        self.assertEqual(proc2.returncode, 0, proc2.stderr)
        lines2 = proc2.stdout.splitlines()
        self.assertEqual(lines2[-1], "own-open: none")

        stub3 = overlap_stub(
            open_pulls=[open_pull(number=25612), open_pull(number=25700)],
            pull_files={25612: [OVERLAP_FILE], 25700: [OVERLAP_FILE]},
        )
        proc3 = self.overlap(stub3, "--overlap-ok", "25700")
        self.assertEqual(proc3.returncode, 77, proc3.stderr)
        lines3 = proc3.stdout.splitlines()
        self.assertEqual(lines3[-1], "own-open: 25612")
        self.assertNotIn("25700", proc3.stdout)

    def test_unlistable_pulls_fail_closed(self) -> None:
        """Failure to list open pulls exits one without reporting an own-open line."""

        stub = overlap_stub(fail_pulls=True)
        proc = self.overlap(stub)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertNotIn("own-open:", proc.stdout)

    def test_bad_overlap_ok_is_usage_error(self) -> None:
        """Non-positive or non-numeric --overlap-ok values exit 64."""

        stub = overlap_stub()
        for bad in ("0", "x"):
            with self.subTest(bad=bad):
                proc = self.overlap(stub, "--overlap-ok", bad)
                self.assertEqual(proc.returncode, 64, proc.stderr)

    def test_zero_open_pulls_report_none(self) -> None:
        """When there are no own open pulls, report none without file requests."""

        stub = overlap_stub(open_pulls=[])
        proc = self.overlap(stub)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = proc.stdout.splitlines()
        self.assertEqual(lines[-1], "own-open: none")
        self.assertFalse(any("/files" in path for _, path, _ in stub.requests))

    def test_stale_fetch_head_does_not_change_the_base(self) -> None:
        """Diff against the fetched base ignores a pre-existing stale FETCH_HEAD."""

        git_path = git_output(self.repo, "rev-parse", "--git-path", "FETCH_HEAD")
        fetch_head_path = Path(git_path) if Path(git_path).is_absolute() else self.repo / git_path
        topic_sha = git_output(self.repo, "rev-parse", "topic")
        stale_content = f"{topic_sha}\t\tbranch 'topic' of elsewhere\n".encode()
        fetch_head_path.write_bytes(stale_content)

        stub = overlap_stub(
            open_pulls=[open_pull(number=25612)],
            pull_files={25612: [OVERLAP_FILE]},
        )
        proc = self.overlap(stub)
        self.assertEqual(proc.returncode, 77, proc.stderr)
        lines = proc.stdout.splitlines()
        self.assertEqual(lines[-1], "own-open: 25612")
        self.assertEqual(fetch_head_path.read_bytes(), stale_content)
        refs = git_output(self.repo, "for-each-ref", "refs/ffmpeg-forgejo")
        self.assertEqual(refs, "")

    def test_pull_closed_during_listing_is_rechecked(self) -> None:
        """A pull closed during listing triggers a second round that catches missed pulls."""

        open_pulls = [open_pull(number=n) for n in range(25000, 25051)]
        stub = overlap_stub(
            open_pulls=open_pulls,
            pull_files={25050: [OVERLAP_FILE]},
        )
        original_pulls = stub.pulls
        first_page1 = True

        def side_effect(query: dict[str, list[str]]) -> list[dict]:
            nonlocal first_page1
            result = original_pulls(query)
            if first_page1 and query.get("page", ["1"])[0] == "1":
                first_page1 = False
                stub.open_pulls.pop(0)
            return result

        with unittest.mock.patch.object(stub, "pulls", side_effect=side_effect):
            proc = self.overlap(stub)
        self.assertEqual(proc.returncode, 77, proc.stderr)
        lines = proc.stdout.splitlines()
        self.assertEqual(lines[-1], "own-open: 25050")
        page1_requests = [
            path for _, path, _ in stub.requests if "/pulls?" in path and "page=1" in path
        ]
        self.assertGreaterEqual(len(page1_requests), 4)

    def test_listing_that_never_settles_fails_closed(self) -> None:
        """A pull list whose head SHA changes across every check fails closed after three rounds."""

        stub = overlap_stub(open_pulls=[open_pull(number=25612)])
        counter = 0

        def side_effect(query: dict[str, list[str]]) -> list[dict]:
            nonlocal counter
            page = query.get("page", ["1"])[0]
            if page != "1":
                return []
            counter += 1
            return [
                {
                    "number": 25612,
                    "state": "open",
                    "user": {"login": "tester"},
                    "head": {"sha": f"{counter:040x}"},
                }
            ]

        with unittest.mock.patch.object(stub, "pulls", side_effect=side_effect):
            proc = self.overlap(stub)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("changed during 3 overlap checks", proc.stderr)
        self.assertNotIn("own-open:", proc.stdout)
        page1_requests = [
            path for _, path, _ in stub.requests if "/pulls?" in path and "page=1" in path
        ]
        self.assertEqual(len(page1_requests), 6)

    def test_merge_base_that_never_settles_fails_closed(self) -> None:
        """A pull list whose merge base changes across every check fails closed
        after three rounds.
        """

        stub = overlap_stub(open_pulls=[open_pull(number=25612)])
        counter = 0

        def side_effect(query: dict[str, list[str]]) -> list[dict]:
            """Return the pull with a new merge base on every read."""
            nonlocal counter
            page = query.get("page", ["1"])[0]
            if page != "1":
                return []
            counter += 1
            return [
                {
                    "number": 25612,
                    "state": "open",
                    "user": {"login": "tester"},
                    "head": {"sha": f"{25612:040x}"},
                    "base": {"ref": "master"},
                    "merge_base": f"{counter:040x}",
                }
            ]

        with unittest.mock.patch.object(stub, "pulls", side_effect=side_effect):
            proc = self.overlap(stub)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("changed during 3 overlap checks", proc.stderr)
        self.assertNotIn("own-open:", proc.stdout)
        page1_requests = [
            path for _, path, _ in stub.requests if "/pulls?" in path and "page=1" in path
        ]
        self.assertEqual(len(page1_requests), 6)

    def test_retargeted_base_is_rechecked(self) -> None:
        """A pull retargeted between the two reads of a round is read again before
        the result is accepted.
        """

        stub = overlap_stub(
            open_pulls=[open_pull(number=25612)],
            pull_files={25612: ["libavfilter/vf_other.c"]},
        )
        counter = 0

        def side_effect(query: dict[str, list[str]]) -> list[dict]:
            """Return the pull with a release base on the first read and master after."""
            nonlocal counter
            page = query.get("page", ["1"])[0]
            if page != "1":
                return []
            counter += 1
            return [
                {
                    "number": 25612,
                    "state": "open",
                    "user": {"login": "tester"},
                    "head": {"sha": f"{25612:040x}"},
                    "base": {"ref": "release/8.0" if counter == 1 else "master"},
                    "merge_base": f"{1:040x}",
                }
            ]

        with unittest.mock.patch.object(stub, "pulls", side_effect=side_effect):
            proc = self.overlap(stub)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.splitlines()[-1], "own-open: none")
        page1_requests = [
            path for _, path, _ in stub.requests if "/pulls?" in path and "page=1" in path
        ]
        self.assertEqual(len(page1_requests), 4)

    def test_shared_paths_on_every_file_page_are_listed(self) -> None:
        """All shared paths across multiple file pages are accumulated and reported."""

        identity = [
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "commit.gpgSign=false",
        ]
        commit = [*identity, "commit", "-qm"]
        second = self.repo / "libavfilter" / "vf_second.c"
        second.write_text("second\n", encoding="utf-8")
        git(self.repo, "add", "libavfilter/vf_second.c")
        git(self.repo, *commit, "second file")

        files = (
            [OVERLAP_FILE]
            + [f"libavfilter/filler_{number}.c" for number in range(49)]
            + ["libavfilter/vf_second.c"]
        )
        stub = overlap_stub(
            open_pulls=[open_pull(number=25612)],
            pull_files={25612: files},
        )
        proc = self.overlap(stub)
        self.assertEqual(proc.returncode, 77, proc.stderr)
        self.assertIn(
            '#25612 ["libavfilter/vf_find_rect.c", "libavfilter/vf_second.c"]',
            proc.stdout,
        )
        lines = proc.stdout.splitlines()
        self.assertEqual(lines[-1], "own-open: 25612")

    def test_interrupted_overlap_cleans_up(self) -> None:
        """An interrupted overlap check cleans up its candidate file and private base ref."""

        clean_tmp = self.tmp / "interrupted-tmp"
        clean_tmp.mkdir()
        stub = overlap_stub(
            open_pulls=[open_pull(number=25612)],
            pull_files={25612: [OVERLAP_FILE]},
        )
        original_files = stub.files
        term_sent = threading.Event()
        proc: subprocess.Popen[str] | None = None
        stderr = ""

        def files_side_effect(number: int, query: dict[str, list[str]]) -> list[dict] | None:
            if not term_sent.is_set() and proc is not None:
                proc.terminate()
                term_sent.set()
            return original_files(number, query)

        server = serve(stub)
        try:
            with unittest.mock.patch.object(stub, "files", side_effect=files_side_effect):
                env = self.command_environment(server, env_updates={"TMPDIR": str(clean_tmp)})
                proc = subprocess.Popen(  # nosec B603 - fixed helper invocation in test
                    ["bash", str(self.helper), "overlap", "topic"],
                    env=env,
                    cwd=self.repo,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                _, stderr = proc.communicate(timeout=10)
        finally:
            server.shutdown()
            server.server_close()
            if proc is not None and proc.poll() is None:
                proc.kill()
                proc.communicate(timeout=5)

        self.assertIsNotNone(proc)
        self.assertEqual(proc.returncode, 143, stderr)
        self.assertEqual(list(clean_tmp.iterdir()), [])
        refs = git_output(self.repo, "for-each-ref", "refs/ffmpeg-forgejo")
        self.assertEqual(refs, "")

    def test_overlap_errors_name_no_pull_request(self) -> None:
        """The overlap verb never claims no pull request was opened on probe errors."""

        overlap_proc = self.overlap(overlap_stub(fail_pulls=True))
        self.assertEqual(overlap_proc.returncode, 1, overlap_proc.stderr)
        self.assertIn("overlap not proven absent", overlap_proc.stderr)
        self.assertNotIn("no pull request opened", overlap_proc.stderr)

        pr_proc = self.pr(overlap_stub(fail_pulls=True))
        self.assertEqual(pr_proc.returncode, 1, pr_proc.stderr)
        self.assertIn("no pull request opened", pr_proc.stderr)

    def test_overlap_unreachable_upstream_fails_closed(self) -> None:
        """The overlap verb fails closed when the live base cannot be fetched."""

        missing = self.tmp / "missing.git"
        proc = self.run_command(
            overlap_stub(),
            "overlap",
            "topic",
            env_updates={"FFMPEG_FORGEJO_GIT": str(missing)},
        )
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn(
            f"cannot fetch master from {missing}; overlap not proven absent",
            proc.stderr,
        )
        self.assertEqual(git_output(self.repo, "for-each-ref", "refs/ffmpeg-forgejo"), "")


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

    def checkout_copy(self, *, validator: bool, checkout: bool = True) -> Path:
        """Return a helper copy under scripts/, a Git checkout unless checkout is False."""

        checkout_dir = self.tmp / "checkout"
        scripts = checkout_dir / "scripts"
        scripts.mkdir(parents=True, exist_ok=True)
        if checkout:
            git(checkout_dir, "init", "-q")
        helper = scripts / "ffmpeg-forgejo"
        shutil.copyfile(HELPER, helper)
        if validator:
            copy_sweep_validator(scripts)
        return helper

    def test_sweep_names_missing_module(self) -> None:
        """A helper missing its validator reports the module and refuses the push."""

        self.helper = self.checkout_copy(validator=False)
        result = self.sweep_command("push", "--sweep", str(self.sweep))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(
            f"sweep: missing validator: {self.helper.parent / 'variant_sweep.py'}", result.stderr
        )
        self.assertEqual(self.sweep_requests, [])
        self.assertNotIn("stub push", result.stdout)

    def test_sweep_missing_sibling_rejects_cwd_decoy(self) -> None:
        """A missing sibling fails by name without executing a checkout-level decoy."""

        self.helper = self.checkout_copy(validator=False)
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
                    f"sweep: missing validator: {self.helper.parent / 'variant_sweep.py'}",
                    result.stderr,
                )
                self.assertEqual(self.sweep_requests, [])
                self.assertNotIn("stub push", result.stdout)

    def assert_guard_refused(self) -> None:
        """Assert push and pr exit 74 naming self.helper before any request, push or Git call."""

        for command in ("push", "pr"):
            with self.subTest(command=command):
                result = self.sweep_command(command, "--sweep", str(self.sweep))
                self.assertEqual(result.returncode, 74, result.stderr)
                self.assertIn(
                    f"sweep: helper is not the repository script: {self.helper}", result.stderr
                )
                self.assertEqual(self.sweep_requests, [])
                self.assertNotIn("stub push", result.stdout)
        calls = self.tmp / "git-calls.jsonl"
        self.assertFalse(
            calls.exists(), calls.read_text(encoding="utf-8") if calls.exists() else ""
        )

    def test_sweep_refuses_installed_copy(self) -> None:
        """An installed copy outside a checkout refuses push and pr even with its validator."""

        self.helper = self.bin_dir / "ffmpeg-forgejo"
        shutil.copyfile(HELPER, self.helper)
        copy_sweep_validator(self.bin_dir)
        self.assert_guard_refused()

    def test_sweep_refuses_scripts_copy_outside_checkout(self) -> None:
        """A scripts/ffmpeg-forgejo copy outside a checkout is refused like an installed copy."""

        self.helper = self.checkout_copy(validator=True, checkout=False)
        self.assert_guard_refused()

    def test_sweep_accepts_checkout_copy(self) -> None:
        """A helper in another Git checkout passes the guard and pushes."""

        self.helper = self.checkout_copy(validator=True)
        result = self.sweep_command("push", "--sweep", str(self.sweep))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("stub push", result.stdout)

    def test_sweep_refuses_bare_git_marker(self) -> None:
        """A .git that is an empty file, an empty directory or a gitdir line without
        HEAD is refused."""

        self.helper = self.checkout_copy(validator=True, checkout=False)
        marker = self.helper.parent.parent / ".git"
        missing = self.tmp / "missing-gitdir"
        for shape in ("empty file", "empty directory", "gitdir without HEAD"):
            with self.subTest(shape=shape):
                try:
                    if shape == "empty directory":
                        marker.mkdir()
                    else:
                        marker.write_text(
                            "" if shape == "empty file" else f"gitdir: {missing}\n",
                            encoding="utf-8",
                        )
                    self.assert_guard_refused()
                finally:
                    if marker.is_dir():
                        marker.rmdir()
                    else:
                        marker.unlink(missing_ok=True)
                    (self.tmp / "git-calls.jsonl").unlink(missing_ok=True)

    def test_sweep_accepts_gitdir_file_copy(self) -> None:
        """A .git file naming a directory that holds HEAD passes, by absolute and by
        relative path."""

        self.helper = self.checkout_copy(validator=True, checkout=False)
        checkout_dir = self.helper.parent.parent
        gitdir = self.tmp / "worktree-gitdir"
        gitdir.mkdir()
        (gitdir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        for line in (f"gitdir: {gitdir}", "gitdir: ../worktree-gitdir"):
            with self.subTest(line=line):
                (checkout_dir / ".git").write_text(line + "\n", encoding="utf-8")
                result = self.sweep_command("push", "--sweep", str(self.sweep))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("stub push", result.stdout)

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
        self,
        command: str,
        *options: str,
        stub: Stub | None = None,
        env_updates: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Attempt an outbound operation with API and Git push stubbed."""

        real_git = shutil.which("git")
        if real_git is None:
            raise RuntimeError("git missing")
        fake = self.bin_dir / "git"
        fake.write_text(
            "#!/usr/bin/env python3\nimport json, os, sys\n"
            f"with open({str(self.tmp / 'git-calls.jsonl')!r}, 'a', encoding='utf-8') as calls:\n"
            "    calls.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            'if "merge-tree" in sys.argv:\n'
            f"    with open({str(self.tmp / 'merge-tree-env.jsonl')!r}, 'a',"
            " encoding='utf-8') as env_log:\n"
            '        env_log.write(json.dumps({"GIT_ATTR_NOSYSTEM":'
            ' os.environ.get("GIT_ATTR_NOSYSTEM"), "GIT_GRAFT_FILE":'
            ' os.environ.get("GIT_GRAFT_FILE")}) + "\\n")\n'
            '    mode = os.environ.get("STUB_MERGE_TREE")\n'
            '    if mode == "fail":\n'
            "        sys.exit(128)\n"
            '    if mode == "flood":\n'
            '        buf = memoryview(b"a" * (2 * 1024 * 1024))\n'
            "        try:\n"
            "            for _ in range(2 * 1024 * 1024):\n"
            "                if not buf:\n"
            "                    break\n"
            "                buf = buf[os.write(1, buf):]\n"
            "        except BrokenPipeError:\n"
            "            pass\n"
            "        sys.exit(0)\n"
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
        result = self.run_command(stub, command, *options, *positional, env_updates=env_updates)
        self.sweep_requests = stub.requests
        return result

    def test_push_refuses_head_conflicting_with_live_base(self) -> None:
        """Refuse push when the swept head conflicts with the live upstream base."""

        base_sha = advance_upstream(self.tmp, self.upstream, OVERLAP_FILE, "upstream\n")
        result = self.sweep_command("push", "--sweep", str(self.sweep))
        self.assertEqual(result.returncode, 73, result.stderr)
        self.assertIn("does not merge cleanly into live master", result.stderr)
        self.assertIn(base_sha[:12], result.stderr)
        self.assertIn(OVERLAP_FILE, result.stderr)
        self.assertFalse((self.tmp / "push-argv.json").exists())
        self.assertEqual(git_output(self.repo, "for-each-ref", "refs/ffmpeg-forgejo"), "")

    def test_pr_refuses_head_conflicting_with_live_base(self) -> None:
        """Refuse PR creation when the swept head conflicts with the live upstream base."""

        base_sha = advance_upstream(self.tmp, self.upstream, OVERLAP_FILE, "upstream\n")
        result = self.sweep_command("pr", "--sweep", str(self.sweep))
        self.assertEqual(result.returncode, 73, result.stderr)
        self.assertIn("does not merge cleanly into live master", result.stderr)
        self.assertIn(base_sha[:12], result.stderr)
        self.assertIn(OVERLAP_FILE, result.stderr)
        self.assertFalse(any(method == "POST" for method, _, _ in self.sweep_requests))
        self.assertEqual(git_output(self.repo, "for-each-ref", "refs/ffmpeg-forgejo"), "")

    def test_push_accepts_live_base_advanced_without_conflict(self) -> None:
        """Allow push when the live upstream base advanced without conflicting."""

        advance_upstream(self.tmp, self.upstream, "other.txt", "upstream\n")
        result = self.sweep_command("push", "--sweep", str(self.sweep))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("stub push", result.stdout)
        self.assertEqual(git_output(self.repo, "for-each-ref", "refs/ffmpeg-forgejo"), "")

    def test_push_refuses_unfetchable_live_base(self) -> None:
        """Refuse push when the live upstream base cannot be fetched."""

        result = self.sweep_command(
            "push",
            "--sweep",
            str(self.sweep),
            env_updates={"FFMPEG_FORGEJO_GIT": str(self.tmp / "missing.git")},
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("merge with the live base not proven", result.stderr)
        self.assertFalse((self.tmp / "push-argv.json").exists())
        self.assertEqual(git_output(self.repo, "for-each-ref", "refs/ffmpeg-forgejo"), "")

    def test_push_refuses_failed_merge_tree(self) -> None:
        """Refuse push when git merge-tree fails with an unexpected status."""

        result = self.sweep_command(
            "push",
            "--sweep",
            str(self.sweep),
            env_updates={"STUB_MERGE_TREE": "fail"},
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("merge-tree failed with status 128", result.stderr)
        self.assertFalse((self.tmp / "push-argv.json").exists())
        self.assertEqual(git_output(self.repo, "for-each-ref", "refs/ffmpeg-forgejo"), "")

    def test_push_refuses_oversized_merge_tree_output(self) -> None:
        """Refuse push when git merge-tree output exceeds the byte cap."""

        result = self.sweep_command(
            "push",
            "--sweep",
            str(self.sweep),
            env_updates={"STUB_MERGE_TREE": "flood"},
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("merge-tree output exceeds 1048576 bytes", result.stderr)
        self.assertFalse((self.tmp / "push-argv.json").exists())
        self.assertEqual(git_output(self.repo, "for-each-ref", "refs/ffmpeg-forgejo"), "")

    def test_push_refuses_merge_driver_setting(self) -> None:
        """Refuse push when local Git config defines a custom merge driver."""

        git(self.repo, "config", "merge.fixture.driver", "true")
        result = self.sweep_command("push", "--sweep", str(self.sweep))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(
            "merge setting merge.fixture.driver may change the merge result",
            result.stderr,
        )
        self.assertFalse((self.tmp / "push-argv.json").exists())
        self.assertEqual(git_output(self.repo, "for-each-ref", "refs/ffmpeg-forgejo"), "")

    def test_push_accepts_neutral_merge_setting(self) -> None:
        """Allow push when local Git config sets an allow-listed merge option."""

        git(self.repo, "config", "merge.conflictStyle", "diff3")
        advance_upstream(self.tmp, self.upstream, "other.txt", "upstream\n")
        result = self.sweep_command("push", "--sweep", str(self.sweep))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("stub push", result.stdout)
        self.assertEqual(git_output(self.repo, "for-each-ref", "refs/ffmpeg-forgejo"), "")

    def test_push_refuses_local_info_attributes(self) -> None:
        """Refuse push when the repository info/attributes file defines attributes."""

        attr_path = self.repo / git_output(self.repo, "rev-parse", "--git-path", "info/attributes")
        attr_path.parent.mkdir(parents=True, exist_ok=True)
        attr_path.write_text("* merge=union\n", encoding="utf-8")
        result = self.sweep_command("push", "--sweep", str(self.sweep))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("sets attributes", result.stderr)
        self.assertFalse((self.tmp / "push-argv.json").exists())
        self.assertEqual(git_output(self.repo, "for-each-ref", "refs/ffmpeg-forgejo"), "")

    def test_push_conflict_ignores_worktree_union_attribute(self) -> None:
        """Ignore worktree .gitattributes union rules when checking live-base conflicts."""

        base_sha = advance_upstream(self.tmp, self.upstream, OVERLAP_FILE, "upstream\n")
        (self.repo / ".gitattributes").write_text(f"{OVERLAP_FILE} merge=union\n", encoding="utf-8")
        result = self.sweep_command("push", "--sweep", str(self.sweep))
        self.assertEqual(result.returncode, 73, result.stderr)
        self.assertIn("does not merge cleanly into live master", result.stderr)
        self.assertIn(base_sha[:12], result.stderr)
        self.assertIn(OVERLAP_FILE, result.stderr)
        self.assertFalse((self.tmp / "push-argv.json").exists())
        self.assertEqual(git_output(self.repo, "for-each-ref", "refs/ffmpeg-forgejo"), "")

    def test_push_conflict_ignores_local_replace_ref(self) -> None:
        """Ignore local replace refs when checking live-base conflicts."""

        base_sha = advance_upstream(self.tmp, self.upstream, OVERLAP_FILE, "upstream\n")
        new_blob = git_output(self.upstream, "rev-parse", f"{base_sha}:{OVERLAP_FILE}")
        old_blob = git_output(self.upstream, "rev-parse", f"{base_sha}~1:{OVERLAP_FILE}")
        git(self.repo, "update-ref", f"refs/replace/{new_blob}", old_blob)
        result = self.sweep_command("push", "--sweep", str(self.sweep))
        self.assertEqual(result.returncode, 73, result.stderr)
        self.assertIn("does not merge cleanly into live master", result.stderr)
        self.assertIn(base_sha[:12], result.stderr)
        self.assertIn(OVERLAP_FILE, result.stderr)
        self.assertFalse((self.tmp / "push-argv.json").exists())
        self.assertEqual(git_output(self.repo, "for-each-ref", "refs/ffmpeg-forgejo"), "")

    def test_push_conflict_ignores_info_grafts(self) -> None:
        """Ignore repository info/grafts parent overrides when checking live-base conflicts."""

        base_sha = advance_upstream(self.tmp, self.upstream, OVERLAP_FILE, "upstream\n")
        topic_head = git_output(self.repo, "rev-parse", "topic")
        graft_path = self.repo / git_output(self.repo, "rev-parse", "--git-path", "info/grafts")
        graft_path.parent.mkdir(parents=True, exist_ok=True)
        graft_path.write_text(f"{topic_head} {base_sha}\n", encoding="utf-8")
        result = self.sweep_command("push", "--sweep", str(self.sweep))
        self.assertEqual(result.returncode, 73, result.stderr)
        self.assertIn("does not merge cleanly into live master", result.stderr)
        self.assertIn(base_sha[:12], result.stderr)
        self.assertIn(OVERLAP_FILE, result.stderr)
        self.assertFalse((self.tmp / "push-argv.json").exists())
        self.assertEqual(git_output(self.repo, "for-each-ref", "refs/ffmpeg-forgejo"), "")

    def test_push_conflict_ignores_graft_file_env(self) -> None:
        """Ignore GIT_GRAFT_FILE overrides when checking live-base conflicts."""

        base_sha = advance_upstream(self.tmp, self.upstream, OVERLAP_FILE, "upstream\n")
        topic_head = git_output(self.repo, "rev-parse", "topic")
        graft_path = self.tmp / "grafts"
        graft_path.write_text(f"{topic_head} {base_sha}\n", encoding="utf-8")
        result = self.sweep_command(
            "push",
            "--sweep",
            str(self.sweep),
            env_updates={"GIT_GRAFT_FILE": str(graft_path)},
        )
        self.assertEqual(result.returncode, 73, result.stderr)
        self.assertIn("does not merge cleanly into live master", result.stderr)
        self.assertIn(base_sha[:12], result.stderr)
        self.assertIn(OVERLAP_FILE, result.stderr)
        self.assertFalse((self.tmp / "push-argv.json").exists())
        self.assertEqual(git_output(self.repo, "for-each-ref", "refs/ffmpeg-forgejo"), "")

    def test_merge_tree_ignores_system_attributes(self) -> None:
        """System attributes are root-owned, so the test checks the merge-tree environment."""

        advance_upstream(self.tmp, self.upstream, "other.txt", "upstream\n")
        result = self.sweep_command("push", "--sweep", str(self.sweep))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("stub push", result.stdout)
        lines = (self.tmp / "merge-tree-env.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertGreaterEqual(len(lines), 1)
        for line in lines:
            self.assertEqual(
                json.loads(line),
                {"GIT_ATTR_NOSYSTEM": "1", "GIT_GRAFT_FILE": "/dev/null"},
            )
        self.assertEqual(git_output(self.repo, "for-each-ref", "refs/ffmpeg-forgejo"), "")

    def test_pr_accepts_live_base_advanced_without_conflict(self) -> None:
        """Allow PR creation when the live upstream base advanced without conflicting."""

        advance_upstream(self.tmp, self.upstream, "other.txt", "upstream\n")
        stub = Stub(reviewers=[["alice"]])
        result = self.sweep_command("pr", "--sweep", str(self.sweep), stub=stub)
        self.assertEqual(result.returncode, 0, result.stderr)
        pulls_posts = [
            (method, path, body)
            for method, path, body in self.sweep_requests
            if method == "POST" and path.endswith("/pulls")
        ]
        self.assertEqual(len(pulls_posts), 1)
        self.assertEqual(git_output(self.repo, "for-each-ref", "refs/ffmpeg-forgejo"), "")

    def test_push_accepts_rename_when_diff_renames_disabled(self) -> None:
        """Allow push when topic renames a file edited on live base and diff.renames is false."""

        lines = [f"line {number}\n" for number in range(1, 201)]
        self._prepare_topic_renames([("rename_src.txt", "rename_dst.txt", lines, None)])
        edited = list(lines)
        edited[99] = "upstream line 100\n"
        advance_upstream(self.tmp, self.upstream, "rename_src.txt", "".join(edited))
        git(self.repo, "config", "diff.renames", "false")
        result = self.sweep_command("push", "--sweep", str(self.sweep))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("stub push", result.stdout)
        self.assertEqual(git_output(self.repo, "for-each-ref", "refs/ffmpeg-forgejo"), "")

    def test_push_accepts_inexact_renames_with_low_diff_rename_limit(self) -> None:
        """Allow push across two inexact renames when local diff.renameLimit is 1."""

        lines_1 = [f"a1 line {number}\n" for number in range(1, 201)]
        lines_2 = [f"a2 line {number}\n" for number in range(1, 201)]
        self._prepare_topic_renames(
            [
                ("a1.txt", "b1.txt", lines_1, "a1 topic line 1\n"),
                ("a2.txt", "b2.txt", lines_2, "a2 topic line 1\n"),
            ]
        )
        edited_1 = list(lines_1)
        edited_1[99] = "a1 upstream line 100\n"
        edited_2 = list(lines_2)
        edited_2[99] = "a2 upstream line 100\n"
        advance_upstream(self.tmp, self.upstream, "a1.txt", "".join(edited_1))
        advance_upstream(self.tmp, self.upstream, "a2.txt", "".join(edited_2))
        git(self.repo, "config", "diff.renameLimit", "1")
        result = self.sweep_command("push", "--sweep", str(self.sweep))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("stub push", result.stdout)
        self.assertEqual(git_output(self.repo, "for-each-ref", "refs/ffmpeg-forgejo"), "")

    def _prepare_topic_renames(self, renames: list[tuple[str, str, list[str], str | None]]) -> None:
        """Seed upstream files, rename them on topic, and refresh the sweep report."""

        for src, _, base_lines, _ in renames:
            advance_upstream(self.tmp, self.upstream, src, "".join(base_lines))
        git(self.repo, "fetch", "-q", str(self.upstream), "+refs/heads/master:refs/heads/master")
        git(self.repo, "reset", "--hard", "master")
        for src, dst, _, topic_first_line in renames:
            git(self.repo, "mv", src, dst)
            if topic_first_line is not None:
                target = self.repo / dst
                lines = target.read_text(encoding="utf-8").splitlines(keepends=True)
                lines[0] = topic_first_line
                target.write_text("".join(lines), encoding="utf-8")
                git(self.repo, "add", dst)
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
            "rename on topic",
        )
        sha = git_output(self.repo, "rev-parse", "topic")
        self.sweep.write_text(full_sweep_report(sha), encoding="utf-8")

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

    def large_promise_stub(self) -> Stub:
        """Build 60 large own pulls and more than 200 KiB of comments."""

        stub = self.promise_stub(body="x" * 4096)
        stub.open_pulls.extend(
            {
                "number": 30000 + n,
                "user": {"login": "tester"},
                "head": {"ref": f"other-{n}"},
                "merge_base": A,
                "body": "x" * 4096,
            }
            for n in range(59)
        )
        stub.comments[25619].extend(
            {"id": 90000 + n, "user": {"login": "tester"}, "body": "z" * 45000} for n in range(5)
        )
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

        (self.tmp / "push-argv.json").unlink(missing_ok=True)
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
        self.sweep.write_text(full_sweep_report(sha), encoding="utf-8")
        result = self.promise_push(self.promise_stub(), self.promise_file())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.tmp / "push-argv.json").exists())

    def test_push_accepts_large_listings_with_commit_evidence(self) -> None:
        """Large pull and comment listings reach Git push with commit evidence."""

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
            "qualify the close errors",
        )
        sha = git_output(self.repo, "rev-parse", "topic")
        self.sweep.write_text(full_sweep_report(sha), encoding="utf-8")
        stub = self.large_promise_stub()
        self.assertGreater(len(json.dumps(stub.comments[25619])), 200 * 1024)
        result = self.promise_push(stub, self.promise_file())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.tmp / "push-argv.json").exists())

    def test_push_refuses_large_listings_without_commit_evidence(self) -> None:
        """Large listings still reject an unkept promise with its comment ID."""

        stub = self.large_promise_stub()
        self.assertGreater(len(json.dumps(stub.comments[25619])), 200 * 1024)
        result = self.promise_push(stub, self.promise_file())
        self.assertEqual(result.returncode, 79, result.stderr)
        self.assertIn("81815", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertFalse((self.tmp / "push-argv.json").exists())

    def test_push_filters_own_pulls_and_default_reviews_keep_numbers(self) -> None:
        """Push and default review listing share the filtered open-pull query."""

        stub = self.promise_stub(comment="No promise here.")
        stub.open_pulls.append(dict(stub.open_pulls[0], number=25620, head={"ref": "other"}))
        stub.open_pulls.append(dict(stub.open_pulls[0], number=25621, user={"login": "other"}))
        result = self.promise_push(stub)
        self.assertEqual(result.returncode, 0, result.stderr)
        pull_paths = [
            path for method, path, _ in stub.requests if method == "GET" and "/pulls?" in path
        ]
        self.assertTrue(pull_paths)
        self.assertTrue(
            all("state=open" in path and "poster=tester" in path for path in pull_paths)
        )
        reviews = self.run_command(stub, "fairy-reviews")
        self.assertEqual(reviews.returncode, 0, reviews.stderr)
        self.assertEqual(reviews.stdout.splitlines(), ["25619\t0\t-\t-", "25620\t0\t-\t-"])

    def test_push_holds_quoted_reviewer_sentence_until_answered(self) -> None:
        """A quoted reviewer marker holds until a promise row has literal evidence."""

        comment = "> Will you qualify the close errors in the next push?"
        stub = self.promise_stub(comment=comment, body="qualify the close errors")
        self.assert_promise_refused(stub, None, comment)
        result = self.promise_push(stub, self.promise_file())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.tmp / "push-argv.json").exists())

    def test_push_holds_quoted_own_comment_with_context(self) -> None:
        """A marker in a quoted line holds even between plain context and an answer."""

        comment = "Context:\n  > I will fix it with the next push.\nAcknowledged."
        self.assert_promise_refused(self.promise_stub(comment=comment), None, "81815")

    def test_push_holds_quotes_with_zero_to_three_spaces(self) -> None:
        """Every formerly excluded quote indentation now requires a promise row."""

        for indent in range(4):
            with self.subTest(indent=indent):
                comment = " " * indent + "> I will fix this in the next push."
                self.assert_promise_refused(self.promise_stub(comment=comment), None, "81815")

    def test_push_holds_review_0fcb_code_markers(self) -> None:
        """Review 0fcb N2 code examples retain their marker without grammar exceptions."""

        comments = ("Example: `the next push`.", "```text\nthe next push\n```")
        for index in range(2):
            with self.subTest(comment=comments[index]):
                self.assert_promise_refused(
                    self.promise_stub(comment=comments[index]), None, "81815"
                )

    def test_push_holds_review_273e_literal_commitments(self) -> None:
        """Review 273e F1 fenced and indented greater-than commitments must hold."""

        backtick_commitment = (
            "My commitment is the following literal line:\n"
            "```text\n> I will fix this in the next push.\n```"
        )
        tilde_commitment = (
            "My commitment is the following literal line:\n"
            "~~~text\n> I will fix this in the next push.\n~~~"
        )
        comments = (
            backtick_commitment,
            tilde_commitment,
            "    > I will fix this in the next push.",
            "\t> I will fix this in the next push.",
        )
        for index in range(4):
            with self.subTest(comment=comments[index]):
                self.assert_promise_refused(
                    self.promise_stub(comment=comments[index]), None, "81815"
                )

    def test_push_holds_review_e6e4_list_and_bom_fences(self) -> None:
        """Review e6e4 D1 and D2 list-contained and BOM-prefixed fences retain markers."""

        comments = (
            "- ```text\n  > I will fix this in the next push.\n  ```",
            "1. ```text\n   > I will fix this in the next push.\n   ```",
            "- ~~~text\n  > I will fix this in the next push.\n  ~~~",
            "\ufeff```\n> I will fix this in the next push.\n```",
        )
        for index in range(4):
            with self.subTest(comment=comments[index]):
                self.assert_promise_refused(
                    self.promise_stub(comment=comments[index]), None, "81815"
                )

    def test_push_holds_review_e6e4_mixed_quote_and_block(self) -> None:
        """Review e6e4 mixed quote and block examples all hold under whole-body matching."""

        comments = (
            "- ```text\n  sample\n  ```\n\n> I will fix this in the next push.",
            "> ```text\n> > I will fix this in the next push.\n> ```",
            "```c\nint x = 0;\n```\n\n> I will fix this in the next push.",
            "> ```\nI will fix this in the next push.",
        )
        for index in range(4):
            with self.subTest(comment=comments[index]):
                self.assert_promise_refused(
                    self.promise_stub(comment=comments[index]), None, "81815"
                )

    def test_push_holds_review_1a5a_html_behind_list_markers(self) -> None:
        """Review 1a5a F1 HTML blocks retain markers behind bullet and ordered lists."""

        markers = ("-", "+", "*", "1.", "1)")
        blocks = (
            ("<pre>", ""),
            ("<div>", ""),
            ("<!--", "-->"),
            ("<?instruction", ""),
            ("<![CDATA[", "]]>"),
        )
        for index in range(5):
            marker = markers[index]
            indent = " " * (len(marker) + 1)
            for pos in range(5):
                opener, closer = blocks[pos]
                with self.subTest(marker=marker, opener=opener):
                    comment = f"{marker} {opener}\n{indent}> I will fix this in the next push." + (
                        f"\n{indent}{closer}" if closer else ""
                    )
                    self.assert_promise_refused(self.promise_stub(comment=comment), None, "81815")

    def test_push_holds_review_1a5a_html_block_starts(self) -> None:
        """Review 1a5a HTML start variants require no special opener recognition."""

        openers = ("<pre>", "<!--", "<?instruction", "<!DOCTYPE", "<![CDATA[", "<custom>")
        for index in range(6):
            with self.subTest(opener=openers[index]):
                comment = f"{openers[index]}\n> I will fix this in the next push."
                self.assert_promise_refused(self.promise_stub(comment=comment), None, "81815")

    def test_push_holds_bom_prefixed_quote(self) -> None:
        """Dropping the leading BOM leaves a quoted marker in the recorded window."""

        comment = "\ufeff> I will fix this in the next push.\nThanks."
        self.assert_promise_refused(
            self.promise_stub(comment=comment),
            None,
            ": > I will fix this in the next push. Thanks.; --promises <file> required",
        )

    def test_push_holds_quoted_reviewer_fenced_snippet(self) -> None:
        """A fully quoted reviewer snippet now holds regardless of its fence spelling."""

        comment = (
            "> Reviewer snippet:\n>\n> ```text\n"
            "> > I will fix this in the next push.\n> ```\n\nThanks."
        )
        self.assert_promise_refused(self.promise_stub(comment=comment), None, "81815")

    def test_push_holds_plain_quote_with_plain_answer(self) -> None:
        """A plain answer does not exempt a quoted reviewer marker from the promise file."""

        comment = "> I will fix this in the next push.\nThanks for the review."
        self.assert_promise_refused(self.promise_stub(comment=comment), None, "81815")

    def test_push_holds_markers_with_lf_crlf_and_cr(self) -> None:
        """Markers hold across LF, CRLF and CR in both quotes and fenced bodies."""

        endings = ("\n", "\r\n", "\r")
        for index in range(3):
            ending = endings[index]
            comments = (
                ending.join(("````text", "```", "> I will fix this in the next push.")),
                ending.join(("> I will fix this in the next push.", "Thanks.")),
                f"I will fix this in the{ending}next{ending}push.",
            )
            for pos in range(3):
                with self.subTest(ending=repr(ending), comment=comments[pos]):
                    self.assert_promise_refused(
                        self.promise_stub(comment=comments[pos]), None, "81815"
                    )

    def test_push_refuses_cr_only_comment_above_line_cap(self) -> None:
        """Review 1a5a F2 CR-only overflow refuses only after line-ending normalization."""

        comment = "\r" * 65536
        self.assertEqual(len(comment), 65536)
        self.assert_promise_refused(
            self.promise_stub(comment=comment),
            None,
            "comment 81815: body exceeds 65536-line cap",
        )

    def test_push_accepts_empty_lines_at_line_cap(self) -> None:
        """Exactly 65536 normalized lines without a marker still permit a push."""

        endings = ("\n", "\r")
        push_argv = self.tmp / "push-argv.json"
        for index in range(2):
            with self.subTest(ending=repr(endings[index])):
                push_argv.unlink(missing_ok=True)
                self.assertFalse(push_argv.exists())
                comment = endings[index] * 65535
                result = self.promise_push(self.promise_stub(comment=comment))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(push_argv.exists())

    def test_push_matches_new_promise_wording(self) -> None:
        """New prepositions and next-revision nouns require promise rows."""

        for sentence in (
            "I\u2019ll fold it into the next push.",
            "I'll fold it into the next push.",
            "I will fix that in my next revision.",
            "I will fix it for the next version.",
            "I will fix it on my next iteration.",
            "I will fix it with the next round.",
        ):
            with self.subTest(sentence=sentence):
                self.assert_promise_refused(self.promise_stub(comment=sentence), None, "81815")
        self.assert_promise_refused(
            self.promise_stub(comment="the next push from the bot"), None, "81815"
        )

    def test_push_matches_subject_independent_promise_wording(self) -> None:
        """Each missed wording needs a promise row fulfilled by a pushed commit message."""

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
            "qualify the close errors",
        )
        sha = git_output(self.repo, "rev-parse", "topic")
        self.sweep.write_text(full_sweep_report(sha), encoding="utf-8")
        sentences = (
            "The commit message does not yet qualify the wording; that follows with the next push.",
            "Will fix in the next push.",
            "The scope qualification will be added in the next revision.",
            "We will qualify the commit message with the next push.",
            "I am going to qualify the commit message in the next push.",
            "Fixed locally, coming with the next push.",
            "The next push carries the qualified commit message.",
        )
        for index in range(7):
            sentence = sentences[index]
            with self.subTest(sentence=sentence):
                (self.tmp / "push-argv.json").unlink(missing_ok=True)
                stub = self.promise_stub(comment=sentence)
                self.assert_promise_refused(stub, None, sentence)
                result = self.promise_push(stub, self.promise_file())
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue((self.tmp / "push-argv.json").exists())

    def test_push_ignores_past_revision_wording(self) -> None:
        """Past revisions without a next-push marker impose no promise row."""

        sentences = (
            "The last push qualified the commit message.",
            "This was fixed in the previous revision.",
        )
        for index in range(2):
            sentence = sentences[index]
            with self.subTest(sentence=sentence):
                (self.tmp / "push-argv.json").unlink(missing_ok=True)
                result = self.promise_push(self.promise_stub(comment=sentence))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue((self.tmp / "push-argv.json").exists())

    def test_push_refuses_2000_character_paragraph_with_marker_at_end(self) -> None:
        """A 2000-character own paragraph ending with the marker refuses with a bounded window."""

        marker = "the next push"
        comment = "x" * (2000 - len(marker) - 1) + " " + marker
        expected = "x" * 149 + " " + marker
        self.assertEqual(len(comment), 2000)
        self.assertLessEqual(len(expected), 150 + len(marker) + 150)
        self.assert_promise_refused(
            self.promise_stub(comment=comment),
            None,
            f": {expected}; --promises <file> required",
        )

    def test_push_records_first_whole_body_promise_match(self) -> None:
        """Record the first whole-body marker even when it occurs in a quoted line."""

        first = "Should that wait for the next push?"
        filler = " " + "x" * 160 + " "
        comment = (
            "> The next push carries quoted changes.\n"
            f"{first}{filler}We will add more in my next revision."
        )
        expected = "> The next push carries quoted changes. " + first + " " + "x" * 89
        result = self.promise_push(self.promise_stub(comment=comment))
        self.assertEqual(result.returncode, 79, result.stderr)
        self.assertIn(f": {expected}; --promises <file> required", result.stderr)
        self.assertNotIn("We will add more", result.stderr)
        self.assertFalse((self.tmp / "push-argv.json").exists())

    def test_push_refuses_wrapped_promise_marker(self) -> None:
        """A marker wrapped inside a paragraph still requires a promise row."""

        comment = "I will fix parser.c in the next\npush."
        expected = " ".join(comment.split())
        self.assert_promise_refused(self.promise_stub(comment=comment), None, expected)

    def test_push_refuses_wrapped_list_promise(self) -> None:
        """A continuation line inside a list item still matches the split marker."""

        comment = "- I will fix parser.c with the next\n  push."
        expected = " ".join(comment.split())
        self.assert_promise_refused(self.promise_stub(comment=comment), None, expected)

    def test_push_refuses_promise_with_parenthesized_question(self) -> None:
        """An internal parenthesized question still requires a promise row."""

        comment = "I will fix this in the next push (OK?)."
        self.assert_promise_refused(self.promise_stub(comment=comment), None, comment)

    def test_push_refuses_question_naming_next_push(self) -> None:
        """Questions naming the next push require a promise row like any other own match."""

        questions = (
            "Should that wait for the next push?",
            "should this go into the next push?",
            "Does the next push fix parser.c?",
        )
        for index in range(3):
            question = questions[index]
            with self.subTest(question=question):
                self.assert_promise_refused(
                    self.promise_stub(comment=question),
                    None,
                    f": {question}; --promises <file> required",
                )

    def test_push_holds_review_6fd4_round_bracket_internal_question(self) -> None:
        """Review 6fd4 D1 internal round-bracket questions retain the whole window."""

        comment = "I will fix parser.c in the next push (OK? please wait)."
        self.assert_promise_refused(
            self.promise_stub(comment=comment),
            None,
            f": {comment}; --promises <file> required",
        )

    def test_push_refuses_promise_with_square_bracket_internal_question(self) -> None:
        """A question mark inside square brackets records the full surrounding window."""

        comment = "I will fix parser.c in the next push [OK? please wait]."
        self.assert_promise_refused(
            self.promise_stub(comment=comment),
            None,
            f": {comment}; --promises <file> required",
        )

    def test_push_refuses_promise_ending_inside_bracket_before_question(self) -> None:
        """A promise before a bracketed period and trailing question is refused."""

        comment = "I will fix parser.c in the next push (including tests.) Should I mention tests?"
        self.assert_promise_refused(
            self.promise_stub(comment=comment),
            None,
            f": {comment}; --promises <file> required",
        )

    def test_push_refuses_promise_ending_inside_quote_before_question(self) -> None:
        """A promise before a quoted period and trailing question is refused."""

        comment = 'I will fix parser.c in the next push as "promised." Should I mention tests?'
        self.assert_promise_refused(
            self.promise_stub(comment=comment),
            None,
            f": {comment}; --promises <file> required",
        )

    def test_push_refuses_promise_with_parenthesized_question_before_continuation(self) -> None:
        """A parenthesized question before trailing text records the full window."""

        comment = "I will fix this in the next push (OK?) after review."
        self.assert_promise_refused(
            self.promise_stub(comment=comment),
            None,
            f": {comment}; --promises <file> required",
        )

    def test_push_refuses_parenthesized_question_with_promise_marker(self) -> None:
        """A parenthesized question naming the next push is refused."""

        comment = "(Does the next push fix parser.c?)"
        self.assert_promise_refused(self.promise_stub(comment=comment), None, comment)

    def test_push_refuses_quoted_question_with_promise_marker(self) -> None:
        """A question ending inside quotes and naming the next push is refused."""

        comment = 'Does the next push fix "parser.c?"'
        self.assert_promise_refused(self.promise_stub(comment=comment), None, comment)

    def test_push_holds_review_cbb7_numbered_list_internal_question(self) -> None:
        """Review cbb7 F1 numbered-list parentheses cannot hide an internal-question marker."""

        comment = "1) I will fix parser.c in the next push (OK? please wait)."
        self.assert_promise_refused(
            self.promise_stub(comment=comment),
            None,
            f": {comment}; --promises <file> required",
        )

    def test_push_refuses_nested_numbered_list_promise_with_internal_question(self) -> None:
        """A nested numbered list item with a closing parenthesis and question is refused."""

        comment = "- Outer\n  1) I will fix parser.c in the next push (OK? please wait)."
        expected = " ".join(comment.split())
        self.assert_promise_refused(
            self.promise_stub(comment=comment),
            None,
            f": {expected}; --promises <file> required",
        )

    def test_push_refuses_bracketed_period_before_promise_and_question(self) -> None:
        """An earlier period inside parentheses cannot hide a later next-push promise."""

        comment = (
            "I will fix parser.c (tests first. It will land in the next push, OK? please wait)."
        )
        self.assert_promise_refused(
            self.promise_stub(comment=comment),
            None,
            f": {comment}; --promises <file> required",
        )

    def test_push_holds_review_cbb7_typographic_quote_before_question(self) -> None:
        """Review cbb7 F2 typographic closers and a later question cannot hide the marker."""

        comment = (
            "I will fix parser.c in the next push as \u201cpromised.\u201d Should I mention tests?"
        )
        self.assert_promise_refused(
            self.promise_stub(comment=comment),
            None,
            f": {comment}; --promises <file> required",
        )

    def test_push_promise_window_marker_at_position_zero(self) -> None:
        """A marker at position zero records from the comment start through 150 trailing chars."""

        marker = "The next push"
        after = " " + "b" * 148 + "Z"
        comment = marker + after + "Y"
        expected = marker + after
        self.assert_promise_refused(
            self.promise_stub(comment=comment),
            None,
            f": {expected}; --promises <file> required",
        )

    def test_push_promise_window_marker_at_end(self) -> None:
        """A marker at the end of the comment stops at the comment end without trailing padding."""

        before = "A" + "a" * 148 + " "
        marker = "the next push"
        comment = "X" + before + marker
        expected = before + marker
        self.assert_promise_refused(
            self.promise_stub(comment=comment),
            None,
            f": {expected}; --promises <file> required",
        )

    def test_push_promise_window_with_150_characters_on_each_side(self) -> None:
        """A marker with 150 characters on each side keeps both outer boundary characters."""

        before = "A" + "a" * 148 + " "
        marker = "the next push"
        after = " " + "b" * 148 + "Z"
        comment = before + marker + after
        self.assertEqual(len(before), 150)
        self.assertEqual(len(after), 150)
        self.assert_promise_refused(
            self.promise_stub(comment=comment),
            None,
            f": {comment}; --promises <file> required",
        )

    def test_push_promise_window_cuts_at_151_characters_on_each_side(self) -> None:
        """A marker with 151 characters on each side cuts the 151st character on both sides."""

        before = "A" + "a" * 148 + " "
        marker = "the next push"
        after = " " + "b" * 148 + "Z"
        expected = before + marker + after
        comment = "X" + expected + "Y"
        self.assert_promise_refused(
            self.promise_stub(comment=comment),
            None,
            f": {expected}; --promises <file> required",
        )

    def test_push_records_complete_collapsed_wrapped_promise(self) -> None:
        """Recorded text collapses whitespace and preserves dotted tokens intact."""

        comment = "I will fix parser.c\tand v1.2 in my\n  next\tpush."
        expected = " ".join(comment.split())
        result = self.promise_push(self.promise_stub(comment=comment))
        self.assertEqual(result.returncode, 79, result.stderr)
        self.assertIn(f": {expected}; --promises <file> required", result.stderr)
        self.assertFalse((self.tmp / "push-argv.json").exists())

    def test_push_accepts_fulfilled_collapsed_wrapped_promise(self) -> None:
        """A collapsed recorded promise can supply the unchanged promise-file fragment."""

        comment = "I will fix parser.c\tand v1.2 in my\n  next\tpush."
        expected = " ".join(comment.split())
        stub = self.promise_stub(comment=comment, body=comment)
        result = self.promise_push(stub, self.promise_file(f"81815: {expected}\n"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.tmp / "push-argv.json").exists())

    def test_push_refuses_comment_above_byte_cap(self) -> None:
        """An own comment one byte above the raw cap refuses before normalization."""

        comment = "> " + "x" * 65535
        self.assertEqual(len(comment.encode("utf-8")), 65537)
        self.assert_promise_refused(
            self.promise_stub(comment=comment), None, "comment 81815: body exceeds 65536-byte cap"
        )

    def test_push_refuses_multibyte_comment_above_byte_cap(self) -> None:
        """The own-comment cap counts UTF-8 bytes rather than Unicode characters."""

        comment = "\u00e9" * 32768 + "x"
        self.assertEqual(len(comment.encode("utf-8")), 65537)
        self.assert_promise_refused(
            self.promise_stub(comment=comment), None, "comment 81815: body exceeds 65536-byte cap"
        )

    def test_push_accepts_comment_at_byte_cap_without_promise(self) -> None:
        """A comment exactly at the byte cap remains eligible for a normal push."""

        comment = "x" * 65536
        self.assertEqual(len(comment.encode("utf-8")), 65536)
        result = self.promise_push(self.promise_stub(comment=comment))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.tmp / "push-argv.json").exists())

    def test_push_accepts_multibyte_comment_at_byte_cap_without_promise(self) -> None:
        """A multibyte comment of exactly 65536 UTF-8 bytes is accepted."""

        comment = "\u00e9" * 32768
        self.assertEqual(len(comment.encode("utf-8")), 65536)
        result = self.promise_push(self.promise_stub(comment=comment))
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

    def test_push_refuses_more_than_1000_comments(self) -> None:
        """More than 1000 issue comments refuse the push before inspecting promises."""

        stub = self.promise_stub(comment="No promise here.")
        stub.comments[25619] = [
            {
                "id": 10000 + n,
                "user": {"login": "tester"},
                "body": "No promise here.",
            }
            for n in range(1000)
        ]
        stub.comments[25619].append(
            {
                "id": 11001,
                "user": {"login": "tester"},
                "body": "I will qualify the close errors in the commit message with the next push.",
                "html_url": "https://example.invalid/comments/11001",
            }
        )
        self.assert_promise_refused(stub, None, "too many issue comments")

    def test_push_fragment_match_collapses_whitespace_in_pr_body(self) -> None:
        """Collapsing whitespace runs in the PR body satisfies a promise fragment."""

        stub = self.promise_stub(body="qualify\r\n  the close\terrors")
        result = self.promise_push(stub, self.promise_file())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.tmp / "push-argv.json").exists())

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
        self.sweep.write_text(full_sweep_report(sha), encoding="utf-8")
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

        self.sweep.write_text(full_sweep_report(A), encoding="utf-8")
        self.assert_sweep_refused("--sweep", str(self.sweep))

    def test_sweep_missing_class(self) -> None:
        """A report without its bug class is refused."""

        self.sweep.write_text(
            self.sweep.read_text(encoding="utf-8").replace("class: unchecked-return\n", ""),
            encoding="utf-8",
        )
        self.assert_sweep_refused("--sweep", str(self.sweep))

    def test_push_refuses_missing_review_class(self) -> None:
        """Push refuses an omitted recurring class and names the missing id."""
        item = review_classes.load_classes()[0]
        row = f"| {item['id']} | {item['question']} | no-variant: fixture |\n"
        self.sweep.write_text(
            self.sweep.read_text(encoding="utf-8").replace(row, ""), encoding="utf-8"
        )
        result = self.sweep_command("push", "--sweep", str(self.sweep))
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertIn("review-classes:", result.stderr)
        self.assertIn(item["id"], result.stderr)
        self.assertNotIn("stub push", result.stdout)

    def test_push_refuses_review_class_todo(self) -> None:
        """Push refuses an unanswered recurring class disposition."""
        self.sweep.write_text(
            self.sweep.read_text(encoding="utf-8").replace("no-variant: fixture", "TODO", 1),
            encoding="utf-8",
        )
        result = self.sweep_command("push", "--sweep", str(self.sweep))
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertIn("review-classes:", result.stderr)
        self.assertIn(review_classes.load_classes()[0]["id"], result.stderr)
        self.assertNotIn("stub push", result.stdout)

    def test_sweep_todo(self) -> None:
        """An unresolved table disposition blocks outbound operations."""

        self.add_sweep_rows("TODO")
        self.assert_sweep_refused("--sweep", str(self.sweep))

    def test_sweep_bad_disposition(self) -> None:
        """Complete reports reject dispositions outside the three named forms."""

        for value in (
            "ignore",
            "",
            "follow-up:",
            "fold later",
            "TODO follow-up: x",
            "no-variant",
            "no-variant:",
        ):
            with self.subTest(value=value):
                self.sweep.write_text(
                    full_sweep_report(git_output(self.repo, "rev-parse", "topic")), encoding="utf-8"
                )
                self.add_sweep_rows(value)
                self.assert_sweep_refused("--sweep", str(self.sweep))

    def add_sweep_rows(self, *values: str) -> None:
        """Replace the unchecked-return no-hits marker with planted disposition rows."""

        rows = "".join(f"| sibling.c:4 | play | discarded | no | {value} |\n" for value in values)
        self.sweep.write_text(
            self.sweep.read_text(encoding="utf-8").replace("no hits\n", rows, 1),
            encoding="utf-8",
        )

    def test_sweep_rejects_single_class_report(self) -> None:
        """A single reviewed class refuses PR and leased push."""

        sha = git_output(self.repo, "rev-parse", "topic")
        report = full_sweep_report(sha).split("\n\nclass: state-advance", 1)[0] + "\n"
        self.sweep.write_text(report, encoding="utf-8")
        self.assert_sweep_refused("--sweep", str(self.sweep))
        result = self.sweep_command("push", "--lease", A, "--sweep", str(self.sweep))
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertIn("sweep rejected", result.stderr)

    def test_sweep_rejects_duplicate_and_unknown_class(self) -> None:
        """Duplicate and unknown classes refuse PR and leased push."""

        sha = git_output(self.repo, "rev-parse", "topic")
        report = full_sweep_report(sha)
        state_section = report.split("class: state-advance", 1)[1].split("class: doc-claims", 1)[0]
        for invalid in (
            report + "\nclass: state-advance" + state_section,
            report.replace("class: state-advance", "class: unknown-class"),
        ):
            with self.subTest(report=invalid):
                self.sweep.write_text(invalid, encoding="utf-8")
                self.assert_sweep_refused("--sweep", str(self.sweep))
                result = self.sweep_command("push", "--lease", A, "--sweep", str(self.sweep))
                self.assertNotEqual(result.returncode, 0, result.stderr)
                self.assertIn("sweep rejected", result.stderr)

    def test_sweep_rejects_one_section_on_other_head(self) -> None:
        """A stale doc-claims head refuses PR and leased push."""

        sha = git_output(self.repo, "rev-parse", "topic")
        report = full_sweep_report(sha).replace(
            f"class: doc-claims\nhead: {sha}", f"class: doc-claims\nhead: {A}"
        )
        self.sweep.write_text(report, encoding="utf-8")
        self.assert_sweep_refused("--sweep", str(self.sweep))
        result = self.sweep_command("push", "--lease", A, "--sweep", str(self.sweep))
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertIn("sweep rejected", result.stderr)

    def test_sweep_accepts_rows_in_later_section(self) -> None:
        """Reviewed state-advance rows allow PR and leased push."""

        sha = git_output(self.repo, "rev-parse", "topic")
        prefix, later = full_sweep_report(sha).split("class: state-advance", 1)
        report = (
            prefix
            + "class: state-advance"
            + later.replace("no hits\n", "| f.c:9 | ctx->n++ | 7 | fold |\n", 1)
        )
        self.sweep.write_text(report, encoding="utf-8")
        for command in ("pr", "push"):
            with self.subTest(command=command):
                options = ["--sweep", str(self.sweep)]
                if command == "push":
                    options = ["--lease", A, *options]
                result = self.sweep_command(command, *options)
                self.assertEqual(result.returncode, 0, result.stderr)
                if command == "push":
                    self.assertIn("stub push", result.stdout)

    def test_sweep_reviewed(self) -> None:
        """Reviewed fold, follow-up and no-variant rows allow PR and leased push."""

        self.add_sweep_rows(
            "fold",
            "follow-up: evidence/ancestor-reproducer.py",
            "no-variant: pre-existing check outside the series",
        )
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
        'if require_current_base "$1" 0 "$snapshot" "$sha" "$2"; then break; fi',
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


def top_level_trap_locals(source: str) -> list[str]:
    """Return names a top-level EXIT trap expands that some function also declares local."""

    lines = source.splitlines()
    try:
        case_start = lines.index('case "$cmd" in')
        case_end = lines.index("esac", case_start + 1)
    except ValueError as err:
        raise ValueError("dispatch case not found") from err

    local_names: set[str] = set()
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith("local "):
            continue
        for token in shlex.split(stripped.removeprefix("local "), comments=True):
            name = token.split("=", 1)[0]
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                local_names.add(name)

    top_lines = lines[case_start : case_end + 1] + [
        line for line in lines if line.startswith("trap ")
    ]
    trapped: set[str] = set()
    for line in top_lines:
        handlers = " ".join(re.findall(r"\btrap\s+'([^']*)'\s+EXIT\b", line))
        trapped.update(re.findall(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)", handlers))
    return sorted(trapped & local_names)


@contextlib.contextmanager
def mutant_case(
    source: str,
    old: str,
    new: str,
    base: type[unittest.TestCase],
    name: str,
) -> Iterator[type[unittest.TestCase]]:
    """Write one mutated helper into a checkout-shaped temp copy and yield its test case class."""

    with tempfile.TemporaryDirectory() as tmp:
        checkout = Path(tmp) / "checkout"
        scripts = checkout / "scripts"
        scripts.mkdir(parents=True)
        git(checkout, "init", "-q")
        shutil.copyfile(HELPER.parent / "base_claims.py", scripts / "base_claims.py")
        shutil.copyfile(HELPER.parent / "forge_text.py", scripts / "forge_text.py")
        copy_sweep_validator(scripts)
        mutant = scripts / "ffmpeg-forgejo"
        mutant.write_text(source.replace(old, new), encoding="utf-8")
        yield type(name, (base,), {"helper": mutant})


def run_selected(
    source: str, old: str, new: str, base: type[unittest.TestCase], test_name: str
) -> unittest.TestResult:
    """Run one selected test against the helper source with old replaced by new."""

    with mutant_case(source, old, new, base, "Selected") as case:
        result = unittest.TestResult()
        case(test_name).run(result)
        return result


def assert_mutant_killed(
    check: unittest.TestCase,
    source: str,
    mutation: tuple[str, str],
    selected: tuple[type[unittest.TestCase], str],
    label: str,
) -> unittest.TestResult:
    """Require the selected test to pass unmutated, then to fail or error with the mutation."""

    old, new = mutation
    base, test_name = selected
    clean = run_selected(source, old, old, base, test_name)
    check.assertTrue(clean.wasSuccessful(), f"{label}: {test_name} fails without the mutation")
    mutated = run_selected(source, old, new, base, test_name)
    check.assertTrue(mutated.failures or mutated.errors, f"{label} survived")
    return mutated


class TopLevelTrapTest(unittest.TestCase):
    """A top-level EXIT trap must not expand a name a function shadows with local."""

    PLANTED = (
        "f() {\n"
        "  local x body=1\n"
        "  exit 76\n"
        "}\n"
        'case "$cmd" in\n'
        "  a)\n"
        '    body="$(mktemp)"\n'
        "    trap 'rm -f \"$body\"' EXIT\n"
        "    f\n"
        "    ;;\n"
        "esac\n"
    )

    def test_helper_traps_name_no_function_local(self) -> None:
        """The helper's top-level EXIT traps expand no function-local variable names."""

        source = HELPER.read_text(encoding="utf-8")
        self.assertEqual(
            top_level_trap_locals(source),
            [],
            "top-level EXIT trap expands a function-local name",
        )
        self.assertEqual(source.count('trap \'rm -f "$reply_snapshot" "$reply_payload"\' EXIT'), 1)
        self.assertEqual(source.count("trap 'rm -f \"$close_payload\"' EXIT"), 1)

    def test_planted_shadowed_trap_is_found(self) -> None:
        """Shadowed top-level EXIT traps in dispatch branches or at column zero are reported."""

        self.assertEqual(top_level_trap_locals(self.PLANTED), ["body"])
        braced = self.PLANTED.replace('"$body"', '"${body}"')
        self.assertEqual(top_level_trap_locals(braced), ["body"])
        column_zero = "trap 'rm -f \"$body\"' EXIT\n" + self.PLANTED.replace(
            "    trap 'rm -f \"$body\"' EXIT\n", ""
        )
        self.assertEqual(top_level_trap_locals(column_zero), ["body"])

    def test_unshadowed_and_missing_case_boundaries(self) -> None:
        """Unshadowed traps pass, local flags are skipped, and a missing dispatch case fails."""

        unshadowed = self.PLANTED.replace('"$body"', '"$payload"')
        self.assertEqual(top_level_trap_locals(unshadowed), [])
        readonly_local = self.PLANTED.replace("local x body=1", "local -r body")
        self.assertEqual(top_level_trap_locals(readonly_local), ["body"])
        with self.assertRaises(ValueError):
            top_level_trap_locals(self.PLANTED.replace('case "$cmd" in\n', ""))
        with self.assertRaises(ValueError):
            top_level_trap_locals(self.PLANTED.replace("esac\n", ""))


class MutantTest(unittest.TestCase):
    """Each planted defect in the guard must make at least one EditBodyTest case fail."""

    def test_every_mutant_is_caught(self) -> None:
        """Every planted guard defect is killed by its selected edit-body case."""

        source = HELPER.read_text(encoding="utf-8")
        for name, (old, new, test_name) in MUTANTS.items():
            with self.subTest(mutant=name):
                self.assertEqual(source.count(old), 1, "mutation site missing")
                assert_mutant_killed(self, source, (old, new), (EditBodyTest, test_name), name)


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
        result = assert_mutant_killed(
            self,
            source,
            (old, ""),
            (FairyReviewsTest, "test_request_review_is_not_submitted"),
            "naive Fairy counter",
        )
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
                '"$OVERLAP_REF...$branch"',
                '"$BASE...$branch"',
                ("test_upstream_base_avoids_stale_local_master",),
            ),
        )
        for old, new, cases in mutations:
            self.assertEqual(source.count(old), 1, "mutation site missing")
            for name in cases:
                with self.subTest(test=name):
                    assert_mutant_killed(
                        self, source, (old, new), (PullRequestOverlapTest, name), name
                    )

    def test_overlap_verb_mutants_are_caught(self) -> None:
        """Each planted overlap verb defect fails its targeted test case."""

        source = HELPER.read_text(encoding="utf-8")
        mutations = (
            (
                '"$OVERLAP_REF...$branch" --',
                "\"$OVERLAP_REF...$branch\" -- 'lib*'",
                ("test_fate_list_overlap_is_listed",),
            ),
            (
                'overlapping_records+=("$number $shared")',
                'overlapping_records+=("$number $shared"); overlap_cleanup; return 77',
                ("test_every_overlapping_pull_is_listed",),
            ),
            (
                '[ "$count" -ne 0 ] || break',
                ":",
                ("test_zero_open_pulls_report_none",),
            ),
            (
                '"$OVERLAP_REF...$branch"',
                '"FETCH_HEAD...$branch"',
                ("test_stale_fetch_head_does_not_change_the_base",),
            ),
            (
                "--no-write-fetch-head ",
                "",
                ("test_stale_fetch_head_does_not_change_the_base",),
            ),
            (
                '[ "$snapshot_a" = "$snapshot_b" ]',
                "[ 0 -eq 0 ]",
                ("test_pull_closed_during_listing_is_rechecked",),
            ),
            (
                'shared+=("${page_shared[@]}")',
                'shared+=("${page_shared[@]}"); break',
                ("test_shared_paths_on_every_file_page_are_listed",),
            ),
            (
                '[[ " $allowed " == *" $number "* ]] && continue',
                '[[ -n "$allowed" && "$number" == "${numbers[0]}" ]] && continue',
                ("test_overlap_ok_skips_exact_pull",),
            ),
        )
        for old, new, cases in mutations:
            self.assertEqual(source.count(old), 1, "mutation site missing")
            for name in cases:
                with self.subTest(test=name):
                    assert_mutant_killed(self, source, (old, new), (OverlapVerbTest, name), name)


class MutantCaseTest(unittest.TestCase):
    """The mutation harness copy is a runnable repository helper."""

    def test_copy_has_siblings_in_a_checkout(self) -> None:
        """The temp copy sits at scripts/ffmpeg-forgejo beside every required validator."""

        with mutant_case(HELPER.read_text(encoding="utf-8"), "", "", EditBodyTest, "Probe") as case:
            self.assertEqual(case.helper.name, "ffmpeg-forgejo")
            self.assertEqual(case.helper.parent.name, "scripts")
            self.assertTrue((case.helper.parent / "base_claims.py").is_file())
            self.assertTrue((case.helper.parent / "forge_text.py").is_file())
            self.assertTrue((case.helper.parent / "variant_sweep.py").is_file())
            self.assertTrue((case.helper.parent.parent / ".git" / "HEAD").is_file())

    def test_broken_copy_is_reported(self) -> None:
        """A selected test that fails without the mutation fails the kill check by name."""

        class PlantedFailure(unittest.TestCase):
            helper = HELPER

            def test_planted(self) -> None:
                """Fail unconditionally."""

                self.fail("planted")

        old, new, _ = MUTANTS["seq loop"]
        with self.assertRaises(AssertionError) as raised:
            assert_mutant_killed(
                self,
                HELPER.read_text(encoding="utf-8"),
                (old, new),
                (PlantedFailure, "test_planted"),
                "planted",
            )
        self.assertIn("test_planted fails without the mutation", str(raised.exception))


class CloseMutantTest(unittest.TestCase):
    """A close verb without the author check lets the foreign pull test fail."""

    def test_dropped_author_check_is_caught(self) -> None:
        """The foreign pull test passes on the unmutated helper and fails once
        the author check is gone."""

        old = "if not isinstance(author, str) or author != me:"
        new = "if False:"
        source = HELPER.read_text(encoding="utf-8")
        self.assertEqual(source.count(old), 1, "mutation site missing")
        for replacement, expect_pass in ((old, True), (new, False)):
            with mutant_case(source, old, replacement, CloseTest, "MutatedClose") as case:
                result = unittest.TestResult()
                case("test_foreign_pull_is_refused_before_any_write").run(result)
                if expect_pass:
                    self.assertTrue(
                        result.wasSuccessful(),
                        "test_foreign_pull_is_refused_before_any_write fails without the mutation",
                    )
                else:
                    self.assertTrue(
                        result.failures or result.errors,
                        "dropped author check survived",
                    )


class ReplyMutantTest(unittest.TestCase):
    """Reply mutants dropping the anchor copy, range read-back or body snapshot,
    or shadowing the payload trap, are caught."""

    def test_reply_mutants_are_caught(self) -> None:
        """Each planted anchor-copy defect fails its targeted reply test case."""

        source = HELPER.read_text(encoding="utf-8")
        body_fn = source.split("\nreview_reply_body() {\n", 1)[1].split("\n}\n", 1)[0]
        id_old = 'row.get("id") == int(target_id)]'
        self.assertEqual(body_fn.count(id_old), 1, "mutation site missing")
        self.assertEqual(source.count(body_fn), 1, "mutation site missing")
        assert_mutant_killed(
            self,
            source,
            (body_fn, body_fn.replace(id_old, "True]", 1)),
            (ReplyTest, "test_reply_copies_the_anchor_of_the_answered_comment"),
            "target id filter removed",
        )
        pos_old = '"old_position": target["original_position"],'
        self.assertEqual(source.count(pos_old), 1, "mutation site missing")
        assert_mutant_killed(
            self,
            source,
            (pos_old, '"old_position": 0,'),
            (ReplyTest, "test_old_side_comment_keeps_its_old_position"),
            "old_position dropped",
        )
        reply_branch = source.split("\n  reply)\n", 1)[1].split("\n    ;;\n", 1)[0]
        self.assertEqual(reply_branch.count("reply_payload"), 5, "mutation site missing")
        self.assertEqual(source.count(reply_branch), 1, "mutation site missing")
        assert_mutant_killed(
            self,
            source,
            (reply_branch, reply_branch.replace("reply_payload", "body")),
            (ReplyTest, "test_moved_head_exit_removes_the_reply_payload"),
            "reply payload trap shadowed by a function local",
        )

    def test_reply_snapshot_and_range_mutants_are_caught(self) -> None:
        """Each planted snapshot or range defect fails its targeted reply test case."""

        source = HELPER.read_text(encoding="utf-8")
        reply_branch = source.split("\n  reply)\n", 1)[1].split("\n    ;;\n", 1)[0]
        self.assertEqual(source.count(reply_branch), 1, "mutation site missing")
        snapshot_mutations = (
            (
                'check_comment_duplicate "$1" "$reply_snapshot" "$me"',
                'check_comment_duplicate "$1" "$4" "$me"',
            ),
            (
                'review_reply_body "$2" "$3" "$reply_snapshot"',
                'review_reply_body "$2" "$3" "$4"',
            ),
            (
                'require_reply_posted "$2" "$3" "$reply_snapshot" "$me"',
                'require_reply_posted "$2" "$3" "$4" "$me"',
            ),
        )
        for old, new in snapshot_mutations:
            self.assertEqual(reply_branch.count(old), 1, "mutation site missing")
            assert_mutant_killed(
                self,
                source,
                (reply_branch, reply_branch.replace(old, new, 1)),
                (ReplyTest, "test_body_rewritten_after_listing_posts_the_snapshot"),
                f"reply body read again after the snapshot: {new}",
            )
        posted_fn = source.split("\nrequire_reply_posted() {\n", 1)[1].split("\n}\n", 1)[0]
        range_old = (
            '\n        and row.get("extra_lines_count", 0) == target.get("extra_lines_count", 0)'
        )
        self.assertEqual(posted_fn.count(range_old), 1, "mutation site missing")
        self.assertEqual(source.count(posted_fn), 1, "mutation site missing")
        assert_mutant_killed(
            self,
            source,
            (posted_fn, posted_fn.replace(range_old, "", 1)),
            (ReplyTest, "test_reply_stored_with_another_range_fails_read_back"),
            "reply read-back ignores the line range",
        )


def load_tests(
    loader: unittest.TestLoader, tests: unittest.TestSuite, pattern: str | None
) -> unittest.TestSuite:
    """Schedule independent methods separately without changing test IDs or bodies."""
    # No module/class fixtures: each method owns its scratch tree and HTTP server.
    # Singleton suites let --level class preserve class fixtures in other modules.
    return unit_test_budget.individual_test_suites(tests)


if __name__ == "__main__":
    unittest.main()
