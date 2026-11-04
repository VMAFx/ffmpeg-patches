# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Tests for scripts/upstream_drift.py with a fake forge; nothing is fetched."""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess  # nosec B404 - fixed bash workflow command with inert Git and a timeout.
import sys
import tempfile
import unittest
from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import patch

import yaml

from tests.test_subprocess_nosec import find_subprocess_imports_without_nosec

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import upstream_drift  # noqa: E402

FORGE = "https://code.ffmpeg.org/FFmpeg/FFmpeg/pulls/"
API = "https://code.ffmpeg.org/api/v1/repos/FFmpeg/FFmpeg/pulls/"
COMMIT = "7e93014c5bbb61e7c22afebb478bf92f7a08d59a"
BASE = "1" * 40
MERGED_AT = "2026-10-12T03:43:42Z"
TODAY = date(2026, 11, 4)


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
        "head": {"sha": COMMIT},
        "merge_base": BASE,
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


class Fixture:
    """Two patches and their contribution records; no Git or credential settings."""

    def __init__(self, scratch: Path) -> None:
        self.root = scratch / "series"
        (self.root / "patches").mkdir(parents=True)
        self.names = ["0001-lib-change-one.patch", "0002-lib-change-two.patch"]
        (self.root / "series.txt").write_text("\n".join(self.names) + "\n", encoding="utf-8")
        (self.root / "base.env").write_text(
            f"FFMPEG_REMOTE=https://example.invalid/up.git\nFFMPEG_TAG=v1\nFFMPEG_COMMIT={BASE}\n",
            encoding="utf-8",
        )
        for number, name in enumerate(self.names, 1):
            self.patch(number).write_text(
                f"From {'0' * 40} Mon Sep 17 00:00:00 2001\n"
                "From: Dev <dev@invalid>\nDate: Mon, 2 Nov 2026 00:00:00 +0000\n"
                "Subject: [PATCH] lib: change\n\nSigned-off-by: Dev <dev@invalid>\n"
                "---\ndiff --git a/lib.c b/lib.c\n",
                encoding="utf-8",
            )
            (self.root / "patches" / name).with_suffix(".toml").write_text(
                'status = "submitted"\n'
                f'upstream = "https://example.invalid/pulls/{number}"\nconsumers = ["a"]\n',
                encoding="utf-8",
            )
        (self.root / "README.md").write_text(
            f"# Fixture\n\n{upstream_drift.series.TABLE_START}\n"
            f"{upstream_drift.series.TABLE_END}\n\nText after the table.\n",
            encoding="utf-8",
        )
        self.write_records([self.record(1), self.record(2)])

    def record(self, number: int, **changes: object) -> dict[str, object]:
        """Return a contribution record with the requested field overrides."""
        entry = dict.fromkeys(upstream_drift.series.CONTRIBUTION_KEYS, "")
        entry.update(number=number, url=f"https://example.invalid/pulls/{number}")
        entry.update(title=f"lib: change {number}", base="master", state="open", project="a")
        entry["patch"] = self.names[number - 1] if number <= len(self.names) else ""
        return entry | changes

    def write_records(self, entries: list[dict]) -> None:
        """Write contribution records and regenerate the fixture table."""
        data = {"checked": "2026-01-02", "contributions": entries}
        (self.root / upstream_drift.series.CONTRIBUTIONS).write_text(
            json.dumps(data, indent=2) + "\n", encoding="utf-8"
        )
        upstream_drift.series.contributions(self.root, write=True)

    def patch(self, number: int) -> Path:
        """Return the path of the numbered fixture patch."""
        return self.root / "patches" / self.names[number - 1]


class Utf8ReadTest(unittest.TestCase):
    """Invalid UTF-8 includes the input path and preserves the decode cause."""

    def test_bounded_lines_invalid_utf8_names_sidecar(self) -> None:
        """Wrap an undecodable sidecar in a path-specific drift error."""
        path = Path("patches/0001-change.toml")
        with self.assertRaises(upstream_drift.DriftError) as caught:
            upstream_drift.bounded_lines(path, b"\xff", upstream_drift.MAX_SIDECAR_LINES)
        self.assertIn(str(path), str(caught.exception))
        self.assertIsInstance(caught.exception.__cause__, UnicodeDecodeError)

    def test_table_drift_invalid_utf8_names_readme(self) -> None:
        """Wrap undecodable README bytes from disk or snapshot with the full path."""
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            path = root / "README.md"
            path.write_bytes(b"\xff")
            for original in (None, b"\xff"):
                with self.subTest(original=original):
                    with self.assertRaises(upstream_drift.DriftError) as caught:
                        upstream_drift.table_drift(root, {}, original)
                    self.assertIn(str(path), str(caught.exception))
                    self.assertIsInstance(caught.exception.__cause__, UnicodeDecodeError)


class DriftTest(unittest.TestCase):
    def setUp(self) -> None:
        """Build a fixture series whose records point at the fake forge."""
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.scratch = Path(scratch.name)
        self.fx = Fixture(self.scratch)
        for name in self.fx.names:
            metadata = self.fx.root / "patches" / Path(name).with_suffix(".toml")
            text = metadata.read_text(encoding="utf-8").replace(
                "https://example.invalid/pulls/", FORGE
            )
            metadata.write_text(text, encoding="utf-8")
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

    def master_form(self) -> None:
        """Give patch 1 a master_form record."""
        path = self.fx.patch(1).with_suffix(".toml")
        path.write_text(
            path.read_text(encoding="utf-8")
            + f'master_form = {{ base = "{BASE}", head = "{COMMIT}" }}\n',
            encoding="utf-8",
        )

    def test_master_form_head_moved_is_one_drift(self) -> None:
        self.master_form()
        forge = FakeForge({1: pull(1, head={"sha": "0" * 40}), 2: pull(2)})
        status, out, err = self.run_main(forge)
        self.assertEqual(status, 1)
        self.assertEqual(err, "")
        self.assertIn(
            f"{self.fx.names[0]}: master_form head is {'0' * 40!r} on the forge, "
            f"{COMMIT!r} in patches/{self.fx.patch(1).with_suffix('.toml').name}",
            out,
        )
        self.assertIn("1 field(s) drifted.", out)

    def covered_by(self, number: int = 3) -> None:
        """Mark patch 1 as covered by another pull request."""
        self.fx.patch(1).with_suffix(".toml").write_text(
            'status = "unsubmitted"\nupstream = ""\nconsumers = ["a"]\n'
            f'covered_by = "{FORGE}{number}"\n',
            encoding="utf-8",
        )
        self.write(self.record(2))

    def test_master_form_matches_and_shared_pull_is_read_once(self) -> None:
        self.master_form()
        forge = FakeForge({1: pull(1), 2: pull(2)})
        status, out, err = self.run_main(forge)
        self.assertEqual((status, err), (0, ""))
        self.assertEqual(forge.read, [f"{API}1", f"{API}2"])
        self.assertIn("2 pull request(s) read from the forge, 0 field(s) drifted.", out)

    def test_master_form_merge_base_moved_is_one_drift(self) -> None:
        self.master_form()
        status, out, _ = self.run_main(FakeForge({1: pull(1, merge_base="0" * 40), 2: pull(2)}))
        self.assertEqual(status, 1)
        self.assertIn(f"{self.fx.names[0]}: master_form base is {'0' * 40!r}", out)
        self.assertIn("1 field(s) drifted.", out)

    def test_master_form_missing_or_odd_fields_are_tool_errors(self) -> None:
        self.master_form()
        missing_head, missing_base = pull(1), pull(1)
        del missing_head["head"]
        del missing_base["merge_base"]
        answers = [missing_head, missing_base]
        answers.extend(pull(1, head=value) for value in (None, [], "sha", {}))
        answers.extend(pull(1, head={"sha": value}) for value in (None, 7, []))
        answers.extend(pull(1, merge_base=value) for value in (None, 7, {}))
        for answer in answers:
            with self.subTest(answer=answer):
                status, out, err = self.run_main(FakeForge({1: answer, 2: pull(2)}))
                self.assertEqual(status, 2)
                self.assertEqual(out, "")
                self.assertIn("unexpected pull request answer", err)

    def test_master_form_both_commits_moved_are_two_drifts(self) -> None:
        self.master_form()
        forge = FakeForge({1: pull(1, head={"sha": "0" * 40}, merge_base="2" * 40), 2: pull(2)})
        status, out, _ = self.run_main(forge)
        self.assertEqual(status, 1)
        self.assertEqual(out.count("::error title=Upstream drift::"), 2)
        self.assertIn("2 field(s) drifted.", out)

    def test_covered_by_open_passes_and_is_counted(self) -> None:
        self.covered_by()
        forge = FakeForge({2: pull(2), 3: pull(3)})
        status, out, err = self.run_main(forge)
        self.assertEqual((status, err), (0, ""))
        self.assertEqual(forge.read, [f"{API}2", f"{API}3"])
        self.assertIn("2 pull request(s) read from the forge, 0 field(s) drifted.", out)

    def test_covered_by_merged_reports_commit_and_drop_advice(self) -> None:
        self.covered_by()
        merged = pull(3, merged=True, state="closed", merged_at=MERGED_AT, merge_commit_sha=COMMIT)
        status, out, _ = self.run_main(FakeForge({2: pull(2), 3: merged}))
        self.assertEqual(status, 1)
        self.assertIn(
            f"::error title=Upstream drift::{self.fx.names[0]}: covered_by pull request 3 "
            f"is merged on the forge (merge commit {COMMIT}); "
            "drop the patch once the pinned base contains it",
            out,
        )
        self.assertIn("1 field(s) drifted.", out)

    def test_covered_by_closed_is_one_drift(self) -> None:
        self.covered_by()
        status, out, _ = self.run_main(FakeForge({2: pull(2), 3: pull(3, state="closed")}))
        self.assertEqual(status, 1)
        self.assertIn(f"{self.fx.names[0]}: covered_by pull request 3 is closed on the forge", out)
        self.assertIn("1 field(s) drifted.", out)
        self.assertNotIn("merge commit", out)

    def test_covered_by_unreachable_is_a_tool_error(self) -> None:
        self.covered_by()
        status, out, err = self.run_main(FakeForge({2: pull(2)}))
        self.assertEqual((status, out), (2, ""))
        self.assertIn("pulls/3: HTTP Error 404", err)

    def test_covered_by_unexpected_answer_is_a_tool_error(self) -> None:
        self.covered_by()
        for answer in ({}, pull(3, state="draft"), pull(3, base=None)):
            with self.subTest(answer=answer):
                status, out, err = self.run_main(FakeForge({2: pull(2), 3: answer}))
                self.assertEqual((status, out), (2, ""))
                self.assertTrue(err.startswith("error:"))

    def test_only_covering_pull_with_no_contributions_is_read(self) -> None:
        """Read only the covering pull request when nothing else is recorded."""
        self.covered_by()
        self.fx.patch(2).with_suffix(".toml").write_text(
            'status = "unsubmitted"\nupstream = ""\nconsumers = ["a"]\n', encoding="utf-8"
        )
        self.write()
        forge = FakeForge({3: pull(3)})
        status, out, _ = self.run_main(forge)
        self.assertEqual(status, 0)
        self.assertEqual(forge.read, [f"{API}3"])
        self.assertIn("1 pull request(s) read from the forge, 0 field(s) drifted.", out)

    def test_fetch_cache_is_local_to_each_run(self) -> None:
        self.master_form()
        forge = FakeForge({1: pull(1), 2: pull(2)})
        self.assertEqual(self.run_main(forge)[0], 0)
        forge.answers[f"{API}1"]["head"] = {"sha": "0" * 40}
        self.assertEqual(self.run_main(forge)[0], 1)
        self.assertEqual(forge.read, [f"{API}1", f"{API}2", f"{API}1", f"{API}2"])

    def test_covered_by_shared_with_contribution_and_master_form_is_read_once(self) -> None:
        """Read a pull request once when several records name it."""
        self.covered_by(2)
        path = self.fx.patch(2).with_suffix(".toml")
        path.write_text(
            path.read_text(encoding="utf-8")
            + f'master_form = {{ base = "{BASE}", head = "{COMMIT}" }}\n',
            encoding="utf-8",
        )
        forge = FakeForge({2: pull(2)})
        status, out, _ = self.run_main(forge)
        self.assertEqual(status, 0)
        self.assertEqual(forge.read, [f"{API}2"])
        self.assertIn("1 pull request(s) read from the forge, 0 field(s) drifted.", out)

    def test_unsound_master_metadata_is_a_tool_error(self) -> None:
        """Fail as a tool error on a malformed master_form record."""
        path = self.fx.patch(1).with_suffix(".toml")
        path.write_text(
            path.read_text(encoding="utf-8") + 'master_form = { head = "short" }\n',
            encoding="utf-8",
        )
        status, _, err = self.run_main(FakeForge({1: pull(1), 2: pull(2)}))
        self.assertEqual(status, 2)
        self.assertIn("master_form must be a table", err)

    def test_unsound_covered_by_metadata_is_a_tool_error(self) -> None:
        """Fail as a tool error on a malformed covered_by record."""
        path = self.fx.patch(1).with_suffix(".toml")
        path.write_text(
            path.read_text(encoding="utf-8") + 'covered_by = "https://example.invalid/pulls/3"\n',
            encoding="utf-8",
        )
        status, _, err = self.run_main(FakeForge({1: pull(1), 2: pull(2)}))
        self.assertEqual(status, 2)
        self.assertIn("covered_by is allowed only", err)
        self.assertIn("covered_by must be a pull request URL", err)

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

    def test_title_case_change_is_drift(self) -> None:
        forge = FakeForge({1: pull(1, title="lib: Change 1"), 2: pull(2)})
        status, out, _ = self.run_main(forge)
        self.assertEqual(status, 1)
        self.assertIn("1: title is 'lib: Change 1' on the forge, 'lib: change 1' in", out)
        self.assertIn("1 field(s) drifted", out)

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
                upstream_drift.api_url(url, 1)

    def test_unsound_data_file_is_a_tool_error(self) -> None:
        self.write(self.record(1, state="unknown"), self.record(2))
        status, _, err = self.run_main(FakeForge({1: pull(1), 2: pull(2)}))
        self.assertEqual(status, 2)
        self.assertIn("state must be one of", err)

    def test_fetch_refuses_an_answer_that_is_not_an_object(self) -> None:
        """Refuse a forge answer that is not a JSON object or is missing."""
        for name, text in (("list.json", "[1]"), ("broken.json", "{")):
            path = self.scratch / name
            path.write_text(text, encoding="utf-8")
            with self.subTest(name=name), self.assertRaises(upstream_drift.DriftError):
                upstream_drift.fetch_json(path.as_uri())
        with self.assertRaises(upstream_drift.DriftError):
            upstream_drift.fetch_json((self.scratch / "none.json").as_uri())

    # Boundary.

    def test_no_record_reads_nothing_and_passes(self) -> None:
        """Read nothing and pass when no patch carries a record."""
        for name in self.fx.names:
            metadata = self.fx.root / "patches" / Path(name).with_suffix(".toml")
            metadata.write_text(
                'status = "unsubmitted"\nupstream = ""\nconsumers = ["a"]\n', encoding="utf-8"
            )
        self.write()
        forge = FakeForge({})
        status, out, _ = self.run_main(forge)
        self.assertEqual((status, forge.read), (0, []))
        self.assertIn("0 pull request(s) read from the forge, 0 field(s) drifted.", out)


class FixTest(unittest.TestCase):
    """Fix fixtures need only files: no Git commands or credential configuration."""

    def setUp(self) -> None:
        """Create file-only fixtures for drift repair tests."""
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        fixture = Fixture(Path(scratch.name))
        self.root = fixture.root
        self.name = fixture.names[0]
        for number in range(1, len(fixture.names) + 1):
            fixture.patch(number).with_suffix(".toml").write_text(
                'status = "unsubmitted"\nupstream = ""\nconsumers = ["a"]\n', encoding="utf-8"
            )
        self.sidecar = (self.root / "patches" / self.name).with_suffix(".toml")
        self.readme = self.root / "README.md"
        self.data_path = self.root / upstream_drift.series.CONTRIBUTIONS
        entry = fixture.record(
            1, url=f"{FORGE}1", patch="", note="caf\N{LATIN SMALL LETTER E WITH ACUTE}"
        )
        self.data = {"checked": "2026-01-02", "contributions": [entry]}
        self.write_data()

    def write_data(self, *, ensure_ascii: bool = True) -> None:
        """Write contribution data and regenerate the README table."""
        self.data_path.write_text(
            json.dumps(self.data, indent=2, ensure_ascii=ensure_ascii) + "\n", encoding="utf-8"
        )
        self.assertEqual(upstream_drift.series.contributions(self.root, write=True), 0)

    def snapshot(self) -> dict[Path, bytes]:
        """Return every fixture file and its original bytes."""
        return {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}

    def fix(self, answer: dict, today: date | None = TODAY) -> tuple[int, str, str]:
        """Return repair status, standard output, and standard error for a forge answer."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = upstream_drift.main(
                ["--root", str(self.root), "--fix"], FakeForge({1: answer}), today=today
            )
        return status, out.getvalue(), err.getvalue()

    def master_form(self, line: str | None = None) -> None:
        """Configure a submitted patch with a master-form commit pair."""
        self.sidecar.write_text(
            'status = "submitted"\n'
            f'upstream = "{FORGE}1"\nconsumers = ["a"]\n'
            f"# Commit elsewhere: {COMMIT}\n"
            + (line or f'master_form = {{ base = "{BASE}", head = "{COMMIT}" }}\n'),
            encoding="utf-8",
        )
        self.data["contributions"][0]["patch"] = self.name
        self.write_data()

    def test_fix_merge_fields_and_regenerate_readme(self) -> None:
        """Repair merge fields and regenerate the README while preserving surrounding text."""
        before = self.readme.read_text(encoding="utf-8")
        merged = pull(1, merged=True, state="closed", merged_at=MERGED_AT, merge_commit_sha=COMMIT)
        status, out, err = self.fix(merged)
        self.assertEqual((status, err), (0, ""))
        actual = json.loads(self.data_path.read_bytes())
        expected = self.data["contributions"][0] | {
            "state": "merged",
            "merged_at": MERGED_AT,
            "merged_commit": COMMIT,
        }
        self.assertEqual(
            actual, self.data | {"checked": TODAY.isoformat(), "contributions": [expected]}
        )
        self.assertEqual(out.count("fixed: "), 3)
        self.assertNotIn("::error", out)
        self.assertIn("0 field(s) drifted.", out)
        after = self.readme.read_text(encoding="utf-8")
        self.assertNotEqual(before, after)
        self.assertIn(f"State read from the FFmpeg forge on {TODAY.isoformat()}.", after)
        self.assertEqual(
            after.split(upstream_drift.series.TABLE_END)[1], "\n\nText after the table.\n"
        )
        self.assertEqual(upstream_drift.series.check(self.root).errors, [])

    def test_fix_title_and_base_preserves_json_format_and_key_order(self) -> None:
        """Repair title and base while preserving JSON formatting and key order."""
        title = "lib: caf\N{LATIN SMALL LETTER E WITH ACUTE} update"
        for ensure_ascii in (True, False):
            with self.subTest(ensure_ascii=ensure_ascii):
                self.write_data(ensure_ascii=ensure_ascii)
                status, out, err = self.fix(pull(1, title=title, base={"ref": "release/9.0"}))
                expected = self.data["contributions"][0] | {"title": title, "base": "release/9.0"}
                data = self.data | {"checked": TODAY.isoformat(), "contributions": [expected]}
                self.assertEqual((status, err), (0, ""))
                self.assertEqual(
                    self.data_path.read_bytes(),
                    (json.dumps(data, indent=2, ensure_ascii=ensure_ascii) + "\n").encode(),
                )
                self.assertEqual(out.count("fixed: "), 2)
                self.assertNotIn("::error", out)

    def test_fix_master_head_rewrites_only_its_line(self) -> None:
        """Repair only the master-form head line while preserving CRLF endings."""
        self.master_form()
        before = self.sidecar.read_bytes().replace(b"\n", b"\r\n")
        self.sidecar.write_bytes(before)
        status, out, err = self.fix(pull(1, head={"sha": "0" * 40}))
        old = f'master_form = {{ base = "{BASE}", head = "{COMMIT}" }}'.encode()
        new = old.replace(COMMIT.encode(), b"0" * 40)
        self.assertEqual((status, err), (0, ""))
        self.assertEqual(self.sidecar.read_bytes(), before.replace(old, new))
        self.assertIn("fixed: " + self.name + ": master_form head", out)
        self.assertNotIn("::error", out)
        self.assertEqual(json.loads(self.data_path.read_bytes())["checked"], TODAY.isoformat())

    def test_fix_master_commits_can_swap_without_cascading_replacements(self) -> None:
        """Swap master-form commits without cascading replacements."""
        self.master_form()
        status, out, err = self.fix(pull(1, head={"sha": BASE}, merge_base=COMMIT))
        self.assertEqual((status, err), (0, ""))
        self.assertIn(
            f'master_form = {{ base = "{COMMIT}", head = "{BASE}" }}',
            self.sidecar.read_text(encoding="utf-8"),
        )
        self.assertEqual(out.count("fixed: "), 2)

    def test_fix_absent_old_sha_refuses_all_writes(self) -> None:
        """Reject a missing literal old SHA without writing any files."""
        escaped = COMMIT[:1] + "\\u0065" + COMMIT[2:]
        self.master_form(f'master_form = {{ base = "{BASE}", head = "{escaped}" }}\n')
        before = self.snapshot()
        status, out, err = self.fix(pull(1, title="changed", head={"sha": "0" * 40}))
        self.assertEqual((status, out), (2, ""))
        self.assertIn("old head SHA must occur exactly once on master_form line", err)
        self.assertEqual(self.snapshot(), before)

    def test_fix_escaped_sha_substring_is_not_a_literal_value(self) -> None:
        """Reject an escaped SHA substring as a literal value without writing any files."""
        escaped = "\\u0037" + COMMIT[1:]
        self.master_form(f'master_form = {{ base = "{BASE}", head = "{escaped}" }}\n')
        before = self.snapshot()
        status, out, err = self.fix(pull(1, head={"sha": "0" * 40}))
        self.assertEqual((status, out), (2, ""))
        self.assertIn("exactly once", err)
        self.assertEqual(self.snapshot(), before)

    def test_fix_duplicate_old_sha_refuses_all_writes(self) -> None:
        """Reject duplicate old SHA occurrences without writing any files."""
        self.master_form(f'master_form = {{ base = "{BASE}", head = "{COMMIT}" }} # {COMMIT}\n')
        before = self.snapshot()
        status, out, err = self.fix(pull(1, head={"sha": "0" * 40}))
        self.assertEqual((status, out), (2, ""))
        self.assertIn("exactly once", err)
        self.assertEqual(self.snapshot(), before)

    def test_fix_non_inline_master_form_refuses_all_writes(self) -> None:
        """Reject a non-inline master-form table without writing any files."""
        self.master_form(f'[master_form]\nbase = "{BASE}"\nhead = "{COMMIT}"\n')
        before = self.snapshot()
        status, out, err = self.fix(pull(1, head={"sha": "0" * 40}))
        self.assertEqual((status, out), (2, ""))
        self.assertIn("expected exactly one inline master_form line", err)
        self.assertEqual(self.snapshot(), before)

    def test_fix_invalid_forge_sha_refuses_all_writes(self) -> None:
        """Reject an invalid forge SHA without writing any files."""
        self.master_form()
        before = self.snapshot()
        status, out, err = self.fix(pull(1, head={"sha": "not-a-sha"}))
        self.assertEqual((status, out), (2, ""))
        self.assertIn("full 40-hex commits", err)
        self.assertEqual(self.snapshot(), before)

    def test_fix_covered_by_drift_is_unfixed_and_changes_no_files(self) -> None:
        """Report covering-pull drift without repairing it or changing any files."""
        self.sidecar.write_text(
            self.sidecar.read_text(encoding="utf-8") + f'covered_by = "{FORGE}1"\n',
            encoding="utf-8",
        )
        self.data["contributions"] = []
        self.write_data()
        for answer in (pull(1, state="closed"), pull(1, merged=True, merge_commit_sha=COMMIT)):
            with self.subTest(answer=answer):
                before = self.snapshot()
                status, out, err = self.fix(answer)
                self.assertEqual((status, err), (1, ""))
                self.assertIn("::error title=Upstream drift::" + self.name + ": covered_by", out)
                self.assertNotIn("fixed:", out)
                self.assertEqual(self.snapshot(), before)

    def test_fix_keeps_repairs_when_covered_by_still_needs_review(self) -> None:
        """Keep completed repairs when covering-pull drift still needs review."""
        self.sidecar.write_text(
            self.sidecar.read_text(encoding="utf-8") + f'covered_by = "{FORGE}1"\n',
            encoding="utf-8",
        )
        before = self.sidecar.read_bytes()
        status, out, err = self.fix(pull(1, state="closed", title="changed"))
        self.assertEqual((status, err), (1, ""))
        self.assertEqual(self.sidecar.read_bytes(), before)
        self.assertEqual(out.count("fixed: "), 2)
        self.assertIn("::error title=Upstream drift::" + self.name + ": covered_by", out)
        self.assertIn("1 field(s) drifted.", out)
        self.assertEqual(
            json.loads(self.data_path.read_bytes())["contributions"][0]["state"], "closed"
        )

    def test_fix_closed_record_clears_previous_merge_fields(self) -> None:
        """Clear stale merge fields when repairing a closed contribution record."""
        self.data["contributions"][0].update(
            state="merged", merged_at=MERGED_AT, merged_commit=COMMIT
        )
        self.write_data()
        status, out, err = self.fix(pull(1, state="closed"))
        self.assertEqual((status, err), (0, ""))
        actual = json.loads(self.data_path.read_bytes())["contributions"][0]
        self.assertEqual(
            (actual["state"], actual["merged_at"], actual["merged_commit"]), ("closed", "", "")
        )
        self.assertEqual(out.count("fixed: "), 3)

    def test_fix_unreachable_forge_changes_no_files(self) -> None:
        """Leave all files unchanged when the forge is unreachable."""
        before = self.snapshot()
        with patch.object(
            upstream_drift, "forge_record", side_effect=upstream_drift.DriftError("offline")
        ):
            status, out, err = self.fix(pull(1))
        self.assertEqual((status, out), (2, ""))
        self.assertIn("offline", err)
        self.assertEqual(self.snapshot(), before)

    def test_fix_no_drift_keeps_all_bytes_and_checked_date(self) -> None:
        """Preserve every byte and the checked date when no drift exists."""
        self.master_form()
        for ensure_ascii in (True, False):
            with self.subTest(ensure_ascii=ensure_ascii):
                self.write_data(ensure_ascii=ensure_ascii)
                before = self.snapshot()
                status, out, err = self.fix(pull(1))
                self.assertEqual((status, err), (0, ""))
                self.assertNotIn("fixed:", out)
                self.assertEqual(self.snapshot(), before)

    def test_fix_uses_utc_day_when_not_injected(self) -> None:
        """Use the current UTC day when no date is injected."""
        instant = datetime(2026, 11, 3, 23, 30, tzinfo=UTC)
        with patch.object(upstream_drift, "datetime") as clock:
            clock.now.return_value = instant
            status, _, err = self.fix(pull(1, title="changed"), today=None)
        self.assertEqual((status, err), (0, ""))
        clock.now.assert_called_once_with(UTC)
        self.assertEqual(json.loads(self.data_path.read_bytes())["checked"], "2026-11-03")

    def test_fix_table_failure_restores_original_bytes(self) -> None:
        """Restore original bytes when README table regeneration fails."""
        self.readme.write_text("# No table markers\n", encoding="utf-8")
        before = self.snapshot()
        status, out, err = self.fix(pull(1, title="changed"))
        self.assertEqual((status, out), (2, ""))
        self.assertIn("README.md lacks one", err)
        self.assertEqual(self.snapshot(), before)

    def test_fix_concurrent_edits_abort_before_any_write(self) -> None:
        """Preserve intervening JSON, sidecar and README edits without any repair writes."""
        self.master_form()
        baseline = self.snapshot()
        for target in (self.data_path, self.sidecar, self.readme):
            with self.subTest(target=target):
                for path, content in baseline.items():
                    path.write_bytes(content)
                changed = baseline[target] + b"\n"

                def fetch(url: str, target: Path = target, changed: bytes = changed) -> dict:
                    """Change a target after input reads, while the fake forge responds."""
                    target.write_text(changed.decode(), encoding="utf-8")
                    return pull(1, title="changed", head={"sha": "0" * 40})

                with (
                    patch.object(Path, "write_bytes") as write,
                    contextlib.redirect_stdout(io.StringIO()),
                    contextlib.redirect_stderr(io.StringIO()) as err,
                ):
                    status = upstream_drift.main(["--root", str(self.root), "--fix"], fetch)
                self.assertEqual(status, 2)
                self.assertIn("changed since read", err.getvalue())
                write.assert_not_called()
                self.assertEqual(self.snapshot(), baseline | {target: changed})

    def test_fix_deleted_target_aborts_before_any_write(self) -> None:
        """A deleted README target aborts without overwriting other repair files."""
        before = self.snapshot()

        def fetch(url: str) -> dict:
            """Remove a target between input reads and repair writes."""
            self.readme.unlink()
            return pull(1, title="changed")

        with (
            patch.object(Path, "write_bytes") as write,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            status = upstream_drift.main(["--root", str(self.root), "--fix"], fetch)
        self.assertEqual(status, 2)
        write.assert_not_called()
        self.assertEqual(self.snapshot(), {p: b for p, b in before.items() if p != self.readme})

    def test_fix_stale_table_without_forge_drift_preserves_data(self) -> None:
        """Recover an interrupted table update without changing data or its checked date."""
        expected = self.readme.read_bytes()
        original_data = self.data_path.read_bytes()
        self.readme.write_bytes(expected.replace(b"lib: change 1", b"obsolete title"))
        status, out, err = self.fix(pull(1))
        self.assertEqual((status, err), (0, ""))
        self.assertIn("fixed: README.md: contribution table differs", out)
        self.assertEqual(self.readme.read_bytes(), expected)
        self.assertEqual(self.data_path.read_bytes(), original_data)
        self.assertNotIn("fixed:", self.fix(pull(1))[1])

    def test_plain_run_reports_stale_table_without_writing(self) -> None:
        """Report a stale table on a plain run even when every forge field matches."""
        self.readme.write_text(
            self.readme.read_text(encoding="utf-8").replace("lib: change 1", "obsolete"),
            encoding="utf-8",
        )
        before = self.snapshot()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = upstream_drift.main(["--root", str(self.root)], FakeForge({1: pull(1)}))
        self.assertEqual((status, err.getvalue()), (1, ""))
        self.assertIn("README.md: contribution table differs", out.getvalue())
        self.assertEqual(self.snapshot(), before)

    def test_stale_table_missing_or_duplicate_markers_refuses_writes(self) -> None:
        """Reject missing, duplicated or reversed table markers before any repair write."""
        start, end = upstream_drift.series.TABLE_START, upstream_drift.series.TABLE_END
        for text in ("# Missing", f"{start}\n{start}\n{end}", f"{end}\n{start}"):
            with self.subTest(text=text):
                self.readme.write_text(text, encoding="utf-8")
                before = self.snapshot()
                status, out, err = self.fix(pull(1))
                self.assertEqual((status, out), (2, ""))
                self.assertIn("README.md lacks one", err)
                self.assertEqual(self.snapshot(), before)

    def test_fix_quoted_master_form_keys(self) -> None:
        """Repair bare, basic-quoted and literal-quoted TOML master-form keys."""
        for key in ("master_form", '"master_form"', "'master_form'"):
            with self.subTest(key=key):
                self.master_form(f'{key} = {{ base = "{BASE}", head = "{COMMIT}" }}\n')
                status, _, err = self.fix(pull(1, head={"sha": "0" * 40}))
                self.assertEqual((status, err), (0, ""))
                self.assertIn(
                    f'{key} = {{ base = "{BASE}", head = "{"0" * 40}" }}',
                    self.sidecar.read_text(encoding="utf-8"),
                )

    def test_quoted_master_form_keeps_exactly_once_guard(self) -> None:
        """Reject duplicated or escaped old SHAs for quoted master-form keys."""
        escaped = COMMIT[:1] + "\\u0065" + COMMIT[2:]
        for value, suffix in ((COMMIT, f" # {COMMIT}"), (escaped, "")):
            with self.subTest(value=value):
                self.master_form(
                    f'"master_form" = {{ base = "{BASE}", head = "{value}" }}{suffix}\n'
                )
                before = self.snapshot()
                status, out, err = self.fix(pull(1, head={"sha": "0" * 40}))
                self.assertEqual((status, out), (2, ""))
                self.assertIn("exactly once", err)
                self.assertEqual(self.snapshot(), before)

    def test_series_entry_cap_boundary_passes(self) -> None:
        """Accept a series exactly at the configured entry cap."""
        with patch.object(upstream_drift, "MAX_SERIES_ENTRIES", 2):
            self.assertEqual(self.fix(pull(1, title="changed"))[0], 0)

    def test_series_entry_cap_refuses_before_metadata_or_forge_reads(self) -> None:
        """Reject an oversized series before sidecar traversal or forge access."""
        before = self.snapshot()
        with (
            patch.object(upstream_drift, "MAX_SERIES_ENTRIES", 1),
            patch.object(upstream_drift.series, "note_metadata") as metadata,
            patch.object(upstream_drift, "drift") as forge,
        ):
            status, out, err = self.fix(pull(1, title="changed"))
        self.assertEqual((status, out), (2, ""))
        self.assertIn("exceeds 1 lines", err)
        metadata.assert_not_called()
        forge.assert_not_called()
        self.assertEqual(self.snapshot(), before)

    def test_contribution_entry_cap_refuses_before_forge_reads(self) -> None:
        """Reject too many contribution records before comparison traversal."""
        forge = FakeForge({1: pull(1)})
        with (
            patch.object(upstream_drift, "MAX_SERIES_ENTRIES", 1),
            self.assertRaisesRegex(upstream_drift.DriftError, "contribution entries exceed 1"),
        ):
            upstream_drift.drift(self.data["contributions"] * 2, forge, fix=True)
        self.assertEqual(forge.read, [])

    def test_contribution_entry_cap_boundary_passes(self) -> None:
        """Compare contribution records exactly at the configured entry cap."""
        with patch.object(upstream_drift, "MAX_SERIES_ENTRIES", 1):
            self.assertEqual(
                upstream_drift.drift(self.data["contributions"], FakeForge({1: pull(1)})), []
            )

    def test_contribution_file_cap_refuses_before_record_validation(self) -> None:
        """Reject an oversized contribution file before traversing its records."""
        self.data["contributions"] *= 3
        self.data_path.write_text(json.dumps(self.data), encoding="utf-8")
        before = self.snapshot()
        with (
            patch.object(upstream_drift, "MAX_SERIES_ENTRIES", 2),
            patch.object(upstream_drift.series, "checked_contributions") as validate,
        ):
            status, out, err = self.fix(pull(1))
        self.assertEqual((status, out), (2, ""))
        self.assertIn("contribution entries exceed 2", err)
        validate.assert_not_called()
        self.assertEqual(self.snapshot(), before)

    def test_sidecar_line_cap_boundary_passes(self) -> None:
        """Accept a sidecar exactly at the line cap and repair its master form."""
        self.master_form()
        limit = len(self.sidecar.read_bytes().splitlines())
        with patch.object(upstream_drift, "MAX_SIDECAR_LINES", limit):
            self.assertEqual(self.fix(pull(1, head={"sha": "0" * 40}))[0], 0)

    def test_sidecar_line_cap_refuses_before_traversal_or_writes(self) -> None:
        """Reject a sidecar one line over the cap before parsing or repair scans."""
        self.master_form()
        before = self.snapshot()
        limit = len(self.sidecar.read_bytes().splitlines()) - 1
        with patch.object(upstream_drift, "MAX_SIDECAR_LINES", limit):
            status, out, err = self.fix(pull(1, head={"sha": "0" * 40}))
            with self.assertRaisesRegex(upstream_drift.DriftError, f"exceeds {limit} lines"):
                upstream_drift.master_form_fix(
                    self.sidecar, {"base": BASE, "head": COMMIT}, pull(1, head={"sha": "0" * 40})
                )
        self.assertEqual((status, out), (2, ""))
        self.assertIn(f"exceeds {limit} lines", err)
        self.assertEqual(self.snapshot(), before)

    def test_repair_path_cap_boundary_passes(self) -> None:
        """Accept a snapshot set exactly at the repair-path cap."""
        with patch.object(upstream_drift, "MAX_REPAIR_PATHS", 4):
            self.assertEqual(self.fix(pull(1, title="changed"))[0], 0)

    def test_repair_path_cap_refuses_before_filesystem_traversal(self) -> None:
        """Reject oversized snapshot and target sets before reading or writing paths."""
        originals = {
            path: path.read_bytes() for path in (self.sidecar, self.data_path, self.readme)
        }
        sets = (
            upstream_drift.Repairs(originals=originals),
            upstream_drift.Repairs(
                originals={path: data for path, data in originals.items() if path != self.sidecar},
                files={self.sidecar: b""},
                lines=["changed"],
            ),
        )
        for repairs in sets:
            with (
                self.subTest(repairs=repairs),
                patch.object(upstream_drift, "MAX_REPAIR_PATHS", 2),
                patch.object(Path, "read_bytes") as read,
                patch.object(Path, "write_bytes") as write,
                self.assertRaisesRegex(upstream_drift.DriftError, "repair paths exceed 2"),
            ):
                upstream_drift.write_repairs(self.root, [], repairs, TODAY)
            read.assert_not_called()
            write.assert_not_called()

    def test_fix_table_failure_after_writes_rolls_back(self) -> None:
        """Restore all original target bytes when table generation fails after writes."""
        self.master_form()
        before = self.snapshot()
        with patch.object(upstream_drift.series, "contributions", return_value=1):
            status, out, err = self.fix(pull(1, title="changed", head={"sha": "0" * 40}))
        self.assertEqual((status, out), (2, ""))
        self.assertIn("cannot regenerate", err)
        self.assertEqual(self.snapshot(), before)

    def assert_invalid_series_name(self, name: str) -> None:
        """Reject traversal entries before sidecar reads, forge access or repair writes."""
        outside = self.root.parent / "outside.patch"
        outside.write_text("untouched\n", encoding="utf-8")
        (self.root / "series.txt").write_text(self.name + "\n" + name + "\n", encoding="utf-8")
        before = self.snapshot()
        forge = FakeForge({1: pull(1, title="changed")})
        read_bytes = Path.read_bytes
        reads = []

        def read(path: Path) -> bytes:
            """Record filesystem reads made while validating the series."""
            reads.append(path)
            return read_bytes(path)

        with (
            patch.object(upstream_drift.series, "note_metadata") as metadata,
            patch.object(Path, "read_bytes", read),
            patch.object(Path, "write_bytes") as write,
            contextlib.redirect_stdout(io.StringIO()) as out,
            contextlib.redirect_stderr(io.StringIO()) as err,
        ):
            status = upstream_drift.main(["--root", str(self.root), "--fix"], forge)
        self.assertEqual((status, out.getvalue()), (2, ""))
        self.assertIn(f"{name}: name is not NNNN-slug.patch", err.getvalue())
        metadata.assert_not_called()
        write.assert_not_called()
        self.assertEqual(forge.read, [])
        self.assertTrue(all(path.suffix != ".toml" for path in reads), reads)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(outside.read_text(encoding="utf-8"), "untouched\n")

    def test_fix_rejects_parent_series_entry(self) -> None:
        """Reject a parent traversal entry without touching files outside the root."""
        self.assert_invalid_series_name("../outside.patch")

    def test_fix_rejects_nested_series_traversal(self) -> None:
        """Reject a prefixed traversal entry without touching files outside the root."""
        self.assert_invalid_series_name("0001-x/../../y.patch")

    def test_restore_failure_still_restores_remaining_target(self) -> None:
        """Restore remaining targets after one restore fails and preserve the original error."""
        originals = {path: path.read_bytes() for path in (self.data_path, self.readme)}
        repairs = upstream_drift.Repairs(originals=originals, lines=["changed"])
        original = upstream_drift.DriftError("original table failure")
        restored = []
        write_bytes = Path.write_bytes

        def fail_table(root: Path, *, write: bool) -> int:
            """Change both targets before reporting a table failure."""
            for path in originals:
                write_bytes(path, b"partial repair")
            raise original

        def restore(path: Path, content: bytes) -> int:
            """Fail the first restore and allow every later restore write."""
            if content == originals[path]:
                restored.append(path)
                if len(restored) == 1:
                    raise OSError("restore denied")
            return write_bytes(path, content)

        with (
            patch.object(upstream_drift.series, "contributions", side_effect=fail_table),
            patch.object(Path, "write_bytes", restore),
            self.assertRaises(upstream_drift.DriftError) as caught,
        ):
            upstream_drift.write_repairs(self.root, self.data["contributions"], repairs, TODAY)
        self.assertEqual(len(restored), 2)
        self.assertEqual(set(restored), set(originals))
        self.assertEqual(restored[1].read_bytes(), originals[restored[1]])
        self.assertIn(str(original), str(caught.exception))
        self.assertIn(str(restored[0]), str(caught.exception))
        self.assertIs(caught.exception.__cause__, original)

    def test_repair_target_without_snapshot_refuses_all_writes(self) -> None:
        """Reject an unsnapshotted target before writing any prepared repair."""
        repairs = upstream_drift.Repairs(
            originals={
                self.data_path: self.data_path.read_bytes(),
                self.readme: self.readme.read_bytes(),
            },
            files={self.sidecar: b"replacement"},
            lines=["changed"],
        )
        before = self.snapshot()
        with self.assertRaisesRegex(upstream_drift.DriftError, "lacks an original snapshot"):
            upstream_drift.write_repairs(self.root, self.data["contributions"], repairs, TODAY)
        self.assertEqual(self.snapshot(), before)


class ImportRationaleTest(unittest.TestCase):
    """Check this module's suppression with positive and planted negative source."""

    def test_subprocess_import_rationale_and_planted_omission(self) -> None:
        """Accept the targeted B404 rationale and reject its planted omission."""
        source = Path(__file__).read_text(encoding="utf-8")
        self.assertEqual(find_subprocess_imports_without_nosec(source), [])
        planted = source.replace(source.splitlines()[10], "import subprocess")
        self.assertEqual(find_subprocess_imports_without_nosec(planted), [11])


class WorkflowTest(unittest.TestCase):
    """Exercise the patch-producing shell with an inert Git stub."""

    def setUp(self) -> None:
        """Load workflow steps and create isolated patch-output fixtures."""
        path = Path(__file__).resolve().parent.parent / ".github/workflows/upstream.yml"
        self.workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        self.steps = self.workflow["jobs"]["drift"]["steps"]
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.output = self.root / "output"
        self.summary = self.root / "summary"

    def prepare_patch(self, *, changed: bool) -> subprocess.CompletedProcess[str]:
        """Return the patch-producing workflow result using an inert Git stub."""
        git = self.root / "git"
        git.write_text(
            '#!/bin/sh\nif [ "$*" = "diff --exit-code" ]; then\n'
            f"exit {int(changed)}\nfi\n"
            "printf '%s\\n' 'diff --git a/README.md b/README.md' '-old' '+new'\n",
            encoding="utf-8",
        )
        git.chmod(0o755)
        step = next(step for step in self.steps if step.get("id") == "patch")
        environment = os.environ | {
            "PATH": str(self.root) + os.pathsep + os.defpath,
            "GITHUB_OUTPUT": str(self.output),
            "GITHUB_STEP_SUMMARY": str(self.summary),
        }
        return subprocess.run(  # nosec B603 B607 - workflow shell with inert fixture Git.
            ["bash", "-e", "-o", "pipefail", "-c", step["run"]],
            cwd=self.root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    def test_changed_tree_produces_patch_and_summary(self) -> None:
        """Produce a patch artifact and diff summary for a changed tree."""
        result = self.prepare_patch(changed=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        diff = (self.root / "upstream-drift.patch").read_text(encoding="utf-8")
        self.assertEqual(diff, "diff --git a/README.md b/README.md\n-old\n+new\n")
        self.assertEqual(self.output.read_text(encoding="utf-8"), "changed=true\n")
        self.assertIn("```diff\n" + diff + "```", self.summary.read_text(encoding="utf-8"))

    def test_clean_tree_produces_no_patch(self) -> None:
        """Produce no patch or summary for a clean tree."""
        result = self.prepare_patch(changed=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.output.read_text(encoding="utf-8"), "changed=false\n")
        self.assertFalse((self.root / "upstream-drift.patch").exists())
        self.assertFalse(self.summary.exists())

    def test_failures_still_publish_before_final_failure_with_read_only_permissions(self) -> None:
        """Publish drift artifacts before final failure with read-only workflow permissions."""
        drift = next(step for step in self.steps if step.get("id") == "drift")
        prepare = next(step for step in self.steps if step.get("id") == "patch")
        upload = next(
            step for step in self.steps if "actions/upload-artifact@" in step.get("uses", "")
        )
        fail = self.steps[-1]
        self.assertEqual(drift["run"], "python3 scripts/upstream_drift.py --fix")
        self.assertFalse(drift.get("continue-on-error", False))
        self.assertIn("failure()", prepare["if"])
        self.assertIn("!cancelled()", upload["if"])
        self.assertIn("steps.patch.outputs.changed == 'true'", upload["if"])
        self.assertRegex(upload["uses"], r"^actions/upload-artifact@[0-9a-f]{40}$")
        self.assertEqual(upload["with"]["path"], "upstream-drift.patch")
        self.assertIn("steps.patch.outputs.changed == 'true'", fail["if"])
        self.assertIn("steps.drift.outcome == 'failure'", fail["if"])
        self.assertEqual(fail["run"], "exit 1")
        self.assertLess(self.steps.index(upload), self.steps.index(fail))
        self.assertEqual(self.workflow["permissions"], {"contents": "read"})


if __name__ == "__main__":
    unittest.main()
