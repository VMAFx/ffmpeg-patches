# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Tests for scripts/ffmpeg-forgejo against a stub Forgejo API."""

from __future__ import annotations

import http.server
import json
import os
import subprocess
import tempfile
import threading
import unittest
import unittest.mock
import urllib.parse
from pathlib import Path

from tests.helpers import isolated_git_environment

ROOT = Path(__file__).resolve().parent.parent
HELPER = ROOT / "scripts" / "ffmpeg-forgejo"
A = "a" * 40
B = "b" * 40
LIVE = "77c2f4b1c12b" + "0" * 28
STALE = "c1f8e9c2a0"
BODY = "new body\nline two\n"
OVERLAP_FILE = "libavfilter/vf_find_rect.c"
HELPER_SUBCOMMANDS = frozenset({"comment", "edit-body", "issue", "pr"})


def label(status: str, n: int = 0) -> dict:
    """Build an Autolabel status payload."""

    return {"context": f"Autolabel / Labeler {n} (pull_request_target)", "status": status}


def open_pull(number: int = 25612, login: str = "tester") -> dict:
    """Build one open pull request list item."""

    return {"number": number, "state": "open", "user": {"login": login}}


def git(repo: Path, *args: str) -> None:
    """Run Git with fixed argv in a temporary test repository."""

    subprocess.run(  # nosec B603 B607 - fixed git argv, temporary test repository, test literals
        ["git", "-c", "core.hooksPath=/dev/null", *args],
        cwd=repo,
        env=isolated_git_environment(),
        check=True,
        timeout=120,
    )


def git_output(repo: Path, *args: str) -> str:
    """Return the stripped output of Git with fixed argv in a temporary test repository."""

    return subprocess.run(  # nosec B603 B607 - fixed git argv, temporary test repository, test literals
        ["git", "-c", "core.hooksPath=/dev/null", *args],
        cwd=repo,
        env=isolated_git_environment(),
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    ).stdout.strip()


OTHER = {"context": "Test / Fate", "status": "pending"}
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
        """Fixture Git commands bypass hooks from the caller's global config."""

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            hooks = root / "hooks"
            hooks.mkdir()
            pre_commit = hooks / "pre-commit"
            pre_commit.write_text("#!/bin/sh\nexit 1\n")
            pre_commit.chmod(0o755)
            global_config = root / "gitconfig"
            global_config.write_text(f"[core]\n\thooksPath = {hooks}\n")
            with unittest.mock.patch.dict(
                os.environ,
                {"HOME": str(root / "home"), "GIT_CONFIG_GLOBAL": str(global_config)},
            ):
                self.assertEqual(
                    isolated_git_environment()["GIT_CONFIG_GLOBAL"], str(global_config)
                )
                git(repo, "init", "-q", "-b", "master")
                repo.joinpath("tracked").write_text("content\n")
                git(repo, "add", "tracked")
                git(
                    repo,
                    "-c",
                    "user.name=Test",
                    "-c",
                    "user.email=test@example.invalid",
                    "-c",
                    "commit.gpgSign=false",
                    "commit",
                    "-qm",
                    "fixture",
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
        fail_status: bool = False,
        pull_status: int = 200,
        reviewers: list[list[str]] | None = None,
    ):
        """Initialize scripted pull, status, and reviewer responses."""

        heads = heads or [A]
        polls = polls or {}
        self.heads, self.polls, self.fail_status = heads, polls, fail_status
        self.pull_status = pull_status
        self.reviewers = reviewers or [[]]
        self.open_pulls: list[dict] = []
        self.pull_files: dict[int, list[ChangedFile]] = {}
        self.fail_pulls = False
        self.fail_files: set[int] = set()
        self.head_reads = 0
        self.reviewer_reads = 0
        self.poll_index = {sha: -1 for sha in polls}
        self.requests: list[tuple[str, str, str]] = []
        self.pull_headers: list[dict[str, str]] = []

    def head(self) -> object:
        """Return the next scripted pull head, repeating the last."""

        sha = self.heads[min(self.head_reads, len(self.heads) - 1)]
        self.head_reads += 1
        return sha

    def statuses(self, sha: str, query: dict[str, list[str]]) -> list[dict]:
        """Return the requested page from the current scripted status poll."""

        page = int(query.get("page", ["1"])[0])
        limit = int(query.get("limit", ["30"])[0])
        if page == 1:
            self.poll_index[sha] += 1
        seq = self.polls.get(sha, [[]])
        full = seq[min(max(self.poll_index.get(sha, 0), 0), len(seq) - 1)]
        return full[(page - 1) * limit : page * limit]

    def pull(self) -> dict:
        """Return a pull payload with the next scripted reviewer sets."""

        reviewers = self.reviewers[min(self.reviewer_reads, len(self.reviewers) - 1)]
        self.reviewer_reads += 1
        return {
            "number": 1,
            "head": {"sha": self.head()},
            "html_url": "stub/pulls/1",
            "requested_reviewers": [
                {"login": login} for login in reviewers if not login.startswith("team:")
            ],
            "requested_reviewers_teams": [
                {"name": name.removeprefix("team:")}
                for name in reviewers
                if name.startswith("team:")
            ],
        }

    def pulls(self, query: dict[str, list[str]]) -> list[dict]:
        """Return one filtered page of open pull requests."""

        poster = query.get("poster", [None])[0]
        rows = [row for row in self.open_pulls if poster is None or row["user"]["login"] == poster]
        page = int(query.get("page", ["1"])[0])
        limit = int(query.get("limit", ["30"])[0])
        return rows[(page - 1) * limit : page * limit]

    def files(self, number: int, query: dict[str, list[str]]) -> list[dict] | None:
        """Return one page of changed files for a pull request."""

        page = int(query.get("page", ["1"])[0])
        limit = int(query.get("limit", ["30"])[0])
        rows = self.pull_files.get(number, [])
        result = [
            row if isinstance(row, dict) else {"filename": row}
            for row in rows[(page - 1) * limit : page * limit]
        ]
        return None if page > 1 and not result else result


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

    def do_GET(self) -> None:
        """Serve scripted user, pull, and commit-status reads."""

        stub = self.stub()
        url = urllib.parse.urlsplit(self.path)
        stub.requests.append(("GET", self.path, ""))
        parts = url.path.split("/")
        is_status = len(parts) > 3 and parts[-3] == "commits" and parts[-1] == "status"
        is_pull_list = url.path.endswith("/pulls")
        is_pull_files = len(parts) > 3 and parts[-3] == "pulls" and parts[-1] == "files"
        if url.path == "/api/v1/user":
            self.reply(200, {"login": "tester"})
        elif is_pull_list and stub.fail_pulls:
            self.reply(500, {"message": "pull list failed"})
        elif is_pull_list:
            self.reply(200, stub.pulls(urllib.parse.parse_qs(url.query)))
        elif is_pull_files and int(parts[-2]) in stub.fail_files:
            self.reply(500, {"message": "pull files failed"})
        elif is_pull_files:
            self.reply(200, stub.files(int(parts[-2]), urllib.parse.parse_qs(url.query)))
        elif url.path.endswith("/pulls/1"):
            stub.pull_headers.append(dict(self.headers.items()))
            if stub.pull_status == 200:
                self.reply(200, stub.pull())
            else:
                self.reply(stub.pull_status, {"message": "pull read failed"})
        elif is_status and stub.fail_status:
            self.reply(500, {"message": "boom"})
        elif is_status:
            st = stub.statuses(parts[-2], urllib.parse.parse_qs(url.query))
            self.reply(200, {"statuses": st, "total_count": len(st)})
        else:
            self.reply(404, {})

    def do_POST(self) -> None:
        """Record and serve pull creation and issue-comment writes."""

        data = self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode()
        self.stub().requests.append(("POST", self.path, data))
        if self.path.endswith("/pulls"):
            self.reply(201, {"number": 1, "html_url": "stub/pulls/1"})
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
        tool.write_text("#!/bin/sh\necho testtoken\n")
        tool.chmod(0o755)
        self.bin_dir = bin_dir
        (self.tmp / "body.md").write_text(BODY)
        self.repo = self.tmp / "ffmpeg"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "master")
        candidate = self.repo / OVERLAP_FILE
        candidate.parent.mkdir(parents=True)
        candidate.write_text("base\n")
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
        candidate.write_text("topic\n")
        git(self.repo, "add", OVERLAP_FILE)
        git(self.repo, *commit, "topic")

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
        server = serve(stub)
        try:
            env = isolated_git_environment()
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


class EditBodyTest(ForgejoTest):
    def run_helper(self, stub: Stub, *args: str, path_first: Path | None = None) -> int:
        """Run an unclaimed body edit and return its status."""

        proc = self.run_command(
            stub,
            "edit-body",
            *args,
            "--no-test-claims",
            "1",
            str(self.tmp / "body.md"),
            path_first=path_first,
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
        """Pending or absent Autolabel statuses refuse a body edit."""

        self.assert_refused(Stub([A], {A: [[label("pending"), OTHER]]}))
        self.assert_refused(Stub([A], {A: [[OTHER]]}))

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
        seq.write_text("#!/bin/sh\nexit 1\n")
        seq.chmod(0o755)
        stub = Stub([A], {A: [[label("pending")]]})
        self.assertEqual(self.run_helper(stub, path_first=broken), 75)
        self.assertEqual(self.patches(stub), [])


class TestClaimHeadTest(ForgejoTest):
    def comment(self, stub: Stub, *args: str) -> subprocess.CompletedProcess[str]:
        """Run a comment command with the shared issue and body arguments."""

        return self.run_command(stub, "comment", *args, "1", str(self.tmp / "body.md"))

    def edit_body(self, stub: Stub, *args: str) -> subprocess.CompletedProcess[str]:
        """Run an edit-body command with the shared pull and body arguments."""

        return self.run_command(stub, "edit-body", *args, "1", str(self.tmp / "body.md"))

    def posts(self, stub: Stub) -> list[str]:
        """Return bodies sent by issue-comment POST requests."""

        return self.sent_bodies(stub, "POST")

    def patches(self, stub: Stub) -> list[str]:
        """Return bodies sent by pull-body PATCH requests."""

        return self.sent_bodies(stub, "PATCH")

    def test_stale_tested_head_is_refused_without_post(self) -> None:
        """A stale tested head exits 76 without posting a comment."""

        stub = Stub([LIVE])
        proc = self.comment(stub, "--tested-head", STALE)
        self.assertEqual(proc.returncode, 76)
        self.assertIn(STALE, proc.stderr)
        self.assertIn(LIVE, proc.stderr)
        self.assertEqual(self.posts(stub), [])

    def test_comment_pull_server_failure_exits_one_without_post(self) -> None:
        """A pull HTTP 500 exits 1 with its status and never posts a comment."""

        stub = Stub(pull_status=500)
        proc = self.comment(stub, "--tested-head", A)
        self.assertEqual(proc.returncode, 1)
        self.assertIn(
            f"cannot read the head of #1 (HTTP 500); tested head {A} not checked",
            proc.stderr,
        )
        self.assertEqual(self.posts(stub), [])

    def test_edit_body_pull_auth_failure_exits_one_without_patch(self) -> None:
        """A pull HTTP 401 exits 1 with its status and never patches the body."""

        stub = Stub(pull_status=401)
        proc = self.edit_body(stub, "--tested-head", A)
        self.assertEqual(proc.returncode, 1)
        self.assertIn(
            f"cannot read the head of #1 (HTTP 401); tested head {A} not checked",
            proc.stderr,
        )
        self.assertEqual(self.patches(stub), [])

    def test_curlrc_fail_keeps_a_missing_pull_as_exit_76(self) -> None:
        """A curlrc fail option cannot turn a missing pull into a curl failure."""

        (self.tmp / ".curlrc").write_text("fail\n")
        stub = Stub(pull_status=404)
        proc = self.run_command(
            stub,
            "comment",
            "--tested-head",
            A,
            "1",
            str(self.tmp / "body.md"),
            env_updates={"HOME": str(self.tmp), "CURL_HOME": str(self.tmp)},
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
        curl.write_text('#!/bin/sh\nprintf \'%s\\n\' "$*" >>"$CURL_LOG"\nexit 7\n')
        curl.chmod(0o755)
        stub = Stub()
        proc = self.run_command(
            stub,
            "comment",
            "--tested-head",
            A,
            "1",
            str(self.tmp / "body.md"),
            path_first=fake_bin,
            env_updates={"CURL_LOG": str(log)},
        )
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("curl rc 7", proc.stderr)
        invocations = log.read_text().splitlines()
        self.assertEqual(len(invocations), 1)
        self.assertIn("-X GET", invocations[0])

    def test_curlrc_header_reaches_the_pull_read(self) -> None:
        """The tested-head pull read preserves headers configured in curlrc."""

        (self.tmp / ".curlrc").write_text('header = "X-Curlrc: 1"\n')
        stub = Stub()
        proc = self.run_command(
            stub,
            "comment",
            "--tested-head",
            A,
            "1",
            str(self.tmp / "body.md"),
            env_updates={"HOME": str(self.tmp), "CURL_HOME": str(self.tmp)},
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(stub.pull_headers[0].get("X-Curlrc"), "1")

    def test_null_live_head_exits_one_without_post(self) -> None:
        """A null live head exits 1 as unreadable without posting a comment."""

        stub = Stub([None])
        proc = self.comment(stub, "--tested-head", A)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("cannot read the head", proc.stderr)
        self.assertEqual(self.posts(stub), [])

    def test_short_live_head_exits_one_without_post(self) -> None:
        """A short live head exits 1 as unreadable without posting a comment."""

        stub = Stub(["abc"])
        proc = self.comment(stub, "--tested-head", A)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("cannot read the head", proc.stderr)
        self.assertEqual(self.posts(stub), [])

    def test_matching_seven_character_live_head_exits_one_without_post(self) -> None:
        """A matching seven-character live head cannot authorize a comment."""

        short = LIVE[:7]
        stub = Stub([short])
        proc = self.comment(stub, "--tested-head", short)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("cannot read the head", proc.stderr)
        self.assertEqual(self.posts(stub), [])

    def test_matching_prefix_and_full_head_are_posted(self) -> None:
        """Both matching head prefixes and full hashes authorize a comment."""

        for tested in (LIVE[:12], LIVE):
            with self.subTest(tested=tested):
                stub = Stub([LIVE])
                proc = self.comment(stub, "--tested-head", tested)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(self.posts(stub), [BODY])

    def test_invalid_tested_heads_are_usage_errors(self) -> None:
        """Short and nonhex tested heads are usage errors without writes."""

        for tested in (LIVE[:6], "not-hex"):
            with self.subTest(tested=tested):
                stub = Stub([LIVE])
                proc = self.comment(stub, "--tested-head", tested)
                self.assertEqual(proc.returncode, 64)
                self.assertIn("usage: comment", proc.stderr)
                self.assertEqual(self.posts(stub), [])

    def test_missing_or_conflicting_claim_options_are_usage_errors(self) -> None:
        """Missing or conflicting claim modes are rejected without writes."""

        for args in ((), ("--tested-head", LIVE, "--no-test-claims")):
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
        proc = self.edit_body(stub, "--tested-head", A)
        self.assertEqual(proc.returncode, 76)
        self.assertIn(A, proc.stderr)
        self.assertIn(B, proc.stderr)
        self.assertEqual(self.patches(stub), [])

    def test_edit_body_matching_tested_head_is_patched(self) -> None:
        """A matching tested-head prefix authorizes the body PATCH."""

        stub = Stub([A], {A: [[label("success")]]})
        proc = self.edit_body(stub, "--tested-head", A[:12])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.patches(stub), [BODY])

    def test_edit_body_requires_exactly_one_claim_option(self) -> None:
        """Body edits require exactly one claim mode and make no invalid PATCH."""

        for args in ((), ("--tested-head", A, "--no-test-claims")):
            with self.subTest(args=args):
                stub = Stub([A], {A: [[label("success")]]})
                proc = self.edit_body(stub, *args)
                self.assertEqual(proc.returncode, 64)
                self.assertIn("usage: edit-body", proc.stderr)
                self.assertEqual(self.patches(stub), [])

    def test_issue_refuses_tested_head_and_accepts_no_test_claims(self) -> None:
        """Issue comments reject tested-head claims but accept no-test claims."""

        stub = Stub(pull_status=404)
        proc = self.comment(stub, "--tested-head", A)
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
        proc = self.edit_body(stub, "--tested-head", A)
        self.assertEqual(proc.returncode, 76)
        self.assertIn(f"#1 is not a pull request; tested head {A} cannot be checked", proc.stderr)
        self.assertEqual(self.patches(stub), [])


class PullRequestReviewerTest(ForgejoTest):
    def pr(self, stub: Stub) -> subprocess.CompletedProcess[str]:
        """Create a pull request and read its requested reviewers back."""

        return self.run_command(
            stub,
            "pr",
            "topic",
            "title",
            str(self.tmp / "body.md"),
        )

    def test_empty_reviewers_three_times_exits_three(self) -> None:
        """Three empty user and team reviewer reads exit 3."""

        stub = Stub(reviewers=[[], [], []])
        proc = self.pr(stub)
        self.assertEqual(proc.returncode, 3)
        self.assertEqual(proc.stdout, "stub/pulls/1\n")
        self.assertIn("#1 has no requested reviewer", proc.stderr)
        reads = [
            path for method, path, _ in stub.requests if method == "GET" and "/pulls/1" in path
        ]
        self.assertEqual(len(reads), 3)

    def test_requested_reviewers_are_printed(self) -> None:
        """Requested user reviewers are printed after pull creation."""

        stub = Stub(reviewers=[["alice", "bob"]])
        proc = self.pr(stub)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            proc.stdout,
            "stub/pulls/1\n#1 requested reviewers: alice bob\n",
        )

    def test_requested_team_reviewers_are_printed(self) -> None:
        """A team-only reviewer read succeeds and prints its prefixed name."""

        stub = Stub(reviewers=[["team:codec"], ["team:codec"], ["team:codec"]])
        proc = self.pr(stub)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            proc.stdout,
            "stub/pulls/1\n#1 requested reviewers: team:codec\n",
        )


class PullRequestOverlapTest(ForgejoTest):
    def pr(self, stub: Stub, *options: str) -> subprocess.CompletedProcess[str]:
        """Attempt to create the shared topic pull request."""

        return self.run_command(
            stub,
            "pr",
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
        upstream_work.joinpath("upstream-file").write_text("upstream\n")
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
        self.repo.joinpath(OVERLAP_FILE).write_text("topic on upstream\n")
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


MUTANTS = {
    "guard removed": ('if [ "$state" = "done" ]; then', "if true; then"),
    "head not rechecked": ('[ "$(head_sha "$1")" = "$sha" ] && break', "break"),
    "first page only": ("page <= 20; page++", "page <= 1; page++"),
    "terminal set narrowed": ('"warning", "skipped"}', "}"),
    "wait ignored": ('if [ "$wait" = 0 ] || [ "$i" = 80 ]; then', "if true; then"),
    "seq loop": ("for ((i = 1; i <= 80; i++)); do", "for i in $(seq 1 80); do"),
}


class MutantTest(unittest.TestCase):
    """Each planted defect in the guard must make at least one EditBodyTest case fail."""

    def test_every_mutant_is_caught(self) -> None:
        """Every planted guard defect is killed by the edit-body suite."""

        source = HELPER.read_text()
        for name, (old, new) in MUTANTS.items():
            with self.subTest(mutant=name):
                self.assertEqual(source.count(old), 1, "mutation site missing")
                with tempfile.TemporaryDirectory() as tmp:
                    mutant = Path(tmp) / "ffmpeg-forgejo"
                    mutant.write_text(source.replace(old, new))
                    case = type("Mutated", (EditBodyTest,), {"helper": mutant})
                    suite = unittest.defaultTestLoader.loadTestsFromTestCase(case)
                    result = unittest.TestResult()
                    suite.run(result)
                    self.assertTrue(result.failures or result.errors, f"{name} survived")


class PullOverlapMutantTest(unittest.TestCase):
    """Check that pagination and upstream regressions are detected."""

    def test_overlap_mutants_are_caught(self) -> None:
        """Each planted pagination or base defect fails at least one overlap case."""

        source = HELPER.read_text()
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
            with tempfile.TemporaryDirectory() as tmp:
                mutant = Path(tmp) / "ffmpeg-forgejo"
                mutant.write_text(source.replace(old, new))
                case = type("MutatedOverlap", (PullRequestOverlapTest,), {"helper": mutant})
                for name in cases:
                    with self.subTest(test=name):
                        result = unittest.TestResult()
                        case(name).run(result)
                        self.assertTrue(result.failures or result.errors, f"{name} survived")


if __name__ == "__main__":
    unittest.main()
