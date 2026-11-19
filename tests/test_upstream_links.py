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

# Handwritten top-level key/type match for the supplied pull.json; neutral values.
COUNT_PULL = {
    "additions": 1,
    "allow_maintainer_edit": False,
    "assignee": None,
    "assignees": None,
    "base": {"ref": "master"},
    "body": "Fixes #99",
    "changed_files": 15,
    "closed_at": None,
    "comments": 0,
    "created_at": "2026-11-01T00:00:00Z",
    "deletions": 0,
    "diff_url": "https://example.invalid/change.diff",
    "draft": False,
    "due_date": None,
    "flow": 0,
    "head": {"sha": "a" * 40},
    "html_url": f"{FORGE}25879",
    "id": 1,
    "is_locked": False,
    "labels": [],
    "merge_base": "b" * 40,
    "merge_commit_sha": None,
    "mergeable": True,
    "merged": False,
    "merged_at": None,
    "merged_by": None,
    "milestone": None,
    "number": 25879,
    "patch_url": "https://example.invalid/change.patch",
    "pin_order": 0,
    "requested_reviewers": [],
    "requested_reviewers_teams": [],
    "review_comments": 0,
    "state": "open",
    "title": "component: neutral change",
    "updated_at": "2026-11-11T00:00:00Z",
    "url": f"{API}/pulls/25879",
    "user": {"login": "owner"},
}


PLANTED_OWN_25805_JSON = """{
  "number": 25805,
  "head": {"sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
  "base": {"ref": "master"},
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
  "head": {"sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
  "base": {"ref": "master"},
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
  "head": {"sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
  "base": {"ref": "master"},
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
    keep_progress: bool = False,
) -> tuple[int, str, str]:
    """Capture CLI results, optionally retaining the separately tested progress line."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = upstream_links.main(
            ["--root", str(root), *args],
            fetch=forge,
            today=today,
            sleep=sleep if sleep is not None else lambda _: None,
        )
    output = out.getvalue()
    if not keep_progress:
        output = "".join(
            line
            for line in output.splitlines(keepends=True)
            if not (line.startswith("window ") and "; expected requests " in line)
        )
    return rc, output, err.getvalue()


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
        """A same-author candidate needs no file request or new relation."""
        same_author_json = PLANTED_OTHER_25950_JSON.replace('"bcoudurier"', '"lusoris"')
        forge = self.planted_forge(other_json=same_author_json)
        rc, out, err = run_cli(self.root, forge, "check", "--pull", "25805")
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(out, "")
        self.assertNotIn(f"{API}/pulls/25950/files?limit=50&page=1", forge.read)

    def test_planted_control_other_file_exits_zero(self) -> None:
        """A candidate touching another file is not related and exits 0."""
        forge = self.planted_forge(other_files_json='[{"filename": "tests/checkasm/other.c"}]')
        rc, out, err = run_cli(self.root, forge, "check", "--pull", "25805")
        self.assertEqual((rc, out, err), (0, "", ""))

    def test_planted_control_merged_before_own_opened_exits_zero(self) -> None:
        """A candidate merged before the own pull was opened is not an overlap and exits 0."""
        merged_before_json = """{
          "number": 25950,
  "head": {"sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
  "base": {"ref": "master"},
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
        self.assertEqual(out, "")
        self.assertNotIn(f"{API}/pulls/25950/files?limit=50&page=1", forge.read)

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
        """Read complete hash and configured URL items after closing keywords."""
        body = (
            "Summary of change.\n\n"
            "Fixes #25052, #25010\n"
            "Closes: #24900\n"
            "Refs https://code.ffmpeg.org/FFmpeg/FFmpeg/issues/24800 and #25052.\n"
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
            "head": {"sha": "a" * 40},
            "base": {"ref": "master"},
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
            "head": {"sha": "a" * 40},
            "base": {"ref": "master"},
            "user": {"login": "alice"},
            "title": "avutil/test: change 20",
            "state": "closed",
            "merged": True,
            "created_at": "2026-11-07T00:00:00Z",
            "updated_at": "2026-11-10T00:00:00Z",
            "merged_at": "2026-11-10T00:00:00Z",
            "body": "Closes #77",
        }
        pull_25 = {
            "number": 25,
            "head": {"sha": "a" * 40},
            "base": {"ref": "master"},
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
                f"{API}/pulls?state=all&sort=recentupdate&limit=1&page=1": [],
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
        "head": {"sha": "a" * 40},
        "base": {"ref": "master"},
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
        with unittest.mock.patch("time.sleep") as pause, contextlib.redirect_stdout(io.StringIO()):
            upstream_links.fill_manifest(self.root, forge, today=TODAY)
        self.assertEqual(len(forge.read), 12)
        self.assertEqual(pause.call_count, 12)
        pause.assert_called_with(upstream_links.REQUEST_PAUSE_SECONDS)
        self.assertEqual(len(forge.read), len(set(forge.read)))
        self.assertEqual(forge.read.count(self.page), 1)
        rows = json.loads(self.path.read_text(encoding="utf-8"))["contributions"]
        self.assertTrue(all(len(row["related"]) == 5 for row in rows[:3]))
        second = FakeForge(self.answers)
        with unittest.mock.patch("time.sleep"), contextlib.redirect_stdout(io.StringIO()):
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
            with unittest.mock.patch.object(upstream_links, "request_cap", return_value=13):
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
        with unittest.mock.patch("time.sleep"), contextlib.redirect_stdout(io.StringIO()):
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
        with unittest.mock.patch("time.sleep"), contextlib.redirect_stdout(io.StringIO()):
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
        for command in ("fill", "check"):
            forge, pauses = FakeForge(self.answers), []
            with unittest.mock.patch.object(upstream_links, "request_cap", return_value=8):
                rc, out, err = run_cli(self.root, forge, command, sleep=pauses.append)
            self.assertEqual((rc, out), (2, ""))
            self.assertIn("count 9", err)
            self.assertIn("cap 8", err)
            self.assertEqual(len(forge.read), 8)
            self.assertEqual(pauses, [upstream_links.REQUEST_PAUSE_SECONDS] * 8)
            self.assertEqual(self.path.read_bytes(), before)

    def test_budget_boundary_and_pause_once_per_request(self) -> None:
        """Exactly the cap succeeds and every fetch receives one injected fixed pause."""
        for command in ("fill", "check"):
            forge, pauses = FakeForge(self.answers), []
            args = ["--root", str(self.root), command]
            if command == "fill":
                args.append("--dry-run")
            with (
                unittest.mock.patch.object(upstream_links, "request_cap", return_value=12),
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


def measured_window_forge() -> FakeForge:
    """Model the measured 556-row window with 503 possible overlap candidates."""
    own = [forge_pull(number, "2026-11-05") for number in (10, 20)]
    candidates = [forge_pull(number, "2026-11-06") for number in range(1000, 1556)]
    for index, row in enumerate(candidates[:556]):
        row["created_at"] = "2026-11-01T00:00:00Z"
        if index < 181 or 356 <= index < 452:
            row["created_at"] = "2026-11-06T00:00:00Z"
        if 356 <= index < 533:
            row.update(state="closed", merged=True, merged_at="2026-11-07T00:00:00Z")
            if index >= 503:
                row["merged_at"] = "2026-11-04T00:00:00Z"
        elif index >= 533:
            row["state"] = "closed"
    answers = {f"{API}/pulls/{row['number']}": row for row in own[:2]}
    for number in (10, 20):
        answers[f"{API}/pulls/{number}/files?limit=50&page=1"] = [{"filename": "shared.c"}]
    for row in candidates[:556]:
        number = row["number"]
        name = "shared.c" if number == 1000 else f"other/{number}.c"
        answers[f"{API}/pulls/{number}/files?limit=50&page=1"] = [{"filename": name}]
    for page in range(1, 13):
        url = f"{API}/pulls?state=all&sort=recentupdate&limit=50&page={page}"
        answers[url] = candidates[(page - 1) * 50 : page * 50]
    return FakeForge(answers)


class MeasuredWindowTest(unittest.TestCase):
    """Exercise real-window request counts and each metadata-only exclusion offline."""

    def setUp(self) -> None:
        """Create an isolated two-pull manifest with a nightly cutoff matching the window."""
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.path = write_manifest(self.root, [manifest_entry(10), manifest_entry(20)])

    def test_measured_window_fill_and_check_finish_under_derived_caps(self) -> None:
        """Finish both 556-row scans with exactly 519 paced requests instead of budget errors."""
        for command in ("fill", "check"):
            with self.subTest(command=command):
                write_manifest(self.root, [manifest_entry(10), manifest_entry(20)])
                forge, pauses = measured_window_forge(), []
                rc, out, err = run_cli(self.root, forge, command, sleep=pauses.append)
                self.assertEqual((rc, err), (int(command == "check"), ""))
                self.assertEqual(len(forge.read), 2 + 12 + 2 + 503)
                self.assertEqual(len(forge.read), len(set(forge.read)))
                self.assertEqual(pauses, [0.5] * 519)
                self.assertLessEqual(len(forge.read), upstream_links.request_cap(2))
                self.assertIn("1000", out)
                self.assertTrue(
                    all(
                        f"/pulls/{n}/files?" not in url
                        for n in range(1503, 1556)
                        for url in forge.read[:519]
                    )
                )

    def test_progress_precedes_first_files_request_and_accounts_for_pages(self) -> None:
        """Publish candidate count and honest one-page and maximum costs before any files read."""
        forge = measured_window_forge()
        output, errors, at_files = io.StringIO(), io.StringIO(), []
        # A second file page must increase the measured cost without changing the estimate.
        url = f"{API}/pulls/1001/files?limit=50&page=1"
        forge.answers[url] = [{"filename": f"other/{n}.c"} for n in range(50)]
        forge.answers[url.replace("page=1", "page=2")] = [{"filename": "last.c"}]

        def fetch(url: str) -> dict | list:
            """Record visible progress at each file request before serving the fixture."""
            if "/files?" in url:
                at_files.append(output.getvalue())
            return forge(url)

        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            rc = upstream_links.main(
                ["--root", str(self.root), "check"], fetch, TODAY, sleep=lambda _: None
            )
        self.assertEqual((rc, errors.getvalue()), (1, ""))
        expected = (
            "window 2026-11-05T00:00:00+00:00: candidates 556, file candidates 503; "
            "expected requests 519 (one page per file listing), at most 10114; cap 20062; "
            "same-patch-id not checked live\n"
        )
        self.assertEqual(at_files[0], expected)
        self.assertEqual(output.getvalue().count("expected requests"), 1)
        self.assertEqual(len(forge.read), 520)

    def test_metadata_filters_keep_old_open_and_strictly_later_merges(self) -> None:
        """Skip author, closed, early or equal merge, self and old-update rows before files."""
        write_manifest(self.root, [manifest_entry(10)])
        own = forge_pull(10, "2026-11-06")
        candidates = [forge_pull(n, "2026-11-01") for n in range(101, 108)]
        candidates[0]["user"] = {"login": "alice"}
        candidates[1]["state"] = "closed"
        for index, day in ((2, 5), (3, 6), (4, 7)):
            candidates[index].update(
                state="closed", merged=True, merged_at=f"2026-11-{day:02}T00:00:00Z"
            )
        candidates[6]["updated_at"] = "2026-11-05T00:00:00Z"
        answers = {
            f"{API}/pulls/10": own,
            f"{API}/pulls/10/files?limit=50&page=1": [{"filename": "shared.c"}],
            f"{API}/pulls?state=all&sort=recentupdate&limit=50&page=1": candidates + [own],
        }
        for number in (105, 106):
            answers[f"{API}/pulls/{number}/files?limit=50&page=1"] = [{"filename": "shared.c"}]
        forge = FakeForge(answers)
        rc, out, err = run_cli(self.root, forge, "fill")
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(len(forge.read), 5)
        rows = json.loads(self.path.read_text(encoding="utf-8"))["contributions"][0]["related"]
        self.assertEqual([row["number"] for row in rows], [105, 106])
        self.assertNotIn('"number": 101', out)

    def test_derived_cap_admits_last_request_and_rejects_one_more_with_context(self) -> None:
        """Allow exactly the derived bound and reject the next request with window and remedy."""
        write_manifest(self.root, [manifest_entry(10)])
        answers = {
            f"{API}/pulls/10": forge_pull(10, "2026-11-05"),
            f"{API}/pulls/10/files?limit=50&page=1": [{"filename": "shared.c"}],
            f"{API}/pulls?state=all&sort=recentupdate&limit=50&page=1": [forge_pull(101)],
            f"{API}/pulls/101/files?limit=50&page=1": [{"filename": "other.c"}],
        }
        with (
            unittest.mock.patch.object(upstream_links, "MAX_PULL_PAGES", 1),
            unittest.mock.patch.object(upstream_links, "MAX_CANDIDATES", 1),
            unittest.mock.patch.object(upstream_links, "MAX_FILE_PAGES", 1),
        ):
            self.assertEqual(upstream_links.request_cap(1), 4)
            for command in ("fill", "check"):
                forge, pauses = FakeForge(answers), []
                request = upstream_links.RequestBudget(
                    forge, upstream_links.request_cap(1), pauses.append
                )
                with contextlib.redirect_stdout(io.StringIO()):
                    if command == "fill":
                        upstream_links.fill_manifest(self.root, request, dry_run=True, today=TODAY)
                    else:
                        upstream_links.check_overlaps(self.root, [], request, today=TODAY)
                with self.assertRaises(upstream_links.DriftError) as caught:
                    request("https://example.invalid/one-too-many")
                message = str(caught.exception)
                self.assertIn("count 5, cap 4", message)
                self.assertIn("window start 2026-11-05T00:00:00+00:00", message)
                self.assertIn("candidates 1", message)
                self.assertIn("--pull N" if command == "fill" else "--since YYYY-MM-DD", message)
                self.assertEqual(len(forge.read), 4)
                self.assertEqual(pauses, [0.5] * 4)

    def test_fill_preserves_checked_and_recorded_rows_without_replaying_files(self) -> None:
        """Preserve snapshot and relations while checks fetch only new overlaps."""
        forge = measured_window_forge()
        self.assertEqual(run_cli(self.root, forge, "fill", today=date(2026, 11, 19))[0], 0)
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(data["checked"], "2026-11-12")
        self.assertEqual([row["number"] for row in data["contributions"][0]["related"]], [1000])
        repeated = measured_window_forge()
        self.assertEqual(run_cli(self.root, repeated, "check"), (0, "", ""))
        self.assertEqual(len(repeated.read), 2 + 12 + 2 + 502)
        self.assertNotIn(f"{API}/pulls/1000/files?limit=50&page=1", repeated.read)
        before = self.path.read_bytes()
        page = f"{API}/pulls?state=all&sort=recentupdate&limit=50&page=1"
        repeated.answers[page][0]["user"] = {"login": "alice"}
        self.assertEqual(run_cli(self.root, FakeForge(repeated.answers), "fill")[0], 0)
        self.assertEqual(self.path.read_bytes(), before)


class FillAgeWindowTest(unittest.TestCase):
    """Bound fill history independently of old own pulls and the nightly snapshot."""

    def setUp(self) -> None:
        """Create old and recent own pulls with candidates on both sides of the age boundary."""
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.path = write_manifest(self.root, [manifest_entry(10), manifest_entry(20)])
        self.before = self.path.read_bytes()
        self.page = f"{API}/pulls?state=all&sort=recentupdate&limit=2&page="
        self.answers = {
            f"{API}/pulls/10": forge_pull(10, "2026-06-01"),
            f"{API}/pulls/20": forge_pull(20, "2026-11-10"),
            self.page + "1": [forge_pull(101), forge_pull(102, updated="2026-10-29")],
        }
        for page in range(2, 21):
            self.answers[self.page + str(page)] = [
                forge_pull(103, updated="2026-10-28"),
                forge_pull(104, updated="2026-10-28"),
            ]
        for number in (10, 20, 101, 102, 103):
            self.answers[f"{API}/pulls/{number}/files?limit=2&page=1"] = [{"filename": "shared.c"}]

    def test_old_and_recent_fill_finishes_with_exact_cost_and_honest_notice(self) -> None:
        """Stop before old updates, warn only for the old own pull and persist proven links."""
        for flags in (("--dry-run",), ()):
            forge, pauses = FakeForge(self.answers), []
            with unittest.mock.patch.object(upstream_links, "PAGE_SIZE", 2):
                rc, out, err = run_cli(
                    self.root, forge, "fill", *flags, sleep=pauses.append, keep_progress=True
                )
            self.assertEqual((rc, err), (0, ""))
            self.assertEqual(len(forge.read), 8)
            self.assertEqual(len(set(forge.read)), 8)
            self.assertEqual(pauses, [0.5] * 8)
            self.assertEqual(
                out.splitlines()[0],
                "window 2026-10-29T00:00:00+00:00: candidates 2, file candidates 2; "
                "expected requests 8 (one page per file listing), at most 84; cap 20062; "
                "same-patch-id not checked live",
            )
            self.assertEqual(
                [line for line in out.splitlines()[:4] if "not read" in line],
                [
                    "10: candidates updated before 2026-10-29T00:00:00+00:00 "
                    "not read (fill age limit)"
                ],
            )
            self.assertEqual(out.count("not read"), 1)
            self.assertNotIn(self.page + "3", forge.read)
            self.assertFalse(any("/pulls/103/files?" in url for url in forge.read[:8]))
            if flags:
                self.assertEqual(self.path.read_bytes(), self.before)
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertNotEqual(self.path.read_bytes(), self.before)
        self.assertEqual(data["checked"], "2026-11-12")
        self.assertEqual(
            [
                [row["number"] for row in entry["related"][:2]]
                for entry in data["contributions"][:2]
            ],
            [[101, 102], [101]],
        )
        self.assertEqual(set(data), {"checked", "contributions"})
        for entry in data["contributions"][:2]:
            self.assertEqual(set(entry), set(manifest_entry(10)) | {"fixes_issues", "related"})

    def test_single_old_pull_uses_same_age_boundary(self) -> None:
        """Selecting only an old pull still terminates at the bounded fill window."""
        forge = FakeForge(self.answers)
        with unittest.mock.patch.object(upstream_links, "PAGE_SIZE", 2):
            rc, out, err = run_cli(self.root, forge, "fill", "--pull", "10", keep_progress=True)
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(len(forge.read), 6)
        self.assertIn("expected requests 6 (one page per file listing), at most 63; cap 20041", out)
        self.assertEqual(out.count("not read"), 1)
        self.assertNotIn(f"{API}/pulls/20", forge.read)
        rows = json.loads(self.path.read_text(encoding="utf-8"))["contributions"]
        self.assertNotIn("related", rows[1])

    def test_direct_fill_preserves_old_record_without_claiming_coverage(self) -> None:
        """Direct fill uses the age bound and retains prior relations outside that window."""
        previous = {
            "number": 103,
            "kind": "pull",
            "author": "bob",
            "reason": "same-files",
            "state": "open",
            "seen": "2026-06-02",
        }
        write_manifest(self.root, [manifest_entry(10, related=[previous]), manifest_entry(20)])
        forge = FakeForge(self.answers)
        with (
            unittest.mock.patch.object(upstream_links, "PAGE_SIZE", 2),
            unittest.mock.patch("time.sleep"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            lines = upstream_links.fill_manifest(self.root, forge, today=TODAY)
        self.assertEqual(len(forge.read), 8)
        self.assertEqual(sum("not read" in line for line in lines[:3]), 1)
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(data["checked"], "2026-11-12")
        self.assertEqual(data["contributions"][0]["related"][-1], previous)
        self.assertFalse(any("/pulls/103/files?" in url for url in forge.read[:8]))

    def test_own_opened_exactly_at_boundary_needs_no_notice(self) -> None:
        """An own opening equal to the cutoff has no omitted eligible update interval."""
        self.answers[f"{API}/pulls/10"] = forge_pull(10, "2026-10-29")
        forge = FakeForge(self.answers)
        with unittest.mock.patch.object(upstream_links, "PAGE_SIZE", 2):
            rc, out, err = run_cli(self.root, forge, "fill")
        self.assertEqual((rc, err), (0, ""))
        self.assertNotIn("not read", out)
        self.assertEqual(len(forge.read), 8)

    def test_notice_and_undecided_do_not_truncate_fill_change_line(self) -> None:
        """The bounded output includes the new notice alongside undecided and changed rows."""
        write_manifest(self.root, [manifest_entry(10)])
        self.answers[self.page + "1"] = [forge_pull(101) | {"files": ["other.c", "extra.c"]}]
        forge = FakeForge(self.answers)
        with (
            unittest.mock.patch.object(upstream_links, "PAGE_SIZE", 2),
            unittest.mock.patch.object(upstream_links, "MAX_CANDIDATES", 1),
            unittest.mock.patch.object(upstream_links, "MAX_FILE_ROWS", 1),
            unittest.mock.patch.object(upstream_links.upstream_drift, "MAX_SERIES_ENTRIES", 1),
        ):
            rc, out, err = run_cli(self.root, forge, "fill")
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(len(out.splitlines()), 3)
        self.assertIn("not read", out.splitlines()[0])
        self.assertIn("same-files undecided: 1 files read", out.splitlines()[1])
        self.assertEqual(out.splitlines()[2], "10: fixes_issues=[99] related=[]")
        self.assertEqual(len(forge.read), 3)

    def test_widened_fill_reads_older_candidates_and_reports_new_boundary(self) -> None:
        """Widening the option admits additional old updates without implying full coverage."""
        self.answers[self.page + "2"] = [forge_pull(103, updated="2026-10-20")]
        forge = FakeForge(self.answers)
        with unittest.mock.patch.object(upstream_links, "PAGE_SIZE", 2):
            rc, out, err = run_cli(
                self.root, forge, "fill", "--max-age-days", "30", keep_progress=True
            )
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(len(forge.read), 9)
        self.assertIn(
            "expected requests 9 (one page per file listing), at most 104; cap 20062", out
        )
        self.assertIn("10: candidates updated before 2026-10-13T00:00:00+00:00 not read", out)
        rows = json.loads(self.path.read_text(encoding="utf-8"))["contributions"]
        self.assertEqual([row["number"] for row in rows[0]["related"][:3]], [101, 102, 103])

    def test_page_cap_inside_age_boundary_aborts_without_writing(self) -> None:
        """A full last permitted recent page remains a hard failure with no partial writes."""
        self.answers[self.page + "1"] = [forge_pull(101), forge_pull(102)]
        self.answers[self.page + "2"] = [forge_pull(103), forge_pull(104)]
        forge = FakeForge(self.answers)
        with (
            unittest.mock.patch.object(upstream_links, "PAGE_SIZE", 2),
            unittest.mock.patch.object(upstream_links, "MAX_PULL_PAGES", 2),
        ):
            rc, out, err = run_cli(self.root, forge, "fill")
        self.assertEqual((rc, out), (2, ""))
        self.assertIn(f"{API}/pulls: exceeds 2 pages", err)
        self.assertEqual(
            forge.read,
            [f"{API}/pulls/10", f"{API}/pulls/20", self.page + "1", self.page + "2"],
        )
        self.assertEqual(self.path.read_bytes(), self.before)

    def test_age_option_rejects_out_of_range_before_requests(self) -> None:
        """Reject invalid ages without fetching or writing, including an empty scope."""
        for entries in ([manifest_entry(10)], []):
            write_manifest(self.root, entries)
            before = self.path.read_bytes()
            for value in ("0", "-1", "366", "1.5", "unknown"):
                forge = FakeForge({})
                rc, out, err = run_cli(self.root, forge, "fill", "--max-age-days", value)
                self.assertEqual((rc, out), (2, ""))
                self.assertIn("--max-age-days", err)
                self.assertEqual(forge.read, [])
                self.assertEqual(self.path.read_bytes(), before)

    def test_age_range_endpoints_and_calendar_underflow(self) -> None:
        """Admit both age endpoints but reject unrepresentable dates and non-integer calls."""
        write_manifest(self.root, [])
        for value in ("1", "365"):
            self.assertEqual(
                run_cli(self.root, FakeForge({}), "fill", "--max-age-days", value), (0, "", "")
            )
        forge = FakeForge({})
        rc, out, err = run_cli(self.root, forge, "fill", today=date.min)
        self.assertEqual((rc, out), (2, ""))
        self.assertIn("calendar range", err)
        self.assertEqual(forge.read, [])
        for value in (True, 1.5, "14", None):
            with self.assertRaises(upstream_links.DriftError):
                upstream_links.fill_window(value, TODAY)
        self.assertEqual(forge.read, [])

    def test_nightly_old_snapshot_keeps_its_window_and_does_not_write(self) -> None:
        """Nightly scans can extend beyond the fill age and retain the seven-day margin."""
        data = json.loads(self.before)
        data["checked"] = "2026-07-01"
        self.path.write_text(json.dumps(data), encoding="utf-8")
        before = self.path.read_bytes()
        self.answers[self.page + "1"] = [
            forge_pull(101, "2026-06-01", "2026-06-24"),
            forge_pull(102, "2026-06-01", "2026-06-23"),
        ]
        forge = FakeForge(self.answers)
        with unittest.mock.patch.object(upstream_links, "PAGE_SIZE", 2):
            rc, out, err = run_cli(self.root, forge, "check", keep_progress=True)
        self.assertEqual((rc, err), (1, ""))
        self.assertEqual(
            out.splitlines()[0],
            "window 2026-06-24T00:00:00+00:00: candidates 1, file candidates 1; "
            "expected requests 6 (one page per file listing), at most 63; cap 20062; "
            "same-patch-id not checked live",
        )
        self.assertEqual(len(forge.read), 6)
        self.assertIn("10 101 bob same-files open", out)
        self.assertNotIn("not read", out)
        self.assertNotIn("10 102", out)
        self.assertEqual(self.path.read_bytes(), before)


class ForgeFileCountTest(unittest.TestCase):
    """Separate forge file counts from inline names with literal offline answers."""

    def setUp(self) -> None:
        """Create one own pull with the measured top-level payload shape."""
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.path = write_manifest(self.root, [manifest_entry(25879)])
        self.page = f"{API}/pulls?state=all&sort=recentupdate&limit=50&page=1"
        self.rows = [
            {
                "additions": 1,
                "changes": 1,
                "contents_url": "https://example.invalid/file",
                "deletions": 0,
                "filename": f"file-{n}.c",
                "html_url": "https://example.invalid/file",
                "raw_url": "https://example.invalid/raw",
                "status": "changed",
            }
            for n in range(15)
        ]
        self.answers = {
            f"{API}/pulls/25879": dict(COUNT_PULL),
            f"{API}/pulls/25879/files?limit=50&page=1": self.rows,
            self.page: [],
        }

    def test_count_payload_fill_dry_run_and_check(self) -> None:
        """Accept the measured integer count through both complete CLI paths."""
        before = self.path.read_bytes()
        for args in (("fill", "--dry-run"), ("check",)):
            forge = FakeForge(self.answers)
            rc, out, err = run_cli(self.root, forge, *args)
            self.assertEqual((rc, err), (0, ""))
            self.assertEqual(forge.read.count(f"{API}/pulls/25879/files?limit=50&page=1"), 1)
            self.assertEqual(self.path.read_bytes(), before)
            if args[0] == "fill":
                self.assertIn("25879: fixes_issues=[99]", out)
            else:
                self.assertEqual(out, "")

    def candidate_answers(self, **changes: object) -> dict:
        """Move the measured count payload to a list row beside a minimal own pull."""
        write_manifest(self.root, [manifest_entry(10)])
        return {
            f"{API}/pulls/10": forge_pull(10),
            f"{API}/pulls/10/files?limit=50&page=1": [{"filename": "file-0.c"}],
            self.page: [COUNT_PULL | changes],
            f"{API}/pulls/25879/files?limit=50&page=1": self.rows,
        }

    def test_candidate_integer_count_reads_endpoint_and_reports_overlap(self) -> None:
        """Read integer-count candidate names from the endpoint and preserve overlap."""
        forge = FakeForge(self.candidate_answers())
        rc, out, err = run_cli(self.root, forge, "check")
        self.assertEqual((rc, err), (1, ""))
        self.assertEqual(out.strip(), "10 25879 owner same-files open")
        self.assertEqual(forge.read.count(f"{API}/pulls/25879/files?limit=50&page=1"), 1)

    def test_complete_count_disagreement_finishes(self) -> None:
        """Accept zero and differing counts beside nonempty own and candidate listings."""
        for candidate in (False, True):
            for count in (0, 16):
                for args in (("fill", "--dry-run"), ("fill",), ("check",)):
                    with self.subTest(candidate=candidate, count=count, args=args):
                        write_manifest(self.root, [manifest_entry(25879)])
                        answers = (
                            self.candidate_answers(changed_files=count)
                            if candidate
                            else dict(self.answers)
                        )
                        if not candidate:
                            answers[f"{API}/pulls/25879"] = COUNT_PULL | {"changed_files": count}
                        rc, out, err = run_cli(self.root, FakeForge(answers), *args)
                        self.assertEqual((rc, err), (int(candidate and args[0] == "check"), ""))
                        if candidate:
                            self.assertIn("25879", out)
                        else:
                            self.assertEqual(
                                len(answers[f"{API}/pulls/25879/files?limit=50&page=1"]), 15
                            )

    def test_wrong_count_types_are_not_hidden_by_inline_names(self) -> None:
        """Reject malformed counts even when a valid inline files list is present."""
        for candidate in (False, True):
            for value in ("15", True, -1, {}, [], None, 1.5):
                with self.subTest(candidate=candidate, value=value):
                    raw = COUNT_PULL | {"changed_files": value, "files": ["file-0.c"]}
                    answers = self.candidate_answers() if candidate else dict(self.answers)
                    if candidate:
                        answers[self.page] = [raw]
                    else:
                        answers[f"{API}/pulls/25879"] = raw
                    rc, out, err = run_cli(self.root, FakeForge(answers), "check")
                    self.assertEqual((rc, out), (2, ""))
                    self.assertIn(f"{API}/pulls/25879", err)
                    self.assertIn("changed_files", err)
                    self.assertIn(type(value).__name__, err)
                    self.assertNotIn("at most", err)

    def test_wrong_files_types_name_key_type_and_pull(self) -> None:
        """Reject malformed inline files with type diagnostics instead of size errors."""
        for candidate in (False, True):
            for value in ("file.c", True, -1, {}, None, (), set(), 15):
                with self.subTest(candidate=candidate, value=value):
                    raw = COUNT_PULL | {"files": value}
                    answers = self.candidate_answers() if candidate else dict(self.answers)
                    if candidate:
                        answers[self.page] = [raw]
                    else:
                        answers[f"{API}/pulls/25879"] = raw
                    rc, out, err = run_cli(self.root, FakeForge(answers), "check")
                    self.assertEqual((rc, out), (2, ""))
                    self.assertIn(f"{API}/pulls/25879", err)
                    self.assertIn("files", err)
                    self.assertIn(type(value).__name__, err)
                    self.assertNotIn("at most", err)

    def test_inline_names_include_empty_without_endpoint_reads(self) -> None:
        """Use present inline lists including empty lists without an endpoint fallback."""
        for names in ([], ["file-0.c"]):
            answers = dict(self.answers)
            answers[f"{API}/pulls/25879"] = COUNT_PULL | {
                "files": names,
                "changed_files": len(names),
            }
            del answers[f"{API}/pulls/25879/files?limit=50&page=1"]
            forge = FakeForge(answers)
            self.assertEqual(run_cli(self.root, forge, "check"), (0, "", ""))
            self.assertFalse(any("/files?" in url for url in forge.read[:4]))

    def test_inline_complete_count_disagreement_finishes(self) -> None:
        """Ignore a differing inline count while retaining the validated names."""
        self.answers[f"{API}/pulls/25879"] = COUNT_PULL | {"files": ["file-0.c"]}
        forge = FakeForge(self.answers)
        self.assertEqual(run_cli(self.root, forge, "check"), (0, "", ""))
        self.assertFalse(any("/files?" in url for url in forge.read[:4]))

    def test_rename_aliases_do_not_inflate_changed_file_count(self) -> None:
        """Compare changed rows while keeping both rename names for overlap matches."""
        answers = self.candidate_answers(changed_files=1)
        answers[f"{API}/pulls/25879/files?limit=50&page=1"] = [
            {"filename": "new.c", "status": "renamed", "previous_filename": "file-0.c"}
        ]
        rc, out, err = run_cli(self.root, FakeForge(answers), "check")
        self.assertEqual((rc, err), (1, ""))
        self.assertIn("10 25879 owner same-files open", out)

    def test_incomplete_candidate_does_not_reconcile_count(self) -> None:
        """Preserve bounded candidate evidence when its count exceeds the read prefix."""
        for inline in (False, True):
            answers = self.candidate_answers(changed_files=15)
            if inline:
                answers[self.page][0]["files"] = ["other.c"] * 3
            else:
                answers[f"{API}/pulls/25879/files?limit=50&page=1"] = self.rows[:2]
            for number in (10, 25879):
                answers[f"{API}/pulls/{number}/files?limit=2&page=1"] = answers.pop(
                    f"{API}/pulls/{number}/files?limit=50&page=1"
                )
            answers[self.page.replace("limit=50", "limit=2")] = answers.pop(self.page)
            with (
                unittest.mock.patch.object(upstream_links, "PAGE_SIZE", 2),
                unittest.mock.patch.object(upstream_links, "MAX_FILE_ROWS", 2),
                unittest.mock.patch.object(upstream_links, "MAX_FILE_PAGES", 1),
            ):
                rc, out, err = run_cli(self.root, FakeForge(answers), "check")
            self.assertEqual((rc, err), (1, ""))
            self.assertIn("undecided" if inline else "same-files open", out)

    def test_zero_count_complete_empty_endpoint_succeeds(self) -> None:
        """Accept zero changed files when the complete endpoint is empty."""
        self.answers[f"{API}/pulls/25879"]["changed_files"] = 0
        self.answers[f"{API}/pulls/25879/files?limit=50&page=1"] = []
        self.assertEqual(run_cli(self.root, FakeForge(self.answers), "check"), (0, "", ""))

    def test_internal_names_remain_separate_from_forge_counts(self) -> None:
        """Keep internal files fixtures usable without interpreting changed_files as names."""
        raw = {"number": 10, "author": "owner", "title": "component: change"}
        self.assertEqual(upstream_links.normalize_item(raw | {"files": ["a.c"]})["files"], ["a.c"])
        self.assertEqual(upstream_links.normalize_item(raw | {"changed_files": 15})["files"], [])


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
            with unittest.mock.patch.object(upstream_links, "request_cap", return_value=budget):
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


class FailClosedForgeTest(unittest.TestCase):
    """Plant hostile forge fields through complete offline fill and check paths."""

    def setUp(self) -> None:
        """Create one own pull and one overlapping candidate with complete metadata."""
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.path = write_manifest(self.root, [manifest_entry(10)])
        self.before = self.path.read_bytes()
        self.own = forge_pull(10) | {"head": {"sha": "a" * 40}, "base": {"ref": "master"}}
        self.candidate = forge_pull(101) | {"head": {"sha": "b" * 40}, "base": {"ref": "master"}}
        self.page = f"{API}/pulls?state=all&sort=recentupdate&limit=50&page=1"
        self.answers = {
            f"{API}/pulls/10": self.own,
            self.page: [self.candidate],
            f"{API}/pulls/10/files?limit=50&page=1": [{"filename": "shared.c"}],
            f"{API}/pulls/101/files?limit=50&page=1": [{"filename": "shared.c"}],
        }

    def assert_rejected(self, field: str, number: int | None = 101) -> None:
        """Require an identified error and byte-identical manifest for both commands."""
        for command in ("fill", "check"):
            with self.subTest(command=command):
                self.path.write_bytes(self.before)
                rc, _, err = run_cli(self.root, FakeForge(self.answers), command)
                self.assertEqual(rc, 2, err)
                self.assertIn(str(number) if number is not None else API, err)
                self.assertIn(field, err)
                self.assertEqual(self.path.read_bytes(), self.before)

    def test_missing_update_is_error_before_age_stop(self) -> None:
        """A missing update cannot turn an old candidate into a successful empty scan."""
        self.candidate["created_at"] = "2020-01-01T00:00:00Z"
        del self.candidate["updated_at"]
        self.assert_rejected("updated_at")

    def test_null_update_is_error_before_age_stop(self) -> None:
        """A null update cannot use the old creation date as a fallback."""
        self.candidate.update(created_at="2020-01-01T00:00:00Z", updated_at=None)
        self.assert_rejected("updated_at")

    def test_malformed_update_is_error(self) -> None:
        """Wrong types and invalid update instants identify the candidate and field."""
        for value in ("broken", "", 123, [], "2026-11-11", "2026-11-11T00:00:00"):
            with self.subTest(value=value):
                self.candidate["updated_at"] = value
                self.assert_rejected("updated_at")

    def test_update_at_cutoff_is_retained(self) -> None:
        """The exact own-opening boundary remains eligible for a proven overlap."""
        self.candidate["updated_at"] = self.own["created_at"]
        rc, out, err = run_cli(self.root, FakeForge(self.answers), "check", "--since", "2026-11-01")
        self.assertEqual((rc, err), (1, ""))
        self.assertIn("101 bob same-files", out)

    def test_required_forge_fields_fail_closed(self) -> None:
        """Absent and malformed metadata cannot become normalized harmless defaults."""
        for field in ("created_at", "merged_at", "state", "number", "head", "base", "body"):
            original = self.candidate[field]
            for missing in (True, False):
                with self.subTest(field=field, missing=missing):
                    if missing:
                        del self.candidate[field]
                    else:
                        self.candidate[field] = []
                    self.assert_rejected(field, None if field == "number" else 101)
                    self.candidate[field] = original

    def test_nested_head_and_base_are_validated(self) -> None:
        """Missing, blank and non-string commit and branch fields fail closed."""
        for field, key in (("head", "sha"), ("base", "ref")):
            original = self.candidate[field]
            for value in ({}, {key: None}, {key: ""}, {key: "z"}):
                if field == "base" and value == {key: "z"}:
                    continue
                with self.subTest(field=field, value=value):
                    self.candidate[field] = value
                    self.assert_rejected(field)
            self.candidate[field] = original

    def test_forge_only_normalized_extras_are_ignored(self) -> None:
        """Forge author, kind and seen cannot override trusted normalization."""
        self.candidate.update(author="alice", kind="issue", seen="not-a-date")
        rc, _, err = run_cli(self.root, FakeForge(self.answers), "fill")
        self.assertEqual((rc, err), (0, ""))
        rows = json.loads(self.path.read_bytes())["contributions"][0]["related"]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(
            (row["author"], row["kind"], row["seen"]), ("bob", "pull", TODAY.isoformat())
        )

    def test_live_patch_ids_are_not_claimed(self) -> None:
        """Neither supplied patch-ID field can create a live same-patch-id record."""
        self.answers[f"{API}/pulls/101/files?limit=50&page=1"] = [{"filename": "other.c"}]
        for field in ("patch_ids", "stable_patch_ids"):
            with self.subTest(field=field):
                self.own[field] = self.candidate[field] = ["forged-id"]
                rc, out, err = run_cli(
                    self.root, FakeForge(self.answers), "check", keep_progress=True
                )
                self.assertEqual((rc, err), (0, ""))
                self.assertIn("same-patch-id not checked live", out)
                self.assertEqual(self.path.read_bytes(), self.before)
                del self.own[field], self.candidate[field]

    def test_issue_clause_cap_is_not_silent(self) -> None:
        """Clause 65 aborts without recording a partial issue list."""
        self.own["body"] = "\n".join(f"Fixes #{n}" for n in range(1, 66))
        self.assert_rejected("65", 10)

    def assert_issue_items(self, body: str, expected: list[int], ignored: int = 0) -> None:
        """Fill a real fixture manifest and verify complete local items and one count line."""
        self.path.write_bytes(self.before)
        self.own["body"] = body
        rc, out, err = run_cli(self.root, FakeForge(self.answers), "fill")
        self.assertEqual((rc, err), (0, ""))
        entry = json.loads(self.path.read_bytes())["contributions"][0]
        self.assertEqual(entry.get("fixes_issues", []), expected)
        self.assertEqual(out.count("foreign issue addresses"), int(bool(ignored)))
        if ignored:
            self.assertIn(f"10: ignored {ignored} foreign issue addresses", out.splitlines())

    def test_prose_input_output_counts_are_not_issues(self) -> None:
        """A prose conjunction cannot turn output counts into issue references."""
        self.assert_issue_items("Fixes a crash with 10 inputs and 20 outputs", [])

    def test_comma_separated_complete_hash_items_are_recorded(self) -> None:
        """A closing clause records both complete comma-separated hash items."""
        self.assert_issue_items("Fixes #10, #20", [10, 20])

    def test_comma_and_conjunction_keep_complete_hash_items(self) -> None:
        """A comma followed by a conjunction separates complete issue items."""
        self.assert_issue_items("Fixes #10, #20, and #30 or #40", [10, 20, 30, 40])

    def test_closing_keyword_does_not_cross_a_line_boundary(self) -> None:
        """A closing keyword cannot borrow a hash from a different line."""
        for body in ("Fixes\n#10", "Refs:\r\n#20", "Closes \n #30"):
            with self.subTest(body=body):
                self.assert_issue_items(body, [])

    def test_prose_after_hash_ends_issue_list(self) -> None:
        """An unhashed output count after a valid hash ends the issue list."""
        self.assert_issue_items("Fixes #10 and 20 outputs, #30", [10])

    def test_foreign_qualified_hash_is_counted_not_recorded(self) -> None:
        """A hash glued to another repository cannot be reinterpreted as a local hash."""
        self.assert_issue_items("Fixes Other/Repo#123", [], 1)

    def test_exact_repository_url_and_foreign_host_in_fill(self) -> None:
        """A matching repository path is local only on the configured host."""
        self.assert_issue_items(
            "Fixes https://code.ffmpeg.org/FFmpeg/FFmpeg/issues/10, "
            "https://other.invalid/FFmpeg/FFmpeg/issues/20",
            [10],
            1,
        )

    def test_version_after_keyword_is_not_an_issue(self) -> None:
        """A dotted version after a keyword cannot contribute its major number."""
        self.assert_issue_items("Fixes 1.30", [])

    def test_word_embedded_hash_and_number_are_not_issues(self) -> None:
        """A number inside a word or a glued hash ends the item list."""
        for body in ("Refs build20", "Refs word#20", "Refs #20suffix"):
            with self.subTest(body=body):
                self.assert_issue_items(body, [])

    def test_configured_repository_shorthand_is_counted_not_recorded(self) -> None:
        """Even local repository shorthand requires a full URL or standalone hash."""
        self.assert_issue_items("Refs FFmpeg/FFmpeg#20", [], 1)

    def test_keyword_horizontal_spacing_table(self) -> None:
        """Tabs and several spaces after each closing keyword preserve complete items."""
        cases = (
            "Fixes\t#10, #20",
            "Closes\t#10, #20",
            "Refs\t#10, #20",
            "Fixes    #10, #20",
            "Closes    #10, #20",
            "Refs    #10, #20",
        )
        for body in cases[:6]:
            with self.subTest(body=body):
                self.assert_issue_items(body, [10, 20])

    def test_keyword_inside_longer_word_table(self) -> None:
        """Embedded keywords never start a closing clause."""
        cases = ("prefixFixes #10", "FixesSuffix #10", "_Closes #10", "Refs2 #10")
        for body in cases[:4]:
            with self.subTest(body=body):
                self.assert_issue_items(body, [])

    def test_keyword_and_conjunction_case_table(self) -> None:
        """Upper and lower case keywords and conjunctions keep both complete items."""
        cases = (
            "FIXES #10 AND #20",
            "fixes #10 and #20",
            "CLOSES #10 OR #20",
            "closes #10 or #20",
            "REFS #10 AND #20",
            "refs #10 or #20",
        )
        for body in cases[:6]:
            with self.subTest(body=body):
                self.assert_issue_items(body, [10, 20])

    def test_hash_bracket_or_colon_suffix_table(self) -> None:
        """A bracket or colon glued to a hash ends the list without later salvage."""
        cases = (
            ("Fixes #10], #20", []),
            ("Fixes #10:, #20", []),
            ("Fixes #10, #20], #30", [10]),
            ("Fixes #10, #20:, #30", [10]),
        )
        for body, expected in cases[:4]:
            with self.subTest(body=body):
                self.assert_issue_items(body, expected)

    def test_uppercase_issue_scheme_or_host_table(self) -> None:
        """Uppercase schemes stop the list while uppercase hosts are ignored and counted."""
        cases = (
            ("HTTPS://code.ffmpeg.org", [10], 0),
            ("HTTP://code.ffmpeg.org", [10], 0),
            ("https://CODE.FFMPEG.ORG", [10, 30], 1),
            ("https://Code.ffmpeg.org", [10, 30], 1),
        )
        for origin, expected, ignored in cases[:4]:
            with self.subTest(origin=origin):
                self.assert_issue_items(
                    f"Fixes #10, {origin}/FFmpeg/FFmpeg/issues/20, #30", expected, ignored
                )

    def test_issue_address_user_or_port_table(self) -> None:
        """User parts and ports fail literal origin matching without losing later valid items."""
        origins = (
            "https://user@code.ffmpeg.org",
            "https://code.ffmpeg.org:443",
            "https://code.ffmpeg.org:8443",
        )
        for origin in origins[:3]:
            with self.subTest(origin=origin):
                self.assert_issue_items(
                    f"Fixes #10, {origin}/FFmpeg/FFmpeg/issues/20, #30", [10, 30], 1
                )

    def test_pull_address_between_valid_items_table(self) -> None:
        """Pull addresses are neither recorded nor counted and later hashes remain readable."""
        for scheme in ("https", "http"):
            with self.subTest(scheme=scheme):
                self.assert_issue_items(
                    f"Fixes #10, {scheme}://code.ffmpeg.org/FFmpeg/FFmpeg/pulls/20, #30",
                    [10, 30],
                )

    def test_keyboard_interrupt_during_write_cleans_staging(self) -> None:
        """KeyboardInterrupt inside a partial write preserves old bytes and cleans siblings."""
        factory = tempfile.NamedTemporaryFile

        def interrupt(*args: object, **kwargs: object) -> object:
            """Interrupt after staging partial bytes rather than before opening the file."""
            stream = factory(*args, **kwargs)

            def write(data: bytes) -> None:
                """Write a prefix before raising the actual interrupt exception."""
                stream.file.write(data[:7])
                raise KeyboardInterrupt("planted keyboard interrupt")

            stream.write = write
            return stream

        with (
            unittest.mock.patch.object(tempfile, "NamedTemporaryFile", side_effect=interrupt),
            self.assertRaises(KeyboardInterrupt),
        ):
            run_cli(self.root, FakeForge(self.answers), "fill")
        self.assertEqual(self.path.read_bytes(), self.before)
        self.assertEqual(list(self.root.iterdir()), [self.path])

    def assert_published_with_leftover(self, suffix: str, *, both: bool = False) -> Path:
        """Require published bytes, every cleanup leftover and the distinct failure status."""
        unlink = Path.unlink
        replace = upstream_links.os.replace
        failed: list[Path] = []

        def publish(source: Path, target: Path) -> None:
            """Replace for real and plant an actual staging leftover for its cleanup test."""
            replace(source, target)
            if suffix == ".tmp" or both:
                source.write_bytes(b"planted leftover staging")

        def fail_cleanup(path: Path, *args: object, **kwargs: object) -> None:
            """Raise only after new manifest bytes are already visible at the destination."""
            suffixes = (".tmp", ".lock") if both else (suffix,)
            if path.name.endswith(suffixes) and self.path.read_bytes() != self.before:
                failed.append(path)
                raise OSError("planted cleanup failure")
            unlink(path, *args, **kwargs)

        with (
            unittest.mock.patch.object(Path, "unlink", autospec=True, side_effect=fail_cleanup),
            unittest.mock.patch.object(upstream_links.os, "replace", side_effect=publish),
        ):
            rc, out, err = run_cli(self.root, FakeForge(self.answers), "fill")
        self.assertEqual((rc, err), (3, ""))
        self.assertEqual(len(failed), 2 if both else 1)
        for leftover in failed[:2]:
            self.assertTrue(leftover.is_file())
            self.assertIn(
                f"leftover: {leftover}: cleanup failed: planted cleanup failure", out.splitlines()
            )
        self.assertNotEqual(self.path.read_bytes(), self.before)
        entry = json.loads(self.path.read_bytes())["contributions"][0]
        self.assertEqual(entry["fixes_issues"], [99])
        self.assertIn(f"published {self.path}; cleanup incomplete; exit 3", out.splitlines())
        self.assertEqual(out.count("leftover: "), len(failed))
        return failed[0]

    def test_staging_cleanup_failure_reports_published(self) -> None:
        """A staging unlink error after replacement cannot report an unchanged manifest."""
        self.assert_published_with_leftover(".tmp")

    def test_lock_cleanup_failure_reports_published(self) -> None:
        """A lock unlink error after replacement leaves new bytes and reports the lock."""
        lock = self.assert_published_with_leftover(".lock")
        self.assertTrue(lock.is_file())

    def test_combined_cleanup_failure_reports_both_leftovers(self) -> None:
        """Both post-publication cleanup failures retain new bytes and report every path."""
        self.assert_published_with_leftover(".tmp", both=True)

    def test_clean_publication_returns_zero_without_leftover_lines(self) -> None:
        """Clean publication returns zero, writes the new issues and removes both siblings."""
        rc, out, err = run_cli(self.root, FakeForge(self.answers), "fill")
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(
            json.loads(self.path.read_bytes())["contributions"][0]["fixes_issues"], [99]
        )
        self.assertNotIn("leftover:", out)
        self.assertNotIn("cleanup incomplete", out)
        self.assertEqual(list(self.root.iterdir()), [self.path])

    def test_published_state_survives_bounded_change_output(self) -> None:
        """Two ordinary lines and one leftover leave state visible with an ordinary cap of three."""
        self.own["created_at"] = "2020-01-01T00:00:00Z"
        with (
            unittest.mock.patch.object(upstream_links.upstream_drift, "MAX_SERIES_ENTRIES", 1),
            unittest.mock.patch.object(upstream_links, "MAX_CANDIDATES", 1),
        ):
            self.assert_published_with_leftover(".lock")

    def test_later_fill_names_leftover_lock_and_stale_rule(self) -> None:
        """A subsequent writing fill names its leftover lock and manual stale criterion."""
        lock = self.assert_published_with_leftover(".lock")
        self.own["body"] = "Fixes #100"
        published = self.path.read_bytes()
        rc, _, err = run_cli(self.root, FakeForge(self.answers), "fill")
        self.assertEqual(rc, 2, err)
        self.assertIn(str(lock), err)
        self.assertIn("stale", err)
        self.assertIn("confirm no fill is running", err)
        self.assertIn("remove", err)
        self.assertEqual(self.path.read_bytes(), published)

    def test_atomic_replacement_preserves_manifest_file_mode(self) -> None:
        """Atomic publication keeps the existing manifest permission bits."""
        for mode in (0o640, 0o600):
            with self.subTest(mode=oct(mode)):
                self.path.write_bytes(self.before)
                self.path.chmod(mode)
                rc, _, err = run_cli(self.root, FakeForge(self.answers), "fill")
                self.assertEqual((rc, err), (0, ""))
                self.assertEqual(self.path.stat().st_mode & 0o7777, mode)

    def test_atomic_replacement_drops_special_mode_bits(self) -> None:
        """Publication copies ordinary permissions without set-id or sticky bits."""
        for special in (0o4000, 0o2000, 0o1000, 0o7000):
            with self.subTest(special=oct(special)):
                self.path.write_bytes(self.before)
                self.path.chmod(special | 0o750)
                self.assertEqual(self.path.stat().st_mode & 0o7777, special | 0o750)
                rc, _, err = run_cli(self.root, FakeForge(self.answers), "fill")
                self.assertEqual((rc, err), (0, ""))
                self.assertEqual(self.path.stat().st_mode & 0o7777, 0o750)

    def test_failed_mode_change_aborts_before_publication(self) -> None:
        """A chmod failure returns the existing error status and never publishes bytes."""
        with (
            unittest.mock.patch.object(Path, "chmod", side_effect=OSError("planted chmod failure")),
            unittest.mock.patch.object(upstream_links.os, "replace") as replace,
        ):
            rc, _, err = run_cli(self.root, FakeForge(self.answers), "fill")
        self.assertEqual(rc, 2, err)
        self.assertIn("planted chmod failure", err)
        replace.assert_not_called()
        self.assertEqual(self.path.read_bytes(), self.before)
        self.assertEqual(list(self.root.iterdir()), [self.path])

    def test_staging_stays_private_until_written_and_synced(self) -> None:
        """Staging stays private through writing and sync, then chmod precedes replacement."""
        factory, fsync = tempfile.NamedTemporaryFile, upstream_links.os.fsync
        chmod, replace = Path.chmod, upstream_links.os.replace
        events: list[str] = []
        self.path.chmod(0o640)

        def prepare(*args: object, **kwargs: object) -> object:
            """Observe the actual staging permissions before the first write."""
            stream = factory(*args, **kwargs)
            write = stream.write

            def observe(data: bytes) -> int:
                """Require private staging while content is still incomplete."""
                self.assertEqual(Path(stream.name).stat().st_mode & 0o7777, 0o600)
                events.append("write")
                return write(data)

            stream.write = observe
            return stream

        def sync(fd: int) -> None:
            """Complete real sync before any permission change is allowed."""
            self.assertEqual(events, ["write"])
            fsync(fd)
            events.append("sync")

        def set_mode(path: Path, mode: int) -> None:
            """Require complete synced content before widening staging permissions."""
            self.assertEqual(events, ["write", "sync"])
            self.assertEqual(
                json.loads(path.read_bytes())["contributions"][0]["fixes_issues"], [99]
            )
            chmod(path, mode)
            events.append("mode")

        def publish(source: Path, target: Path) -> None:
            """Require the final ordinary mode before replacing the manifest."""
            self.assertEqual(events, ["write", "sync", "mode"])
            self.assertEqual(source.stat().st_mode & 0o7777, 0o640)
            replace(source, target)
            events.append("replace")

        with (
            unittest.mock.patch.object(tempfile, "NamedTemporaryFile", side_effect=prepare),
            unittest.mock.patch.object(upstream_links.os, "fsync", side_effect=sync),
            unittest.mock.patch.object(Path, "chmod", autospec=True, side_effect=set_mode),
            unittest.mock.patch.object(upstream_links.os, "replace", side_effect=publish),
        ):
            rc, _, err = run_cli(self.root, FakeForge(self.answers), "fill")
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(events, ["write", "sync", "mode", "replace"])

    def test_issue_clause_cap_boundary_is_accepted(self) -> None:
        """All 64 allowed clauses reach the stored issue list."""
        self.own["body"] = "\n".join(f"Fixes #{n}" for n in range(1, 65))
        rc, _, err = run_cli(self.root, FakeForge(self.answers), "fill")
        self.assertEqual((rc, err), (0, ""))
        entry = json.loads(self.path.read_bytes())["contributions"][0]
        self.assertEqual(entry["fixes_issues"], list(range(1, 65)))

    def test_foreign_issue_namespace_is_counted_not_recorded(self) -> None:
        """Foreign issue URLs are counted once while bare prose numbers end the list."""
        self.own["body"] = (
            "Fixes https://foreign.invalid/FFmpeg/FFmpeg/issues/123, "
            "https://code.ffmpeg.org/Other/FFmpeg/issues/124, "
            "https://code.ffmpeg.org/FFmpeg/FFmpeg/issues/125/extra, "
            "https://code.ffmpeg.org/FFmpeg/FFmpeg/issues/126?x=1, "
            "https://code.ffmpeg.org/FFmpeg/FFmpeg/issues/127, #128, 129, version 130"
        )
        rc, out, err = run_cli(self.root, FakeForge(self.answers), "fill")
        self.assertEqual((rc, err), (0, ""))
        entry = json.loads(self.path.read_bytes())["contributions"][0]
        self.assertEqual(entry["fixes_issues"], [127, 128])
        self.assertIn("10: ignored 4 foreign issue addresses", out)
        self.assertEqual(out.count("foreign issue addresses"), 1)

    def test_text_field_limits_reject_overflow(self) -> None:
        """Titles, account names, branch names, bodies and inline paths have hard ceilings."""
        for field, limit in (("title", 4096), ("body", 262144), ("files", 4096)):
            original = self.candidate.get(field)
            value = "x" * (limit + 1)
            self.candidate[field] = [value] if field == "files" else value
            with self.subTest(field=field):
                self.assert_rejected(field)
            if original is None:
                del self.candidate[field]
            else:
                self.candidate[field] = original
        self.candidate["user"] = {"login": "x" * 257}
        self.assert_rejected("login")
        self.candidate["user"] = {"login": "bob"}
        self.candidate["base"] = {"ref": "x" * 257}
        self.assert_rejected("base")

    def test_text_field_limit_boundaries_are_accepted(self) -> None:
        """Maximum valid title, body, login, branch and path lengths remain readable."""
        self.candidate.update(title="x" * 4096, body="x" * 262144, files=["x" * 4096])
        self.candidate["user"] = {"login": "x" * 256}
        self.candidate["base"] = {"ref": "x" * 256}
        rc, _, err = run_cli(self.root, FakeForge(self.answers), "fill")
        self.assertEqual((rc, err), (0, ""))

    def test_file_row_path_limits_include_rename_alias(self) -> None:
        """Neither current nor previous endpoint filename can exceed the path ceiling."""
        for field in ("filename", "previous_filename"):
            self.answers[f"{API}/pulls/101/files?limit=50&page=1"] = [
                {"filename": "shared.c", field: "x" * 4097}
            ]
            with self.subTest(field=field):
                self.assert_rejected(field)

    def test_failed_manifest_sync_keeps_original_and_cleans_temp(self) -> None:
        """An fsync OSError cannot truncate the destination or leave a staging file."""
        with unittest.mock.patch("os.fsync", side_effect=OSError("planted interrupted sync")):
            rc, _, err = run_cli(self.root, FakeForge(self.answers), "fill")
        self.assertEqual(rc, 2, err)
        self.assertEqual(self.path.read_bytes(), self.before)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), [self.path.name])

    def test_failed_replace_keeps_original_and_cleans_temp(self) -> None:
        """A failed atomic replacement preserves the old bytes and removes the sibling."""
        with unittest.mock.patch("os.replace", side_effect=OSError("planted replacement failure")):
            rc, _, err = run_cli(self.root, FakeForge(self.answers), "fill")
        self.assertEqual(rc, 2, err)
        self.assertEqual(self.path.read_bytes(), self.before)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), [self.path.name])

    def test_manifest_changed_during_preparation_is_not_overwritten(self) -> None:
        """A concurrent edit after temporary preparation aborts the manifest replacement."""
        changed = self.before + b" "
        with unittest.mock.patch("os.fsync", side_effect=lambda _: self.path.write_bytes(changed)):
            rc, _, err = run_cli(self.root, FakeForge(self.answers), "fill")
        self.assertEqual(rc, 2, err)
        self.assertIn("changed since read", err)
        self.assertEqual(self.path.read_bytes(), changed)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), [self.path.name])

    def test_manifest_changed_during_scan_is_not_overwritten(self) -> None:
        """A changed destination before temporary preparation is also refused."""
        forge = FakeForge(self.answers)
        changed = self.before + b" "

        def fetch(url: str) -> dict | list:
            """Plant a concurrent manifest edit while the list endpoint is read."""
            if url == self.page:
                self.path.write_bytes(changed)
            return forge(url)

        rc, _, err = run_cli(self.root, fetch, "fill")
        self.assertEqual(rc, 2, err)
        self.assertIn("changed since read", err)
        self.assertEqual(self.path.read_bytes(), changed)

    def test_stored_seen_is_validated_on_read(self) -> None:
        """An invalid retained discovery date aborts before any forge read."""
        self.path = write_manifest(
            self.root,
            [
                manifest_entry(
                    10,
                    related=[
                        {
                            "number": 101,
                            "kind": "pull",
                            "author": "bob",
                            "reason": "same-files",
                            "state": "open",
                            "seen": "2026-02-30",
                        }
                    ],
                )
            ],
        )
        forge = FakeForge({})
        rc, _, err = run_cli(self.root, forge, "fill")
        self.assertEqual(rc, 2)
        self.assertIn("seen", err)
        self.assertEqual(forge.read, [])

    def test_partial_stream_write_keeps_original_and_cleans_temp(self) -> None:
        """A real partial sibling write followed by an exception leaves the target intact."""
        factory = tempfile.NamedTemporaryFile

        def interrupted(*args: object, **kwargs: object) -> object:
            """Write a real temporary prefix before raising the planted interruption."""
            stream = factory(*args, **kwargs)

            def write(data: bytes) -> None:
                """Leave partial temporary bytes so cleanup must remove a real file."""
                stream.file.write(data[:7])
                raise OSError("planted partial write")

            stream.write = write
            return stream

        with unittest.mock.patch.object(tempfile, "NamedTemporaryFile", side_effect=interrupted):
            rc, _, err = run_cli(self.root, FakeForge(self.answers), "fill")
        self.assertEqual(rc, 2, err)
        self.assertIn("planted partial write", err)
        self.assertEqual(self.path.read_bytes(), self.before)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), [self.path.name])

    def test_successful_atomic_write_is_synced_before_replace(self) -> None:
        """Publish complete bytes only after a sibling file was flushed and synced."""
        events: list[str] = []
        replace = upstream_links.os.replace if hasattr(upstream_links, "os") else None

        def sync(descriptor: int) -> None:
            """Record the sync before replacement while the destination still holds old bytes."""
            self.assertIsInstance(descriptor, int)
            self.assertEqual(self.path.read_bytes(), self.before)
            staging = list(self.root.glob(f".{self.path.name}.*.tmp"))
            self.assertEqual(len(staging), 1)
            self.assertEqual(json.loads(staging[0].read_bytes())["checked"], "2026-11-12")
            events.append("sync")

        def publish(source: Path, target: Path) -> None:
            """Inspect complete sibling content before performing the real replacement."""
            self.assertEqual(source.parent, target.parent)
            self.assertEqual(target, self.path)
            self.assertEqual(events, ["sync"])
            self.assertEqual(json.loads(source.read_bytes())["checked"], "2026-11-12")
            events.append("replace")
            replace(source, target)

        with (
            unittest.mock.patch("os.fsync", side_effect=sync),
            unittest.mock.patch("os.replace", side_effect=publish),
        ):
            rc, _, err = run_cli(self.root, FakeForge(self.answers), "fill")
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(events, ["sync", "replace"])
        self.assertNotEqual(self.path.read_bytes(), self.before)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), [self.path.name])

    def test_competing_fill_lock_is_not_removed_or_overwritten(self) -> None:
        """An existing sibling lock blocks publication without taking another fill's lock."""
        lock = self.path.with_name(f".{self.path.name}.lock")
        lock.write_bytes(b"another writer")
        rc, _, err = run_cli(self.root, FakeForge(self.answers), "fill")
        self.assertEqual(rc, 2, err)
        self.assertIn("another fill", err)
        self.assertEqual(lock.read_bytes(), b"another writer")
        self.assertEqual(self.path.read_bytes(), self.before)

    def test_clock_is_read_once_and_forge_seen_is_ignored(self) -> None:
        """Every fresh discovery date comes from one local UTC clock read."""
        self.candidate["seen"] = "2000-01-01"
        real_clock = upstream_links.datetime
        instant = real_clock(2026, 11, 12, tzinfo=upstream_links.UTC)
        with unittest.mock.patch.object(upstream_links, "datetime", wraps=real_clock) as clock:
            clock.now.return_value = instant
            rc, _, err = run_cli(self.root, FakeForge(self.answers), "fill", today=None)
        self.assertEqual((rc, err), (0, ""))
        clock.now.assert_called_once_with(upstream_links.UTC)
        row = json.loads(self.path.read_bytes())["contributions"][0]["related"][0]
        self.assertEqual(row["seen"], "2026-11-12")

    def test_inline_file_row_ceiling_rejects_overflow(self) -> None:
        """An oversized inline envelope fails rather than allocate an unlimited prefix copy."""
        self.candidate["files"] = ["a.c"] * 2001
        self.assert_rejected("files")

    def test_inline_file_row_ceiling_accepts_boundary_with_notice(self) -> None:
        """A 2000-row inline listing retains and discloses only its 1000-name prefix."""
        self.candidate["files"] = ["shared.c"] * 2000
        rc, out, err = run_cli(self.root, FakeForge(self.answers), "check")
        self.assertEqual((rc, err), (1, ""))
        self.assertIn("files incomplete: 1000 names read; same-files proven from prefix", out)

    def test_malformed_inline_tail_is_not_hidden_by_prefix(self) -> None:
        """A malformed inline name after the retained prefix still aborts the run."""
        for tail in (None, 9, "x" * 4097):
            with self.subTest(tail_type=type(tail).__name__):
                self.candidate["files"] = ["shared.c"] * 1000 + [tail]
                self.assert_rejected("files")

    def test_endpoint_path_ceiling_accepts_boundary(self) -> None:
        """Current and previous filenames at the exact character ceiling remain readable."""
        self.answers[f"{API}/pulls/101/files?limit=50&page=1"] = [
            {"filename": "x" * 4096, "previous_filename": "y" * 4096}
        ]
        rc, _, err = run_cli(self.root, FakeForge(self.answers), "fill")
        self.assertEqual((rc, err), (0, ""))

    def test_clause_limit_also_counts_same_line_clauses(self) -> None:
        """Repeated keywords on one line cannot evade the 64-clause ceiling."""
        self.own["body"] = " ".join(f"Fixes #{n}" for n in range(1, 66))
        self.assert_rejected("65", 10)

    def test_exact_issue_namespace_uses_configured_host_and_path(self) -> None:
        """Only the exact configured issue origin and repository count as URL references."""
        body = (
            "Refs https://custom.invalid/Owner/Repo/issues/1, "
            "https://custom.invalid/owner/Repo/issues/2, "
            "https://code.ffmpeg.org/FFmpeg/FFmpeg/issues/3, #4"
        )
        out = io.StringIO()
        with (
            unittest.mock.patch.object(
                upstream_links.upstream_drift,
                "repo_api_url",
                return_value="https://custom.invalid/api/v1/repos/Owner/Repo",
            ),
            contextlib.redirect_stdout(out),
        ):
            self.assertEqual(upstream_links.body_issues(body), [1, 4])
        self.assertIn("ignored 2 foreign issue addresses\n", out.getvalue())

    def test_invalid_forge_seen_is_not_stored(self) -> None:
        """A forge-supplied invalid discovery date never enters an otherwise valid relation."""
        self.candidate["seen"] = "not-a-date"
        rc, _, err = run_cli(self.root, FakeForge(self.answers), "fill")
        self.assertEqual((rc, err), (0, ""))
        row = json.loads(self.path.read_bytes())["contributions"][0]["related"][0]
        self.assertEqual(row["seen"], TODAY.isoformat())

    def test_stored_text_overflow_is_rejected_before_fetch(self) -> None:
        """Stored titles, branch names and related account names obey the same ceilings."""
        for field, limit in (("title", 4096), ("base", 256), ("author", 256)):
            entry = manifest_entry(10)
            if field == "author":
                entry["related"] = [
                    {
                        "number": 101,
                        "kind": "pull",
                        "author": "x" * (limit + 1),
                        "reason": "same-files",
                        "state": "open",
                        "seen": TODAY.isoformat(),
                    }
                ]
            else:
                entry[field] = "x" * (limit + 1)
            write_manifest(self.root, [entry])
            forge = FakeForge(self.answers)
            with self.subTest(field=field):
                rc, _, err = run_cli(self.root, forge, "fill")
                self.assertEqual(rc, 2, err)
                self.assertIn(field, err)
                self.assertEqual(forge.read, [])

    def test_stored_text_boundaries_remain_valid(self) -> None:
        """Maximum stored title, branch and account lengths pass before a normal fill."""
        entry = manifest_entry(10, title="x" * 4096, base="x" * 256)
        entry["related"] = [
            {
                "number": 101,
                "kind": "pull",
                "author": "x" * 256,
                "reason": "same-files",
                "state": "open",
                "seen": TODAY.isoformat(),
            }
        ]
        write_manifest(self.root, [entry])
        rc, _, err = run_cli(self.root, FakeForge(self.answers), "fill")
        self.assertEqual((rc, err), (0, ""))


class BoundedResponseTest(unittest.TestCase):
    """Exercise the shared byte reader without any network or credential lookup."""

    def test_response_ceiling_rejects_one_extra_byte_before_json(self) -> None:
        """Object and list readers reject cap-plus-one even when valid JSON fits inside."""
        drift = upstream_links.upstream_drift
        for allow_list in (False, True):
            answer = io.BytesIO(b"{}" + b" " * 127)
            with (
                unittest.mock.patch.object(drift, "MAX_RESPONSE_BYTES", 128, create=True),
                unittest.mock.patch.object(drift.urllib.request, "urlopen", return_value=answer),
                self.assertRaisesRegex(drift.DriftError, "response.*128"),
            ):
                drift.fetch_json(f"{API}/pulls/101", allow_list=allow_list)

    def test_response_ceiling_accepts_exact_boundary_and_short_body(self) -> None:
        """Complete JSON bodies at the exact byte ceiling and below remain readable."""
        drift = upstream_links.upstream_drift
        for prefix, expected in ((b"{}", {}), (b"[]", [])):
            for size in (2, 128):
                answer = io.BytesIO(prefix + b" " * (size - 2))
                with (
                    unittest.mock.patch.object(drift, "MAX_RESPONSE_BYTES", 128, create=True),
                    unittest.mock.patch.object(
                        drift.urllib.request, "urlopen", return_value=answer
                    ),
                ):
                    self.assertEqual(
                        drift.fetch_json(f"{API}/pulls/101", allow_list=True), expected
                    )

    def test_repeated_issue_tokens_have_a_scalar_limit(self) -> None:
        """Repeating one issue cannot evade the reference-loop ceiling."""
        body = "Fixes " + ", ".join(["#1"] * 1025)
        with self.assertRaisesRegex(upstream_links.DriftError, "references.*1025"):
            upstream_links.body_issues(body)

    def test_repeated_issue_tokens_accept_limit_boundary(self) -> None:
        """Exactly 1024 references remain accepted and deduplicated."""
        body = "Fixes " + ", ".join(["#1"] * 1024)
        self.assertEqual(upstream_links.body_issues(body), [1])

    def test_response_overflow_is_not_parsed(self) -> None:
        """Reject bytes above the ceiling before invoking the JSON decoder."""
        drift = upstream_links.upstream_drift
        with (
            unittest.mock.patch.object(drift, "MAX_RESPONSE_BYTES", 128, create=True),
            unittest.mock.patch.object(
                drift.urllib.request, "urlopen", return_value=io.BytesIO(b"x" * 129)
            ),
            unittest.mock.patch.object(drift.json, "loads", return_value={}) as decode,
            self.assertRaisesRegex(drift.DriftError, "response.*128"),
        ):
            upstream_links.fetch_forge(f"{API}/pulls/101")
        decode.assert_not_called()

    def test_duplicate_response_keys_are_rejected(self) -> None:
        """Duplicate decision keys fail instead of selecting a last-value default."""
        drift = upstream_links.upstream_drift
        answer = io.BytesIO(b'{"number": 1, "number": 2}')
        with (
            unittest.mock.patch.object(drift.urllib.request, "urlopen", return_value=answer),
            self.assertRaisesRegex(drift.DriftError, "written twice"),
        ):
            drift.fetch_json(f"{API}/pulls/101")

    def test_issue_id_width_is_bounded(self) -> None:
        """Issue ID conversion rejects digits beyond the 20-digit ceiling."""
        with self.assertRaisesRegex(upstream_links.DriftError, "issue number exceeds 20"):
            upstream_links.body_issues("Fixes #" + "9" * 21)
        self.assertEqual(upstream_links.body_issues("Fixes #" + "9" * 20), [int("9" * 20)])

    def test_response_object_key_ceiling_rejects_overflow(self) -> None:
        """Oversized JSON objects abort before any unbounded duplicate-key traversal."""
        drift = upstream_links.upstream_drift
        answer = io.BytesIO(json.dumps({str(n): n for n in range(1025)}).encode())
        with (
            unittest.mock.patch.object(drift.urllib.request, "urlopen", return_value=answer),
            self.assertRaisesRegex(drift.DriftError, "response object exceeds 1024 keys"),
        ):
            drift.fetch_json(f"{API}/pulls/101")

    def test_response_object_key_ceiling_accepts_boundary(self) -> None:
        """A JSON object with exactly 1024 unique keys remains readable."""
        drift = upstream_links.upstream_drift
        expected = {str(n): n for n in range(1024)}
        answer = io.BytesIO(json.dumps(expected).encode())
        with unittest.mock.patch.object(drift.urllib.request, "urlopen", return_value=answer):
            self.assertEqual(drift.fetch_json(f"{API}/pulls/101"), expected)


class BoundedMatcherInputTest(unittest.TestCase):
    """Protect matcher entry points from silently truncating caller collections."""

    def test_merge_related_rejects_oversized_inputs(self) -> None:
        """Existing and fresh relation lists must fail before the first slice."""
        cap = upstream_links.series.MAX_RELATED
        rows = [
            {
                "number": n,
                "kind": "pull",
                "author": "bob",
                "reason": "same-files",
                "state": "open",
                "seen": TODAY.isoformat(),
            }
            for n in range(1, cap + 2)
        ]
        for existing, fresh in ((rows, []), ([], rows)):
            with (
                self.subTest(existing=bool(existing)),
                self.assertRaisesRegex(upstream_links.DriftError, "related entries exceed"),
            ):
                upstream_links.merge_related(existing, fresh)
        self.assertEqual(upstream_links.merge_related(rows[:cap], []), rows[:cap])
        self.assertEqual(upstream_links.merge_related([], rows[:cap]), rows[:cap])
        self.assertEqual(upstream_links.merge_related([], []), [])

    def test_run_target_ceiling_is_checked_before_fetch(self) -> None:
        """A target above the scope cap errors while empty and exact-cap scopes still work."""
        url = f"{API}/pulls/10"
        target = (10, f"{FORGE}10", set())
        forge = FakeForge(
            {
                url: forge_pull(10) | {"files": []},
                f"{API}/pulls?state=all&sort=recentupdate&limit=50&page=1": [],
            }
        )
        with unittest.mock.patch.object(upstream_links.upstream_drift, "MAX_SERIES_ENTRIES", 1):
            with self.assertRaisesRegex(upstream_links.DriftError, "targets exceed 1"):
                upstream_links.read_run_pulls([target, target], forge)
            self.assertEqual(forge.read, [])
            own, candidates, _ = upstream_links.read_run_pulls([target], forge)
            self.assertEqual(len(own), 1)
            self.assertEqual(candidates, [])
            self.assertEqual(upstream_links.read_run_pulls([], forge), ([], [], {}))

    def test_subject_ceiling_rejects_overflow_and_accepts_boundary(self) -> None:
        """Subject normalization cannot store text longer than the title ceiling."""
        with self.assertRaisesRegex(upstream_links.DriftError, "subject exceeds 4096"):
            upstream_links.subject_key("x" * 4097)
        self.assertEqual(upstream_links.subject_key("X" * 4096), "x" * 4096)
        self.assertEqual(upstream_links.subject_key("lib: short"), "short")

    def test_standalone_body_ceiling_is_not_bypassed(self) -> None:
        """The public issue extractor enforces its body bound independently of forge parsing."""
        with self.assertRaisesRegex(upstream_links.DriftError, "body exceeds 262144"):
            upstream_links.body_issues("x" * 262145)
        self.assertEqual(upstream_links.body_issues("x" * 262144), [])
        self.assertEqual(upstream_links.body_issues("Fixes #1"), [1])


if __name__ == "__main__":
    unittest.main()
