# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Tests for the edit-body guard of scripts/ffmpeg-forgejo against a stub Forgejo API."""

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
        self, heads: list[str], polls: dict[str, list[list[dict]]], fail_status: bool = False
    ):
        self.heads, self.polls, self.fail_status = heads, polls, fail_status
        self.head_reads = 0
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


def serve(stub: Stub) -> http.server.HTTPServer:
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            del format, args

        def reply(self, code: int, obj: object) -> None:
            data = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            url = urllib.parse.urlsplit(self.path)
            stub.requests.append(("GET", self.path, ""))
            if url.path.endswith("/pulls/1"):
                return self.reply(200, {"head": {"sha": stub.head()}, "html_url": "stub/pulls/1"})
            parts = url.path.split("/")
            if len(parts) > 3 and parts[-3] == "commits" and parts[-1] == "status":
                if stub.fail_status:
                    return self.reply(500, {"message": "boom"})
                st = stub.statuses(parts[-2], urllib.parse.parse_qs(url.query))
                return self.reply(200, {"statuses": st, "total_count": len(st)})
            self.reply(404, {})

        def do_PATCH(self) -> None:
            data = self.rfile.read(int(self.headers["Content-Length"])).decode()
            stub.requests.append(("PATCH", self.path, data))
            self.reply(200, {"html_url": "stub/pulls/1"})

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class EditBodyTest(unittest.TestCase):
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

    def run_helper(self, stub: Stub, *args: str, path_first: Path | None = None) -> int:
        server = serve(stub)
        try:
            env = dict(os.environ)
            dirs = [str(p) for p in (path_first, self.bin_dir) if p]
            env.update(
                PATH=os.pathsep.join(dirs + [env.get("PATH", "")]),
                FFMPEG_FORGEJO_API=f"http://127.0.0.1:{server.server_port}/api/v1",
                FFMPEG_FORGEJO_POLL="0",
            )
            proc = subprocess.run(
                ["bash", str(self.helper), "edit-body", *args, "1", str(self.tmp / "body.md")],
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            return proc.returncode
        finally:
            server.shutdown()
            server.server_close()

    def patches(self, stub: Stub) -> list[str]:
        return [json.loads(d)["body"] for m, _, d in stub.requests if m == "PATCH"]

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


MUTANTS = {
    "guard removed": ('if [ "$state" = done ]; then', "if true; then"),
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
