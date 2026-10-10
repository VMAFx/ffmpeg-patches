# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Tests for scripts/series.py against a small throwaway upstream repository."""

from __future__ import annotations

import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import series  # noqa: E402

LINES = [f"int line_{n} = {n};" for n in range(1, 41)]
METADATA = 'status = "submitted"\nupstream = "https://example.invalid/pulls/1"\nconsumers = ["a"]\n'


def run_git(tree: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(tree), "-c", "user.name=Dev", "-c", "user.email=dev@invalid", *args],
        env=series.git_env(),
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return result.stdout.strip()


def write_lines(tree: Path, lines: list[str]) -> None:
    (tree / "lib.c").write_text("\n".join(lines) + "\n")


def commit_edit(tree: Path, index: int, text: str, subject: str) -> None:
    lines = (tree / "lib.c").read_text().splitlines()
    lines[index] = text
    write_lines(tree, lines)
    run_git(tree, "commit", "-q", "-s", "-am", subject)


class Fixture:
    """An upstream repository with a pinned base and a two-patch series."""

    def __init__(self, scratch: Path) -> None:
        self.tree = scratch / "upstream"
        self.root = scratch / "series"
        self.tree.mkdir()
        self.root.mkdir()
        run_git(self.tree, "init", "-q")
        write_lines(self.tree, LINES)
        run_git(self.tree, "add", "lib.c")
        run_git(self.tree, "commit", "-q", "-m", "base")
        self.base = run_git(self.tree, "rev-parse", "HEAD")
        (self.root / "base.env").write_text(
            f"FFMPEG_REMOTE=https://example.invalid/up.git\nFFMPEG_TAG=v1\nFFMPEG_COMMIT={self.base}\n"
        )
        (self.root / "series.txt").write_text("# header kept by export\n")
        commit_edit(self.tree, 4, "int line_5 = 50;", "lib: fix line five")
        commit_edit(self.tree, 29, "int line_30 = 300;", "lib: fix line thirty")
        with contextlib.redirect_stderr(io.StringIO()):
            self.names = series.export_series(self.root, self.tree)
        for name in self.names:
            (self.root / "patches" / name).with_suffix(".toml").write_text(METADATA)
        self.reset()

    def reset(self, ref: str | None = None) -> None:
        run_git(self.tree, "checkout", "-q", "--detach", "-f", ref or self.base)

    def patch(self, number: int) -> Path:
        return self.root / "patches" / self.names[number - 1]

    def upstream_commit(self, index: int, text: str) -> str:
        """Commit an upstream change on top of the base and return its id."""
        self.reset()
        commit_edit(self.tree, index, text, "upstream change")
        return run_git(self.tree, "rev-parse", "HEAD")


class SeriesTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._scratch = tempfile.TemporaryDirectory()
        self.fx = Fixture(Path(self._scratch.name))

    def tearDown(self) -> None:
        self._scratch.cleanup()

    def assert_error(self, findings: series.Findings, needle: str) -> None:
        self.assertTrue(
            any(needle in line for line in findings.errors), f"{needle!r} not in {findings.errors}"
        )


class CheckTest(SeriesTestCase):
    def test_valid_series_passes(self) -> None:
        findings = series.check(self.fx.root)
        self.assertEqual(findings.errors, [])
        self.assertEqual(findings.warnings, [])

    def test_missing_metadata_names_the_patch(self) -> None:
        self.fx.patch(2).with_suffix(".toml").unlink()
        self.assert_error(series.check(self.fx.root), f"{self.fx.names[1]}: no metadata file")

    def test_unlisted_patch_fails(self) -> None:
        (self.fx.root / "patches" / "0003-extra.patch").write_text("x")
        self.assert_error(
            series.check(self.fx.root), "0003-extra.patch: in patches/ but not listed"
        )

    def test_listed_but_missing_patch_fails(self) -> None:
        self.fx.patch(1).unlink()
        self.assert_error(series.check(self.fx.root), "missing from patches/")

    def test_missing_signoff_fails(self) -> None:
        path = self.fx.patch(1)
        path.write_text(path.read_text().replace("Signed-off-by: Dev <dev@invalid>\n", ""))
        self.assert_error(series.check(self.fx.root), "lacks a Signed-off-by")

    def test_numbered_subject_fails(self) -> None:
        path = self.fx.patch(1)
        path.write_text(path.read_text().replace("[PATCH]", "[PATCH 1/2]"))
        self.assert_error(series.check(self.fx.root), "lacks 'Subject: [PATCH]'")

    def test_number_must_match_position(self) -> None:
        series_txt = self.fx.root / "series.txt"
        lines = series_txt.read_text().splitlines()
        series_txt.write_text("\n".join([lines[0], lines[2], lines[1]]) + "\n")
        self.assert_error(series.check(self.fx.root), "differs from its series position 0001")

    def test_bad_status_fails(self) -> None:
        self.fx.patch(1).with_suffix(".toml").write_text(METADATA.replace("submitted", "done"))
        self.assert_error(series.check(self.fx.root), "status must be one of")

    def test_unknown_key_fails(self) -> None:
        self.fx.patch(1).with_suffix(".toml").write_text(METADATA + 'upstrem = "typo"\n')
        self.assert_error(series.check(self.fx.root), "unknown key 'upstrem'")

    def test_submitted_needs_link(self) -> None:
        self.fx.patch(1).with_suffix(".toml").write_text(
            'status = "submitted"\nconsumers = ["a"]\n'
        )
        self.assert_error(series.check(self.fx.root), "needs an https upstream link")

    def test_empty_consumers_fail(self) -> None:
        self.fx.patch(1).with_suffix(".toml").write_text(METADATA.replace('["a"]', "[]"))
        self.assert_error(series.check(self.fx.root), "consumers must be a non-empty list")

    def test_short_base_commit_fails(self) -> None:
        env = self.fx.root / "base.env"
        env.write_text(env.read_text().replace(self.fx.base, self.fx.base[:12]))
        self.assert_error(series.check(self.fx.root), "not a full 40-hex commit")

    def test_merged_warns_but_passes(self) -> None:
        self.fx.patch(1).with_suffix(".toml").write_text(METADATA.replace("submitted", "merged"))
        findings = series.check(self.fx.root)
        self.assertEqual(findings.errors, [])
        self.assertIn("drop it", findings.warnings[0])

    def test_unsubmitted_needs_no_link(self) -> None:
        self.fx.patch(1).with_suffix(".toml").write_text(
            'status = "unsubmitted"\nupstream = ""\nconsumers = ["a"]\n'
        )
        self.assertEqual(series.check(self.fx.root).errors, [])


class ApplyTest(SeriesTestCase):
    def apply(self, expect_base: bool = True, keep_going: bool = False) -> series.Results:
        return series.apply_series(self.fx.root, self.fx.tree, expect_base, keep_going)

    def test_series_applies_on_base(self) -> None:
        results = self.apply()
        self.assertEqual([status for _, status in results], ["applied", "applied"])
        self.assertFalse(series.apply_failed(results, expect_base=True))

    def test_conflict_stops_and_leaves_a_clean_tree(self) -> None:
        self.fx.upstream_commit(4, "int line_5 = 7;")
        results = self.apply(expect_base=False)
        self.assertEqual([status for _, status in results], ["conflict", "not-tried"])
        self.assertTrue(series.apply_failed(results, expect_base=False))
        self.assertEqual(run_git(self.fx.tree, "status", "--porcelain"), "")
        self.assertFalse(series.am_in_progress(self.fx.tree))

    def test_keep_going_tries_every_patch(self) -> None:
        self.fx.upstream_commit(4, "int line_5 = 7;")
        results = self.apply(expect_base=False, keep_going=True)
        self.assertEqual([status for _, status in results], ["conflict", "applied"])

    def test_upstream_fix_with_other_context_is_a_drop_candidate(self) -> None:
        self.fx.reset()
        lines = (self.fx.tree / "lib.c").read_text().splitlines()
        lines[4] = "int line_5 = 50;"
        lines[3] = "int line_4 = 4; /* reworded upstream */"
        lines[5] = "int line_6 = 6; /* reworded upstream */"
        write_lines(self.fx.tree, lines)
        run_git(self.fx.tree, "commit", "-q", "-am", "upstream takes the fix")
        results = self.apply(expect_base=False, keep_going=True)
        self.assertEqual([status for _, status in results], ["upstream", "applied"])
        self.assertFalse(series.apply_failed(results, expect_base=False))
        self.assertTrue(series.apply_failed(results, expect_base=True))

    def test_expect_base_refuses_another_head(self) -> None:
        self.fx.upstream_commit(10, "int line_11 = 11; /* moved on */")
        with self.assertRaises(series.SeriesError):
            self.apply(expect_base=True)

    def test_dirty_tree_is_refused(self) -> None:
        (self.fx.tree / "lib.c").write_text("dirty\n")
        with self.assertRaises(series.SeriesError):
            self.apply()

    def test_report_lists_every_patch(self) -> None:
        results = self.apply()
        report = Path(self._scratch.name) / "report.md"
        with contextlib.redirect_stdout(io.StringIO()):
            series.write_report(results, self.fx.tree, report)
        text = report.read_text()
        for name in self.fx.names:
            self.assertIn(f"| `{name}` | applied |", text)


class MainTest(SeriesTestCase):
    def test_tool_error_exits_two(self) -> None:
        (self.fx.tree / "lib.c").write_text("dirty\n")
        argv = ["--root", str(self.fx.root), "apply", str(self.fx.tree)]
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(series.main(argv), 2)
        self.assertIn("local changes", err.getvalue())

    def test_conflict_exits_one(self) -> None:
        self.fx.upstream_commit(4, "int line_5 = 7;")
        argv = ["--root", str(self.fx.root), "apply", "--keep-going", str(self.fx.tree)]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(series.main(argv), 1)

    def test_check_exits_zero_on_a_valid_series(self) -> None:
        self.assertEqual(series.main(["--root", str(self.fx.root), "check"]), 0)


class WarningsTest(SeriesTestCase):
    FILES = frozenset({"libavcodec/vlc.c", "tools/seek_print.c"})

    def test_clean_log_passes(self) -> None:
        log = ["CC\tlibavcodec/vlc.o", "LD\tffmpeg_g"]
        self.assertEqual(series.triage_warnings(log, set(self.FILES)), ([], 0))

    def test_warning_in_a_series_file_is_gated_in_every_path_form(self) -> None:
        log = [
            "libavcodec/vlc.c:12:3: warning: array subscript 9 [-Warray-bounds=]",
            "../ffmpeg/tools/seek_print.c:40:5: warning: '%s' directive output",
            "/home/runner/ffmpeg/libavcodec/vlc.c:7:1: Warning: something",
        ]
        own, other = series.triage_warnings(log, set(self.FILES))
        self.assertEqual(own, log)
        self.assertEqual(other, 0)

    def test_warning_elsewhere_is_counted_not_gated(self) -> None:
        log = [
            "libavfilter/vlc.c:1:1: warning: same base name, other directory",
            "xlibavcodec/vlc.c:1:1: warning: path that only ends in the name",
            "clang: warning: CUDA version is newer than the latest supported version",
        ]
        self.assertEqual(series.triage_warnings(log, set(self.FILES)), ([], 3))

    def test_note_lines_are_not_warnings(self) -> None:
        log = ["libavcodec/vlc.c:12:3: note: in expansion of macro 'X'"]
        self.assertEqual(series.triage_warnings(log, set(self.FILES)), ([], 0))

    def test_gate_reads_the_series_files_from_the_tree(self) -> None:
        series.apply_series(self.fx.root, self.fx.tree, True, False)
        log = Path(self._scratch.name) / "build.log"
        log.write_text("lib.c:5:1: warning: unused variable 'x'\nother.c:1:1: warning: y\n")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            status = series.warnings_gate(self.fx.root, self.fx.tree, log, None)
        self.assertEqual(status, 1)
        self.assertIn("1 compiler warning(s) in files the series changes", out.getvalue())
        self.assertIn("1 elsewhere", out.getvalue())

    def newer_upstream_tree(self) -> tuple[str, Path]:
        """Apply the series on an upstream commit that adds other.c; return it and a log."""
        self.fx.reset()
        (self.fx.tree / "other.c").write_text("int other;\n")
        run_git(self.fx.tree, "add", "other.c")
        run_git(self.fx.tree, "commit", "-q", "-m", "upstream: add other.c")
        upstream = run_git(self.fx.tree, "rev-parse", "HEAD")
        series.apply_series(self.fx.root, self.fx.tree, False, False)
        log = Path(self._scratch.name) / "build.log"
        log.write_text("lib.c:5:1: warning: unused variable 'x'\nother.c:1:1: warning: y\n")
        return upstream, log

    def warnings_main(self, log: Path, *extra: str) -> tuple[int, str, str]:
        argv = ["--root", str(self.fx.root), "warnings", str(self.fx.tree), str(log), *extra]
        with (
            contextlib.redirect_stdout(io.StringIO()) as out,
            contextlib.redirect_stderr(io.StringIO()) as err,
        ):
            status = series.main(argv)
        return status, out.getvalue(), err.getvalue()

    def test_base_limits_the_gate_to_the_series_on_a_newer_upstream(self) -> None:
        upstream, log = self.newer_upstream_tree()
        self.assertEqual(series.series_files(self.fx.root, self.fx.tree), {"lib.c", "other.c"})
        self.assertEqual(series.series_files(self.fx.root, self.fx.tree, upstream), {"lib.c"})
        status, out, _ = self.warnings_main(log, "--base", upstream)
        self.assertEqual(status, 1)
        self.assertIn("1 compiler warning(s) in files the series changes", out)
        self.assertIn("1 elsewhere", out)

    def test_base_that_is_not_an_ancestor_is_a_tool_error(self) -> None:
        side = self.fx.upstream_commit(4, "int line_5 = 7;")
        _, log = self.newer_upstream_tree()
        status, _, err = self.warnings_main(log, "--base", side)
        self.assertEqual(status, 2)
        self.assertIn("not an ancestor of HEAD", err)

    def test_unknown_base_is_a_tool_error(self) -> None:
        _, log = self.newer_upstream_tree()
        status, _, err = self.warnings_main(log, "--base", "no-such-ref")
        self.assertEqual(status, 2)
        self.assertIn("'no-such-ref' is not a commit", err)

    def test_base_at_head_gates_nothing(self) -> None:
        _, log = self.newer_upstream_tree()
        status, out, _ = self.warnings_main(log, "--base", "HEAD")
        self.assertEqual(status, 0)
        self.assertIn("0 compiler warning(s) in files the series changes", out)
        self.assertIn("2 elsewhere", out)


class CoverageTest(SeriesTestCase):
    def test_compiled_sources_reads_cc_lines_only(self) -> None:
        log = ["CC\tlibavcodec/vlc.o", "LD\tffmpeg_g", "HOSTCC\tdoc/print_options.o", "X86ASM\tx.o"]
        self.assertEqual(series.compiled_sources(log), {"libavcodec/vlc.c"})

    def test_every_changed_c_file_compiled_passes(self) -> None:
        files = {"libavcodec/vlc.c", "libavcodec/vlc.h"}
        status, out = series.coverage_report(files, ["CC\tlibavcodec/vlc.o"])
        self.assertEqual(status, 0)
        self.assertIn("1 of 1 C file(s) the series changes were compiled", out[-1])

    def test_file_in_a_disabled_component_fails(self) -> None:
        files = {"libavcodec/vlc.c", "libavfilter/vf_mcdeint.c"}
        status, out = series.coverage_report(files, ["CC\tlibavcodec/vlc.o"])
        self.assertEqual(status, 1)
        self.assertIn("not compiled: libavfilter/vf_mcdeint.c", out)
        self.assertIn("1 of 2 C file(s)", out[-1])

    def test_same_name_in_another_directory_does_not_count(self) -> None:
        status, out = series.coverage_report({"lib.c"}, ["CC\tsub/lib.o"])
        self.assertEqual(status, 1)
        self.assertIn("not compiled: lib.c", out)

    def test_included_templates_are_not_checked(self) -> None:
        files = {"a.c", "b_template.c", "c.h"}
        status, out = series.coverage_report(files, ["CC\ta.o"])
        self.assertEqual(status, 0)
        self.assertIn("1 of 1 C file(s)", out[-1])
        self.assertIn("1 included template(s) not checked", out[-1])

    def test_nothing_to_check_fails(self) -> None:
        status, out = series.coverage_report({"c.h", "b_template.c"}, ["CC\ta.o"])
        self.assertEqual(status, 1)
        self.assertIn("0 of 0 C file(s)", out[-2])
        self.assertIn("no C file to check", out[-1])

    def test_gate_reads_the_series_files_from_the_tree(self) -> None:
        series.apply_series(self.fx.root, self.fx.tree, True, False)
        log = Path(self._scratch.name) / "build.log"
        argv = ["--root", str(self.fx.root), "coverage", str(self.fx.tree), str(log)]
        for text, expected in (("CC\tlib.o\n", 0), ("CC\tother.o\n", 1)):
            log.write_text(text)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(series.main(argv), expected)


class NotesTest(SeriesTestCase):
    def test_notes_list_every_patch_with_its_upstream_state(self) -> None:
        notes = series.release_notes(self.fx.root, "v1.2.3", "Org/repo")
        self.assertIn(f"FFmpeg base: `v1` (`{self.fx.base}`).", notes)
        link = "[link](https://example.invalid/pulls/1)"
        self.assertIn(f"| 0001 | lib: fix line five | submitted | {link} | a |", notes)
        self.assertIn("| 0002 | lib: fix line thirty |", notes)
        self.assertIn(".github/workflows/release-build.yml@refs/tags/v1.2.3", notes)
        self.assertIn("gh attestation verify ffmpeg-patches-v1.2.3.tar.gz --repo Org/repo", notes)

    def test_folded_subject_is_unfolded(self) -> None:
        path = self.fx.patch(1)
        text = path.read_text().replace(
            "Subject: [PATCH] lib: fix line five", "Subject: [PATCH] lib: fix line\n five"
        )
        path.write_text(text)
        self.assertEqual(series.patch_subject(path), "lib: fix line five")

    def test_unsubmitted_patch_has_no_link(self) -> None:
        self.fx.patch(2).with_suffix(".toml").write_text(
            'status = "unsubmitted"\nupstream = ""\nconsumers = ["a", "b"]\n'
        )
        notes = series.release_notes(self.fx.root, "v1.2.3", "Org/repo")
        self.assertIn("| unsubmitted | none | a, b |", notes)

    def test_notes_refuse_a_series_without_base(self) -> None:
        (self.fx.root / "base.env").unlink()
        with self.assertRaises(series.SeriesError):
            series.release_notes(self.fx.root, "v1.2.3", "Org/repo")


class ExportTest(SeriesTestCase):
    def test_round_trip_is_byte_identical(self) -> None:
        before = {p.name: p.read_bytes() for p in (self.fx.root / "patches").iterdir()}
        series_before = (self.fx.root / "series.txt").read_text()
        self.assertFalse(
            series.apply_failed(series.apply_series(self.fx.root, self.fx.tree, True, False), True)
        )
        series.export_series(self.fx.root, self.fx.tree)
        after = {p.name: p.read_bytes() for p in (self.fx.root / "patches").iterdir()}
        self.assertEqual(before, after)
        self.assertEqual((self.fx.root / "series.txt").read_text(), series_before)
        self.assertTrue(series_before.startswith("# header kept by export\n"))

    def test_metadata_follows_its_patch_across_renumbering(self) -> None:
        first = self.fx.patch(1)
        self.fx.reset()
        commit_edit(self.fx.tree, 29, "int line_30 = 300;", "lib: fix line thirty")
        names = series.export_series(self.fx.root, self.fx.tree)
        self.assertEqual(len(names), 1)
        self.assertTrue(names[0].startswith("0001-lib-fix-line-thirty"))
        self.assertFalse(first.exists())
        self.assertTrue((self.fx.root / "patches" / names[0]).with_suffix(".toml").is_file())

    def test_export_refuses_a_tree_off_the_base(self) -> None:
        run_git(self.fx.tree, "checkout", "-q", "--orphan", "other")
        run_git(self.fx.tree, "commit", "-q", "-m", "unrelated")
        with self.assertRaises(series.SeriesError):
            series.export_series(self.fx.root, self.fx.tree)


if __name__ == "__main__":
    unittest.main()
