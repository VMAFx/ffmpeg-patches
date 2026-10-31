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
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HELPER = ROOT / "scripts" / "ffmpeg-forgejo"
A = "a" * 40
B = "b" * 40
LIVE = "77c2f4b1c12b" + "0" * 28
STALE = "c1f8e9c2a0"
BODY = "new body\nline two\n"


def label(status: str, n: int = 0) -> dict:
    return {"context": f"Autolabel / Labeler {n} (pull_request_target)", "status": status}


OTHER = {"context": "Test / Fate", "status": "pending"}


class Stub:
    """Forgejo API stub.

    heads: head sha returned by successive GET /pulls/1 (the last one repeats).
    polls: sha -> list of status lists, one per poll (page 1 starts a poll; the last repeats).
    fail_status: answer the status endpoint with HTTP 500.
    """

    def __init__(
        self,
        heads: list[str] | None = None,
        polls: dict[str, list[list[dict]]] | None = None,
        fail_status: bool = False,
        pull_exists: bool = True,
        reviewers: list[list[str]] | None = None,
    ):
        heads = heads or [A]
        polls = polls or {}
        self.heads, self.polls, self.fail_status = heads, polls, fail_status
        self.pull_exists = pull_exists
        self.reviewers = reviewers or [[]]
        self.head_reads = 0
        self.reviewer_reads = 0
        self.poll_index = {sha: -1 for sha in polls}
        self.requests: list[tuple[str, str, str]] = []

    def head(self) -> str:
        sha = self.heads[min(self.head_reads, len(self.heads) - 1)]
        self.head_reads += 1
        return sha

    def statuses(self, sha: str, query: dict[str, list[str]]) -> list[dict]:
        page = int(query.get("page", ["1"])[0])
        limit = int(query.get("limit", ["30"])[0])
        if page == 1:
            self.poll_index[sha] += 1
        seq = self.polls.get(sha, [[]])
        full = seq[min(max(self.poll_index.get(sha, 0), 0), len(seq) - 1)]
        return full[(page - 1) * limit : page * limit]

    def pull(self) -> dict:
        reviewers = self.reviewers[min(self.reviewer_reads, len(self.reviewers) - 1)]
        self.reviewer_reads += 1
        return {
            "number": 1,
            "head": {"sha": self.head()},
            "html_url": "stub/pulls/1",
            "requested_reviewers": [{"login": login} for login in reviewers],
        }


class Handler(http.server.BaseHTTPRequestHandler):
    def stub(self) -> Stub:
        return self.server.stub  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        """Keep request logging out of the test output."""

    def reply(self, code: int, obj: object) -> None:
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        stub = self.stub()
        url = urllib.parse.urlsplit(self.path)
        stub.requests.append(("GET", self.path, ""))
        parts = url.path.split("/")
        is_status = len(parts) > 3 and parts[-3] == "commits" and parts[-1] == "status"
        if url.path == "/api/v1/user":
            self.reply(200, {"login": "tester"})
        elif url.path.endswith("/pulls/1") and stub.pull_exists:
            self.reply(200, stub.pull())
        elif is_status and stub.fail_status:
            self.reply(500, {"message": "boom"})
        elif is_status:
            st = stub.statuses(parts[-2], urllib.parse.parse_qs(url.query))
            self.reply(200, {"statuses": st, "total_count": len(st)})
        else:
            self.reply(404, {})

    def do_POST(self) -> None:
        data = self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode()
        self.stub().requests.append(("POST", self.path, data))
        if self.path.endswith("/pulls"):
            self.reply(201, {"number": 1, "html_url": "stub/pulls/1"})
        elif self.path.endswith("/issues/1/comments"):
            self.reply(201, {"html_url": "stub/issues/1#issuecomment-1"})
        else:
            self.reply(404, {})

    def do_PATCH(self) -> None:
        data = self.rfile.read(int(self.headers["Content-Length"])).decode()
        self.stub().requests.append(("PATCH", self.path, data))
        self.reply(200, {"html_url": "stub/pulls/1"})


def serve(stub: Stub) -> http.server.HTTPServer:
    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    server.stub = stub  # type: ignore[attr-defined]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class ForgejoTest(unittest.TestCase):
    helper: Path = HELPER

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        tool = bin_dir / "secret-tool"
        tool.write_text("#!/bin/sh\necho testtoken\n")
        tool.chmod(0o755)
        self.bin_dir = bin_dir
        (self.tmp / "body.md").write_text(BODY)

    def run_command(
        self, stub: Stub, *args: str, path_first: Path | None = None
    ) -> subprocess.CompletedProcess[str]:
        server = serve(stub)
        try:
            env = dict(os.environ)
            dirs = [str(p) for p in (path_first, self.bin_dir) if p]
            env.update(
                PATH=os.pathsep.join(dirs + [env.get("PATH", "")]),
                FFMPEG_FORGEJO_API=f"http://127.0.0.1:{server.server_port}/api/v1",
                FFMPEG_FORGEJO_POLL="0",
            )
            return subprocess.run(
                ["bash", str(self.helper), *args],
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        finally:
            server.shutdown()
            server.server_close()

    def sent_bodies(self, stub: Stub, method: str) -> list[str]:
        return [json.loads(data)["body"] for got, _, data in stub.requests if got == method]


class EditBodyTest(ForgejoTest):
    def run_helper(self, stub: Stub, *args: str, path_first: Path | None = None) -> int:
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
        return self.sent_bodies(stub, "PATCH")

    def assert_refused(self, stub: Stub, *args: str) -> None:
        self.assertEqual(self.run_helper(stub, *args), 75)
        self.assertEqual(self.patches(stub), [])

    def assert_edited(self, stub: Stub, *args: str) -> None:
        self.assertEqual(self.run_helper(stub, *args), 0)
        self.assertEqual(self.patches(stub), [BODY])

    def test_pending_and_none_are_refused(self) -> None:
        self.assert_refused(Stub([A], {A: [[label("pending"), OTHER]]}))
        self.assert_refused(Stub([A], {A: [[OTHER]]}))

    def test_every_terminal_state_allows_the_edit(self) -> None:
        for status in ("success", "error", "failure", "warning", "skipped"):
            with self.subTest(status=status):
                self.assert_edited(Stub([A], {A: [[label(status), OTHER]]}))

    def test_unknown_state_is_refused(self) -> None:
        self.assert_refused(Stub([A], {A: [[label("running")]]}))

    def test_any_pending_context_refuses_in_either_order(self) -> None:
        self.assert_refused(Stub([A], {A: [[label("success", 0), label("pending", 1)]]}))
        self.assert_refused(Stub([A], {A: [[label("pending", 0), label("success", 1)]]}))

    def test_pending_context_on_a_later_page_is_seen(self) -> None:
        filler = [{"context": f"Test / job {n}", "status": "success"} for n in range(50)]
        stub = Stub([A], {A: [[label("success")] + filler + [label("pending", 1)]]})
        self.assert_refused(stub)
        self.assertTrue(any("page=2" in p for _, p, _ in stub.requests))

    def test_wait_polls_until_autolabel_finishes(self) -> None:
        stub = Stub([A], {A: [[], [label("pending")], [label("success")]]})
        self.assert_edited(stub, "--wait")
        self.assertGreaterEqual(stub.poll_index[A], 2)

    def test_wait_gives_up_after_80_checks(self) -> None:
        stub = Stub([A], {A: [[label("pending")]]})
        self.assert_refused(stub, "--wait")
        self.assertEqual(stub.poll_index[A], 79)

    def test_a_push_during_the_check_is_not_cleared_by_the_old_head(self) -> None:
        polls = {A: [[label("success")]], B: [[label("pending")]]}
        self.assert_refused(Stub([A, B], polls))
        stub = Stub([A, B], {A: [[label("success")]], B: [[label("pending")], [label("success")]]})
        self.assert_edited(stub, "--wait")
        self.assertGreaterEqual(stub.poll_index[B], 1)

    def test_api_failure_never_edits(self) -> None:
        stub = Stub([A], {A: [[label("success")]]}, fail_status=True)
        self.assertNotEqual(self.run_helper(stub), 0)
        self.assertEqual(self.patches(stub), [])

    def test_a_failing_seq_does_not_skip_the_guard(self) -> None:
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
        return self.run_command(stub, "comment", *args, "1", str(self.tmp / "body.md"))

    def edit_body(self, stub: Stub, *args: str) -> subprocess.CompletedProcess[str]:
        return self.run_command(stub, "edit-body", *args, "1", str(self.tmp / "body.md"))

    def posts(self, stub: Stub) -> list[str]:
        return self.sent_bodies(stub, "POST")

    def patches(self, stub: Stub) -> list[str]:
        return self.sent_bodies(stub, "PATCH")

    def test_stale_tested_head_is_refused_without_post(self) -> None:
        stub = Stub([LIVE])
        proc = self.comment(stub, "--tested-head", STALE)
        self.assertEqual(proc.returncode, 76)
        self.assertIn(STALE, proc.stderr)
        self.assertIn(LIVE, proc.stderr)
        self.assertEqual(self.posts(stub), [])

    def test_matching_prefix_and_full_head_are_posted(self) -> None:
        for tested in (LIVE[:12], LIVE):
            with self.subTest(tested=tested):
                stub = Stub([LIVE])
                proc = self.comment(stub, "--tested-head", tested)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(self.posts(stub), [BODY])

    def test_invalid_tested_heads_are_usage_errors(self) -> None:
        for tested in (LIVE[:6], "not-hex"):
            with self.subTest(tested=tested):
                stub = Stub([LIVE])
                proc = self.comment(stub, "--tested-head", tested)
                self.assertEqual(proc.returncode, 64)
                self.assertIn("usage: comment", proc.stderr)
                self.assertEqual(self.posts(stub), [])

    def test_missing_or_conflicting_claim_options_are_usage_errors(self) -> None:
        for args in ((), ("--tested-head", LIVE, "--no-test-claims")):
            with self.subTest(args=args):
                stub = Stub([LIVE])
                proc = self.comment(stub, *args)
                self.assertEqual(proc.returncode, 64)
                self.assertIn("usage: comment", proc.stderr)
                self.assertEqual(self.posts(stub), [])

    def test_no_test_claims_posts_without_reading_the_head(self) -> None:
        stub = Stub([LIVE])
        proc = self.comment(stub, "--no-test-claims")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(stub.head_reads, 0)
        self.assertEqual(self.posts(stub), [BODY])

    def test_edit_body_uses_head_read_after_autolabel(self) -> None:
        stub = Stub([A, A, B], {A: [[label("success")]]})
        proc = self.edit_body(stub, "--tested-head", A)
        self.assertEqual(proc.returncode, 76)
        self.assertIn(A, proc.stderr)
        self.assertIn(B, proc.stderr)
        self.assertEqual(self.patches(stub), [])

    def test_edit_body_matching_tested_head_is_patched(self) -> None:
        stub = Stub([A], {A: [[label("success")]]})
        proc = self.edit_body(stub, "--tested-head", A[:12])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.patches(stub), [BODY])

    def test_edit_body_requires_exactly_one_claim_option(self) -> None:
        for args in ((), ("--tested-head", A, "--no-test-claims")):
            with self.subTest(args=args):
                stub = Stub([A], {A: [[label("success")]]})
                proc = self.edit_body(stub, *args)
                self.assertEqual(proc.returncode, 64)
                self.assertIn("usage: edit-body", proc.stderr)
                self.assertEqual(self.patches(stub), [])

    def test_issue_refuses_tested_head_and_accepts_no_test_claims(self) -> None:
        stub = Stub(pull_exists=False)
        proc = self.comment(stub, "--tested-head", A)
        self.assertEqual(proc.returncode, 76)
        self.assertIn("not a pull request", proc.stderr)
        self.assertEqual(self.posts(stub), [])

        stub = Stub(pull_exists=False)
        proc = self.comment(stub, "--no-test-claims")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.posts(stub), [BODY])


class PullRequestReviewerTest(ForgejoTest):
    def pr(self, stub: Stub) -> subprocess.CompletedProcess[str]:
        return self.run_command(
            stub,
            "pr",
            "topic",
            "title",
            str(self.tmp / "body.md"),
        )

    def test_empty_reviewers_three_times_exits_three(self) -> None:
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
        stub = Stub(reviewers=[["alice", "bob"]])
        proc = self.pr(stub)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            proc.stdout,
            "stub/pulls/1\n#1 requested reviewers: alice bob\n",
        )


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


if __name__ == "__main__":
    unittest.main()
