# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Tests for scripts/upstream_links.py with literal JSON forge fixtures; no network."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
import unittest.mock
from datetime import date
from pathlib import Path

import yaml

from scripts import upstream_links

FORGE = "https://code.ffmpeg.org/FFmpeg/FFmpeg/pulls/"
API = "https://code.ffmpeg.org/api/v1/repos/FFmpeg/FFmpeg"
TODAY = date(2026, 11, 12)

PLANTED_OWN_25805_JSON = """{
  "number": 25805,
  "user": {"login": "lusoris"},
  "title": "checkasm/scene_sad: use the depth-adjusted width in bench_new()",
  "state": "open",
  "merged": false,
  "created_at": "2026-11-06T09:00:00Z",
  "updated_at": "2026-11-06T09:00:00Z",
  "merged_at": null,
  "body": "Use the depth-adjusted width in bench_new()."
}"""

PLANTED_OWN_25805_CLOSED_JSON = """{
  "number": 25805,
  "user": {"login": "lusoris"},
  "title": "checkasm/scene_sad: use the depth-adjusted width in bench_new()",
  "state": "closed",
  "merged": false,
  "created_at": "2026-11-06T09:00:00Z",
  "updated_at": "2026-11-17T09:00:00Z",
  "merged_at": null,
  "body": "Closed unmerged after 25950 landed."
}"""

PLANTED_OTHER_25950_JSON = """{
  "number": 25950,
  "user": {"login": "bcoudurier"},
  "title": "tests/checkasm/scene_sad: benchmark the width that is tested",
  "state": "closed",
  "merged": true,
  "created_at": "2026-11-11T10:00:00Z",
  "updated_at": "2026-11-16T12:00:00Z",
  "merged_at": "2026-11-16T12:00:00Z",
  "body": ""
}"""

PLANTED_SCENE_SAD_FILES_JSON = '[{"filename": "tests/checkasm/scene_sad.c"}]'


class FakeForge:
    """Serve literal JSON or pre-decoded answers keyed by Forgejo API URL."""

    def __init__(self, answers: dict[str, str | dict | list]) -> None:
        """Store URL answers and initialize the request log."""
        self.answers = answers
        self.read: list[str] = []

    def __call__(self, url: str) -> dict | list:
        """Return the decoded JSON answer for url or raise DriftError."""
        self.read.append(url)
        if url not in self.answers:
            raise upstream_links.DriftError(f"{url}: HTTP Error 404: Not Found")
        raw = self.answers[url]
        return json.loads(raw) if isinstance(raw, str) else raw


def write_manifest(root: Path, entries: list[dict]) -> Path:
    """Write upstream-contributions.json in root with the supplied entries."""
    path = root / upstream_links.series.CONTRIBUTIONS
    data = {"checked": "2026-11-12", "contributions": entries}
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path


def manifest_entry(number: int, **changes: object) -> dict[str, object]:
    """Return a valid contribution manifest entry for pull request number."""
    base: dict[str, object] = {
        "number": number,
        "url": f"{FORGE}{number}",
        "title": f"avutil/test: change {number}",
        "base": "master",
        "state": "open",
        "merged_at": "",
        "merged_commit": "",
        "in_release": "",
        "patch": "",
        "project": "vmafx",
        "note": "",
    }
    return base | changes


def run_cli(
    root: Path, forge: FakeForge, *args: str, today: date | None = TODAY
) -> tuple[int, str, str]:
    """Run upstream_links.main with captured stdout and stderr."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = upstream_links.main(["--root", str(root), *args], fetch=forge, today=today)
    return rc, out.getvalue(), err.getvalue()


class PlantedSceneSadOverlapTest(unittest.TestCase):
    """Planted fixture for own pull 25805 and overlapping pull 25950 with controls."""

    def setUp(self) -> None:
        """Create an isolated temporary root with an empty valid manifest."""
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        write_manifest(self.root, [])

    def planted_forge(
        self,
        *,
        own_json: str = PLANTED_OWN_25805_JSON,
        other_json: str = PLANTED_OTHER_25950_JSON,
        other_files_json: str = PLANTED_SCENE_SAD_FILES_JSON,
    ) -> FakeForge:
        """Build the FakeForge serving literal JSON for 25805 and candidate 25950."""
        pulls_page = f"[{other_json}, {own_json}]"
        return FakeForge(
            {
                f"{API}/pulls/25805": own_json,
                f"{API}/pulls/25805/files?limit=50&page=1": PLANTED_SCENE_SAD_FILES_JSON,
                f"{API}/pulls?state=all&sort=recentupdate&limit=50&page=1": pulls_page,
                f"{API}/pulls/25950/files?limit=50&page=1": other_files_json,
            }
        )

    def test_planted_25805_reports_25950_same_files_and_exits_one(self) -> None:
        """Detect overlapping pull 25950 on tests/checkasm/scene_sad.c and exit 1."""
        for own_json in (PLANTED_OWN_25805_JSON, PLANTED_OWN_25805_CLOSED_JSON):
            with self.subTest(own_state=json.loads(own_json)["state"]):
                forge = self.planted_forge(own_json=own_json)
                rc, out, err = run_cli(self.root, forge, "check", "--pull", "25805")
                self.assertEqual((rc, err), (1, ""))
                self.assertEqual(out.strip(), "25805 25950 bcoudurier same-files merged")

    def test_planted_control_same_author_exits_zero(self) -> None:
        """A candidate by the same author is listed as related but exits 0."""
        same_author_json = PLANTED_OTHER_25950_JSON.replace('"bcoudurier"', '"lusoris"')
        forge = self.planted_forge(other_json=same_author_json)
        rc, out, err = run_cli(self.root, forge, "check", "--pull", "25805")
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(out.strip(), "25805 25950 lusoris same-files merged")

    def test_planted_control_other_file_exits_zero(self) -> None:
        """A candidate touching another file is not related and exits 0."""
        forge = self.planted_forge(other_files_json='[{"filename": "tests/checkasm/other.c"}]')
        rc, out, err = run_cli(self.root, forge, "check", "--pull", "25805")
        self.assertEqual((rc, out, err), (0, "", ""))

    def test_planted_control_merged_before_own_opened_exits_zero(self) -> None:
        """A candidate merged before the own pull was opened is not an overlap and exits 0."""
        merged_before_json = """{
          "number": 25950,
          "user": {"login": "bcoudurier"},
          "title": "tests/checkasm/scene_sad: benchmark the width that is tested",
          "state": "closed",
          "merged": true,
          "created_at": "2026-11-01T10:00:00Z",
          "updated_at": "2026-11-12T12:00:00Z",
          "merged_at": "2026-11-05T12:00:00Z",
          "body": ""
        }"""
        forge = self.planted_forge(other_json=merged_before_json)
        rc, out, err = run_cli(self.root, forge, "check", "--pull", "25805")
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(out.strip(), "25805 25950 bcoudurier same-files merged")

    def test_planted_control_forge_error_exits_two(self) -> None:
        """A forge error keeps the diagnostic on stderr and exits 2."""
        rc, out, err = run_cli(self.root, FakeForge({}), "check", "--pull", "25805")
        self.assertEqual((rc, out), (2, ""))
        self.assertIn("error:", err)
        self.assertIn("25805", err)


class RelatedAndOverlapsPureTest(unittest.TestCase):
    """Pure unit tests for related_for, overlaps and body_issues."""

    def test_related_for_matches_files_patch_id_and_subject(self) -> None:
        """Classify same-patch-id, same-files and same-subject and sort by number."""
        own = {
            "number": 100,
            "author": "alice",
            "title": "avcodec/vvc: Fix intra boundary check",
            "files": ["libavcodec/vvc/intra.c"],
            "patch_ids": ["patch-1"],
            "created_at": "2026-11-06T00:00:00Z",
        }
        candidates = [
            {
                "number": 100,
                "author": "alice",
                "title": "avcodec/vvc: Fix intra boundary check",
                "files": ["libavcodec/vvc/intra.c"],
                "patch_ids": ["patch-1"],
            },
            {
                "number": 105,
                "kind": "issue",
                "author": "carol",
                "title": "vvc:   fix intra boundary check  ",
                "state": "open",
            },
            {
                "number": 102,
                "author": "bob",
                "title": "avcodec/vvc: another title",
                "files": ["libavcodec/vvc/intra.c"],
                "state": "open",
            },
            {
                "number": 101,
                "author": "alice",
                "title": "avcodec/vvc: v1 form",
                "files": ["libavcodec/vvc/other.c"],
                "patch_ids": ["patch-1"],
                "state": "closed",
            },
        ]
        related = upstream_links.related_for(own, candidates, today=TODAY)
        self.assertEqual([r["number"] for r in related], [101, 102, 105])
        self.assertEqual(
            [(r["reason"], r["kind"], r["seen"]) for r in related],
            [
                ("same-patch-id", "pull", "2026-11-12"),
                ("same-files", "pull", "2026-11-12"),
                ("same-subject", "issue", "2026-11-12"),
            ],
        )
        loud = upstream_links.overlaps(related)
        self.assertEqual([r["number"] for r in loud], [102])

    def test_overlaps_filters_closed_same_author_issues_and_early_merges(self) -> None:
        """Keep only open or post-open merged pulls by another author."""
        own = {
            "number": 10,
            "author": "alice",
            "title": "avutil/hw: fix leak",
            "files": ["libavutil/hw.c"],
            "created_at": "2026-11-06T00:00:00Z",
        }
        candidates = [
            {
                "number": 11,
                "author": "alice",
                "title": "avutil/hw: own split",
                "files": ["libavutil/hw.c"],
                "state": "open",
            },
            {
                "number": 12,
                "author": "bob",
                "title": "avutil/hw: abandoned attempt",
                "files": ["libavutil/hw.c"],
                "state": "closed",
            },
            {
                "number": 13,
                "author": "bob",
                "title": "avutil/hw: earlier merge",
                "files": ["libavutil/hw.c"],
                "state": "merged",
                "merged_at": "2026-11-01T00:00:00Z",
            },
            {
                "number": 14,
                "author": "bob",
                "title": "avutil/hw: later merge",
                "files": ["libavutil/hw.c"],
                "state": "merged",
                "merged_at": "2026-11-09T00:00:00Z",
            },
        ]
        related = upstream_links.related_for(own, candidates, today=TODAY)
        self.assertEqual([r["number"] for r in related], [11, 12, 13, 14])
        self.assertEqual([r["number"] for r in upstream_links.overlaps(related)], [14])

    def test_body_issues_extracts_fixes_closes_and_refs(self) -> None:
        """Read issue numbers after Fixes, Closes or Refs in hash, plain and URL forms."""
        body = (
            "Summary of change.\n\n"
            "Fixes #25052, #25010\n"
            "Closes: FFmpeg issue 24900\n"
            "Refs https://code.ffmpeg.org/FFmpeg/FFmpeg/issues/24800 and 25052.\n"
        )
        self.assertEqual(upstream_links.body_issues(body), [24800, 24900, 25010, 25052])
        self.assertEqual(upstream_links.body_issues(None), [])
        with self.assertRaises(upstream_links.DriftError):
            upstream_links.body_issues(123)


class FillAndCheckCommandTest(unittest.TestCase):
    """Tests for fill, check, dry-run, pagination, caps and unknown answer shapes."""

    def setUp(self) -> None:
        """Create a temporary root with one open, one merged and one closed contribution."""
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.entries = [
            manifest_entry(10, state="open"),
            manifest_entry(
                20,
                state="merged",
                merged_at="2026-11-10T00:00:00Z",
                merged_commit="a" * 40,
            ),
            manifest_entry(30, state="closed"),
        ]
        self.path = write_manifest(self.root, self.entries)

    def forge_for_fill(self) -> FakeForge:
        """Return a FakeForge covering pulls 10, 20 and candidate 25."""
        pull_10 = {
            "number": 10,
            "user": {"login": "alice"},
            "title": "avutil/test: change 10",
            "state": "open",
            "merged": False,
            "created_at": "2026-11-05T00:00:00Z",
            "updated_at": "2026-11-05T00:00:00Z",
            "merged_at": None,
            "body": "Fixes #99\nRefs: #42",
        }
        pull_20 = {
            "number": 20,
            "user": {"login": "alice"},
            "title": "avutil/test: change 20",
            "state": "closed",
            "merged": True,
            "created_at": "2026-11-07T00:00:00Z",
            "updated_at": "2026-11-10T00:00:00Z",
            "merged_at": "2026-11-10T00:00:00Z",
            "body": "Closes issue 77",
        }
        pull_25 = {
            "number": 25,
            "user": {"login": "bob"},
            "title": "avutil/test: rival change",
            "state": "open",
            "merged": False,
            "created_at": "2026-11-08T00:00:00Z",
            "updated_at": "2026-11-11T00:00:00Z",
            "merged_at": None,
            "body": "",
        }
        return FakeForge(
            {
                f"{API}/pulls/10": pull_10,
                f"{API}/pulls/10/files?limit=50&page=1": [{"filename": "libavutil/a.c"}],
                f"{API}/pulls/20": pull_20,
                f"{API}/pulls/20/files?limit=50&page=1": [{"filename": "libavutil/b.c"}],
                f"{API}/pulls/25/files?limit=50&page=1": [{"filename": "libavutil/a.c"}],
                f"{API}/pulls?state=all&sort=recentupdate&limit=50&page=1": [
                    pull_25,
                    pull_20,
                    pull_10,
                ],
            }
        )

    def test_fill_dry_run_prints_changes_and_writes_nothing(self) -> None:
        """Print pending fixes_issues and related updates without touching disk on --dry-run."""
        before = self.path.read_bytes()
        rc, out, err = run_cli(self.root, self.forge_for_fill(), "fill", "--dry-run")
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("10: fixes_issues=[42, 99]", out)
        self.assertIn('"number": 25', out)
        self.assertEqual(self.path.read_bytes(), before)

    def test_fill_writes_sorted_fields_and_preserves_bytes_on_second_run(self) -> None:
        """Write fixes_issues and related for open and merged rows and leave closed rows alone."""
        forge = self.forge_for_fill()
        rc, out, err = run_cli(self.root, forge, "fill")
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("10:", out)
        data = json.loads(self.path.read_text(encoding="utf-8"))
        by_num = {e["number"]: e for e in data["contributions"]}
        self.assertEqual(by_num[10]["fixes_issues"], [42, 99])
        self.assertEqual(
            by_num[10]["related"],
            [
                {
                    "number": 25,
                    "kind": "pull",
                    "author": "bob",
                    "reason": "same-files",
                    "state": "open",
                    "seen": "2026-11-12",
                }
            ],
        )
        self.assertEqual(by_num[20]["fixes_issues"], [77])
        self.assertEqual(by_num[20]["related"], [])
        self.assertNotIn("fixes_issues", by_num[30])
        self.assertNotIn("related", by_num[30])
        findings = upstream_links.series.Findings()
        self.assertIsNotNone(upstream_links.series.checked_contributions(self.root, [], findings))
        self.assertEqual(findings.errors, [])

        saved = self.path.read_bytes()
        later = date(2026, 11, 18)
        rc2, out2, err2 = run_cli(self.root, self.forge_for_fill(), "fill", today=later)
        self.assertEqual((rc2, out2, err2), (0, "", ""))
        self.assertEqual(self.path.read_bytes(), saved)

    def test_check_reports_unrecorded_overlap_and_passes_once_filled(self) -> None:
        """Exit 1 when open pull 10 has an unrecorded overlap, and exit 0 after fill records it."""
        rc, out, err = run_cli(self.root, self.forge_for_fill(), "check")
        self.assertEqual((rc, err), (1, ""))
        self.assertEqual(out.strip(), "10 25 bob same-files open")

        self.assertEqual(run_cli(self.root, self.forge_for_fill(), "fill")[0], 0)
        rc2, out2, err2 = run_cli(self.root, self.forge_for_fill(), "check")
        self.assertEqual((rc2, out2, err2), (0, "", ""))

    def test_unknown_answer_shapes_and_cap_overflows_exit_two(self) -> None:
        """Treat malformed forge payloads and page-cap overflows as exit-2 errors."""
        write_manifest(self.root, [manifest_entry(10, state="open")])
        bad_forges = (
            FakeForge({f"{API}/pulls/10": "[]"}),
            FakeForge({f"{API}/pulls/10": '{"number": 10}'}),
            FakeForge(
                {
                    f"{API}/pulls/10": PLANTED_OWN_25805_JSON.replace("25805", "10"),
                    f"{API}/pulls/10/files?limit=50&page=1": '{"not": "a list"}',
                }
            ),
            FakeForge(
                {
                    f"{API}/pulls/10": PLANTED_OWN_25805_JSON.replace("25805", "10"),
                    f"{API}/pulls/10/files?limit=50&page=1": "[{}]",
                }
            ),
        )
        for forge in bad_forges:
            with self.subTest(forge=forge.answers):
                rc, out, err = run_cli(self.root, forge, "check")
                self.assertEqual((rc, out), (2, ""))
                self.assertIn("error:", err)

    def test_page_cap_overflow_exits_two(self) -> None:
        """Refuse a pull file listing that fills every permitted page without terminating."""
        write_manifest(self.root, [manifest_entry(10, state="open")])
        own = json.loads(PLANTED_OWN_25805_JSON) | {"number": 10}
        forge = FakeForge(
            {
                f"{API}/pulls/10": own,
                f"{API}/pulls/10/files?limit=1&page=1": [{"filename": "a.c"}],
            }
        )
        with (
            unittest.mock.patch.object(upstream_links, "PAGE_SIZE", 1),
            unittest.mock.patch.object(upstream_links, "MAX_FILE_PAGES", 1),
        ):
            rc, out, err = run_cli(self.root, forge, "check")
        self.assertEqual((rc, out), (2, ""))
        self.assertIn("exceeds 1 pages", err)

    def test_scheduled_workflow_runs_upstream_links_check_read_only(self) -> None:
        """Verify upstream.yml runs upstream_links.py check and fails the job on overlap."""
        path = Path(__file__).resolve().parent.parent / ".github/workflows/upstream.yml"
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        steps = workflow["jobs"]["drift"]["steps"]
        links = next(step for step in steps if step.get("id") == "links")
        fail = steps[-1]
        self.assertEqual(links["run"], "python3 scripts/upstream_links.py check")
        self.assertIn("failure()", links["if"])
        self.assertIn("steps.links.outcome == 'failure'", fail["if"])
        self.assertEqual(workflow["permissions"], {"contents": "read"})


if __name__ == "__main__":
    unittest.main()
