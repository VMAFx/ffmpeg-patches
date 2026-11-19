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
    root: Path,
    forge: FakeForge,
    *args: str,
    today: date | None = TODAY,
    sleep: upstream_links.Sleep | None = None,
) -> tuple[int, str, str]:
    """Run upstream_links.main with captured stdout and stderr."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = upstream_links.main(
            ["--root", str(root), *args],
            fetch=forge,
            today=today,
            sleep=sleep if sleep is not None else lambda _: None,
        )
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


def forge_pull(number: int, opened: str = "2026-11-01", updated: str = "2026-11-11") -> dict:
    """Build a complete open Forgejo pull with controllable opening and update dates."""
    return {
        "number": number,
        "user": {"login": "alice" if number < 100 else "bob"},
        "title": f"component: distinct change {number}",
        "state": "open",
        "merged": False,
        "created_at": f"{opened}T00:00:00Z",
        "updated_at": f"{updated}T00:00:00Z",
        "merged_at": None,
        "body": "Fixes #99",
    }


class RequestCostTest(unittest.TestCase):
    """Bound request cost using three own pulls and five unrelated-subject candidates."""

    def setUp(self) -> None:
        """Build literal in-memory forge answers and an isolated three-row manifest."""
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.path = write_manifest(self.root, [manifest_entry(n) for n in (10, 20, 30)])
        self.own = [forge_pull(n, f"2026-11-{day:02}") for n, day in ((10, 1), (20, 3), (30, 5))]
        self.candidates = [forge_pull(n) for n in range(101, 106)]
        self.page = f"{API}/pulls?state=all&sort=recentupdate&limit=50&page=1"
        self.answers = {f"{API}/pulls/{p['number']}": p for p in self.own[:3]}
        self.answers[self.page] = self.candidates + self.own
        for number in (10, 20, 30, 101, 102, 103, 104, 105):
            self.answers[f"{API}/pulls/{number}/files?limit=50&page=1"] = [{"filename": "shared.c"}]

    def test_fill_reads_each_address_once(self) -> None:
        """Direct fill shares one candidate scan and file memo across all own pulls."""
        forge = FakeForge(self.answers)
        with unittest.mock.patch("time.sleep") as pause:
            upstream_links.fill_manifest(self.root, forge, today=TODAY)
        self.assertEqual(len(forge.read), 12)
        self.assertEqual(pause.call_count, 12)
        pause.assert_called_with(upstream_links.REQUEST_PAUSE_SECONDS)
        self.assertEqual(len(forge.read), len(set(forge.read)))
        self.assertEqual(forge.read.count(self.page), 1)
        rows = json.loads(self.path.read_text(encoding="utf-8"))["contributions"]
        self.assertTrue(all(len(row["related"]) == 7 for row in rows[:3]))
        second = FakeForge(self.answers)
        with unittest.mock.patch("time.sleep"):
            upstream_links.fill_manifest(self.root, second, today=TODAY)
        self.assertEqual(second.read, forge.read)

    def test_cli_fill_reads_each_address_once(self) -> None:
        """CLI fill makes exactly twelve unique fixture requests, with no process-wide memo."""
        for _ in range(2):
            forge = FakeForge(self.answers)
            self.assertEqual(run_cli(self.root, forge, "fill")[0], 0)
            self.assertEqual(len(forge.read), 12)
            self.assertEqual(len(set(forge.read)), 12)

    def test_paginated_candidates_are_shared_and_count_toward_budget(self) -> None:
        """Shared pagination consumes one request per page and cannot evade the run budget."""
        answers = {f"{API}/pulls/{p['number']}": p for p in self.own[:3]}
        for number in (10, 20, 30, 101, 102, 103, 104, 105):
            answers[f"{API}/pulls/{number}/files?limit=2&page=1"] = [{"filename": "shared.c"}]
        for page in range(1, 4):
            url = f"{API}/pulls?state=all&sort=recentupdate&limit=2&page={page}"
            answers[url] = self.candidates[(page - 1) * 2 : page * 2]
        before = self.path.read_bytes()
        with unittest.mock.patch.object(upstream_links, "PAGE_SIZE", 2):
            forge = FakeForge(answers)
            with unittest.mock.patch.object(upstream_links, "FILL_REQUEST_CAP", 13):
                rc, out, err = run_cli(self.root, forge, "fill")
            self.assertEqual((rc, out), (2, ""))
            self.assertIn("count 14, cap 13", err)
            self.assertEqual(self.path.read_bytes(), before)
            forge = FakeForge(answers)
            self.assertEqual(run_cli(self.root, forge, "fill")[0], 0)
            self.assertEqual(len(forge.read), 14)
            self.assertEqual(len(set(forge.read)), 14)

    def test_check_skips_files_for_already_recorded_candidates(self) -> None:
        """Check leaves recorded candidates unfetched while preserving an unrecorded overlap."""
        with unittest.mock.patch("time.sleep"):
            upstream_links.fill_manifest(self.root, FakeForge(self.answers), today=TODAY)
        forge = FakeForge(self.answers)
        self.assertEqual(run_cli(self.root, forge, "check"), (0, "", ""))
        self.assertEqual(len(forge.read), 7)
        file_reads = {url for url in forge.read[:12] if "/files?" in url}
        self.assertEqual(
            file_reads, {f"{API}/pulls/{n}/files?limit=50&page=1" for n in (10, 20, 30)}
        )
        self.answers[self.page] = [forge_pull(106)] + self.candidates + self.own
        self.answers[f"{API}/pulls/106/files?limit=50&page=1"] = [{"filename": "shared.c"}]
        fresh = FakeForge(self.answers)
        rc, out, err = run_cli(self.root, fresh, "check")
        self.assertEqual((rc, err), (1, ""))
        self.assertEqual(len(out.splitlines()), 3)
        self.assertEqual(len(fresh.read), 8)
        self.assertIn("10 106 bob same-files open", out)

    def test_recorded_for_one_target_still_checks_other_targets(self) -> None:
        """A candidate recorded for one pull still needs files for another pull."""
        with unittest.mock.patch("time.sleep"):
            upstream_links.fill_manifest(self.root, FakeForge(self.answers), today=TODAY)
        data = json.loads(self.path.read_text(encoding="utf-8"))
        data["contributions"][1]["related"] = []
        self.path.write_text(json.dumps(data), encoding="utf-8")
        rc, out, err = run_cli(self.root, FakeForge(self.answers), "check")
        self.assertEqual((rc, err), (1, ""))
        self.assertIn("20 101 bob same-files open", out)
        self.assertNotIn("10 101", out)

    def test_opening_dates_filter_files_and_each_own_pull(self) -> None:
        """Ignore globally old files and filter an intermediate update per own opening date."""
        self.answers[self.page] = [
            forge_pull(101, updated="2026-11-03"),
            forge_pull(102, updated="2026-10-31"),
        ]
        forge = FakeForge(self.answers)
        rc, out, err = run_cli(self.root, forge, "fill")
        self.assertEqual((rc, err), (0, ""))
        self.assertIn('"number": 101', out)
        self.assertNotIn(f"{API}/pulls/102/files?limit=50&page=1", forge.read)
        rows = json.loads(self.path.read_text(encoding="utf-8"))["contributions"]
        self.assertEqual(
            [[r["number"] for r in e["related"][:1]] for e in rows[:3]], [[101], [101], []]
        )

    def test_check_margin_and_since_override(self) -> None:
        """Check uses checked minus seven days and accepts an exact earlier --since date."""
        self.answers[self.page] = [
            forge_pull(101, updated="2026-11-05"),
            forge_pull(102, updated="2026-11-04"),
        ]
        forge = FakeForge(self.answers)
        rc, out, err = run_cli(self.root, forge, "check")
        self.assertEqual((rc, err), (1, ""))
        self.assertIn("10 101 bob same-files open", out)
        self.assertNotIn("10 102", out)
        self.assertNotIn(f"{API}/pulls/102/files?limit=50&page=1", forge.read)
        rc, out, err = run_cli(self.root, FakeForge(self.answers), "check", "--since", "2026-11-04")
        self.assertEqual((rc, err), (1, ""))
        self.assertIn("10 102 bob same-files open", out)
        rc, out, err = run_cli(self.root, FakeForge(self.answers), "check", "--since", "2026-11-06")
        self.assertEqual((rc, out, err), (0, "", ""))

    def test_invalid_since_and_checked_dates_exit_two(self) -> None:
        """Reject impossible or non-date windows before any forge request or manifest write."""
        for value in ("2026-02-30", "2026-11-04T00:00:00Z", "yesterday"):
            forge = FakeForge(self.answers)
            rc, out, err = run_cli(self.root, forge, "check", "--since", value)
            self.assertEqual((rc, out), (2, ""))
            self.assertIn("error:", err)
            self.assertEqual(forge.read, [])
        data = json.loads(self.path.read_text(encoding="utf-8"))
        for checked in ("2026-02-30", "0001-01-01"):
            data["checked"] = checked
            self.path.write_text(json.dumps(data), encoding="utf-8")
            forge = FakeForge(self.answers)
            self.assertEqual(run_cli(self.root, forge, "check")[0], 2)
            self.assertEqual(forge.read, [])

    def test_budget_error_exits_two_without_writing_or_partial_output(self) -> None:
        """Both commands fail on the next request above their caps without writing results."""
        before = self.path.read_bytes()
        for command, constant in (("fill", "FILL_REQUEST_CAP"), ("check", "CHECK_REQUEST_CAP")):
            forge, pauses = FakeForge(self.answers), []
            with unittest.mock.patch.object(upstream_links, constant, 8):
                rc, out, err = run_cli(self.root, forge, command, sleep=pauses.append)
            self.assertEqual((rc, out), (2, ""))
            self.assertIn("count 9", err)
            self.assertIn("cap 8", err)
            self.assertEqual(len(forge.read), 8)
            self.assertEqual(pauses, [upstream_links.REQUEST_PAUSE_SECONDS] * 8)
            self.assertEqual(self.path.read_bytes(), before)

    def test_budget_boundary_and_pause_once_per_request(self) -> None:
        """Exactly the cap succeeds and every fetch receives one injected fixed pause."""
        for command, constant in (("fill", "FILL_REQUEST_CAP"), ("check", "CHECK_REQUEST_CAP")):
            forge, pauses = FakeForge(self.answers), []
            args = ["--root", str(self.root), command]
            if command == "fill":
                args.append("--dry-run")
            with (
                unittest.mock.patch.object(upstream_links, constant, 12),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                rc = upstream_links.main(args, fetch=forge, today=TODAY, sleep=pauses.append)
            self.assertIn(rc, (0, 1))
            self.assertEqual(len(forge.read), 12)
            self.assertEqual(pauses, [upstream_links.REQUEST_PAUSE_SECONDS] * 12)

    def test_fill_pull_limits_manifest_rows_and_deduplicates(self) -> None:
        """Repeatable --pull limits writes and detail reads to eligible named manifest rows."""
        forge = FakeForge(self.answers)
        rc, out, err = run_cli(
            self.root, forge, "fill", "--pull", "30", "--pull", "10", "--pull", "10"
        )
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual([line.split(":")[0] for line in out.splitlines()[:3]], ["10", "30"])
        self.assertNotIn(f"{API}/pulls/20", forge.read)
        self.assertEqual(len(forge.read), len(set(forge.read)))
        rows = json.loads(self.path.read_text(encoding="utf-8"))["contributions"]
        self.assertNotIn("related", rows[1])

    def test_fill_pull_rejects_unknown_closed_and_invalid_numbers(self) -> None:
        """Invalid fill selections fail before fetching and leave the manifest unchanged."""
        write_manifest(self.root, [manifest_entry(10), manifest_entry(20, state="closed")])
        before = self.path.read_bytes()
        for number in ("999", "20", "0", "-1"):
            forge = FakeForge(self.answers)
            rc, out, err = run_cli(self.root, forge, "fill", "--pull", number)
            self.assertEqual((rc, out), (2, ""))
            self.assertIn("error:", err)
            self.assertEqual(forge.read, [])
            self.assertEqual(self.path.read_bytes(), before)

    def test_empty_scope_makes_no_requests(self) -> None:
        """An empty manifest does not spend a request on candidates for either command."""
        write_manifest(self.root, [])
        for command in ("fill", "check"):
            forge = FakeForge({})
            self.assertEqual(run_cli(self.root, forge, command), (0, "", ""))
            self.assertEqual(forge.read, [])


class OversizedCandidateTest(unittest.TestCase):
    """Keep partial external file evidence without hiding other candidates or errors."""

    def setUp(self) -> None:
        """Create a one-pull manifest for bounded, network-free candidate listings."""
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.path = write_manifest(self.root, [manifest_entry(10)])

    def forge_with_files(
        self, count: int, *, shared: bool = False, inline: bool = False, number: int = 101
    ) -> FakeForge:
        """Plant a long file listing followed by an independent overlapping candidate."""
        names = [f"other/{index}.c" for index in range(count)]
        if shared:
            names[min(count, upstream_links.MAX_FILE_ROWS) - 1] = "shared.c"
        pulls = [forge_pull(101), forge_pull(102)]
        own = forge_pull(10)
        if inline:
            target = own if number == 10 else pulls[0]
            target["files"] = names
        answers = {
            f"{API}/pulls/10": own,
            f"{API}/pulls/10/files?limit=50&page=1": [{"filename": "shared.c"}],
            f"{API}/pulls/102/files?limit=50&page=1": [{"filename": "shared.c"}],
            f"{API}/pulls?state=all&sort=recentupdate&limit=50&page=1": pulls,
        }
        for page in range(1, count // 50 + 2):
            part = names[(page - 1) * 50 : page * 50]
            answers[f"{API}/pulls/{number}/files?limit=50&page={page}"] = [
                {"filename": name} for name in part[:50]
            ]
        return FakeForge(answers)

    def test_oversized_paged_candidate_reports_overlap_in_read_prefix(self) -> None:
        """A cap-plus-one candidate still reports a shared file in its read prefix."""
        forge = self.forge_with_files(upstream_links.MAX_FILE_ROWS + 1, shared=True)
        rc, out, err = run_cli(self.root, forge, "check")
        self.assertEqual((rc, err), (1, ""))
        self.assertIn("10 101 bob same-files open", out)
        self.assertIn("10 102 bob same-files open", out)
        self.assertNotIn("undecided", out)

    def test_oversized_inline_candidate_reports_overlap_in_read_prefix(self) -> None:
        """The metadata string reader also accepts bounded external file evidence."""
        forge = self.forge_with_files(upstream_links.MAX_FILE_ROWS + 1, shared=True, inline=True)
        rc, out, err = run_cli(self.root, forge, "check")
        self.assertEqual((rc, err), (1, ""))
        self.assertIn("10 101 bob same-files open", out)
        self.assertNotIn("undecided", out)

    def test_oversized_candidate_is_undecided_and_keeps_other_overlap(self) -> None:
        """An unread possible overlap stays visible and does not hide later candidates."""
        forge = self.forge_with_files(upstream_links.MAX_FILE_ROWS + 1)
        last_page = f"{API}/pulls/101/files?limit=50&page=21"
        forge.answers[last_page] = [{"filename": "shared.c"}]
        rc, out, err = run_cli(self.root, forge, "check")
        self.assertEqual((rc, err), (1, ""))
        line = next(line for line in out.splitlines()[:3] if "undecided" in line)
        self.assertIn("10 101", line)
        self.assertIn("1000 files read", line)
        self.assertIn("same-files", line)
        self.assertIn("10 102 bob same-files open", out)
        self.assertNotIn("10 101 bob same-files open", out)
        self.assertNotIn(last_page, forge.read)

    def test_inline_nonmatch_is_undecided_without_extra_pages(self) -> None:
        """An oversized inline list is bounded and cannot become a silent nonmatch."""
        forge = self.forge_with_files(upstream_links.MAX_FILE_ROWS + 1, inline=True)
        rc, out, err = run_cli(self.root, forge, "check")
        self.assertEqual((rc, err), (1, ""))
        self.assertIn("10 101", out)
        self.assertIn("1000 files read", out)
        self.assertIn("undecided", out)
        self.assertIn("10 102 bob same-files open", out)
        self.assertFalse(any("/pulls/101/files?" in url for url in forge.read[:24]))

    def test_undecided_alone_stays_loud_on_repeated_checks(self) -> None:
        """An undecided candidate cannot pass check or disappear on the next sighting."""
        before = self.path.read_bytes()
        for _ in range(2):
            forge = self.forge_with_files(upstream_links.MAX_FILE_ROWS + 1)
            forge.answers[f"{API}/pulls/102/files?limit=50&page=1"] = []
            rc, out, err = run_cli(self.root, forge, "check")
            self.assertEqual((rc, err), (1, ""))
            self.assertEqual(len(out.splitlines()), 1)
            self.assertIn("undecided", out)
            self.assertEqual(self.path.read_bytes(), before)

    def test_fill_and_dry_run_report_undecided_without_recording_false_relation(self) -> None:
        """Both fill modes expose partial nonmatches and store only proven relations."""
        before = self.path.read_bytes()
        for flags in (("--dry-run",), ()):
            forge = self.forge_with_files(upstream_links.MAX_FILE_ROWS + 1)
            rc, out, err = run_cli(self.root, forge, "fill", *flags)
            self.assertEqual((rc, err), (0, ""))
            self.assertIn("10 101", out)
            self.assertIn("1000 files read", out)
            self.assertIn("undecided", out)
            self.assertIn('"number": 102', out)
            if flags:
                self.assertEqual(self.path.read_bytes(), before)
        rows = json.loads(self.path.read_text(encoding="utf-8"))["contributions"]
        self.assertEqual([row["number"] for row in rows[0]["related"]], [102])
        rc, out, err = run_cli(
            self.root, self.forge_with_files(upstream_links.MAX_FILE_ROWS + 1), "check"
        )
        self.assertEqual((rc, err), (1, ""))
        self.assertIn("undecided", out)
        self.assertNotIn("10 102", out)

    def test_exact_cap_inline_candidate_uses_normal_path(self) -> None:
        """A complete inline list exactly at the cap is a decided nonmatch or overlap."""
        for shared in (False, True):
            forge = self.forge_with_files(upstream_links.MAX_FILE_ROWS, inline=True, shared=shared)
            rc, out, err = run_cli(self.root, forge, "check")
            self.assertEqual((rc, err), (1, ""))
            self.assertNotIn("undecided", out)
            self.assertEqual("10 101 bob same-files open" in out, shared)

    def test_exact_cap_short_final_page_uses_normal_path(self) -> None:
        """Exactly the name cap on a terminating page is not marked incomplete."""
        forge = self.forge_with_files(0)
        for page in range(1, 11):
            start = (page - 1) * 50
            forge.answers[f"{API}/pulls/101/files?limit=50&page={page}"] = [
                {"filename": f"new/{n}", "previous_filename": f"old/{n}"}
                for n in range(start, start + 50)
            ]
        # The final page supplies the last two names while remaining shorter than PAGE_SIZE.
        with unittest.mock.patch.object(upstream_links, "MAX_FILE_ROWS", 902):
            forge.answers[f"{API}/pulls/101/files?limit=50&page=10"] = [
                {"filename": "last.c", "previous_filename": "last-old.c"}
            ]
            rc, out, err = run_cli(self.root, forge, "check")
        self.assertEqual((rc, err), (1, ""))
        self.assertNotIn("undecided", out)
        self.assertIn("10 102 bob same-files open", out)

    def test_own_pull_above_cap_remains_identified_hard_error(self) -> None:
        """Oversized own inline and paged lists still exit 2 and identify pull 10."""
        before = self.path.read_bytes()
        for inline in (False, True):
            forge = self.forge_with_files(
                upstream_links.MAX_FILE_ROWS + 1, number=10, inline=inline
            )
            rc, out, err = run_cli(self.root, forge, "fill")
            self.assertEqual((rc, out), (2, ""))
            self.assertIn(f"{API}/pulls/10/files" if not inline else f"{API}/pulls/10:", err)
            self.assertEqual(self.path.read_bytes(), before)

    def test_page_cap_is_partial_for_candidate_but_hard_for_own(self) -> None:
        """A full last permitted page is undecided only for an external pull."""
        with unittest.mock.patch.object(upstream_links, "MAX_FILE_PAGES", 1):
            forge = self.forge_with_files(51)
            rc, out, err = run_cli(self.root, forge, "check")
            self.assertEqual((rc, err), (1, ""))
            self.assertIn("50 files read", out)
            self.assertIn("undecided", out)
            own = self.forge_with_files(51, number=10)
            rc, out, err = run_cli(self.root, own, "check")
            self.assertEqual((rc, out), (2, ""))
            self.assertIn(f"{API}/pulls/10/files", err)

    def test_oversized_requests_are_bounded_paced_and_budgeted(self) -> None:
        """Every retained candidate page consumes a paced request from the run budget."""
        for budget, expected in ((24, 1), (23, 2)):
            forge, pauses = self.forge_with_files(upstream_links.MAX_FILE_ROWS + 1), []
            with unittest.mock.patch.object(upstream_links, "CHECK_REQUEST_CAP", budget):
                rc, out, err = run_cli(self.root, forge, "check", sleep=pauses.append)
            self.assertEqual(rc, expected)
            self.assertEqual(len(forge.read), budget)
            self.assertEqual(pauses, [upstream_links.REQUEST_PAUSE_SECONDS] * budget)
            file_pages = [url for url in forge.read[:24] if "/pulls/101/files?" in url]
            self.assertEqual(len(file_pages), upstream_links.MAX_FILE_PAGES)
            if expected == 2:
                self.assertEqual(out, "")
                self.assertIn("count 24, cap 23", err)

    def test_malformed_candidate_listings_stay_identified_hard_errors(self) -> None:
        """Malformed inline strings and file rows remain fatal with a source identifier."""
        malformed = (
            {},
            [None],
            [{}],
            [{"filename": " "}],
            [{"filename": 9}],
            [{"filename": "a.c", "previous_filename": 9}],
            [{"filename": "a.c"}] * 51,
        )
        for batch in malformed[:7]:
            forge = self.forge_with_files(1)
            forge.answers[f"{API}/pulls/101/files?limit=50&page=1"] = batch
            rc, out, err = run_cli(self.root, forge, "fill")
            self.assertEqual((rc, out), (2, ""))
            self.assertIn("101", err)
        forge = self.forge_with_files(1, inline=True)
        forge.answers[f"{API}/pulls?state=all&sort=recentupdate&limit=50&page=1"][0]["files"] = [
            None
        ]
        rc, out, err = run_cli(self.root, forge, "check")
        self.assertEqual((rc, out), (2, ""))
        self.assertIn("101", err)

    def test_rename_names_cannot_overrun_candidate_cap(self) -> None:
        """Rename aliases count toward the name cap without aborting the candidate scan."""
        forge = self.forge_with_files(0)
        forge.answers[f"{API}/pulls/101/files?limit=50&page=1"] = [
            {"filename": "a.c", "previous_filename": "shared.c"},
            {"filename": "b.c", "previous_filename": "old-b.c"},
        ]
        with unittest.mock.patch.object(upstream_links, "MAX_FILE_ROWS", 3):
            rc, out, err = run_cli(self.root, forge, "check")
        self.assertEqual((rc, err), (1, ""))
        self.assertIn("10 101 bob same-files open", out)


if __name__ == "__main__":
    unittest.main()
