# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Tests for scripts/upstream_drift.py with a fake forge; nothing is fetched."""

from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import upstream_drift  # noqa: E402
from test_series import Fixture  # noqa: E402

FORGE = "https://code.ffmpeg.org/FFmpeg/FFmpeg/pulls/"
API = "https://code.ffmpeg.org/api/v1/repos/FFmpeg/FFmpeg/pulls/"
COMMIT = "7e93014c5bbb61e7c22afebb478bf92f7a08d59a"
MERGED_AT = "2026-10-12T03:43:42Z"


def pull(number: int, **changes: object) -> dict:
    """A forge answer that matches Fixture.record(number) with code.ffmpeg.org links."""
    answer = {
        "number": number,
        "title": f"lib: change {number}",
        "base": {"ref": "master"},
        "state": "open",
        "merged": False,
        "merged_at": None,
        "merge_commit_sha": None,
    }
    return answer | changes


class FakeForge:
    """Answers from a dict keyed by API URL; records which URLs were read."""

    def __init__(self, answers: dict[int, dict]) -> None:
        self.answers = {f"{API}{number}": answer for number, answer in answers.items()}
        self.read: list[str] = []

    def __call__(self, url: str) -> dict:
        self.read.append(url)
        if url not in self.answers:
            raise upstream_drift.DriftError(f"{url}: HTTP Error 404: Not Found")
        return self.answers[url]


class DriftTest(unittest.TestCase):
    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.scratch = Path(scratch.name)
        self.fx = Fixture(self.scratch)
        for name in self.fx.names:
            metadata = self.fx.root / "patches" / Path(name).with_suffix(".toml")
            text = metadata.read_text().replace("https://example.invalid/pulls/", FORGE)
            metadata.write_text(text)
        self.write(self.record(1), self.record(2))

    def record(self, number: int, **changes: object) -> dict[str, object]:
        return self.fx.record(number, url=f"{FORGE}{number}", **changes)

    def write(self, *entries: dict[str, object]) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            self.fx.write_records(list(entries))

    def run_main(self, forge: FakeForge) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = upstream_drift.main(["--root", str(self.fx.root)], forge)
        return status, out.getvalue(), err.getvalue()

    # Positive.

    def test_matching_records_pass_and_every_record_is_read(self) -> None:
        forge = FakeForge({1: pull(1), 2: pull(2)})
        status, out, _ = self.run_main(forge)
        self.assertEqual(status, 0)
        self.assertEqual(forge.read, [f"{API}1", f"{API}2"])
        self.assertIn("2 pull request(s) read from the forge, 0 field(s) drifted.", out)

    def test_matching_merged_record_passes(self) -> None:
        self.write(
            self.record(1),
            self.record(2),
            self.record(
                3, state="merged", merged_at=MERGED_AT, merged_commit=COMMIT, in_release="n9.0.3"
            ),
        )
        merged = pull(3, state="closed", merged=True, merged_at=MERGED_AT, merge_commit_sha=COMMIT)
        self.assertEqual(self.run_main(FakeForge({1: pull(1), 2: pull(2), 3: merged}))[0], 0)

    # Negative: each recorded field drifts on the forge.

    def test_merge_on_the_forge_is_drift(self) -> None:
        merged = pull(2, state="closed", merged=True, merged_at=MERGED_AT, merge_commit_sha=COMMIT)
        status, out, _ = self.run_main(FakeForge({1: pull(1), 2: merged}))
        self.assertEqual(status, 1)
        self.assertIn("2: state is 'merged' on the forge, 'open' in", out)
        self.assertIn(f"2: merged_at is '{MERGED_AT}' on the forge", out)
        self.assertIn(f"2: merged_commit is '{COMMIT}' on the forge", out)
        self.assertIn("3 field(s) drifted", out)

    def test_closed_without_merge_is_drift(self) -> None:
        status, out, _ = self.run_main(FakeForge({1: pull(1, state="closed"), 2: pull(2)}))
        self.assertEqual(status, 1)
        self.assertIn("::error title=Upstream drift::1: state is 'closed' on the forge", out)

    def test_title_and_base_drift(self) -> None:
        forge = FakeForge(
            {
                1: pull(1, title="[release/9.0] lib: change 1"),
                2: pull(2, base={"ref": "release/9.0"}),
            }
        )
        status, out, _ = self.run_main(forge)
        self.assertEqual(status, 1)
        self.assertIn("1: title is '[release/9.0] lib: change 1' on the forge", out)
        self.assertIn("2: base is 'release/9.0' on the forge, 'master' in", out)

    def test_other_merge_commit_is_drift(self) -> None:
        self.write(
            self.record(1),
            self.record(2),
            self.record(3, state="merged", merged_at=MERGED_AT, merged_commit=COMMIT),
        )
        merged = pull(
            3, merged=True, state="closed", merged_at=MERGED_AT, merge_commit_sha="0" * 40
        )
        status, out, _ = self.run_main(FakeForge({1: pull(1), 2: pull(2), 3: merged}))
        self.assertEqual(status, 1)
        self.assertIn("3: merged_commit is '0000", out)

    # Negative: the comparison cannot run.

    def test_unreachable_forge_or_missing_pull_request_is_a_tool_error(self) -> None:
        status, _, err = self.run_main(FakeForge({1: pull(1)}))
        self.assertEqual(status, 2)
        self.assertIn("pulls/2: HTTP Error 404", err)

    def test_unexpected_answers_are_tool_errors(self) -> None:
        for answer in ({"number": 1}, pull(1, base="master"), pull(1, state="draft")):
            with self.subTest(answer=answer):
                self.assertEqual(self.run_main(FakeForge({1: answer, 2: pull(2)}))[0], 2)

    def test_link_outside_the_ffmpeg_forge_is_a_tool_error(self) -> None:
        for url in (
            "https://example.invalid/pulls/1",
            f"{FORGE}11",
            "http://code.ffmpeg.org/FFmpeg/FFmpeg/pulls/1",
            f"{FORGE}1/files",
            f"https://example.invalid/?u={FORGE}1",
        ):
            with self.subTest(url=url), self.assertRaises(upstream_drift.DriftError):
                upstream_drift.api_url({"url": url, "number": 1})

    def test_unsound_data_file_is_a_tool_error(self) -> None:
        self.write(self.record(1, state="unknown"), self.record(2))
        status, _, err = self.run_main(FakeForge({1: pull(1), 2: pull(2)}))
        self.assertEqual(status, 2)
        self.assertIn("state must be one of", err)

    def test_fetch_refuses_an_answer_that_is_not_an_object(self) -> None:
        for name, text in (("list.json", "[1]"), ("broken.json", "{")):
            path = self.scratch / name
            path.write_text(text)
            with self.subTest(name=name), self.assertRaises(upstream_drift.DriftError):
                upstream_drift.fetch_json(path.as_uri())
        with self.assertRaises(upstream_drift.DriftError):
            upstream_drift.fetch_json((self.scratch / "none.json").as_uri())

    # Boundary.

    def test_no_record_reads_nothing_and_passes(self) -> None:
        for name in self.fx.names:
            metadata = self.fx.root / "patches" / Path(name).with_suffix(".toml")
            metadata.write_text('status = "unsubmitted"\nupstream = ""\nconsumers = ["a"]\n')
        self.write()
        forge = FakeForge({})
        status, out, _ = self.run_main(forge)
        self.assertEqual((status, forge.read), (0, []))
        self.assertIn("0 pull request(s) read from the forge, 0 field(s) drifted.", out)


if __name__ == "__main__":
    unittest.main()
