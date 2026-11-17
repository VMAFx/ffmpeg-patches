# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Planted upstream-lint defects; no repository gates or credential tests.

Deterministic fixtures stub the pre-commit subprocess, never its surrounding
Git, isolation or reporting logic. Real-tool fixtures copy base files with
git show from the named upstream checkout and skip explicitly if unavailable.
"""

from __future__ import annotations

import contextlib
import io
import os
import signal
import subprocess  # nosec B404 - fixed commands in disposable Git repositories.
import sys
import tempfile
import unittest
import unittest.mock
from dataclasses import FrozenInstanceError
from pathlib import Path

from tests.helpers import GIT_ENVIRONMENT_NAMES, isolated_git_environment, run_git

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import upstream_lint  # noqa: E402

UPSTREAM = Path(os.environ["UPSTREAM_TREE"]) if os.environ.get("UPSTREAM_TREE") else None
SOURCE = "int ff_value(void)\n{\n    return 1;\n}\n"
FAKE_CONFIG = "repos: []\n"
FAKE_MESSAGE = '#!/bin/sh\ngit log --format=%s "$1" | grep -Eq "^[^:]+: .+"\n'
FAKE_PATCHECK = "#!/bin/sh\ncat > patcheck.stdout\nprintf 'patcheck fixture\\n'\n"


class RulesTest(unittest.TestCase):
    """Positive, negative and boundary coverage of added-line rules."""

    def findings(self, source: str, added: set[int] | None = None):
        """Check one C source without running external tools."""
        if added is None:
            added = set(range(1, len(source.split("\n")) + 1))
        return upstream_lint._file_rules("libavfilter/vf_x.c", source, added)

    def test_added_tab_fails(self):
        """A planted added TAB fails the mechanical step."""
        count, lines = self.findings("\treturn 1;\n")
        self.assertEqual(count, 1)
        self.assertIn("TAB character", "\n".join(lines))

    def test_added_trailing_whitespace_fails(self):
        """Spaces, TAB and CR at line end all fail, including blank lines."""
        for whitespace in (" ", "\t", "\r", "  "):
            with self.subTest(whitespace=whitespace):
                self.assertGreater(self.findings("return 1;" + whitespace + "\n")[0], 0)
        self.assertEqual(self.findings("  \n")[0], 1)

    def test_function_brace_same_line_fails(self):
        """Single-line function declarations require a separate opening brace."""
        self.assertEqual(self.findings("static int ff_value(void) {\n    return 1;\n}\n")[0], 1)

    def test_control_brace_separate_line_fails(self):
        """Each recognized control header requires its opening brace on that line."""
        for header in (
            "if (x)",
            "for (i = 0; i < 3; i++)",
            "while (x)",
            "switch (x)",
            "else",
            "} else if (x)",
            "do",
        ):
            with self.subTest(header=header):
                count, lines = self.findings(f"    {header}\n    {{\n")
                self.assertEqual(count, 1, lines)

    def test_clean_source_passes(self):
        """K&R function and control braces pass."""
        self.assertEqual(self.findings(SOURCE)[0], 0)
        self.assertEqual(self.findings("    if (x) {\n        ff_value();\n    }\n")[0], 0)

    def test_untouched_tab_passes_and_is_listed(self):
        """An inherited TAB is informational when only a later line is added."""
        count, lines = self.findings("\tff_old();\n    ff_new();\n", {2})
        self.assertEqual(count, 0)
        self.assertEqual(lines, ["pre-existing (untouched): libavfilter/vf_x.c:1: TAB character"])

    def test_untouched_brace_violation_does_not_fail(self):
        """An old function brace is outside the added-line scope."""
        self.assertEqual(self.findings("int ff_value(void) {\n    return 2;\n}\n", {2})[0], 0)

    def test_added_brace_after_untouched_header_fails(self):
        """A changed brace is checked against an unchanged neighboring header."""
        self.assertEqual(self.findings("    if (x)\n    {\n", {2})[0], 1)

    def test_comments_literals_macros_and_initializers_pass(self):
        """Conservative brace matching ignores non-code and ambiguous constructs."""
        source = (
            'const char *s = "int ff_bad(void) {";\n'
            "/* int ff_bad(void) {\n    if (x)\n    {\n*/\n"
            "// int ff_bad(void) {\n"
            "#define FUNCTION(x) int ff_bad(void) {\n"
            "#define MULTILINE(x) \\\n    if (x) \\\n    {\n"
            "const Item items[] = {\n    { 1 },\n};\n"
        )
        self.assertEqual(self.findings(source)[0], 0)

    def test_multiline_and_function_pointer_signatures_are_ignored(self):
        """Unsupported signatures cannot produce speculative brace findings."""
        source = "int ff_value(\n    int x) {\n}\nint ff_apply(int (*fn)(void)) {\n}\n"
        self.assertEqual(self.findings(source)[0], 0)

    def test_patcheck_added_hint_fails_even_with_ansi(self):
        """Colored added-line references in a blocking category fail."""
        output = "double ;\n\x1b[35mpatcheck.stdout\x1b[m:\x1b[32m23\x1b[m:\x1b[m+bad();;\n"
        rc, text = upstream_lint._patcheck_hints(output)
        self.assertEqual(rc, 1)
        self.assertIn("finding:", text)
        self.assertNotIn("\x1b", text)

    def test_patcheck_unlocated_added_hint_fails(self):
        """Patcheck's multiline suggestions can print raw added lines."""
        self.assertEqual(upstream_lint._patcheck_hints("useless if()\n+    if (x)\n")[0], 1)

    def test_patcheck_increment_and_raw_colons_are_added_lines(self):
        """A source increment is not a file header; raw added text keeps its prefix."""
        for line in ("patcheck.stdout:23:+++value;", "+    foo:23:message"):
            with self.subTest(line=line):
                self.assertEqual(upstream_lint._patcheck_hints(f"double ;\n{line}")[0], 1)

    def test_patcheck_printf_hint_requires_review_without_failing(self):
        """Test-program printf hints stay visible without blocking."""
        line = 'patcheck.stdout:23:+    printf("hello");'
        rc, text = upstream_lint._patcheck_hints(f"Please use av_log\n{line}\n")
        self.assertEqual(rc, 0, text)
        self.assertIn(f"hint (review): Please use av_log: {line}", text)

    def test_patcheck_unknown_and_missing_categories_require_review(self):
        """New categories and hits without a header cannot become blockers."""
        for header, category in (
            ("new upstream heuristic\n", "new upstream heuristic"),
            ("", "unknown"),
        ):
            with self.subTest(category=category):
                rc, text = upstream_lint._patcheck_hints(f"{header}+    ff_value();\n")
                self.assertEqual(rc, 0, text)
                self.assertIn(f"hint (review): {category}: +    ff_value();", text)

    def test_patcheck_category_follows_headers_not_context_or_separators(self):
        """Context, blank lines and grep separators preserve the current category."""
        output = (
            "double ;\npatcheck.stdout-22- context\n--\n\n"
            "patcheck.stdout:23:+bad();;\n"
            'Please use av_log\npatcheck.stdout:27:+printf("hello");\n'
        )
        rc, text = upstream_lint._patcheck_hints(output)
        self.assertEqual(rc, 1, text)
        self.assertIn("finding: double ;: patcheck.stdout:23:+bad();;", text)
        self.assertIn("hint (review): Please use av_log: patcheck.stdout:27:", text)

    def test_patcheck_header_requires_an_exact_allow_list_match(self):
        """Similar heuristic names cannot inherit a blocking prefix."""
        rc, text = upstream_lint._patcheck_hints("double ; suggestion\n+bad();;\n")
        self.assertEqual(rc, 0, text)
        self.assertIn("hint (review): double ; suggestion:", text)

    def test_patcheck_context_and_general_hints_are_informational(self):
        """Context, removed lines, file headers and generic advice do not fail."""
        output = (
            "tabs\npatcheck.stdout:23: \told\npatcheck.stdout-24- old\n"
            "patcheck.stdout:25:-removed\npatcheck.stdout:26:+++ b/file.c\n"
            "Missing changelog entry (ignore if minor change)\n"
        )
        rc, text = upstream_lint._patcheck_hints(output)
        self.assertEqual(rc, 0)
        self.assertIn("informational:", text)

    def test_deletion_only_hunk_has_no_added_lines(self):
        """Zero-length new ranges do not accidentally select their neighbor."""
        self.assertEqual(upstream_lint._added_lines("@@ -2 +1,0 @@\n-old\n"), set())
        self.assertEqual(upstream_lint._added_lines("@@ -2 +2,2 @@\n+a\n+b\n"), {2, 3})


class RegressionTestPathTest(unittest.TestCase):
    """Positive, negative and boundary coverage of test and source path predicates."""

    def test_is_test_path(self) -> None:
        """Top-level and nested tests directories match; source directories do not."""
        self.assertTrue(upstream_lint.is_test_path("tests/fate/x.mak"))
        self.assertTrue(upstream_lint.is_test_path("libavutil/tests/x.c"))
        self.assertFalse(upstream_lint.is_test_path("libavfilter/vf_x.c"))

    def test_is_source_path(self) -> None:
        """Library and fftools sources match while test, doc and compat paths do not."""
        for path in ("libavfilter/vf_x.c", "fftools/ffmpeg.c", "libavcodec/x86/x.asm"):
            with self.subTest(path=path):
                self.assertTrue(upstream_lint.is_source_path(path))
        for path in ("libavutil/tests/x.c", "doc/filters.texi", "compat/x.c"):
            with self.subTest(path=path):
                self.assertFalse(upstream_lint.is_source_path(path))


class FixWordTest(unittest.TestCase):
    """Positive, negative and boundary coverage of fix detection in commit messages."""

    def test_subject_and_body_defect_words(self) -> None:
        """A fix or defect word in the subject or the body returns that word."""
        cases = (
            ("avfilter/vf_x: fix y\n", "fix"),
            ("avformat/dashenc: do not delete y\n\nFix this by skipping y.\n", "Fix"),
            ("avcodec/x: skip absent y\n\nReading y would misalign every field.\n", "misalign"),
            ("avcodec/x: bound y\n\nThe next set was read past the end of it.\n", "past the end"),
            ("avformat/x: enlarge y\n\nGCC reports -Wformat-truncation for y.\n", "truncation"),
        )
        for message, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(upstream_lint.fix_word(message), expected)

    def test_wrapped_defect_phrase_is_found(self) -> None:
        """A defect phrase split by a line break or by hyphens returns that phrase."""
        cases = (
            ("avcodec/x: bound y\n\nThe read goes out of\nbounds.\n", "out of\nbounds"),
            ("avcodec/x: bound y\n\nAn out-of-\nbounds read of y.\n", "out-of-\nbounds"),
            ("avcodec/x: bound y\n\nAn out\n-of-bounds read of y.\n", "out\n-of-bounds"),
            ("avcodec/x: bound y\n\nThe read goes past the\nend of y.\n", "past the\nend"),
        )
        for message, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(upstream_lint.fix_word(message), expected)

    def test_split_phrase_words_stay_whole(self) -> None:
        """Phrase words joined without a separator or with other words between return None."""
        for message in (
            "avcodec/x: bound y\n\nThe read stays outof bounds checks.\n",
            "avcodec/x: bound y\n\nThe read goes out of its\nbounds.\n",
            "avcodec/x: bound y\n\nThe read goes past\nits end.\n",
        ):
            with self.subTest(message=message):
                self.assertIsNone(upstream_lint.fix_word(message))

    def test_no_defect_word_returns_none(self) -> None:
        """Messages without a whole fix or defect word outside sign-off lines return None."""
        for message in (
            "avfilter/vf_x: update y\n",
            "avfilter/vf_x: update y\n\nRename the helper.\n",
            "avfilter/vf_x: prefix y\n\nKeep the debug fixture.\n",
            "avfilter/vf_x: update y\n\nSigned-off-by: Bug Fixer <fixer@invalid>\n",
        ):
            with self.subTest(message=message):
                self.assertIsNone(upstream_lint.fix_word(message))


class TestReachTest(unittest.TestCase):
    """Positive, negative and boundary coverage of reach_names and test_words."""

    def test_lavfi_prefix_stripped(self) -> None:
        """Source paths contribute both full stems and lavfi-stripped stems."""
        self.assertEqual(
            upstream_lint.reach_names(["libavfilter/vf_x.c"], ""),
            frozenset({"vf_x", "x"}),
        )

    def test_source_stem_and_hunk_function(self) -> None:
        """Source stems and hunk-header functions are collected while docs are ignored."""
        patch = (
            "diff --git a/libavcodec/aom_film_grain.c b/libavcodec/aom_film_grain.c\n"
            "--- a/libavcodec/aom_film_grain.c\n"
            "+++ b/libavcodec/aom_film_grain.c\n"
            "@@ -4 +4 @@ static int parse_sets(GetBitContext *gb)\n"
            "-    return x;\n"
            "+    return x + 1;\n"
        )
        self.assertEqual(
            upstream_lint.reach_names(["libavcodec/aom_film_grain.c", "doc/x.texi"], patch),
            frozenset({"aom_film_grain", "parse_sets"}),
        )

    def test_deleted_source_file_keeps_old_path_and_function(self) -> None:
        """Deleted source diffs retain the --- a/ path for hunk function extraction."""
        patch = (
            "diff --git a/libavcodec/old.c b/libavcodec/old.c\n"
            "deleted file mode 100644\n"
            "--- a/libavcodec/old.c\n"
            "+++ /dev/null\n"
            "@@ -1,3 +0,0 @@ int ff_old(void)\n"
            "-x\n"
        )
        self.assertEqual(
            upstream_lint.reach_names(["libavcodec/old.c"], patch),
            frozenset({"old", "ff_old"}),
        )

    def test_test_path_hunk_header_adds_nothing(self) -> None:
        """Hunk headers under test paths do not contribute reachable source names."""
        patch = (
            "diff --git a/libavcodec/tests/x.c b/libavcodec/tests/x.c\n"
            "--- a/libavcodec/tests/x.c\n"
            "+++ b/libavcodec/tests/x.c\n"
            "@@ -1 +1 @@ int main(void)\n"
            "+y\n"
        )
        self.assertEqual(upstream_lint.reach_names(["libavcodec/tests/x.c"], patch), frozenset())

    def test_test_words_from_path_and_added_lines(self) -> None:
        """Changed test diffs yield every word from both the path and added lines."""
        patch = (
            "diff --git a/tests/ref/fate/filter-x b/tests/ref/fate/filter-x\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/tests/ref/fate/filter-x\n"
            "@@ -0,0 +1 @@\n"
            "+0\n"
        )
        self.assertEqual(
            upstream_lint.test_words(patch),
            frozenset({"tests", "ref", "fate", "filter", "x", "0"}),
        )

    def test_source_added_lines_add_no_test_words(self) -> None:
        """Added body lines under source paths do not enter test_words."""
        patch = (
            "diff --git a/libavfilter/vf_x.c b/libavfilter/vf_x.c\n"
            "--- a/libavfilter/vf_x.c\n"
            "+++ b/libavfilter/vf_x.c\n"
            "@@ -1 +1 @@\n"
            "+parse_sets();\n"
        )
        self.assertEqual(upstream_lint.test_words(patch), frozenset())

    def test_body_line_resembling_header_is_not_header(self) -> None:
        """An added source line starting with +++ b/tests/ cannot switch file state."""
        patch = (
            "diff --git a/libavfilter/vf_x.c b/libavfilter/vf_x.c\n"
            "--- a/libavfilter/vf_x.c\n"
            "+++ b/libavfilter/vf_x.c\n"
            "@@ -1 +1 @@\n"
            "+++ b/tests/parse_sets\n"
        )
        self.assertEqual(upstream_lint.test_words(patch), frozenset())

    def test_quoted_non_ascii_test_path_words(self) -> None:
        """Quoted non-ASCII test headers decode so path and added-line words are kept."""
        patch = (
            'diff --git "a/tests/caf\\303\\251_x.c" "b/tests/caf\\303\\251_x.c"\n'
            "new file mode 100644\n"
            "--- /dev/null\n"
            '+++ "b/tests/caf\\303\\251_x.c"\n'
            "@@ -0,0 +1 @@\n"
            "+    return ff_value();\n"
        )
        words = upstream_lint.test_words(patch)
        self.assertIn("ff_value", words)
        self.assertIn("tests", words)

    def test_unquote_git_path_escapes(self) -> None:
        """C-style quote, backslash, tab and UTF-8 octal escapes decode to text."""
        self.assertEqual(
            upstream_lint.unquote_git_path('"b/tests/a\\"b\\\\c\\td.c"'),
            'b/tests/a"b\\c\td.c',
        )
        self.assertEqual(
            upstream_lint.unquote_git_path('"b/tests/caf\\303\\251_x.c"'),
            "b/tests/café_x.c",
        )

    def test_unquote_git_path_unquoted_and_lone_quote(self) -> None:
        """Unquoted paths, a lone quote and the empty string return unchanged."""
        self.assertEqual(upstream_lint.unquote_git_path("b/tests/x.c"), "b/tests/x.c")
        self.assertEqual(upstream_lint.unquote_git_path('"'), '"')
        self.assertEqual(upstream_lint.unquote_git_path(""), "")

    def test_space_path_trailing_tab_hunk_names(self) -> None:
        """Space-containing source headers drop Git's trailing TAB before suffix checks."""
        patch = (
            "diff --git a/libavfilter/vf_a b.c b/libavfilter/vf_a b.c\n"
            "--- a/libavfilter/vf_a b.c\t\n"
            "+++ b/libavfilter/vf_a b.c\t\n"
            "@@ -1 +1 @@ int ff_space(void)\n"
            "-x\n"
            "+y\n"
        )
        self.assertEqual(upstream_lint.reach_names([], patch), frozenset({"ff_space"}))

    def test_binary_test_path_words_from_paths(self) -> None:
        """Explicit test paths supply path words even when a binary diff has no headers."""
        self.assertEqual(
            upstream_lint.test_words("", ["tests/data/filter-x.bin", "libavfilter/vf_x.c"]),
            frozenset({"tests", "data", "filter", "x", "bin"}),
        )

    def test_dev_null_header_keeps_path(self) -> None:
        """A deleted test file retains its --- a/ path across a +++ /dev/null header."""
        patch = (
            "diff --git a/tests/ref/fate/filter-x b/tests/ref/fate/filter-x\n"
            "deleted file mode 100644\n"
            "--- a/tests/ref/fate/filter-x\n"
            "+++ /dev/null\n"
            "@@ -1 +0,0 @@\n"
            "-0\n"
        )
        self.assertEqual(
            upstream_lint.test_words(patch),
            frozenset({"tests", "ref", "fate", "filter", "x"}),
        )


FATE_RED = (
    "FATE_SCALE_REFUSE = fate-filter-scale-ipt-c2-refuse-rgb "
    "fate-filter-scale-ipt-c2-refuse-range\n"
    "FATE_SCALE_REFUSE += fate-filter-scale-bt2020c-refuse-rgb\n"
    "FATE_FILTER-$(call ALLYES, TESTSRC2_FILTER FORMAT_FILTER SETPARAMS_FILTER "
    "SCALE_FILTER LAVFI_INDEV WRAPPED_AVFRAME_ENCODER NULL_MUXER) += $(FATE_SCALE_REFUSE)\n"
    "fate-filter-scale-ipt-c2-refuse-rgb: CMD = ! run $(FFMPEG) -nostdin -hide_banner "
    '-f lavfi -i "testsrc2=s=64x64:r=1:d=1,format=yuv420p10le" '
    '-vf "scale,format=gbrp10le" -frames:v 1 -f null none\n'
)
FATE_DASH_MIXED = (
    "FATE_DASH-$(call ALLYES, LAVFI_INDEV TESTSRC2_FILTER "
    "WRAPPED_AVFRAME_DECODER DASH_MUXER) += fate-dash-single-file-trim\n"
    "fate-dash-single-file-trim: CMD = mkdir -p tests/data/dash_single_file_trim && \\\n"
    '    run $(FFMPEG) -nostdin -f lavfi -i "testsrc2=s=64x64:r=1:d=2" \\\n'
    "    $(TARGET_PATH)/tests/data/dash_single_file_trim/out.mpd "
    "2>tests/data/dash_single_file_trim/err.log && \\\n"
    '    sed -n "/failed to delete/p" tests/data/dash_single_file_trim/err.log && \\\n'
    "    test -s tests/data/dash_single_file_trim/media.mp4\n"
)
FATE_SOURCES = {"testsrc2": "video", "sine": "audio", "movie": "mixed"}
FILTER_DECLARATIONS = (
    "extern const FFFilter ff_vsrc_testsrc2;\n"
    "extern const FFFilter ff_asrc_sine;\n"
    "extern const FFFilter ff_avsrc_movie;\n"
)


class FateDepsTest(unittest.TestCase):
    """Exercise fate-deps dependency and mixed-root path checks."""

    def check_diff(
        self, base_source: str, head_source: str, path: str = "tests/fate/x.mak"
    ) -> upstream_lint.Step:
        """Run _fate_deps across committed base and head FATE trees."""
        with tempfile.TemporaryDirectory() as directory:
            tree = Path(directory) / "tree"
            template = Path(directory) / "template"
            tree.mkdir()
            template.mkdir()
            run_git(tree, "init", "-q", f"--template={template}")
            target = tree / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(base_source, encoding="utf-8")
            run_git(tree, "add", path)
            run_git(tree, "commit", "-q", "-m", "tests/fate: add base")
            base = run_git(tree, "rev-parse", "HEAD")
            target.write_text(head_source, encoding="utf-8")
            run_git(tree, "add", path)
            run_git(tree, "commit", "-q", "-m", "tests/fate: update head")
            head = run_git(tree, "rev-parse", "HEAD")
            return upstream_lint._fate_deps(tree, base, head)

    def check_source(
        self,
        source: str = FATE_RED,
        added: set[int] | None = None,
        path: str = "tests/fate/filter-video.mak",
        sources: dict[str, str] | None = None,
    ) -> upstream_lint.Step:
        """Parse one changed FATE file with fixture source declarations."""
        self.assertTrue(hasattr(upstream_lint, "_fate_file"), "fate-deps parser missing")
        if added is None:
            added = set(range(1, len(source.splitlines()) + 1))
        catalog = FATE_SOURCES if sources is None else sources
        report = upstream_lint._fate_file(path, source, added, catalog)
        return upstream_lint._fate_summary(report)

    def test_planted_missing_decoder_fails(self) -> None:
        """The planted encoder dependency cannot satisfy the video decoder."""
        step = self.check_source()
        self.assertEqual(step.rc, 1, step.output)
        self.assertEqual(step.output.count("finding:"), 1)
        self.assertIn(":3: fate-filter-scale-ipt-c2-refuse-rgb:", step.output)
        self.assertIn("needs WRAPPED_AVFRAME_DECODER in its registration", step.output)

    def test_decoder_present_passes(self) -> None:
        """Explicit device and decoder dependencies satisfy the registration."""
        step = self.check_source(
            FATE_RED.replace("NULL_MUXER", "WRAPPED_AVFRAME_DECODER NULL_MUXER")
        )
        self.assertEqual(step.rc, 0, step.output)
        self.assertIn("1 lavfi video test(s) checked", step.output)
        self.assertIn("0 finding(s)", step.output)

    def test_untouched_gap_is_nonfailing(self) -> None:
        """An inherited dependency gap remains visible without failing."""
        step = self.check_source(added=set())
        self.assertEqual(step.rc, 0, step.output)
        self.assertIn("pre-existing (untouched):", step.output)

    def test_non_lavfi_is_not_counted(self) -> None:
        """Ordinary inputs stay outside the lavfi check."""
        step = self.check_source(FATE_RED.replace("-f lavfi", "-f rawvideo"))
        self.assertEqual(step.rc, 0, step.output)
        self.assertIn("0 lavfi video test(s) checked", step.output)

    def test_audio_is_skipped(self) -> None:
        """Audio sources do not require the wrapped video decoder."""
        step = self.check_source(FATE_RED.replace('"testsrc2=', '"sine='))
        self.assertEqual(step.rc, 0, step.output)
        self.assertIn("1 audio skipped", step.output)

    def test_variables_append_and_group_commands(self) -> None:
        """Appended variable targets and grouped CMD assignments expand together."""
        source = FATE_RED.replace(
            "fate-filter-scale-ipt-c2-refuse-rgb: CMD", "$(FATE_SCALE_REFUSE): CMD"
        )
        step = self.check_source(source, {2})
        self.assertEqual(step.rc, 1, step.output)
        self.assertEqual(step.output.count("finding:"), 1)
        self.assertIn("fate-filter-scale-bt2020c-refuse-rgb: lavfi", step.output)
        self.assertIn("3 lavfi video test(s) checked", step.output)

    def test_continuation_line_is_touched(self) -> None:
        """Touching any physical registration line checks its logical line."""
        source = FATE_RED.replace("LAVFI_INDEV ", "LAVFI_INDEV \\\n    ")
        step = self.check_source(source, {4})
        self.assertEqual(step.rc, 1, step.output)
        self.assertIn(":3: fate-filter-scale-ipt-c2-refuse-rgb:", step.output)

    def test_conditional_dependency_passes(self) -> None:
        """An enclosing CONFIG decoder condition supplies that dependency."""
        source = FATE_RED.replace(
            "FATE_FILTER-", "ifeq ($(CONFIG_WRAPPED_AVFRAME_DECODER),yes)\nFATE_FILTER-"
        )
        source = source.replace(
            "fate-filter-scale-ipt-c2-refuse-rgb: CMD",
            "endif\nfate-filter-scale-ipt-c2-refuse-rgb: CMD",
        )
        self.assertEqual(self.check_source(source).rc, 0)

    def test_template_command_is_informational(self) -> None:
        """Touched template commands request review without speculative expansion."""
        step = self.check_source("define TEST\n" + FATE_RED + "endef\n$(eval $(call TEST))\n")
        self.assertEqual(step.rc, 0, step.output)
        self.assertIn("template not checked", step.output)
        self.assertIn("0 lavfi video test(s) checked", step.output)

    def test_unknown_and_mixed_sources_are_unclassified(self) -> None:
        """Unknown and mixed sources remain informational."""
        for name in ("unknown", "movie"):
            with self.subTest(name=name):
                step = self.check_source(FATE_RED.replace('"testsrc2=', f'"{name}='))
                self.assertEqual(step.rc, 0, step.output)
                self.assertIn("unclassified lavfi source", step.output)
                self.assertIn("1 unclassified", step.output)

    def test_expansion_bound_keeps_unresolved_token(self) -> None:
        """Self-references terminate and remain unresolved after eight passes."""
        token = upstream_lint.FateToken("$(LOOP)", frozenset({1}))
        self.assertEqual(upstream_lint._fate_expand([token], {"LOOP": [token]}), [token])
        self.assertEqual(upstream_lint._fate_expand([token], {}), [token])

    def test_registration_macros_do_not_imply_dependencies(self) -> None:
        """Recognized registration wrappers require explicit device and decoder deps."""
        for middle in (
            "$(call ALLYES, TESTSRC2_FILTER)",
            "$(call TRANSCODE, RAWVIDEO, NUT, TESTSRC2_FILTER)",
            "$(call FILTERFRAMECRC, TESTSRC2, SCALE_FILTER)",
            "$(call ENCMUX, RAWVIDEO, NUT)",
            "$(if $(CONFIG_SMALL),no,$(call ALLYES, TESTSRC2_FILTER))",
            "yes",
        ):
            with self.subTest(middle=middle):
                source = f"FATE_X-{middle} += fate-x\nfate-x: CMD = -f lavfi -i testsrc2\n"
                step = self.check_source(source)
                self.assertEqual(step.rc, 1, step.output)
                self.assertIn("needs LAVFI_INDEV WRAPPED_AVFRAME_DECODER", step.output)

    def test_missing_registration_is_informational(self) -> None:
        """A touched video command without a local registration requests review."""
        step = self.check_source("fate-x: CMD = -f lavfi -i testsrc2\n")
        self.assertEqual(step.rc, 0, step.output)
        self.assertIn("registration not found", step.output)

    def test_multiple_registrations_are_checked(self) -> None:
        """A good registration cannot mask a second registration with a gap."""
        source = FATE_RED + "FATE_X-yes += fate-filter-scale-ipt-c2-refuse-rgb\n"
        step = self.check_source(source, {5})
        self.assertEqual(step.output.count("finding:"), 1)
        self.assertIn("pre-existing (untouched)", step.output)

    def test_options_between_format_and_input(self) -> None:
        """Input options and quoted option values preserve source classification."""
        source = FATE_RED.replace(
            "-f lavfi -i", '-f lavfi -thread_queue_size 8 -dumpgraph "a b" -i'
        )
        self.assertEqual(self.check_source(source).rc, 1)

    def test_lavfi_input_scan_stays_linear(self) -> None:
        """A long option run with no `-i` resolves promptly, not by backtracking."""
        command = "-f lavfi " + '-opt "" ' * 5000 + "-vf scale"
        self.assertEqual(upstream_lint._lavfi_inputs(command), [])

    def test_lavfi_input_scan_reads_each_source(self) -> None:
        """The linear scan returns every `-f lavfi` input across option runs."""
        command = '-f lavfi -flags +x -i "testsrc2=s=2x2" -f lavfi -i sine=d=1'
        self.assertEqual(upstream_lint._lavfi_inputs(command), ["testsrc2", "sine"])

    def test_assignment_replacement_and_nested_expansion(self) -> None:
        """Replacement removes stale targets while nested references retain provenance."""
        source = (
            "OLD = fate-old\nOLD := fate-x\nGROUP = $(OLD)\n"
            "FATE_X-yes += $(GROUP)\n$(GROUP): CMD = -f lavfi -i testsrc2\n"
        )
        step = self.check_source(source, {2})
        self.assertEqual(step.rc, 1, step.output)
        self.assertNotIn("fate-old", step.output)
        self.assertIn("1 lavfi video test(s) checked", step.output)

    def test_catalog_declarations_define_source_kinds(self) -> None:
        """Only FFFilter source declarations supply video, audio and mixed names."""
        self.assertEqual(upstream_lint._fate_sources(FILTER_DECLARATIONS), FATE_SOURCES)
        self.assertEqual(upstream_lint._fate_sources("extern const AVFilter ff_vsrc_fake;"), {})

    def test_expansion_stops_at_eight_passes(self) -> None:
        """A chain longer than eight expansions retains its unresolved reference."""
        variables = {
            f"V{index}": [upstream_lint.FateToken(f"$(V{index + 1})", frozenset())]
            for index in range(10)
        }
        result = upstream_lint._fate_expand(
            [upstream_lint.FateToken("$(V0)", frozenset())], variables
        )
        self.assertEqual(result[0].text, "$(V8)")

    def test_explicit_targets_share_command(self) -> None:
        """A multi-target command checks each registered test separately."""
        source = "FATE_X-yes += fate-a fate-b\nfate-a fate-b: CMD = -f lavfi -i testsrc2\n"
        step = self.check_source(source)
        self.assertEqual(step.output.count("finding:"), 2)
        self.assertIn("2 lavfi video test(s) checked", step.output)

    def test_nested_conditions_restore_outer_dependencies(self) -> None:
        """Closing inner conditions preserves only still-enclosing dependency tokens."""
        source = (
            "ifdef CONFIG_LAVFI_INDEV\n"
            "ifndef CONFIG_SMALL\n"
            "ifeq ($(CONFIG_WRAPPED_AVFRAME_DECODER),yes)\n"
            "FATE_X-yes += fate-a\nendif\nelse\n"
            "FATE_X-yes += fate-b\nendif\nendif\n"
            "fate-a fate-b: CMD = -f lavfi -i testsrc2\n"
        )
        step = self.check_source(source)
        self.assertEqual(step.output.count("finding:"), 1)
        self.assertIn("fate-b: lavfi video input needs WRAPPED_AVFRAME_DECODER", step.output)

    def test_unrecognized_lines_are_ignored(self) -> None:
        """Only allow-listed statements can register or assign commands."""
        source = "# fate-x: CMD = -f lavfi -i testsrc2\ninclude other.mak\n"
        source += "FATE_X-no += fate-x\nfate-x: CMD = -f lavfi -i testsrc2\n"
        step = self.check_source(source)
        self.assertEqual(step.rc, 0, step.output)
        self.assertIn("registration not found", step.output)

    def test_branch_polarity_and_else(self) -> None:
        """Only the true dependency branch grants guards, including chained else."""
        registration = "FATE_X-$(call ALLYES, LAVFI_INDEV) += fate-x\n"
        command = "fate-x: CMD = -f lavfi -i testsrc2\n"
        cases = (
            ("ifdef CONFIG_WRAPPED_AVFRAME_DECODER", "else\n", 1),
            ("ifndef CONFIG_WRAPPED_AVFRAME_DECODER", "", 1),
            ("ifneq ($(CONFIG_WRAPPED_AVFRAME_DECODER),yes)", "", 1),
            ("ifeq ($(CONFIG_WRAPPED_AVFRAME_DECODER),)", "", 1),
            ("ifndef CONFIG_WRAPPED_AVFRAME_DECODER", "else\n", 0),
            ("ifneq ($(CONFIG_WRAPPED_AVFRAME_DECODER),)", "", 0),
            ("ifeq ($(CONFIG_WRAPPED_AVFRAME_DECODER),maybe)", "", 1),
            ("ifdef CONFIG_SMALL", "else ifeq ($(CONFIG_WRAPPED_AVFRAME_DECODER),yes)\n", 0),
        )
        for condition, branch, rc in cases:
            with self.subTest(condition=condition, branch=branch):
                source = condition + "\n" + branch + registration + "endif\n" + command
                self.assertEqual(self.check_source(source).rc, rc)
        source = "ifdef CONFIG_SMALL\nelse ifdef CONFIG_WRAPPED_AVFRAME_DECODER\n"
        source += registration + "endif\n" + registration + command
        self.assertEqual(self.check_source(source).rc, 1)

    def test_positive_registration_guard_allowlist(self) -> None:
        """Negative and unknown guards cannot supply dependencies from conditions."""
        command = "fate-x: CMD = -f lavfi -i testsrc2\n"
        cases = (
            ("$(if $(CONFIG_WRAPPED_AVFRAME_DECODER),no,yes)", 1, False),
            (
                "$(if $(CONFIG_SMALL),no,$(call ALLYES, WRAPPED_AVFRAME_DECODER LAVFI_INDEV))",
                0,
                False,
            ),
            ("$(if $(CONFIG_SMALL),$(call ALLYES, WRAPPED_AVFRAME_DECODER LAVFI_INDEV))", 0, False),
            ("$(mystery WRAPPED_AVFRAME_DECODER LAVFI_INDEV)", 1, True),
        )
        for guard, rc, unknown in cases:
            with self.subTest(guard=guard):
                step = self.check_source(f"FATE_X-{guard} += fate-x\n" + command)
                self.assertEqual(step.rc, rc, step.output)
                self.assertEqual("registration guard not classified" in step.output, unknown)
                if guard.startswith("$(if $(CONFIG_WRAPPED"):
                    self.assertIn("needs LAVFI_INDEV WRAPPED_AVFRAME_DECODER", step.output)
        macros = upstream_lint._fate_macros(
            "YES = $(call ALLYES, $(1))\nNO = $(call ALLYES, $(1))\nNO = no\n"
        )
        self.assertEqual(macros, frozenset({"YES"}))
        source = "FATE_X-$(call YES, LAVFI_INDEV WRAPPED_AVFRAME_DECODER) += fate-x\n"
        report = upstream_lint._fate_file(
            "tests/fate/x.mak",
            source + command,
            {1, 2},
            FATE_SOURCES,
            upstream_lint.FateContext(macros=macros),
        )
        self.assertEqual(upstream_lint._fate_summary(report).rc, 0)

    def test_trailing_comments_supply_no_dependencies(self) -> None:
        """Unescaped make comments grant nothing while escaped hashes survive."""
        source = "FATE_X-$(call ALLYES, LAVFI_INDEV) # WRAPPED_AVFRAME_DECODER += fate-x\n"
        step = self.check_source(source + "fate-x: CMD = -f lavfi -i testsrc2\n")
        self.assertNotIn("finding:", step.output)
        source = "ifdef CONFIG_LAVFI_INDEV # WRAPPED_AVFRAME_DECODER\n"
        source += "FATE_X-yes += fate-x\nendif\nfate-x: CMD = -f lavfi -i testsrc2\n"
        step = self.check_source(source)
        self.assertEqual(step.rc, 1)
        self.assertIn("needs WRAPPED_AVFRAME_DECODER", step.output)
        lines = upstream_lint._fate_lines("X = a\\#b # WRAPPED_AVFRAME_DECODER\n")
        self.assertEqual(lines[0].text, "X = a\\#b")

    def test_variable_dependencies_and_assignment_flavor(self) -> None:
        """Embedded dependency variables expand and simple assignments freeze values."""
        source = "DEPS = WRAPPED_AVFRAME_DECODER LAVFI_INDEV\n"
        source += "FATE_X-$(call ALLYES, $(DEPS)) += fate-x\nfate-x: CMD = -f lavfi -i testsrc2\n"
        self.assertEqual(self.check_source(source).rc, 0)
        source = "OLD = fate-old\nG := $(OLD)\nG += $(OLD)\nOLD = fate-new\n"
        source += "FATE_X-yes += $(G)\nfate-old: CMD = -f lavfi -i testsrc2\n"
        parsed = upstream_lint._fate_parse(source)
        self.assertIn("fate-old", parsed.registrations)
        self.assertNotIn("fate-new", parsed.registrations)
        source = "OLD = fate-old\nG = $(OLD)\nG += $(OLD)\nOLD = fate-new\n"
        source += "FATE_X-yes += $(G)\nfate-new: CMD = -f lavfi -i testsrc2\n"
        self.assertIn("fate-new", upstream_lint._fate_parse(source).registrations)
        source = "DEPS = LAVFI_INDEV WRAPPED_AVFRAME_DECODER\n"
        source += "FATE_X-$(call ALLYES,$(DEPS)) += fate-x\nfate-x: CMD = -f lavfi -i testsrc2\n"
        self.assertEqual(self.check_source(source).rc, 0)

    def test_total_expansion_budget(self) -> None:
        """Doubling expansions stop before allocating beyond the token budget."""
        source = "V0 = fate-x\n"
        source += "".join(f"V{i} = " + f"$(V{i - 1}) " * 8 + "\n" for i in range(1, 8))
        source += "FATE_X-yes += $(V7)\nfate-x: CMD = -f lavfi -i testsrc2\n"
        step = self.check_source(source)
        self.assertIn("expansion over 4096 tokens or 65536 characters", step.output)
        source += "FATE_Y-$(call ALLYES, $(V7)) += fate-y\n"
        self.assertIn(
            "expansion over 4096 tokens or 65536 characters", self.check_source(source).output
        )
        token = upstream_lint.FateToken("$(DEPS)", frozenset({1}))
        leaf = upstream_lint.FateToken("fate-x", frozenset({2}))
        overflow: set[int] = set()
        result = upstream_lint._fate_expand([token], {"DEPS": [leaf] * 4096}, overflow)
        self.assertEqual(len(result), 4096)
        self.assertEqual(overflow, set())
        result = upstream_lint._fate_expand([token], {"DEPS": [leaf] * 4097}, overflow)
        self.assertEqual(result, [token])
        self.assertEqual(overflow, {1})

    def test_adjacent_substitution_character_budget(self) -> None:
        """Adjacent substitutions and stored values obey an inclusive character budget."""
        budget = getattr(upstream_lint, "FATE_CHAR_BUDGET", 65536)
        message = "expansion over 4096 tokens or 65536 characters"
        # Fail safely on an unfixed parser before attempting the explosive chain.
        oversized = "VALUE := " + "x" * (budget + 1) + "\n"
        self.assertIn(message, self.check_source(oversized).output)
        for operator in (":=", "="):
            with self.subTest(operator=operator):
                source = f"V0 {operator} x\n"
                source += "".join(f"V{i} {operator} $(V{i - 1})$(V{i - 1})\n" for i in range(1, 31))
                if operator == "=":
                    # Cross the character limit without extending the eight-pass cap.
                    source += "V30 := $(V30)\n"
                source += "FATE_X-yes += $(V30)\nfate-x: CMD = -f lavfi -i testsrc2\n"
                self.assertIn(message, self.check_source(source).output)
                parsed = upstream_lint._fate_parse(source)
                self.assertTrue(parsed.overflows)
                self.assertTrue(
                    all(
                        sum(len(token.text) for token in value) <= budget
                        for value in parsed.variables.values()
                    )
                )
                source = f"VALUE {operator} " + "x" * budget + "\n"
                parsed = upstream_lint._fate_parse(source)
                self.assertEqual(
                    [token.text for token in parsed.variables["VALUE"]], ["x" * budget]
                )
                self.assertFalse(parsed.overflows)
                source = source.rstrip("\n") + "x\n"
                parsed = upstream_lint._fate_parse(source)
                self.assertIn(1, parsed.overflows)
                self.assertIn(message, self.check_source(source).output)
                self.assertLessEqual(sum(len(t.text) for t in parsed.variables["VALUE"]), budget)
        half = upstream_lint.FateToken("x" * (budget // 2), frozenset({2}))
        token = upstream_lint.FateToken("$(HALF)$(HALF)", frozenset({1}))
        variables = {"HALF": [half]}
        self.assertEqual(
            upstream_lint._fate_expand_pass([token], variables),
            [upstream_lint.FateToken("x" * budget, frozenset({1, 2}))],
        )
        token = upstream_lint.FateToken(token.text + "x", token.lines)
        self.assertIsNone(upstream_lint._fate_expand_pass([token], variables))
        self.assertIsNone(upstream_lint._fate_expand_pass([half, half], {}))
        overflow: set[int] = set()
        upstream_lint._fate_expand([token], variables, overflow)
        self.assertEqual(overflow, {1})

    def test_simple_assignment_keeps_undefined_reference_unresolved(self) -> None:
        """Simple assignments freeze undefined whole and embedded references permanently."""
        for value in ("fate-$(NAME)", "$(NAME)"):
            with self.subTest(value=value):
                source = f"GROUP := {value}\nNAME = new\nFATE_X-yes += $(GROUP)\n"
                source += "fate-: CMD = -f lavfi -i testsrc2\n"
                parsed = upstream_lint._fate_parse(source)
                self.assertNotIn("fate-new", parsed.registrations)
                self.assertNotIn("new", parsed.registrations)
                frozen = value.replace("$(NAME)", "$(:=NAME)")
                self.assertIn(frozen, parsed.registrations)
                step = self.check_source(source)
                self.assertIn(f"unresolved target {frozen}", step.output)
                self.assertIn("1 unresolved", step.output)
        source = "NAME = old\nGROUP := fate-$(NAME)\nNAME = new\n"
        source += "FATE_X-yes += $(GROUP)\nfate-old: CMD = -f lavfi -i testsrc2\n"
        parsed = upstream_lint._fate_parse(source)
        self.assertIn("fate-old", parsed.registrations)
        self.assertNotIn("fate-new", parsed.registrations)
        self.assertIn("0 unresolved", self.check_source(source).output)

    def test_unresolved_target_is_reported(self) -> None:
        """Touched cross-file target references remain visible and counted."""
        source = "FATE_X-yes += $(FATE_GROUP)\n$(FATE_GROUP): CMD = -f lavfi -i testsrc2\n"
        step = self.check_source(source)
        self.assertIn("unresolved target $(FATE_GROUP)", step.output)
        self.assertIn("2 unresolved", step.output)
        self.assertNotIn("unresolved target", self.check_source(source, set()).output)

    def test_all_command_variants_checked(self) -> None:
        """A later audio command cannot conceal an earlier video variant."""
        source = "FATE_X-yes += fate-x\nifdef CONFIG_SMALL\n"
        source += "fate-x: CMD = -f lavfi -i testsrc2\nelse\n"
        source += "fate-x: CMD = -f lavfi -i sine\nendif\n"
        self.assertEqual(self.check_source(source).rc, 1)
        self.assertEqual(self.check_source(source.replace("testsrc2", "sine")).rc, 0)

    def test_lavfi_options_fragments_and_chain_sources(self) -> None:
        """Flag options, adjacent quotes and every unlabelled chain preserve video."""
        for value in (
            "-re -i testsrc2",
            '-thread_queue_size ""8 -i testsrc2',
            '-i "sine [a]; testsrc2 [v]"',
        ):
            with self.subTest(value=value):
                source = "FATE_X-yes += fate-x\nfate-x: CMD = -f lavfi " + value + "\n"
                self.assertEqual(self.check_source(source).rc, 1)
        command = "-f lavfi " + '-opt ""8 ' * 5000 + '-i "sine [a]; testsrc2 [v]"'
        self.assertEqual(upstream_lint._lavfi_inputs(command), ["sine", "testsrc2"])
        self.assertEqual(upstream_lint._lavfi_inputs('-f lavfi -i "sine; [a]volume"'), ["sine"])
        source = 'FATE_X-yes += fate-x\nfate-x: CMD = -f lavfi -i "unknown; testsrc2"\n'
        step = self.check_source(source)
        self.assertEqual(step.rc, 1)
        self.assertIn("0 unclassified", step.output)

    def test_registration_specific_touch_and_deletions(self) -> None:
        """Sibling edits stay informational and newly video commands or removals block."""
        source = "FATE_X-yes += fate-x\n"
        source += "FATE_Y-$(call ALLYES, LAVFI_INDEV WRAPPED_AVFRAME_DECODER) += fate-x\n"
        source += "fate-x: CMD = -f lavfi -i testsrc2\n"
        step = self.check_source(source, {2})
        self.assertEqual(step.rc, 0, step.output)
        self.assertIn("pre-existing (untouched)", step.output)
        path = "tests/fate/filter-video.mak"
        for base, expected in ((source, 0), (source.replace("-f lavfi", "-f rawvideo"), 1)):
            with self.subTest(base=base):
                head = source.replace("-i testsrc2", "-re -i testsrc2")
                report = upstream_lint._fate_file(
                    path, head, {3}, FATE_SOURCES, upstream_lint.FateContext(base=base)
                )
                self.assertEqual(upstream_lint._fate_summary(report).rc, expected)
        head = "FATE_X-$(call ALLYES, LAVFI_INDEV) += fate-x\nfate-x: CMD = -f lavfi -i testsrc2\n"
        base = head.replace("LAVFI_INDEV", "LAVFI_INDEV WRAPPED_AVFRAME_DECODER")
        report = upstream_lint._fate_file(
            path,
            head,
            set(),
            FATE_SOURCES,
            upstream_lint.FateContext(base=base, deleted=frozenset({1})),
        )
        self.assertEqual(upstream_lint._fate_summary(report).rc, 1)
        for head, touched in (
            ("DEPS = LAVFI_INDEV\nFATE_X-$(call ALLYES, $(DEPS)) += fate-x\n", {1}),
            ("ifndef CONFIG_WRAPPED_AVFRAME_DECODER\nFATE_X-yes += fate-x\nendif\n", {1}),
        ):
            with self.subTest(head=head):
                self.assertEqual(
                    self.check_source(head + "fate-x: CMD = -f lavfi -i testsrc2\n", touched).rc, 1
                )

    def test_planted_mixed_target_path_fails(self) -> None:
        """A touched CMD mixing $(TARGET_PATH) and relative tests/data fails."""
        step = self.check_source(FATE_DASH_MIXED, path="tests/fate/dashenc.mak")
        self.assertEqual(step.rc, 1, step.output)
        self.assertEqual(step.output.count("finding:"), 1)
        self.assertIn(
            "finding: tests/fate/dashenc.mak:2: fate-dash-single-file-trim: "
            "tests/data/dash_single_file_trim named with and without $(TARGET_PATH)/; "
            "run every access through $(TARGET_PATH)/tests/data/dash_single_file_trim",
            step.output,
        )
        self.assertTrue(
            step.output.endswith(
                "FATE dependencies: wrapped_avframe decoder; FATE paths: $(TARGET_PATH)/tests/data"
            )
        )

    def test_all_rooted_target_path_passes(self) -> None:
        """Rooting every tests/data access under $(TARGET_PATH)/ passes."""
        rooted = FATE_DASH_MIXED.replace("$(TARGET_PATH)/", "").replace(
            "tests/data/", "$(TARGET_PATH)/tests/data/"
        )
        step = self.check_source(rooted, path="tests/fate/dashenc.mak")
        self.assertEqual(step.rc, 0, step.output)
        self.assertEqual(step.output.count("finding:"), 0)

    def test_all_relative_tests_data_passes(self) -> None:
        """Single-root relative tests/data accesses without $(TARGET_PATH) pass."""
        relative = FATE_DASH_MIXED.replace("$(TARGET_PATH)/", "")
        self.assertNotIn("$(TARGET_PATH)", relative)
        step = self.check_source(relative, path="tests/fate/dashenc.mak")
        self.assertEqual(step.rc, 0, step.output)
        self.assertEqual(step.output.count("finding:"), 0)

    def test_untouched_mixed_target_path_is_nonfailing(self) -> None:
        """An untouched mixed tests/data command reports pre-existing without failing."""
        step = self.check_source(FATE_DASH_MIXED, set(), path="tests/fate/dashenc.mak")
        self.assertEqual(step.rc, 0, step.output)
        self.assertEqual(step.output.count("finding:"), 0)
        self.assertEqual(step.output.count("pre-existing (untouched):"), 1)
        self.assertIn(
            "pre-existing (untouched): tests/fate/dashenc.mak:2: fate-dash-single-file-trim: "
            "tests/data/dash_single_file_trim named with and without $(TARGET_PATH)/; "
            "run every access through $(TARGET_PATH)/tests/data/dash_single_file_trim",
            step.output,
        )

    def test_distinct_tests_data_directories_pass(self) -> None:
        """Rooted tests/data/a alongside relative tests/data/b in one CMD passes."""
        source = (
            "FATE_X-yes += fate-x\n"
            "fate-x: CMD = mkdir -p tests/data/b && "
            "run $(FFMPEG) -i $(SAMPLES)/x.raw $(TARGET_PATH)/tests/data/a/out.nut\n"
        )
        step = self.check_source(source)
        self.assertEqual(step.rc, 0, step.output)
        self.assertEqual(step.output.count("finding:"), 0)

    def test_relative_tests_data_prefix_boundaries(self) -> None:
        """Allow-listed prefixes count as relative while slash-prefixed roots do not."""
        for prefix in ("", "echo ", "echo\t", "out=", "<", "2>", '"', "'"):
            suffix = prefix if prefix in ('"', "'") else ""
            with self.subTest(prefix=prefix):
                source = (
                    "FATE_X-yes += fate-x\n"
                    f"fate-x: CMD = {prefix}tests/data/x/err.log{suffix} "
                    "$(TARGET_PATH)/tests/data/x/out.mpd\n"
                )
                step = self.check_source(source)
                self.assertEqual(step.rc, 1, step.output)
                self.assertIn("tests/data/x named with and without $(TARGET_PATH)/", step.output)
        for other_root in ("$(SRC_PATH)/tests/data/x", "foo/tests/data/x"):
            with self.subTest(other_root=other_root):
                source = (
                    "FATE_X-yes += fate-x\n"
                    f"fate-x: CMD = run $(FFMPEG) -i {other_root} $(TARGET_PATH)/tests/data/x\n"
                )
                clean = self.check_source(source)
                self.assertEqual(clean.rc, 0, clean.output)

    def test_cleanfiles_relative_next_to_rooted_cmd_passes(self) -> None:
        """Relative paths in CLEANFILES do not conflict with a rooted CMD."""
        rooted = FATE_DASH_MIXED.replace("$(TARGET_PATH)/", "").replace(
            "tests/data/", "$(TARGET_PATH)/tests/data/"
        )
        cleanfiles = (
            "CLEANFILES += tests/data/dash_single_file_trim/out.mpd "
            "tests/data/dash_single_file_trim/err.log "
            "tests/data/dash_single_file_trim/media.mp4\n"
        )
        step = self.check_source(rooted + cleanfiles, path="tests/fate/dashenc.mak")
        self.assertEqual(step.rc, 0, step.output)
        self.assertEqual(step.output.count("finding:"), 0)

    def test_two_mixed_directories_in_one_command_give_two_findings(self) -> None:
        """Each mixed tests/data directory in one CMD produces its own finding."""
        source = (
            "FATE_X-yes += fate-x\n"
            "fate-x: CMD = mkdir -p tests/data/dir_a tests/data/dir-b.1 && "
            "run $(FFMPEG) $(TARGET_PATH)/tests/data/dir_a/a.nut "
            "$(TARGET_PATH)/tests/data/dir-b.1/b.nut\n"
        )
        step = self.check_source(source)
        self.assertEqual(step.rc, 1, step.output)
        self.assertEqual(step.output.count("finding:"), 2)
        self.assertIn("tests/data/dir-b.1 named with and without $(TARGET_PATH)/", step.output)
        self.assertIn("tests/data/dir_a named with and without $(TARGET_PATH)/", step.output)

    def test_plain_mixed_command_needs_no_filter_catalog(self) -> None:
        """Plain and unresolved-target CMDs fail on mixed paths with an empty source map."""
        for target in ("fate-plain", "$(FATE_PLAIN)"):
            with self.subTest(target=target):
                source = (
                    f"FATE_X-yes += {target}\n"
                    f"{target}: CMD = mkdir -p tests/data/plain && "
                    "run $(FFMPEG) -i $(SAMPLES)/x.raw $(TARGET_PATH)/tests/data/plain/out.nut\n"
                )
                step = self.check_source(source, path="tests/fate/plain.mak", sources={})
                self.assertEqual(step.rc, 1, step.output)
                self.assertEqual(step.output.count("finding:"), 1)
                self.assertIn(
                    f"finding: tests/fate/plain.mak:2: {target}: "
                    "tests/data/plain named with and without $(TARGET_PATH)/",
                    step.output,
                )

    def test_distinct_tests_data_name_continuations_pass(self) -> None:
        """Distinct tests/data names sharing a prefix before + pass in both root orders."""
        for command in (
            "cat tests/data/x/log $(TARGET_PATH)/tests/data/x+extra/out",
            "cat tests/data/x+extra/log $(TARGET_PATH)/tests/data/x/out",
        ):
            with self.subTest(command=command):
                step = self.check_source(f"FATE_X-yes += fate-x\nfate-x: CMD = {command}\n")
                self.assertEqual(step.rc, 0, step.output)
                self.assertNotIn("finding:", step.output)

    def test_tests_data_boundary_allowlist(self) -> None:
        """Each allow-listed boundary after tests/data/x still flags mixed-root access."""
        cases = (
            ("/", "cat tests/data/x/log $(TARGET_PATH)/tests/data/x/out"),
            (" ", "cat tests/data/x $(TARGET_PATH)/tests/data/x end"),
            ("\t", "cat tests/data/x\t$(TARGET_PATH)/tests/data/x\tend"),
            ('"', 'cat "tests/data/x" "$(TARGET_PATH)/tests/data/x"'),
            ("'", "cat 'tests/data/x' '$(TARGET_PATH)/tests/data/x'"),
            (";", "echo tests/data/x; echo $(TARGET_PATH)/tests/data/x; true"),
            ("|", "echo tests/data/x| cat $(TARGET_PATH)/tests/data/x| cat"),
            (")", "(echo tests/data/x) (echo $(TARGET_PATH)/tests/data/x)"),
            ("<", "cat tests/data/x<in $(TARGET_PATH)/tests/data/x<in"),
            (">", "cat tests/data/x>out $(TARGET_PATH)/tests/data/x>out"),
            ("end-rooted", "cat tests/data/x/log $(TARGET_PATH)/tests/data/x"),
            ("end-relative", "cat $(TARGET_PATH)/tests/data/x/out tests/data/x"),
        )
        for boundary, command in cases:
            with self.subTest(boundary=boundary):
                step = self.check_source(f"FATE_X-yes += fate-x\nfate-x: CMD = {command}\n")
                self.assertEqual(step.rc, 1, step.output)
                self.assertIn("tests/data/x named with and without $(TARGET_PATH)/", step.output)

    def test_tests_data_refused_continuations_pass(self) -> None:
        """Non-boundary characters right after x yield no directory match in either form."""
        for char in ("+", "$", ",", ":", "@", "\\"):
            for command in (
                f"cat tests/data/x/log $(TARGET_PATH)/tests/data/x{char}extra/out",
                f"cat tests/data/x{char}extra/log $(TARGET_PATH)/tests/data/x/out",
            ):
                with self.subTest(char=char, command=command):
                    step = self.check_source(f"FATE_X-yes += fate-x\nfate-x: CMD = {command}\n")
                    self.assertEqual(step.rc, 0, step.output)
                    self.assertNotIn("finding:", step.output)

    def test_tests_data_patterns_enforce_name_boundary(self) -> None:
        """Both tests/data patterns match whole directory names and never truncate prefixes."""
        for sample in (
            "$(TARGET_PATH)/tests/data/x+extra/out",
            " tests/data/x+extra/out",
            "$(TARGET_PATH)/tests/data/dir_x+extra/out",
            " tests/data/dir_x+extra/out",
            "$(TARGET_PATH)/tests/data/x\n",
            " tests/data/x\n",
        ):
            with self.subTest(sample=sample):
                self.assertEqual(upstream_lint.FATE_ROOTED_DATA.findall(sample), [])
                self.assertEqual(upstream_lint.FATE_RELATIVE_DATA.findall(sample), [])
        self.assertEqual(
            upstream_lint.FATE_ROOTED_DATA.findall("$(TARGET_PATH)/tests/data/x/out"), ["x"]
        )
        self.assertEqual(upstream_lint.FATE_RELATIVE_DATA.findall(" tests/data/x/out"), ["x"])

    def test_unrelated_deletion_after_mixed_target_path_passes(self) -> None:
        """Deleting an unrelated line after an unchanged mixed-root CMD stays pre-existing."""
        unchanged = (
            "FATE_X-yes += fate-x\n"
            "fate-x: CMD = mkdir -p tests/data/x && "
            "run $(FFMPEG) -i $(SAMPLES)/x.raw $(TARGET_PATH)/tests/data/x/out.nut\n"
        )
        step = self.check_diff(unchanged + "# unrelated line\n", unchanged)
        self.assertEqual(step.rc, 0, step.output)
        self.assertNotIn("finding:", step.output)
        self.assertIn(
            "pre-existing (untouched): tests/fate/x.mak:2: fate-x: "
            "tests/data/x named with and without $(TARGET_PATH)/",
            step.output,
        )

    def test_continuation_deletion_inside_mixed_target_path_fails(self) -> None:
        """Deleting a middle continuation line inside a mixed-root CMD fails."""
        first = (
            "FATE_X-yes += fate-x\n"
            "fate-x: CMD = mkdir -p tests/data/x $(TARGET_PATH)/tests/data/x && \\\n"
        )
        middle = "    echo middle && \\\n"
        last = "    true\n"
        step = self.check_diff(first + middle + last, first + last)
        self.assertEqual(step.rc, 1, step.output)
        self.assertIn(
            "finding: tests/fate/x.mak:2: fate-x: "
            "tests/data/x named with and without $(TARGET_PATH)/",
            step.output,
        )

    def test_added_line_inside_mixed_target_path_fails(self) -> None:
        """Changing one physical line of a mixed-root CMD fails."""
        first = (
            "FATE_X-yes += fate-x\n"
            "fate-x: CMD = mkdir -p tests/data/x $(TARGET_PATH)/tests/data/x && \\\n"
        )
        base = first + "    echo before && \\\n    true\n"
        head = first + "    echo after && \\\n    true\n"
        step = self.check_diff(base, head)
        self.assertEqual(step.rc, 1, step.output)
        self.assertIn(
            "finding: tests/fate/x.mak:2: fate-x: "
            "tests/data/x named with and without $(TARGET_PATH)/",
            step.output,
        )


class DiagnosticCitationTest(unittest.TestCase):
    """Positive, negative and boundary coverage of diagnostic workaround citations."""

    def test_uncited_diagnostic_workaround_returns_flag(self) -> None:
        """An uncited compiler-diagnostic workaround returns the first named flag."""
        message = (
            "avcodec/x: silence -Wmaybe-uninitialized false positive\n\nGCC 14 warns about ctx.\n"
        )
        self.assertEqual(upstream_lint.uncited_diagnostic(message), "-Wmaybe-uninitialized")

    def test_gcc_bugzilla_url_satisfies_citation(self) -> None:
        """A GCC Bugzilla URL cites the named compiler diagnostic."""
        message = (
            "avcodec/x: silence -Wmaybe-uninitialized false positive\n\n"
            "GCC 14 warns about ctx.\n"
            "https://gcc.gnu.org/bugzilla/show_bug.cgi?id=109999\n"
        )
        self.assertIsNone(upstream_lint.uncited_diagnostic(message))

    def test_reproducer_line_satisfies_citation(self) -> None:
        """A Reproducer line with a Compiler Explorer URL cites the diagnostic."""
        message = (
            "avcodec/x: silence -Wmaybe-uninitialized false positive\n\n"
            "GCC 14 warns about ctx.\n"
            "Reproducer: https://godbolt.org/z/abc123\n"
        )
        self.assertIsNone(upstream_lint.uncited_diagnostic(message))

    def test_flag_without_workaround_wording_returns_none(self) -> None:
        """A compiler flag without workaround wording does not require a citation."""
        message = "avcodec/x: build with -Wall\n\nNo behaviour change.\n"
        self.assertIsNone(upstream_lint.uncited_diagnostic(message))

    def test_workaround_wording_without_flag_returns_none(self) -> None:
        """Workaround wording without a compiler diagnostic flag passes."""
        message = "avcodec/x: work around a decoder hang\n"
        self.assertIsNone(upstream_lint.uncited_diagnostic(message))

    def test_signed_off_by_workaround_wording_is_ignored(self) -> None:
        """Signed-off-by trailers cannot supply workaround wording for a body flag."""
        message = "avcodec/x: set flags\n\nUse -Wall.\n\nSigned-off-by: Work Around x@invalid\n"
        self.assertIsNone(upstream_lint.uncited_diagnostic(message))

    def test_empty_reproducer_line_does_not_cite_diagnostic(self) -> None:
        """A Reproducer prefix with nothing after it on the line does not count."""
        prefix = (
            "avcodec/x: silence -Wmaybe-uninitialized false positive\n\nGCC 14 warns about ctx.\n"
        )
        for trailer in ("Reproducer:", "Reproducer:\n", "Reproducer: \t\nSee above.\n"):
            with self.subTest(trailer=trailer):
                self.assertEqual(
                    upstream_lint.uncited_diagnostic(prefix + trailer), "-Wmaybe-uninitialized"
                )

    def test_hash_flag_detected(self) -> None:
        """Clang flags containing a hash in the suffix are detected."""
        message = "avutil/x: silence -W#warnings\n"
        self.assertEqual(upstream_lint.uncited_diagnostic(message), "-W#warnings")

    def test_uppercase_flag_detected(self) -> None:
        """Clang flags containing uppercase letters in the suffix are detected."""
        message = "avutil/x: silence -WCFString-literal\n"
        self.assertEqual(upstream_lint.uncited_diagnostic(message), "-WCFString-literal")

    def test_plus_flag_detected_whole(self) -> None:
        """Flags containing plus signs return the whole flag token."""
        message = "avcodec/x: silence -Wc++11-compat\n"
        self.assertEqual(upstream_lint.uncited_diagnostic(message), "-Wc++11-compat")

    def test_lowercase_w_not_a_flag(self) -> None:
        """Lowercase -w is a general compiler option, not a named diagnostic flag."""
        message = "avcodec/x: silence warnings with -w\n"
        self.assertIsNone(upstream_lint.uncited_diagnostic(message))

    def test_wrapped_false_positive_detected(self) -> None:
        """Line-wrapped false positive wording still triggers the citation check."""
        message = (
            "avcodec/x: avoid -Wmaybe-uninitialized\n\nGCC reports a false\npositive for ctx.\n"
        )
        self.assertEqual(upstream_lint.uncited_diagnostic(message), "-Wmaybe-uninitialized")

    def test_wrapped_work_around_detected(self) -> None:
        """Line-wrapped work around wording still triggers the citation check."""
        message = (
            "avcodec/x: init ctx for -Wmaybe-uninitialized\n\nThis is a work\naround for GCC 14.\n"
        )
        self.assertEqual(upstream_lint.uncited_diagnostic(message), "-Wmaybe-uninitialized")


class ClaimScopeTest(unittest.TestCase):
    """Positive, negative and boundary coverage of verification and scope claims."""

    def test_passes_all_fate_tests_claim_detected(self) -> None:
        """Blanket FATE pass claims return the matched verification phrase."""
        message = "avcodec/x: update y\n\nPasses all FATE tests.\n"
        self.assertEqual(upstream_lint.unverified_claim(message), "Passes all FATE tests")

    def test_fully_verified_claim_detected(self) -> None:
        """Blanket verification claims return the matched phrase."""
        message = "avcodec/x: update y\n\nFully verified on x86.\n"
        self.assertEqual(upstream_lint.unverified_claim(message), "Fully verified")

    def test_specific_command_and_platform_passes(self) -> None:
        """Naming the concrete test command and platform is not flagged."""
        message = "avcodec/x: update y\n\nTested with make fate on x86-64 Linux.\n"
        self.assertIsNone(upstream_lint.unverified_claim(message))

    def test_no_regression_test_explanation_passes(self) -> None:
        """Explaining why there is no regression test is not a no-regressions claim."""
        message = "avcodec/x: fix y\n\nNo regression test: the path needs hardware.\n"
        self.assertIsNone(upstream_lint.unverified_claim(message))

    def test_signed_off_by_verification_words_ignored(self) -> None:
        """Signed-off-by trailers are excluded from verification-claim matching."""
        message = "avcodec/x: update y\n\nSigned-off-by: Fully Tested <x@invalid>\n"
        self.assertIsNone(upstream_lint.unverified_claim(message))

    def test_both_loops_scope_claim_detected(self) -> None:
        """A both-loops commit message returns its multi-site scope phrase."""
        message = "avcodec/x: silence a warning in both loops\n"
        self.assertEqual(upstream_lint.scope_claim(message), "both loops")

    def test_all_three_callers_scope_claim_detected(self) -> None:
        """A quantified callers phrase within two words returns the scope match."""
        message = "avcodec/x: check all three callers\n"
        self.assertEqual(upstream_lint.scope_claim(message), "all three callers")

    def test_ordinary_single_site_message_passes(self) -> None:
        """Messages without multi-site quantifiers return None."""
        self.assertIsNone(upstream_lint.scope_claim("avcodec/x: rename y\n"))

    def test_source_hunks_counts_only_non_test_source_files(self) -> None:
        """Source hunks count library sources while ignoring docs, tests and a
        deletion without its old path.
        """
        two_source = "+++ b/libavcodec/x.c\n@@ -3 +3 @@\n-a\n+b\n@@ -9 +9 @@\n-c\n+d\n"
        self.assertEqual(upstream_lint.source_hunks(two_source), 2)
        source_and_doc = (
            "+++ b/libavcodec/x.c\n@@ -3 +3 @@\n-a\n+b\n+++ b/doc/x.texi\n@@ -3 +3 @@\n-c\n+d\n"
        )
        self.assertEqual(upstream_lint.source_hunks(source_and_doc), 1)
        test_and_deleted = (
            "+++ b/libavcodec/tests/x.c\n@@ -3 +3 @@\n-a\n+b\n+++ /dev/null\n@@ -3 +0,0 @@\n-c\n"
        )
        self.assertEqual(upstream_lint.source_hunks(test_and_deleted), 0)

    def test_scope_claim_matches_across_line_breaks(self) -> None:
        """Line-wrapped quantified site phrases still match across newlines."""
        first = "avcodec/x: update y\n\nCheck all three\ncallers before returning.\n"
        self.assertEqual(upstream_lint.scope_claim(first), "all three\ncallers")
        second = "avcodec/x: update y\n\nUpdate both call\nsites in the decoder.\n"
        self.assertEqual(upstream_lint.scope_claim(second), "both call\nsites")

    def test_every_and_each_take_singular_site_nouns(self) -> None:
        """Singular site nouns after every or each return their scope phrase."""
        cases = (
            ("avcodec/x: clamp y in each branch\n", "each branch"),
            ("avcodec/x: free frame on every error path\n", "every error path"),
            ("avcodec/x: check return value at each site\n", "each site"),
        )
        for message, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(upstream_lint.scope_claim(message), expected)

    def test_every_and_each_take_plural_branches(self) -> None:
        """Plural branches after every or each return their scope phrase within two words."""
        cases = (
            ("avcodec/x: clamp y in each of the branches\n", "each of the branches"),
            ("avcodec/x: reset state in each of these branches\n", "each of these branches"),
        )
        for message, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(upstream_lint.scope_claim(message), expected)
        self.assertIsNone(
            upstream_lint.scope_claim("avcodec/x: reset state in every one of these branches\n")
        )

    def test_scope_claim_two_word_boundary(self) -> None:
        """At most two intervening words separate the quantifier from its noun."""
        hit = "avcodec/x: check all three inner callers\n"
        self.assertEqual(upstream_lint.scope_claim(hit), "all three inner callers")
        too_far = "avcodec/x: check all of the three callers\n"
        self.assertIsNone(upstream_lint.scope_claim(too_far))
        non_site = "avcodec/x: both decoders use the same table\n"
        self.assertIsNone(upstream_lint.scope_claim(non_site))

    def test_scope_claim_ignores_signoff_in_any_case(self) -> None:
        """Signed-off-by lines in any letter case are excluded from scope matching."""
        for prefix in ("Signed-off-by", "signed-off-by"):
            with self.subTest(prefix=prefix):
                message = f"avcodec/x: update y\n\n{prefix}: Both Loops <x@invalid>\n"
                self.assertIsNone(upstream_lint.scope_claim(message))

    def test_lowercase_signoff_verification_words_ignored(self) -> None:
        """Lowercase signed-off-by trailers cannot trigger verification claims."""
        message = "avcodec/x: update y\n\nsigned-off-by: Fully Tested <x@invalid>\n"
        self.assertIsNone(upstream_lint.unverified_claim(message))

    def test_negated_and_argued_claims_pass(self) -> None:
        """Negated verification phrases and argued safety statements pass."""
        messages = (
            "avcodec/x: fix y\n\nNo regression testing is possible without hardware.\n",
            "avcodec/x: free buffer\n\nFreeing here is completely safe because p is NULL.\n",
            "avcodec/x: fix y\n\nThis fallback path was never fully tested by FATE.\n",
            "avcodec/x: fix y\n\nThis path has not been fully tested.\n",
            "avcodec/x: fix y\n\nThis path cannot be fully tested without hardware.\n",
            "avcodec/x: fix y\n\nIt is 100% safe since p is NULL.\n",
        )
        for message in messages:
            with self.subTest(message=message):
                self.assertIsNone(upstream_lint.unverified_claim(message))

    def test_negation_must_precede_the_claim(self) -> None:
        """An earlier negation or test note does not excuse a blanket claim."""
        first = "avcodec/x: fix y\n\nThis is not slow and fully tested.\n"
        self.assertEqual(upstream_lint.unverified_claim(first), "fully tested")
        second = "avcodec/x: fix y\n\nNo regression testing; all FATE tests pass.\n"
        self.assertEqual(upstream_lint.unverified_claim(second), "all FATE tests pass")

    def test_more_blanket_claims_detected(self) -> None:
        """Expanded blanket test, verification and regression phrases fail."""
        cases = (
            ("avcodec/x: update y\n\nEvery test passed.\n", "Every test passed"),
            ("avcodec/x: update y\n\nAll unit tests pass.\n", "All unit tests pass"),
            ("avcodec/x: update y\n\nThoroughly verified.\n", "Thoroughly verified"),
            (
                "avcodec/x: optimize loop\n\nRuns 2x faster with no performance regressions.\n",
                "no performance regressions",
            ),
        )
        for message, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(upstream_lint.unverified_claim(message), expected)

    def test_no_regressions_and_100_percent_safe_detected(self) -> None:
        """Unargued no-regressions and 100%-safe claims return their phrase."""
        first = "avcodec/x: fix y\n\nNo regressions.\n"
        self.assertEqual(upstream_lint.unverified_claim(first), "No regressions")
        second = "avcodec/x: fix y\n\n100% safe.\n"
        self.assertEqual(upstream_lint.unverified_claim(second), "100% safe")

    def test_speed_claim_detected(self) -> None:
        """An encode or decode speed claim without an end-to-end figure returns its phrase."""
        cases = (
            (
                "avcodec/x: paint maps once\n\nThe map painter benchmark ran seven rounds, "
                "so encoding is 10x faster.\n",
                "encoding is 10x faster",
            ),
            ("avcodec/x: cache maps\n\nThis gives faster encoding.\n", "faster encoding"),
            ("avcodec/x: cache maps\n\nThis speeds up the encoder.\n", "speeds up the encoder"),
            ("avcodec/x: cache maps\n\nAn end-to-end speedup of 4%.\n", "end-to-end speedup"),
            ("avcodec/x: cache maps\n\nDecoding speed-up for 4K.\n", "Decoding speed-up"),
            ("avcodec/x: cache maps\n\nThe decoder is faster.\n", "decoder is faster"),
        )
        for message, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(upstream_lint.speed_claim(message), expected)

    def test_speed_claim_with_end_to_end_figure_passes(self) -> None:
        """A speed claim next to an fps, -benchmark, speed= or hyperfine figure passes."""
        claim = "avcodec/x: cache maps\n\nEncoding is faster: {}.\n"
        for figure in (
            "120 fps instead of 100 fps",
            "ffmpeg -benchmark reports bench: utime=4.1s",
            "speed=1.5x instead of speed=1.2x",
            "hyperfine shows 4.1 s against 4.5 s",
            "118.5 frames/s",
        ):
            with self.subTest(figure=figure):
                self.assertIsNone(upstream_lint.speed_claim(claim.format(figure)))

    def test_speed_claim_negated_or_off_topic_passes(self) -> None:
        """Negated speed wording and speed words without an encode or decode word pass."""
        for message in (
            "avcodec/x: cache maps\n\nThis does not make encoding faster.\n",
            "avcodec/x: cache maps\n\nThe helper runs without an end-to-end speedup.\n",
            "avcodec/x: paint maps 10x faster\n\nThe map painter is quicker.\n",
            "avcodec/x: cache maps\n\nNothing here changes.\n",
        ):
            with self.subTest(message=message):
                self.assertIsNone(upstream_lint.speed_claim(message))

    def test_speed_negation_must_share_the_sentence(self) -> None:
        """A negation in an earlier sentence does not excuse a later speed claim."""
        message = "avcodec/x: cache maps\n\nThe old path did not cache. Encoding is faster.\n"
        self.assertEqual(upstream_lint.speed_claim(message), "Encoding is faster")

    def test_speed_claim_decimal_in_negation_passes(self) -> None:
        """Decimal points inside numbers do not end the negation window of speed claims."""
        for message in (
            "No 1.5x encoding speedup is claimed.",
            "Not 2.0x faster encoding here.",
            "It does not claim 3.25% encoding speedup.",
        ):
            with self.subTest(message=message):
                self.assertIsNone(upstream_lint.speed_claim(message))

    def test_speed_claim_sentence_end_after_negation_detected(self) -> None:
        """Non-decimal full stops and other sentence marks still end the negation window."""
        cases = (
            ("No change. Encoding speedup of 2x.", "Encoding speedup"),
            ("Not in 1.2. Encoding speedup of 2x.", "Encoding speedup"),
            ("Not in 1.2.3. Encoding speedup of 2x.", "Encoding speedup"),
            ("Not in 1.2.Encoding speedup of 2x.", "Encoding speedup"),
            ("Not 2.Faster encoding now.", "Faster encoding"),
            ("No change in 3._x. Encoding speedup of 2x.", "Encoding speedup"),
            ("Not in 1.2.This gives faster encoding.", "faster encoding"),
            ("Not 2.This gives faster encoding.", "faster encoding"),
            ("No change in 3._x, encoding speedup of 2x.", "encoding speedup"),
            ("Not in x_.2x faster encoding.", "faster encoding"),
            ("Encoding speedup of 1.5x.", "Encoding speedup"),
            ("No .5x encoding speedup is claimed.", "encoding speedup"),
            ("Not here.2x faster encoding.", "faster encoding"),
            ("No 1..5x encoding speedup is claimed.", "encoding speedup"),
            ("No 1;5x encoding speedup is claimed.", "encoding speedup"),
            ("No 1!5x encoding speedup is claimed.", "encoding speedup"),
            ("No 1?5x encoding speedup is claimed.", "encoding speedup"),
        )
        for message, expected in cases:
            with self.subTest(message=message):
                self.assertEqual(upstream_lint.speed_claim(message), expected)

    def test_speed_claim_decimal_negation_boundaries(self) -> None:
        """Window-boundary decimals and multi-dot numbers do not end speed negation."""
        dot_at_start = "Version 1.5 of this encoder patch does not claim any new encoding speedup."
        window_start = dot_at_start.index("encoding speedup") - 48
        self.assertEqual(dot_at_start[window_start - 1 : window_start + 2], "1.5")
        self.assertIsNone(upstream_lint.speed_claim(dot_at_start))
        digit_at_start = (
            "Version 1.5 of this encoder patch does not claim any real encoding speedup."
        )
        after_dot = digit_at_start.index("encoding speedup") - 48
        self.assertEqual(digit_at_start[after_dot - 2 : after_dot + 1], "1.5")
        self.assertIsNone(upstream_lint.speed_claim(digit_at_start))
        for message in (
            "No 1.2.3 encoding speedup is claimed.",
            "Not in 1.2.3 is faster encoding claimed.",
        ):
            with self.subTest(message=message):
                self.assertIsNone(upstream_lint.speed_claim(message))

    def test_speed_claim_finding_names_the_fix(self) -> None:
        """The claim-scope finding for a speed claim asks for an end-to-end figure."""
        findings = upstream_lint._unverified_findings(
            "0123456789abcdef", "avcodec/x: cache maps\n\nEncoding is faster.\n"
        )
        self.assertEqual(
            findings,
            [
                '0123456789ab: claims "Encoding is faster" without an end-to-end measurement; '
                "name an ffmpeg -benchmark or fps result, or drop the claim"
            ],
        )

    def test_source_hunks_reads_headers_outside_hunk_bodies(self) -> None:
        """Added +++ and removed --- C lines inside a hunk do not reset headers."""
        added_plus = (
            "diff --git a/libavcodec/x.c b/libavcodec/x.c\n"
            "--- a/libavcodec/x.c\n"
            "+++ b/libavcodec/x.c\n"
            "@@ -3 +3 @@\n"
            "-a\n"
            "+++ ptr;\n"
            "@@ -9 +9 @@\n"
            "-c\n"
            "+d\n"
        )
        self.assertEqual(upstream_lint.source_hunks(added_plus), 2)
        removed_minus = added_plus.replace("-a\n+++ ptr;\n", "--- ptr;\n+a\n")
        self.assertEqual(upstream_lint.source_hunks(removed_minus), 2)

    def test_source_hunks_counts_deleted_source_files(self) -> None:
        """Deleted library sources count their hunks while deleted tests do not."""
        deleted_and_modified = (
            "diff --git a/libavcodec/old.c b/libavcodec/old.c\n"
            "deleted file mode 100644\n"
            "--- a/libavcodec/old.c\n"
            "+++ /dev/null\n"
            "@@ -1,4 +0,0 @@\n"
            "-int ff_old(void) { return 0; }\n"
            "diff --git a/libavcodec/new.c b/libavcodec/new.c\n"
            "--- a/libavcodec/new.c\n"
            "+++ b/libavcodec/new.c\n"
            "@@ -3 +3 @@\n"
            "-return 1;\n"
            "+return 2;\n"
        )
        self.assertEqual(upstream_lint.source_hunks(deleted_and_modified), 2)
        second = (
            "diff --git a/libavcodec/x.c b/libavcodec/x.c\n"
            "--- a/libavcodec/x.c\n"
            "+++ b/libavcodec/x.c\n"
            "@@ -3 +3 @@\n"
            "-a\n"
            "+b\n"
            "diff --git a/libavcodec/y.c b/libavcodec/y.c\n"
            "--- a/libavcodec/y.c\n"
            "+++ /dev/null\n"
            "@@ -3 +0,0 @@\n"
            "-c\n"
        )
        self.assertEqual(upstream_lint.source_hunks(second), 2)
        deleted_test = second.replace("libavcodec/y.c", "libavcodec/tests/y.c")
        self.assertEqual(upstream_lint.source_hunks(deleted_test), 1)

    def test_source_hunks_decodes_quoted_headers(self) -> None:
        """Quoted non-ASCII source headers count for deleted and changed files alike."""
        deleted = (
            'diff --git "a/libavcodec/caf\\303\\251.c" "b/libavcodec/caf\\303\\251.c"\n'
            "deleted file mode 100644\n"
            '--- "a/libavcodec/caf\\303\\251.c"\n'
            "+++ /dev/null\n"
            "@@ -1 +0,0 @@\n"
            "-int ff_old(void) { return 0; }\n"
        )
        self.assertEqual(upstream_lint.source_hunks(deleted), 1)
        changed = (
            'diff --git "a/libavcodec/caf\\303\\251.c" "b/libavcodec/caf\\303\\251.c"\n'
            '--- "a/libavcodec/caf\\303\\251.c"\n'
            '+++ "b/libavcodec/caf\\303\\251.c"\n'
            "@@ -3 +3 @@\n"
            "-a\n"
            "+b\n"
        )
        self.assertEqual(upstream_lint.source_hunks(changed), 1)
        quoted_test = deleted.replace("libavcodec/caf", "libavcodec/tests/caf")
        self.assertEqual(upstream_lint.source_hunks(quoted_test), 0)

    def test_source_hunks_fftools_added_and_empty(self) -> None:
        """Context-bearing fftools diffs and added files count; empty diffs give 0."""
        fftools = (
            "diff --git a/fftools/ffmpeg.c b/fftools/ffmpeg.c\n"
            "--- a/fftools/ffmpeg.c\n"
            "+++ b/fftools/ffmpeg.c\n"
            "@@ -3,2 +3,2 @@\n"
            " a\n"
            "-b\n"
            "+c\n"
        )
        self.assertEqual(upstream_lint.source_hunks(fftools), 1)
        added_file = (
            "diff --git a/libavcodec/n.c b/libavcodec/n.c\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/libavcodec/n.c\n"
            "@@ -0,0 +1,2 @@\n"
            "+a\n"
            "+b\n"
        )
        self.assertEqual(upstream_lint.source_hunks(added_file), 1)
        self.assertEqual(upstream_lint.source_hunks(""), 0)


class DocTermTest(unittest.TestCase):
    """Positive, negative and boundary coverage of doc terms the added code requires."""

    def test_yuv_wording_for_non_rgb_code_is_found(self) -> None:
        """Added docs saying YUV for a non-RGB code test name the missing case."""
        for code in (
            "    if (s->src_format.color_family != ZIMG_COLOR_RGB &&",
            "    if (!(desc->flags & AV_PIX_FMT_FLAG_RGB))",
        ):
            with self.subTest(code=code):
                findings = upstream_lint.doc_term_findings(code, "kept for YUV input only.")
                self.assertEqual(len(findings), 1)
                self.assertTrue(findings[0].startswith("non-RGB input: added code has "))
                self.assertIn("say non-RGB or name gray", findings[0])

    def test_reversed_operand_non_rgb_code_is_found(self) -> None:
        """A non-RGB test with the RGB constant as the left operand is found as well."""
        for code in (
            "    if (ZIMG_COLOR_RGB != s->src_format.color_family &&",
            "    if (!(AV_PIX_FMT_FLAG_RGB & desc->flags))",
        ):
            with self.subTest(code=code):
                findings = upstream_lint.doc_term_findings(code, "kept for YUV input only.")
                self.assertEqual(len(findings), 1)
                self.assertTrue(findings[0].startswith("non-RGB input: added code has "))
                self.assertIn("say non-RGB or name gray", findings[0])

    def test_range_comparison_without_unspecified_is_found(self) -> None:
        """Added docs naming a range option for a range comparison must say unspecified."""
        code = "        s->src_format.pixel_range != convert_range(in->color_range)))"
        findings = upstream_lint.doc_term_findings(code, "@option{rangein} too,")
        self.assertEqual(len(findings), 1)
        self.assertIn('unspecified range: added code has "pixel_range !="', findings[0])

    def test_named_cases_pass(self) -> None:
        """Docs that say non-RGB, gray or how an unspecified range is treated pass."""
        code = (
            "    if (!(desc->flags & AV_PIX_FMT_FLAG_RGB) && in->color_range != AVCOL_RANGE_JPEG)"
        )
        for docs in (
            "kept for non-RGB input. An unspecified\nrange counts as limited.",
            "kept for YUV and gray input; an unknown range is treated as limited.",
        ):
            with self.subTest(docs=docs):
                self.assertEqual(upstream_lint.doc_term_findings(code, docs), [])

    def test_missing_code_or_wording_passes(self) -> None:
        """No finding without the code behaviour or without the triggering doc wording."""
        for code, docs in (
            ("    return 1;", "kept for YUV input; @option{rangein} too."),
            ("    if (!(desc->flags & AV_PIX_FMT_FLAG_RGB))", ""),
            ("    if (in->color_range == AVCOL_RANGE_JPEG)", "kept for every input."),
            ("    if (s->src_format.color_family == ZIMG_COLOR_RGB)", "kept for YUV input."),
            ("    if (ZIMG_COLOR_RGB == s->src_format.color_family)", "kept for YUV input."),
            ("    if (AV_PIX_FMT_FLAG_RGB & desc->flags)", "kept for YUV input."),
        ):
            with self.subTest(code=code, docs=docs):
                self.assertEqual(upstream_lint.doc_term_findings(code, docs), [])

    def test_doc_path_is_texinfo_under_doc(self) -> None:
        """Only Texinfo files below the top-level doc directory count as docs."""
        self.assertTrue(upstream_lint.is_doc_path("doc/filters.texi"))
        for name in ("filters.texi", "doc/filters.txt", "libavfilter/doc/x.texi"):
            with self.subTest(name=name):
                self.assertFalse(upstream_lint.is_doc_path(name))


WRAP_TEXT = (
    "@section libsvtav1\n"
    "@table @option\n"
    "@item qpfile @var{filename}\n"
    "SVT-AV1 applies the maps only with CRF or constant-QP rate control, so\n"
    "the option is refused unless the effective rate-control mode is 0, and\n"
    "both. Internal\n"
    "scaling, a @samp{superres-mode} other than 0, is refused too, because\n"
    "SVT-AV1 would apply the map to the scaled frame with the full layout.\n"
    "@end table\n"
)


class DocWrapTest(unittest.TestCase):
    """Positive, negative and boundary coverage of early line breaks in added docs."""

    def wrap(self, before: str, line: str, after: str) -> list[str]:
        """Check the middle line of a three-line filled paragraph."""
        return upstream_lint.wrap_findings("doc/x.texi", [before, line, after], {2})

    def test_early_break_is_found(self) -> None:
        """An added line ending a filled paragraph line early names the line and widths."""
        lines = WRAP_TEXT.splitlines()
        findings = upstream_lint.wrap_findings("doc/encoders.texi", lines, {6})
        self.assertEqual(len(findings), 1)
        self.assertTrue(
            findings[0].startswith(
                'doc/encoders.texi:6: "both. Internal" ends a filled paragraph line early (14 of '
            ),
            findings[0],
        )

    def test_unadded_or_full_lines_pass(self) -> None:
        """A short line that was not added and full added lines pass."""
        lines = WRAP_TEXT.splitlines()
        for added in (set(), {4, 5, 7, 8}, {1, 9}, {0, 10}):
            with self.subTest(added=added):
                self.assertEqual(upstream_lint.wrap_findings("doc/x.texi", lines, added), [])

    def test_sentence_end_passes(self) -> None:
        """A short line ending a sentence or forcing a break passes; a comma does not."""
        after = "y " + "z" * 58
        for end in (".", ":", ";", "!", "?", "@*"):
            with self.subTest(end=end):
                self.assertEqual(self.wrap("x" * 60, "a" * 20 + end, after), [])
        self.assertEqual(len(self.wrap("x" * 60, "a" * 20 + ",", after)), 1)

    def test_half_width_boundary(self) -> None:
        """A line under half its neighbours' width fails; exactly half passes."""
        after = "y " + "z" * 58
        self.assertEqual(len(self.wrap("x" * 60, "a" * 29, after)), 1)
        self.assertEqual(self.wrap("x" * 60, "a" * 30, after), [])

    def test_neighbour_width_boundary(self) -> None:
        """Neighbours of WRAP_MIN_NEIGHBOUR columns are checked; one column less is not."""
        width = upstream_lint.WRAP_MIN_NEIGHBOUR
        after = "y " + "z" * (width - 2)
        self.assertEqual(len(self.wrap("x" * width, "a" * 20, after)), 1)
        self.assertEqual(self.wrap("x" * (width - 1), "a" * 20, after), [])

    def test_next_word_fit_boundary(self) -> None:
        """The break fails only when the next word fits on the line before."""
        self.assertEqual(len(self.wrap("x" * 60, "a" * 20, "y" * 39 + " " + "z" * 50)), 1)
        self.assertEqual(self.wrap("x" * 60, "a" * 20, "y" * 40 + " " + "z" * 50), [])

    def test_inline_command_counts_as_one_word(self) -> None:
        """A next line opening with an inline command counts up to its closing brace."""
        after = "@option{svtav1-params}, is refused too, because SVT-AV1 would apply"
        self.assertEqual(len(self.wrap("x" * 60, "a" * 20, after)), 1)
        self.assertEqual(self.wrap("x" * 60, "a" * 37, after), [])

    def test_unlisted_block_passes(self) -> None:
        """Lines inside a block missing from TEXI_FILLED are not filled prose."""
        body = ["x" * 60, "a" * 20, "y " + "z" * 58]
        for block, expected in (("example", 0), ("verbatim", 0), ("itemize", 1), ("table", 1)):
            with self.subTest(block=block):
                lines = [f"@{block}", *body, f"@end {block}"]
                findings = upstream_lint.wrap_findings("doc/x.texi", lines, {3})
                self.assertEqual(len(findings), expected)

    def test_texi_prose_flags(self) -> None:
        """Command lines and lines inside unlisted blocks are not prose; inline starts are."""
        lines = [
            "@table @option",
            "@item a",
            "@code{a} sets b",
            "@c note",
            "",
            "@example",
            "a b",
            "@end example",
            "plain",
            "@end table",
        ]
        expected = [False, False, True, False, False, False, False, False, True, False]
        self.assertEqual(upstream_lint.texi_prose(lines), expected)

    def test_raw_block_end_of_other_block_is_text(self) -> None:
        """An @end line of an outer block inside a raw block is literal text."""
        for opener in ("verbatim", "ignore", "macro foo", "rmacro foo", "linemacro foo"):
            name = opener.split()[0]
            lines = [
                "@table @option",
                "@item a",
                "Text one.",
                f"@{opener}",
                "@end table",
                "literal",
                f"@end {name}",
                "Text after.",
                "@end table",
            ]
            expected = [False, False, True, False, False, False, False, True, False]
            with self.subTest(opener=opener):
                self.assertEqual(upstream_lint.texi_prose(lines), expected)

    def test_raw_block_nesting(self) -> None:
        """A raw block nests only the openers texi2any counts inside it."""
        cases = {
            "same name": ["@verbatim", "@verbatim", "@end verbatim", "a b", "@end verbatim"],
            "indented opener": ["@ignore", "  @ignore", "@end ignore", "a b", "@end ignore"],
            "longer name": ["@verbatim", "@verbatimx", "a b", "@end verbatim"],
            "other raw block": ["@ignore", "@verbatim", "a b", "@end ignore"],
        }
        for outer, inner in (("macro", "linemacro"), ("linemacro", "rmacro"), ("rmacro", "macro")):
            body = [f"@{inner} g", f"@end {outer}", "a b", f"@end {inner}"]
            cases[f"{inner} in {outer}"] = [f"@{outer} f", *body, f"@end {outer}"]
        for name, lines in cases.items():
            with self.subTest(case=name):
                flags = upstream_lint.texi_prose([*lines, "plain"])
                self.assertEqual(flags, [False] * len(lines) + [True])

    def test_raw_block_end_line_boundaries(self) -> None:
        """A raw block ends on @end and its name after leading blanks, before trailing text."""
        cases = {
            "leading blanks": (["@verbatim", "x y", "  @end verbatim", "plain"], 3),
            "trailing text": (["@verbatim", "x y", "@end verbatim @c note", "plain"], 3),
            "longer name": (["@verbatim", "@end verbatim-x", "x y", "@end verbatim", "p"], 4),
        }
        for name, (lines, first_prose) in cases.items():
            with self.subTest(case=name):
                expected = [index >= first_prose for index in range(len(lines))]
                self.assertEqual(upstream_lint.texi_prose(lines), expected)

    CODE_PARAGRAPH = (
        "@code{x} starts a prose line that is long enough to count",
        "Each neighbour in this filled paragraph is sixty-six columns wide,",
        "short",
        "and the following prose line is also sixty-six columns wide here.",
    )

    def test_raw_block_end_adds_no_block_name(self) -> None:
        """An @end inside a raw block adds no block name; one outside still does."""
        for opener in ("verbatim", "ignore", "macro foo", "rmacro foo", "linemacro foo"):
            name = opener.split()[0]
            lines = [f"@{opener}", "@end code", f"@end {name}", "", *self.CODE_PARAGRAPH]
            with self.subTest(opener=opener):
                self.assertEqual(upstream_lint.texi_prose(lines), [False] * 4 + [True] * 4)
                findings = upstream_lint.wrap_findings("doc/x.texi", lines, {7})
                self.assertEqual(len(findings), 1)
                self.assertIn('"short" ends a filled paragraph line early', findings[0])
        outside = ["@end code", "", *self.CODE_PARAGRAPH]
        self.assertEqual(upstream_lint.texi_prose(outside), [False] * 6)
        self.assertEqual(upstream_lint.wrap_findings("doc/x.texi", outside, {5}), [])

    def test_nested_and_mixed_raw_block_end_names(self) -> None:
        """Nested raw blocks hide inner @end names unless an outside @end names them."""
        nested = {
            "verbatim": [
                "@verbatim",
                "@verbatim",
                "@end code",
                "@end verbatim",
                "@end code",
                "@end verbatim",
            ],
            "ignore": [
                "@ignore",
                "  @ignore",
                "@end code",
                "@end ignore",
                "@end code",
                "@end ignore",
            ],
            "macro": [
                "@macro f",
                "@rmacro g",
                "@end macro",
                "@end code",
                "@end rmacro",
                "@end code",
                "@end macro",
            ],
        }
        for case, raw in nested.items():
            lines = [*raw, "", *self.CODE_PARAGRAPH]
            short_line = len(raw) + 4
            with self.subTest(case=case):
                self.assertEqual(
                    upstream_lint.texi_prose(lines), [False] * (len(raw) + 1) + [True] * 4
                )
                findings = upstream_lint.wrap_findings("doc/x.texi", lines, {short_line})
                self.assertEqual(len(findings), 1)
        mixed = (
            (
                "after",
                ["@verbatim", "@end code", "@end verbatim", "", *self.CODE_PARAGRAPH, "@end code"],
                7,
            ),
            (
                "before",
                ["@ignore", "@end code", "@end ignore", "@end code", "", *self.CODE_PARAGRAPH],
                8,
            ),
        )
        for case, lines, short_line in mixed:
            with self.subTest(case=case):
                self.assertEqual(upstream_lint.texi_prose(lines), [False] * len(lines))
                findings = upstream_lint.wrap_findings("doc/x.texi", lines, {short_line})
                self.assertEqual(findings, [])


class TestProgramTest(unittest.TestCase):
    """Positive, negative and boundary coverage of new registered lib*/tests programs."""

    def test_registered_new_program_is_found(self) -> None:
        """A new lib*/tests C file listed by a TESTPROGS assignment of its Makefile is found."""
        for makefile in (
            "TESTPROGS-$(CONFIG_ITUT_T35)              += aom_film_grain\n",
            "TESTPROGS = avcodec                                    \\\n"
            "            aom_film_grain                             \\\n"
            "            golomb\n",
            "TESTPROGS += aom_film_grain # needs a sample\n",
            "TESTPROGS-yes += golomb aom_film_grain\n",
            "TESTPROGS := golomb aom_film_grain\n",
        ):
            with self.subTest(makefile=makefile):
                found = upstream_lint.registered_test_programs(
                    ["libavcodec/tests/aom_film_grain.c"], {"libavcodec/Makefile": makefile}
                )
                self.assertEqual(found, ["libavcodec/tests/aom_film_grain.c"])

    def test_unregistered_or_other_library_passes(self) -> None:
        """No finding without the stem as a TESTPROGS word in the same library Makefile."""
        program = ["libavcodec/tests/aom_film_grain.c"]
        for path, makefile in (
            ("libavutil/Makefile", "TESTPROGS += aom_film_grain\n"),
            ("libavcodec/Makefile", "TESTPROGS += aom_film_grain_sets\n"),
            ("libavcodec/x86/Makefile", "TESTPROGS += aom_film_grain\n"),
            ("libavcodec/Makefile", "OBJS-$(CONFIG_ITUT_T35) += aom_film_grain.o\n"),
            ("libavcodec/Makefile", "OBJS += aom_film_grain.o\n"),
            ("libavcodec/Makefile", "# TESTPROGS += aom_film_grain\n"),
            ("libavcodec/Makefile", "TESTPROGS += golomb # aom_film_grain next\n"),
            ("libavcodec/Makefile", "TESTPROGS += golomb # see \\\naom_film_grain\n"),
            ("libavcodec/Makefile", "TOOLS = golomb \\\n    aom_film_grain\n"),
        ):
            with self.subTest(path=path, makefile=makefile):
                found = upstream_lint.registered_test_programs(program, {path: makefile})
                self.assertEqual(found, [])
        self.assertEqual(upstream_lint.registered_test_programs(program, {}), [])

    def test_testprogs_names_reads_only_testprogs_words(self) -> None:
        """TESTPROGS words come from joined logical lines with make comments dropped."""
        makefile = (
            "OBJS += golomb.o\n"
            "TESTPROGS = avcodec \\\n"
            "            jpeg2000dwt # utf8 \\\n"
            "            bitstream_be\n"
            "TESTPROGSX += x\n"
            "TESTPROGS-$(CONFIG_GOLOMB) += golomb\n"
        )
        self.assertEqual(
            upstream_lint.testprogs_names(makefile), frozenset({"avcodec", "jpeg2000dwt", "golomb"})
        )
        self.assertEqual(upstream_lint.testprogs_names(""), frozenset())

    def test_test_program_path_is_c_under_lib_tests(self) -> None:
        """Only C files directly below a lib*/tests directory count as test programs."""
        self.assertTrue(upstream_lint.is_test_program_path("libavcodec/tests/x.c"))
        for name in (
            "libavcodec/tests/x.h",
            "libavcodec/tests/sub/x.c",
            "tests/api/x.c",
            "libavcodec/x.c",
            "tools/tests/x.c",
        ):
            with self.subTest(name=name):
                self.assertFalse(upstream_lint.is_test_program_path(name))


class FixtureTest(unittest.TestCase):
    """Shared disposable Git fixture; stub the pre-commit subprocess only."""

    def setUp(self) -> None:
        """Create a minimal committed base without inheriting template hooks."""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name) / "tree"
        self.root.mkdir()
        template = Path(directory.name) / "template"
        template.mkdir()
        self.git("init", "-q", f"--template={template}")
        self.write(upstream_lint.CONFIG, FAKE_CONFIG)
        self.write("tools/check_commit_msg.sh", FAKE_MESSAGE)
        self.write("tools/patcheck", FAKE_PATCHECK)
        self.write("libavfilter/vf_x.c", SOURCE)
        self.write("libavfilter/allfilters.c", FILTER_DECLARATIONS)
        self.commit("avfilter/vf_x: initial fixture")
        self.base = self.git("rev-parse", "HEAD").strip()
        self.real_run = upstream_lint._run
        self.hook_rc = 0
        self.hook_effect = lambda cwd: None
        self.hook_calls = []

    def git(self, *args: str) -> str:
        """Run bounded fixture Git commands with explicit author and signing settings."""
        result = subprocess.run(  # nosec B603 B607 - fixed Git, fixture-only arguments.
            [
                "git",
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@invalid",
                "-c",
                "commit.gpgsign=false",
                "-c",
                "core.hooksPath=/dev/null",
                *args,
            ],
            cwd=self.root,
            env=isolated_git_environment(),
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        return result.stdout

    def write(self, name, source):
        """Write one fixture path, creating its parent directories."""
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")

    def commit(self, subject="avfilter/vf_x: update y"):
        """Commit fixture changes; never touch the implementation repository."""
        self.git("add", ".")
        self.git("commit", "-qm", subject)

    def command(
        self, argv: list[str], cwd: Path, data: str | None = None, timeout: float = 60
    ) -> upstream_lint.RunResult:
        """Intercept only pre-commit; execute the real bounded runner otherwise."""
        if Path(argv[0]).name == upstream_lint.PRE_COMMIT:
            self.hook_calls.append((argv, Path(argv[3]).read_text(encoding="utf-8")))
            self.hook_effect(Path(cwd))
            return upstream_lint.RunResult(tuple(argv), self.hook_rc, "fixture pre-commit\n")
        return self.real_run(argv, cwd, data, timeout)

    def check(self, *args: str) -> tuple[int, str]:
        """Capture the complete gate report with a controlled pre-commit result."""
        output = io.StringIO()
        with (
            unittest.mock.patch.object(
                upstream_lint, "_pre_commit", return_value="/fixture/bin/pre-commit"
            ),
            unittest.mock.patch.object(upstream_lint, "_run", side_effect=self.command),
            contextlib.redirect_stdout(output),
        ):
            rc = upstream_lint.main(["--tree", str(self.root), "--base", self.base, *args])
        return rc, output.getvalue()


class PipelineTest(FixtureTest):
    """Run Git and base tools in a throwaway repository; stub pre-commit only."""

    def test_clean_commit_passes_and_collects_all_steps(self) -> None:
        """A correctly named clean commit passes every collected check."""
        self.write("libavfilter/vf_x.c", SOURCE.replace("return 1", "return 2"))
        self.commit()
        rc, output = self.check()
        self.assertEqual(rc, 0, output)
        for name in (
            "pre-commit",
            "commit-msg",
            "patcheck",
            "developer.texi",
            "fate-deps",
            "regression-test",
            "diagnostic-citation",
            "claim-scope",
            "doc-terms",
            "doc-wrap",
            "test-program",
        ):
            self.assertIn(f"step: {name} rc: 0", output)

    def test_fix_without_test_fails(self) -> None:
        """A source fix commit with no changed test path in the range fails."""
        self.write("libavfilter/vf_x.c", SOURCE.replace("return 1", "return 2"))
        self.commit("avfilter/vf_x: fix y")
        rc, output = self.check()
        self.assertEqual(rc, 1, output)
        self.assertIn("step: regression-test rc: 1", output)
        for name in ("pre-commit", "commit-msg", "patcheck", "developer.texi", "fate-deps"):
            self.assertIn(f"step: {name} rc: 0", output)

    def test_fix_with_test_passes(self) -> None:
        """A source fix accompanied by a test file in the same commit passes."""
        self.write("libavfilter/vf_x.c", SOURCE.replace("return 1", "return 2"))
        self.write("tests/ref/fate/filter-x", "0\n")
        self.commit("avfilter/vf_x: fix y")
        rc, output = self.check()
        self.assertEqual(rc, 0, output)
        self.assertIn("step: regression-test rc: 0", output)

    def test_fix_with_test_in_later_commit_passes(self) -> None:
        """A test added in a separate commit within the same range satisfies the check."""
        self.write("libavfilter/vf_x.c", SOURCE.replace("return 1", "return 2"))
        self.commit("avfilter/vf_x: fix y")
        self.write("tests/ref/fate/filter-x", "0\n")
        self.commit("tests/fate: add filter-x")
        rc, output = self.check()
        self.assertEqual(rc, 0, output)
        self.assertIn("step: regression-test rc: 0", output)

    def test_body_fix_without_test_fails(self) -> None:
        """A source commit whose body says fix fails without a changed test path."""
        self.write("libavfilter/vf_x.c", SOURCE.replace("return 1", "return 2"))
        self.commit("avfilter/vf_x: do not delete y\n\nFix this by skipping y.")
        rc, output = self.check()
        self.assertEqual(rc, 1, output)
        self.assertIn("step: regression-test rc: 1", output)
        self.assertIn(": avfilter/vf_x: do not delete y: fixes C or assembly sources", output)

    def test_body_defect_word_without_test_fails(self) -> None:
        """A source commit whose body names a defect fails without a changed test path."""
        self.write("libavfilter/vf_x.c", SOURCE.replace("return 1", "return 2"))
        self.commit("avfilter/vf_x: skip absent y\n\nReading y would misalign every field.")
        rc, output = self.check()
        self.assertEqual(rc, 1, output)
        self.assertIn("step: regression-test rc: 1", output)

    def test_wrapped_body_defect_phrase_without_test_fails(self) -> None:
        """A source commit whose body wraps a defect phrase fails without a changed test path."""
        self.write("libavfilter/vf_x.c", SOURCE.replace("return 1", "return 2"))
        self.commit("avfilter/vf_x: bound y\n\nThe read goes out of\nbounds.")
        rc, output = self.check()
        self.assertEqual(rc, 1, output)
        self.assertIn("step: regression-test rc: 1", output)

    def test_body_without_defect_word_passes(self) -> None:
        """A source commit whose subject and body name no defect needs no test path."""
        self.write("libavfilter/vf_x.c", SOURCE.replace("return 1", "return 2"))
        self.commit("avfilter/vf_x: update y\n\nRename the helper.")
        rc, output = self.check()
        self.assertEqual(rc, 0, output)
        self.assertIn("step: regression-test rc: 0", output)

    def test_body_fix_waiver_passes(self) -> None:
        """A non-blank --no-test-reason waives a source commit whose body says fix."""
        self.write("libavfilter/vf_x.c", SOURCE.replace("return 1", "return 2"))
        self.commit("avfilter/vf_x: do not delete y\n\nFix this by skipping y.")
        rc, output = self.check("--no-test-reason", "hardware-only path")
        self.assertEqual(rc, 0, output)
        self.assertIn("waived: hardware-only path", output)

    def test_fix_waiver_passes(self) -> None:
        """A non-blank --no-test-reason waives a source fix without a test path."""
        self.write("libavfilter/vf_x.c", SOURCE.replace("return 1", "return 2"))
        self.commit("avfilter/vf_x: fix y")
        rc, output = self.check("--no-test-reason", "hardware-only path")
        self.assertEqual(rc, 0, output)
        self.assertIn("waived: hardware-only path", output)

    def test_blank_waiver_fails(self) -> None:
        """Whitespace-only --no-test-reason does not waive a missing regression test."""
        self.write("libavfilter/vf_x.c", SOURCE.replace("return 1", "return 2"))
        self.commit("avfilter/vf_x: fix y")
        rc, output = self.check("--no-test-reason", "  ")
        self.assertEqual(rc, 1, output)
        self.assertIn("step: regression-test rc: 1", output)

    def test_fix_outside_sources_passes(self) -> None:
        """A fix commit touching only documentation does not require a test path."""
        self.write("doc/x.texi", "fixed wording\n")
        self.commit("doc/x: fix a typo")
        rc, output = self.check()
        self.assertEqual(rc, 0, output)
        self.assertIn("step: regression-test rc: 0", output)

    def test_fix_with_unreaching_test_fails(self) -> None:
        """A source fix fails when changed tests name neither its file nor function."""
        self.write("libavfilter/vf_x.c", SOURCE.replace("return 1", "return 2"))
        self.write("libavutil/tests/film_grain_params.c", "int main(void)\n{\n    return 0;\n}\n")
        self.commit("avfilter/vf_x: fix y")
        rc, output = self.check()
        self.assertEqual(rc, 1, output)
        self.assertIn("step: regression-test rc: 1", output)
        self.assertIn("test paths changed but none names ff_value, vf_x, x", output)

    def test_fix_with_test_naming_function_passes(self) -> None:
        """A source fix passes when an added test line names the changed function."""
        self.write("libavfilter/vf_x.c", SOURCE.replace("return 1", "return 2"))
        self.write(
            "libavutil/tests/film_grain_params.c",
            "int main(void)\n{\n    return ff_value();\n}\n",
        )
        self.commit("avfilter/vf_x: fix y")
        rc, output = self.check()
        self.assertEqual(rc, 0, output)
        self.assertIn("step: regression-test rc: 0", output)

    def test_unreached_fix_waiver_passes(self) -> None:
        """A non-blank --no-test-reason waives an unreached source fix commit."""
        self.write("libavfilter/vf_x.c", SOURCE.replace("return 1", "return 2"))
        self.write("libavutil/tests/film_grain_params.c", "int main(void)\n{\n    return 0;\n}\n")
        self.commit("avfilter/vf_x: fix y")
        rc, output = self.check(
            "--no-test-reason",
            "the parser runs only behind a hardware decoder",
        )
        self.assertEqual(rc, 0, output)
        self.assertIn("waived: the parser runs only behind a hardware decoder", output)

    def test_unreached_fix_blank_waiver_fails(self) -> None:
        """Whitespace-only --no-test-reason does not waive an unreached source fix."""
        self.write("libavfilter/vf_x.c", SOURCE.replace("return 1", "return 2"))
        self.write("libavutil/tests/film_grain_params.c", "int main(void)\n{\n    return 0;\n}\n")
        self.commit("avfilter/vf_x: fix y")
        rc, output = self.check("--no-test-reason", "  ")
        self.assertEqual(rc, 1, output)
        self.assertIn("step: regression-test rc: 1", output)

    def test_one_unreached_fix_of_two_fails(self) -> None:
        """Each fix commit in the range must be reached by the changed tests."""
        self.write("libavfilter/vf_x.c", SOURCE.replace("return 1", "return 2"))
        self.commit("avfilter/vf_x: fix y")
        self.write("tests/ref/fate/filter-x", "0\n")
        self.write("libavfilter/vf_y.c", SOURCE.replace("ff_value", "ff_other"))
        self.commit("avfilter/vf_y: fix z")
        rc, output = self.check()
        self.assertEqual(rc, 1, output)
        self.assertIn("avfilter/vf_y: fix z: test paths changed but none names vf_y, y", output)
        self.assertNotIn("avfilter/vf_x: fix y: test paths changed", output)

    def test_fix_with_binary_test_fixture_passes(self) -> None:
        """A source fix passes when a binary test fixture path names the fixed stem."""
        self.write("libavfilter/vf_x.c", SOURCE.replace("return 1", "return 2"))
        self.write("tests/data/filter-x.bin", "\x00\x01\x02")
        self.commit("avfilter/vf_x: fix y")
        rc, output = self.check()
        self.assertEqual(rc, 0, output)
        self.assertIn("step: regression-test rc: 0", output)

    def test_fix_with_quoted_test_path_passes(self) -> None:
        """A source fix passes when a non-ASCII quoted test file names the function."""
        self.write("libavfilter/vf_x.c", SOURCE.replace("return 1", "return 2"))
        self.write("tests/café_x.c", "int main(void)\n{\n    return ff_value();\n}\n")
        self.commit("avfilter/vf_x: fix y")
        rc, output = self.check()
        self.assertEqual(rc, 0, output)
        self.assertIn("step: regression-test rc: 0", output)

    def test_fix_with_noprefix_config_passes(self) -> None:
        """A source fix passes even when repository config sets diff.noprefix=true."""
        self.git("config", "diff.noprefix", "true")
        self.write("libavfilter/vf_x.c", SOURCE.replace("return 1", "return 2"))
        self.write(
            "libavutil/tests/film_grain_params.c",
            "int main(void)\n{\n    return ff_value();\n}\n",
        )
        self.commit("avfilter/vf_x: fix y")
        rc, output = self.check()
        self.assertEqual(rc, 0, output)
        self.assertIn("step: regression-test rc: 0", output)

    def test_uncited_diagnostic_workaround_fails(self) -> None:
        """An uncited compiler-diagnostic workaround fails only its own step."""
        self.write("libavfilter/vf_x.c", SOURCE.replace("return 1", "return 2"))
        self.commit("avfilter/vf_x: silence -Wmaybe-uninitialized false positive")
        rc, output = self.check()
        self.assertEqual(rc, 1, output)
        self.assertIn("step: diagnostic-citation rc: 1", output)
        for name in ("pre-commit", "commit-msg", "patcheck", "developer.texi", "fate-deps"):
            self.assertIn(f"step: {name} rc: 0", output)

    def test_cited_diagnostic_workaround_passes(self) -> None:
        """A compiler-diagnostic workaround with an upstream bug URL passes."""
        self.write("libavfilter/vf_x.c", SOURCE.replace("return 1", "return 2"))
        self.commit(
            "avfilter/vf_x: silence -Wmaybe-uninitialized false positive\n\n"
            "https://gcc.gnu.org/bugzilla/show_bug.cgi?id=109999"
        )
        rc, output = self.check()
        self.assertEqual(rc, 0, output)
        self.assertIn("step: diagnostic-citation rc: 0", output)

    def test_verification_claim_fails(self) -> None:
        """A blanket verification claim fails claim-scope while other steps pass."""
        self.write("libavfilter/vf_x.c", SOURCE.replace("return 1", "return 2"))
        self.commit("avfilter/vf_x: update y\n\nPasses all FATE tests.")
        rc, output = self.check()
        self.assertEqual(rc, 1, output)
        self.assertIn("step: claim-scope rc: 1", output)
        for name in (
            "pre-commit",
            "commit-msg",
            "patcheck",
            "developer.texi",
            "fate-deps",
            "regression-test",
            "diagnostic-citation",
        ):
            self.assertIn(f"step: {name} rc: 0", output)

    def test_verification_claim_is_not_waived(self) -> None:
        """Passing --claim-reason cannot waive a blanket verification claim."""
        self.write("libavfilter/vf_x.c", SOURCE.replace("return 1", "return 2"))
        self.commit("avfilter/vf_x: update y\n\nPasses all FATE tests.")
        rc, output = self.check("--claim-reason", "ran fate")
        self.assertEqual(rc, 1, output)
        self.assertIn("step: claim-scope rc: 1", output)

    def test_single_hunk_scope_claim_fails(self) -> None:
        """A multi-site claim backed by one source hunk fails without a waiver."""
        self.write("libavfilter/vf_x.c", SOURCE.replace("return 1", "return 2"))
        self.commit("avfilter/vf_x: clamp y in both loops")
        rc, output = self.check()
        self.assertEqual(rc, 1, output)
        self.assertIn("step: claim-scope rc: 1", output)

    def test_single_hunk_each_of_the_branches_claim_fails(self) -> None:
        """A claim about each of the branches backed by one source hunk fails without a waiver."""
        self.write("libavfilter/vf_x.c", SOURCE.replace("return 1", "return 2"))
        self.commit("avfilter/vf_x: clamp y in each of the branches")
        rc, output = self.check()
        self.assertEqual(rc, 1, output)
        self.assertIn("step: claim-scope rc: 1", output)
        self.assertIn("changes 1 source hunk", output)

    def test_scope_claim_waiver_passes(self) -> None:
        """A non-blank --claim-reason waives a single-hunk scope claim."""
        self.write("libavfilter/vf_x.c", SOURCE.replace("return 1", "return 2"))
        self.commit("avfilter/vf_x: clamp y in both loops")
        rc, output = self.check("--claim-reason", "the second loop is generated from one macro")
        self.assertEqual(rc, 0, output)
        self.assertIn("waived: the second loop is generated from one macro", output)

    def test_blank_scope_waiver_fails(self) -> None:
        """Whitespace-only --claim-reason does not waive a single-hunk scope claim."""
        self.write("libavfilter/vf_x.c", SOURCE.replace("return 1", "return 2"))
        self.commit("avfilter/vf_x: clamp y in both loops")
        rc, output = self.check("--claim-reason", "  ")
        self.assertEqual(rc, 1, output)
        self.assertIn("step: claim-scope rc: 1", output)

    def test_two_hunk_scope_claim_passes(self) -> None:
        """A multi-site claim backed by two source hunks passes without a waiver."""
        self.write(
            "libavfilter/vf_x.c",
            SOURCE.replace("ff_value", "ff_first") + "\nint ff_other(void)\n{\n    return 3;\n}\n",
        )
        self.commit("avfilter/vf_x: update both functions")
        rc, output = self.check()
        self.assertEqual(rc, 0, output)
        self.assertIn("step: claim-scope rc: 0", output)

    def test_doc_terms_omission_fails(self) -> None:
        """Added docs saying YUV for added non-RGB code fail the doc-terms step."""
        self.write(
            "libavfilter/vf_x.c", SOURCE.replace("1", "!(desc->flags & AV_PIX_FMT_FLAG_RGB)")
        )
        self.write("doc/filters.texi", "@item keep\nKeep the light level for YUV input.\n")
        self.commit("avfilter/vf_x: keep the light level")
        rc, output = self.check()
        self.assertEqual(rc, 1, output)
        self.assertIn("step: doc-terms rc: 1", output)
        self.assertIn(
            'non-RGB input: added code has "!(desc->flags & AV_PIX_FMT_FLAG_RGB)"', output
        )

    def test_doc_terms_waiver_passes(self) -> None:
        """A non-blank --doc-reason waives the doc-terms step and prints the reason."""
        self.write(
            "libavfilter/vf_x.c", SOURCE.replace("1", "!(desc->flags & AV_PIX_FMT_FLAG_RGB)")
        )
        self.write("doc/filters.texi", "@item keep\nKeep the light level for YUV input.\n")
        self.commit("avfilter/vf_x: keep the light level")
        rc, output = self.check("--doc-reason", "gray input is rejected earlier")
        self.assertEqual(rc, 0, output)
        self.assertIn("step: doc-terms rc: 0\nwaived: gray input is rejected earlier", output)

    def test_doc_terms_named_case_passes(self) -> None:
        """Added docs that say non-RGB for added non-RGB code pass the doc-terms step."""
        self.write(
            "libavfilter/vf_x.c", SOURCE.replace("1", "!(desc->flags & AV_PIX_FMT_FLAG_RGB)")
        )
        self.write("doc/filters.texi", "@item keep\nKeep the light level for non-RGB input.\n")
        self.commit("avfilter/vf_x: keep the light level")
        rc, output = self.check()
        self.assertEqual(rc, 0, output)
        self.assertIn("step: doc-terms rc: 0", output)

    def test_doc_terms_comment_only_passes(self) -> None:
        """An added comment that names the RGB test is not added non-RGB code."""
        self.write(
            "libavfilter/vf_x.c",
            SOURCE.replace(
                "    return 1;", "    /* was !(desc->flags & AV_PIX_FMT_FLAG_RGB) */\n    return 1;"
            ),
        )
        self.write("doc/filters.texi", "@item keep\nKeep the light level for YUV input.\n")
        self.commit("avfilter/vf_x: keep the light level")
        rc, output = self.check()
        self.assertEqual(rc, 0, output)
        self.assertIn("step: doc-terms rc: 0", output)

    def test_doc_terms_added_line_inside_comment_passes(self) -> None:
        """An added line inside an existing block comment is read as comment text."""
        self.write("libavfilter/vf_x.c", "/*\n * Light level.\n */\n" + SOURCE)
        self.commit("avfilter/vf_x: describe the light level")
        self.base = self.git("rev-parse", "HEAD").strip()
        self.write(
            "libavfilter/vf_x.c",
            "/*\n * Light level.\n * The old test was !(desc->flags & AV_PIX_FMT_FLAG_RGB).\n */\n"
            + SOURCE,
        )
        self.write("doc/filters.texi", "@item keep\nKeep the light level for YUV input.\n")
        self.commit("avfilter/vf_x: keep the light level")
        rc, output = self.check()
        self.assertEqual(rc, 0, output)
        self.assertIn("step: doc-terms rc: 0", output)

    def test_speed_claim_fails(self) -> None:
        """An encode speed claim without an end-to-end figure fails claim-scope."""
        self.write("libavfilter/vf_x.c", SOURCE.replace("return 1", "return 2"))
        self.commit(
            "avfilter/vf_x: paint maps once\n\n"
            "The map painter benchmark ran seven rounds, so encoding is faster."
        )
        rc, output = self.check()
        self.assertEqual(rc, 1, output)
        self.assertIn("step: claim-scope rc: 1", output)
        self.assertIn('claims "encoding is faster" without an end-to-end measurement', output)

    def test_speed_claim_is_not_waived(self) -> None:
        """Passing --claim-reason cannot waive an encode speed claim."""
        self.write("libavfilter/vf_x.c", SOURCE.replace("return 1", "return 2"))
        self.commit("avfilter/vf_x: paint maps once\n\nEncoding is faster.")
        rc, output = self.check("--claim-reason", "helper benchmark")
        self.assertEqual(rc, 1, output)
        self.assertIn("step: claim-scope rc: 1", output)

    def test_speed_claim_with_fps_passes(self) -> None:
        """An encode speed claim with an fps figure passes claim-scope."""
        self.write("libavfilter/vf_x.c", SOURCE.replace("return 1", "return 2"))
        self.commit("avfilter/vf_x: paint maps once\n\nEncoding runs at 120 fps, not 100 fps.")
        rc, output = self.check()
        self.assertEqual(rc, 0, output)
        self.assertIn("step: claim-scope rc: 0", output)

    def test_doc_wrap_fails(self) -> None:
        """An added doc line that ends a filled paragraph line early fails doc-wrap."""
        self.write("doc/encoders.texi", WRAP_TEXT)
        self.commit("doc/encoders: describe qpfile")
        rc, output = self.check()
        self.assertEqual(rc, 1, output)
        self.assertIn("step: doc-wrap rc: 1", output)
        self.assertIn('doc/encoders.texi:6: "both. Internal" ends a filled paragraph', output)

    def test_doc_wrap_waiver_passes(self) -> None:
        """A non-blank --doc-reason waives the doc-wrap step and prints the reason."""
        self.write("doc/encoders.texi", WRAP_TEXT)
        self.commit("doc/encoders: describe qpfile")
        rc, output = self.check("--doc-reason", "the break keeps a table aligned")
        self.assertEqual(rc, 0, output)
        self.assertIn("step: doc-wrap rc: 0\nwaived: the break keeps a table aligned", output)

    def test_doc_wrap_blank_waiver_fails(self) -> None:
        """A whitespace-only --doc-reason does not waive the doc-wrap step."""
        self.write("doc/encoders.texi", WRAP_TEXT)
        self.commit("doc/encoders: describe qpfile")
        rc, output = self.check("--doc-reason", "  ")
        self.assertEqual(rc, 1, output)
        self.assertIn("step: doc-wrap rc: 1", output)

    def test_doc_wrap_filled_or_unchanged_passes(self) -> None:
        """A filled paragraph passes, and so does an early break the range did not add."""
        self.write("doc/encoders.texi", WRAP_TEXT.replace("both. Internal\n", "both.\n"))
        self.commit("doc/encoders: describe qpfile")
        rc, output = self.check()
        self.assertEqual(rc, 0, output)
        self.assertIn("step: doc-wrap rc: 0", output)
        self.write("doc/encoders.texi", WRAP_TEXT)
        self.commit("doc/encoders: describe qpfile scaling")
        self.base = self.git("rev-parse", "HEAD").strip()
        self.write("doc/encoders.texi", WRAP_TEXT.replace("full layout", "full-size layout"))
        self.commit("doc/encoders: name the layout")
        rc, output = self.check()
        self.assertEqual(rc, 0, output)
        self.assertIn("step: doc-wrap rc: 0", output)

    def test_test_program_fails(self) -> None:
        """A new lib*/tests program registered in its Makefile fails the test-program step."""
        self.write("libavcodec/tests/x_grain.c", "int main(void)\n{\n    return 0;\n}\n")
        self.write("libavcodec/Makefile", "TESTPROGS += x_grain\n")
        self.commit("avcodec/tests: add x_grain")
        rc, output = self.check()
        self.assertEqual(rc, 1, output)
        self.assertIn("step: test-program rc: 1", output)
        self.assertIn("libavcodec/tests/x_grain.c: new TESTPROGS program", output)

    def test_test_program_waiver_passes(self) -> None:
        """A non-blank --test-program-reason waives the test-program step and prints it."""
        self.write("libavcodec/tests/x_grain.c", "int main(void)\n{\n    return 0;\n}\n")
        self.write("libavcodec/Makefile", "TESTPROGS += x_grain\n")
        self.commit("avcodec/tests: add x_grain")
        rc, output = self.check("--test-program-reason", "no sample reaches the payload")
        self.assertEqual(rc, 0, output)
        self.assertIn("step: test-program rc: 0\nwaived: no sample reaches the payload", output)

    def test_test_program_continuation_line_fails(self) -> None:
        """A new program added to a backslash-continued TESTPROGS list fails the step."""
        self.write("libavcodec/Makefile", "TESTPROGS = golomb \\\n            rangecoder\n")
        self.commit("avcodec: list test programs")
        self.base = self.git("rev-parse", "HEAD").strip()
        self.write("libavcodec/tests/x_grain.c", "int main(void)\n{\n    return 0;\n}\n")
        self.write(
            "libavcodec/Makefile",
            "TESTPROGS = golomb \\\n            x_grain \\\n            rangecoder\n",
        )
        self.commit("avcodec/tests: add x_grain")
        rc, output = self.check()
        self.assertEqual(rc, 1, output)
        self.assertIn("libavcodec/tests/x_grain.c: new TESTPROGS program", output)

    def test_test_program_other_makefile_entry_passes(self) -> None:
        """An OBJS entry or a comment naming the new file's stem is not a registration."""
        self.write("libavcodec/tests/x_grain.c", "int main(void)\n{\n    return 0;\n}\n")
        self.write("libavcodec/Makefile", "OBJS += x_grain.o\n# x_grain needs a sample\n")
        self.commit("avcodec/tests: add x_grain")
        rc, output = self.check()
        self.assertEqual(rc, 0, output)
        self.assertIn("step: test-program rc: 0", output)

    def test_unregistered_test_file_passes(self) -> None:
        """A new lib*/tests C file without a head library Makefile passes."""
        self.write("libavcodec/tests/x_grain.c", "int main(void)\n{\n    return 0;\n}\n")
        self.commit("avcodec/tests: add x_grain")
        rc, output = self.check()
        self.assertEqual(rc, 0, output)
        self.assertIn("step: test-program rc: 0", output)

    def test_existing_test_program_edit_passes(self) -> None:
        """Editing a program and Makefile line that exist at base is not a new program."""
        self.write("libavcodec/tests/x_grain.c", "int main(void)\n{\n    return 0;\n}\n")
        self.write("libavcodec/Makefile", "TESTPROGS += x_grain\n")
        self.commit("avcodec/tests: add x_grain")
        self.base = self.git("rev-parse", "HEAD").strip()
        self.write("libavcodec/tests/x_grain.c", "int main(void)\n{\n    return 1;\n}\n")
        self.write("libavcodec/Makefile", "TESTPROGS += golomb x_grain\n")
        self.commit("avcodec/tests: extend x_grain")
        rc, output = self.check()
        self.assertEqual(rc, 0, output)
        self.assertIn("step: test-program rc: 0", output)

    def test_renamed_two_hunk_scope_claim_passes(self) -> None:
        """A renamed file with two source hunks satisfies a multi-site scope claim."""
        grown = "".join(f"int ff_f{i}(void)\n{{\n    return {i};\n}}\n\n" for i in range(8))
        self.write("libavfilter/vf_x.c", grown)
        self.commit("avfilter/vf_x: grow fixture")
        self.base = self.git("rev-parse", "HEAD").strip()
        self.git("mv", "libavfilter/vf_x.c", "libavfilter/vf_y.c")
        updated = grown.replace("return 1;", "return 11;").replace("return 6;", "return 66;")
        self.write("libavfilter/vf_y.c", updated)
        self.commit("avfilter/vf_y: update both functions")
        _, output = self.check()
        self.assertIn("step: claim-scope rc: 0", output)

    def test_renamed_single_hunk_scope_claim_fails(self) -> None:
        """A renamed file with one source hunk still fails a multi-site scope claim."""
        grown = "".join(f"int ff_f{i}(void)\n{{\n    return {i};\n}}\n\n" for i in range(8))
        self.write("libavfilter/vf_x.c", grown)
        self.commit("avfilter/vf_x: grow fixture")
        self.base = self.git("rev-parse", "HEAD").strip()
        self.git("mv", "libavfilter/vf_x.c", "libavfilter/vf_y.c")
        self.write("libavfilter/vf_y.c", grown.replace("return 1;", "return 11;"))
        self.commit("avfilter/vf_y: clamp y in both loops")
        _, output = self.check()
        self.assertIn("step: claim-scope rc: 1", output)
        self.assertIn("changes 1 source hunk", output)

    def test_quoted_source_deletion_scope_claim_fails(self) -> None:
        """Deleting a quoted non-ASCII source path in one hunk fails a multi-site claim."""
        self.write("libavfilter/café.c", "int ff_old(void)\n{\n    return 0;\n}\n")
        self.commit("avfilter/café: add fixture")
        self.base = self.git("rev-parse", "HEAD").strip()
        self.git("rm", "-q", "libavfilter/café.c")
        self.commit("avfilter/café: drop both functions")
        _, output = self.check()
        self.assertIn("step: claim-scope rc: 1", output)
        self.assertIn("changes 1 source hunk", output)

    def test_unverified_claim_with_waived_scope_claim_fails(self) -> None:
        """Waiving a scope claim still fails when the commit makes a blanket claim."""
        self.write("libavfilter/vf_x.c", SOURCE.replace("return 1", "return 2"))
        self.commit("avfilter/vf_x: clamp y in both loops\n\nPasses all FATE tests.")
        rc, output = self.check("--claim-reason", "one macro")
        self.assertEqual(rc, 1, output)
        self.assertIn("step: claim-scope rc: 1", output)
        self.assertIn('claims "Passes all FATE tests"', output)
        self.assertIn("waived: one macro", output)

    def test_claim_scope_commit_cap(self) -> None:
        """Exceeding MAX_CLAIM_COMMITS fails the claim-scope step with rc 2."""
        self.write("libavfilter/vf_x.c", SOURCE.replace("return 1", "return 2"))
        self.commit("avfilter/vf_x: first update")
        self.write("libavfilter/vf_x.c", SOURCE.replace("return 1", "return 3"))
        self.commit("avfilter/vf_x: second update")
        with unittest.mock.patch.object(upstream_lint, "MAX_CLAIM_COMMITS", 1):
            _, output = self.check()
        self.assertIn("step: claim-scope rc: 2", output)
        self.assertIn("MAX_CLAIM_COMMITS=1: 2", output)

    def test_fate_missing_decoder_fails(self) -> None:
        """A committed lavfi video dependency gap fails without losing other steps."""
        self.write("tests/fate/filter-video.mak", FATE_RED)
        self.commit()
        rc, output = self.check()
        self.assertEqual(rc, 1, output)
        self.assertIn("step: fate-deps rc: 1", output)
        for name in ("pre-commit", "commit-msg", "patcheck", "developer.texi"):
            self.assertIn(f"step: {name} rc: 0", output)

    def test_fate_decoder_present_passes(self) -> None:
        """A committed registration with both dependencies passes the pipeline."""
        self.write(
            "tests/fate/filter-video.mak",
            FATE_RED.replace("NULL_MUXER", "WRAPPED_AVFRAME_DECODER NULL_MUXER"),
        )
        self.commit()
        rc, output = self.check()
        self.assertEqual(rc, 0, output)
        self.assertIn("step: fate-deps rc: 0", output)

    def test_fate_mixed_target_path_fails(self) -> None:
        """A committed FATE file with the planted mixed tests/data command fails fate-deps."""
        self.write("tests/fate/dashenc.mak", FATE_DASH_MIXED)
        self.commit("fate/dashenc: add single-file trim test")
        rc, output = self.check()
        self.assertEqual(rc, 1, output)
        self.assertIn("step: fate-deps rc: 1", output)
        self.assertIn(
            "finding: tests/fate/dashenc.mak:2: fate-dash-single-file-trim: "
            "tests/data/dash_single_file_trim named with and without $(TARGET_PATH)/",
            output,
        )

    def test_fate_missing_allfilters_is_error(self) -> None:
        """A needed source catalog missing at head fails the step explicitly."""
        self.git("rm", "libavfilter/allfilters.c")
        self.write("tests/fate/filter-video.mak", FATE_RED)
        self.commit()
        rc, output = self.check()
        self.assertEqual(rc, 1, output)
        self.assertIn("step: fate-deps rc: 2", output)
        self.assertIn("libavfilter/allfilters.c", output)
        self.assertIn("FATE dependencies: wrapped_avframe decoder", output)

    def test_fate_deleted_dependency_continuation(self) -> None:
        """Deleting a dependency continuation blocks until the guard is restored."""
        path = "tests/fate/filter-video.mak"
        good = (
            "FATE_X-$(call ALLYES, LAVFI_INDEV \\\n"
            " WRAPPED_AVFRAME_DECODER \\\n NULL_MUXER) += fate-x\n"
            "fate-x: CMD = -f lavfi -i testsrc2\n"
        )
        self.write(path, good)
        self.commit()
        self.base = self.git("rev-parse", "HEAD").strip()
        self.write(path, good.replace(" WRAPPED_AVFRAME_DECODER \\\n", ""))
        self.commit()
        rc, output = self.check()
        self.assertEqual(rc, 1, output)
        self.assertIn("step: fate-deps rc: 1", output)
        self.write(path, good)
        self.commit()
        rc, output = self.check()
        self.assertEqual(rc, 0, output)

    def test_fate_macro_catalog_and_missing_file(self) -> None:
        """Head macro definitions grant only ALLYES wrappers and missing catalogs fail."""
        self.write("tests/Makefile", "ALLYES = $(strip $(1))\nVIDEO = $(call ALLYES, $(1))\n")
        self.write(
            "tests/fate/filter-video.mak",
            "FATE_X-$(call VIDEO, LAVFI_INDEV WRAPPED_AVFRAME_DECODER) += fate-x\n"
            "fate-x: CMD = -f lavfi -i testsrc2\n",
        )
        self.write(
            "tests/fate/other.mak",
            "GUARD = $(call VIDEO, LAVFI_INDEV WRAPPED_AVFRAME_DECODER)\n"
            "FATE_Y-$(GUARD) += fate-y\nfate-y: CMD = -f lavfi -i testsrc2\n",
        )
        self.commit()
        with unittest.mock.patch.object(upstream_lint, "_git", wraps=upstream_lint._git) as git:
            rc, output = self.check()
        self.assertEqual(rc, 0, output)
        catalog_reads = [
            call for call in git.call_args_list if call.args[-1].endswith(":tests/Makefile")
        ]
        self.assertEqual(len(catalog_reads), 1)
        self.git("rm", "tests/Makefile")
        self.commit()
        rc, output = self.check()
        self.assertEqual(rc, 1, output)
        self.assertIn("step: fate-deps rc: 2", output)
        self.assertIn("tests/Makefile", output)

    def test_bad_subject_fails_without_skipping_other_checks(self):
        """The base commit-message checker rejects the planted bad subject."""
        self.write("libavfilter/vf_x.c", SOURCE.replace("return 1", "return 2"))
        self.commit("fix stuff")
        rc, output = self.check()
        self.assertEqual(rc, 1, output)
        self.assertIn("step: commit-msg rc: 1", output)
        self.assertIn("step: developer.texi rc: 0", output)

    def test_added_tab_and_trailing_space_fail(self):
        """Added C whitespace defects fail even if all upstream tools exit zero."""
        self.write("libavfilter/vf_x.c", SOURCE.replace("    return 1;", "\treturn 2; "))
        self.commit()
        rc, output = self.check()
        self.assertEqual(rc, 1, output)
        self.assertIn("TAB character", output)
        self.assertIn("trailing whitespace", output)

    def test_inherited_tab_is_nonfailing_and_listed(self):
        """A tabbed line inherited from the base stays informational."""
        self.write("libavfilter/vf_x.c", SOURCE.replace("    return 1;", "\treturn 1;"))
        self.commit()
        self.base = self.git("rev-parse", "HEAD").strip()
        self.write(
            "libavfilter/vf_x.c", "/* updated */\n" + self.git("show", "HEAD:libavfilter/vf_x.c")
        )
        self.commit()
        rc, output = self.check()
        self.assertEqual(rc, 0, output)
        self.assertIn("pre-existing (untouched)", output)

    def test_only_c_and_h_added_lines_are_checked(self):
        """Non-C files and removed violations do not enter mechanical rules."""
        self.write("notes.txt", "\ttrailing \n")
        self.write("libavfilter/vf_x.h", "int ff_value(void);\n")
        self.commit()
        self.assertEqual(self.check()[0], 0)

    def test_base_tools_and_config_cannot_be_replaced_by_head(self):
        """A branch cannot silently substitute its own upstream policy files."""
        self.write(upstream_lint.CONFIG, "broken: branch config\n")
        self.write("tools/check_commit_msg.sh", "exit 1\n")
        self.write("tools/patcheck", "exit 1\n")
        self.commit()
        rc, output = self.check()
        self.assertEqual(rc, 0, output)
        self.assertEqual(self.hook_calls[0][1], FAKE_CONFIG)
        self.assertEqual(self.hook_calls[0][0][-3], self.base)

    def test_precommit_failure_still_collects_other_steps(self):
        """A hook failure retains its rc and cannot short-circuit later checks."""
        self.hook_rc = 7
        rc, output = self.check()
        self.assertEqual(rc, 1, output)
        self.assertIn("step: pre-commit rc: 7", output)
        self.assertIn("step: developer.texi rc: 0", output)

    def test_precommit_timeout_still_collects_other_steps(self):
        """A bounded hook timeout retains rc 124 while later checks still run."""
        self.hook_rc = 124
        rc, output = self.check()
        self.assertEqual(rc, 1, output)
        self.assertIn("step: pre-commit rc: 124", output)
        self.assertIn("step: developer.texi rc: 0", output)

    def test_patcheck_zero_exit_with_added_hint_fails(self):
        """A zero process exit cannot hide an allow-listed double semicolon."""
        self.write(
            "tools/patcheck",
            "#!/bin/sh\ncat > patcheck.stdout\n"
            "printf 'double ;\\npatcheck.stdout:23:+bad();;\\n'\n",
        )
        self.commit()
        self.base = self.git("rev-parse", "HEAD").strip()
        self.write("libavfilter/vf_x.c", SOURCE.replace("return 1;", "return 1;;"))
        self.commit()
        rc, output = self.check()
        self.assertEqual(rc, 1, output)
        self.assertIn("step: commit-msg rc: 0", output)
        self.assertIn("step: patcheck rc: 1\ncommand rc: 0", output)
        self.assertFalse((self.root / "patcheck.stdout").exists())

    def test_patcheck_zero_exit_with_printf_hint_passes(self):
        """Added printf output stays a review hint in the complete report."""
        self.write(
            "tools/patcheck",
            "#!/bin/sh\ncat > patcheck.stdout\n"
            "printf 'Please use av_log\\npatcheck.stdout:23:+printf(\"hello\");\\n'\n",
        )
        self.commit()
        self.base = self.git("rev-parse", "HEAD").strip()
        self.write("libavfilter/vf_x.c", SOURCE.replace("    return 1;", '    printf("hello");'))
        self.commit()
        rc, output = self.check()
        self.assertEqual(rc, 0, output)
        self.assertIn("step: patcheck rc: 0\ncommand rc: 0", output)
        self.assertIn("hint (review): Please use av_log: patcheck.stdout:23:+printf", output)

    def test_deletion_only_does_not_select_inherited_violation(self):
        """Deleting a clean neighbor does not promote an old TAB to a finding."""
        self.write("libavfilter/vf_x.c", "\tint inherited;\n\n")
        self.commit()
        self.base = self.git("rev-parse", "HEAD").strip()
        self.write("libavfilter/vf_x.c", "\tint inherited;\n")
        self.commit()
        rc, output = self.check()
        self.assertEqual(rc, 0, output)
        self.assertIn("pre-existing (untouched)", output)

    def test_added_header_tab_fails(self):
        """Headers enter the mechanical scope just like C sources."""
        self.write("libavfilter/vf_x.h", "\tint ff_value(void);\n")
        self.commit()
        rc, output = self.check()
        self.assertEqual(rc, 1, output)
        self.assertIn("libavfilter/vf_x.h:1: TAB character", output)

    def test_paths_with_spaces_are_checked(self):
        """NUL-separated path discovery avoids parsing quoted diff headers."""
        self.write("libavfilter/with spaces.c", "\tint ff_value;\n")
        self.commit()
        rc, output = self.check()
        self.assertEqual(rc, 1, output)
        self.assertIn("with spaces.c:1: TAB character", output)

    def test_fixer_edits_fail_and_discard_index_and_new_files(self):
        """A zero-exit fixer still fails and leaves the original clean checkout."""

        def modify(cwd):
            (cwd / "libavfilter/vf_x.c").write_text("changed\n", encoding="utf-8")
            (cwd / "new-file.txt").write_text("generated\n", encoding="utf-8")
            self.git("-C", str(cwd), "add", "libavfilter/vf_x.c")

        self.hook_effect = modify
        rc, output = self.check()
        self.assertEqual(rc, 1, output)
        self.assertIn("fixer modified files", output)
        self.assertEqual((self.root / "libavfilter/vf_x.c").read_text(encoding="utf-8"), SOURCE)
        self.assertFalse((self.root / "new-file.txt").exists())
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_fixer_edits_discard_after_command_error(self):
        """The finally path discards files if launching or waiting for a hook fails."""

        def modify(cwd):
            (cwd / "libavfilter/vf_x.c").write_text("changed\n", encoding="utf-8")
            raise OSError("planted hook error")

        self.hook_effect = modify
        rc, output = self.check()
        self.assertEqual(rc, 1, output)
        self.assertIn("step: pre-commit rc: 2", output)
        self.assertIn("step: developer.texi rc: 0", output)
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_hook_ignored_artifact_never_touches_source_tree(self):
        """Ignored hook output stays inside the discarded detached worktree."""
        self.write(".gitignore", "ignored.out\n")
        self.commit()
        worktrees = self.git("worktree", "list", "--porcelain")
        hook_trees = []

        def modify(cwd):
            hook_trees.append(cwd)
            (cwd / "ignored.out").write_text("generated\n", encoding="utf-8")

        self.hook_effect = modify
        rc, output = self.check()
        self.assertEqual(rc, 0, output)
        self.assertFalse((self.root / "ignored.out").exists())
        self.assertNotEqual(hook_trees[0], self.root)
        self.assertFalse(hook_trees[0].exists())
        self.assertEqual(self.git("worktree", "list", "--porcelain"), worktrees)

    def test_deleted_c_path_passes(self):
        """Deleted C files count toward the cap without requiring a head blob."""
        self.git("rm", "libavfilter/vf_x.c")
        self.commit()
        rc, output = self.check()
        self.assertEqual(rc, 0, output)

    def test_path_cap_boundary_passes(self):
        """Exactly the permitted number of changed paths still runs the hooks."""
        self.write("one.txt", "one\n")
        self.commit()
        with unittest.mock.patch.object(upstream_lint, "MAX_PATHS", 1):
            rc, output = self.check()
        self.assertEqual(rc, 0, output)
        self.assertEqual(len(self.hook_calls), 1)

    def test_deadline_exhausted_during_hook_cleans_worktree(self):
        """Hook-budget exhaustion preserves cleanup time and fails the run."""
        worktrees = self.git("worktree", "list", "--porcelain")
        now = upstream_lint.time.monotonic()
        elapsed = [0]
        hook_trees = []

        def expire(cwd):
            hook_trees.append(cwd)
            elapsed[0] = upstream_lint.RUN_TIMEOUT - upstream_lint.CLEANUP_TIMEOUT

        self.hook_effect = expire
        with unittest.mock.patch.object(
            upstream_lint.time, "monotonic", side_effect=lambda: now + elapsed[0]
        ):
            rc, output = self.check()
        self.assertNotEqual(rc, 0, output)
        self.assertIn("overall deadline exhausted", output)
        self.assertFalse(hook_trees[0].exists())
        self.assertEqual(self.git("worktree", "list", "--porcelain"), worktrees)

    def test_over_cap_paths_refused(self):
        """Exceeding the path cap refuses the run before any hook executes."""
        self.write("one.txt", "one\n")
        self.write("two.txt", "two\n")
        self.commit()
        with unittest.mock.patch.object(upstream_lint, "MAX_PATHS", 1, create=True):
            rc, output = self.check()
        self.assertEqual(rc, 2, output)
        self.assertIn("changed paths exceed MAX_PATHS=1", output)
        self.assertEqual(self.hook_calls, [])

    def test_deadline_exhausted_refuses_run(self):
        """An exhausted overall budget fails before launching a subprocess."""
        with unittest.mock.patch.object(upstream_lint, "RUN_TIMEOUT", 0, create=True):
            rc, output = self.check()
        self.assertEqual(rc, 2, output)
        self.assertIn("overall deadline exhausted", output)
        self.assertEqual(self.hook_calls, [])

    def test_dirty_worktree_index_and_untracked_files_are_refused(self):
        """All dirty states fail before hooks run and preserve the user's text."""
        for state in ("unstaged", "staged", "untracked"):
            with self.subTest(state=state):
                name = "new.txt" if state == "untracked" else "libavfilter/vf_x.c"
                self.write(name, "user edit\n")
                if state == "staged":
                    self.git("add", name)
                rc, output = self.check()
                self.assertEqual(rc, 2, output)
                self.assertIn("dirty tree refused", output)
                self.assertEqual(self.hook_calls, [])
                self.assertEqual((self.root / name).read_text(encoding="utf-8"), "user edit\n")

    def test_different_head_is_refused_before_hooks(self):
        """Hooks cannot inspect a checkout that differs from the requested head."""
        self.write("notes.txt", "new\n")
        self.commit()
        rc, output = self.check("--head", self.base)
        self.assertEqual(rc, 2)
        self.assertIn("checked-out HEAD", output)
        self.assertEqual(self.hook_calls, [])

    def test_missing_tool_is_collected_and_report_is_written(self):
        """A missing base tool fails its own step and still produces a full report."""
        self.git("rm", "tools/patcheck")
        self.commit()
        self.base = self.git("rev-parse", "HEAD").strip()
        report = self.root.parent / "reports" / "report.txt"
        rc, output = self.check("--report", str(report))
        self.assertEqual(rc, 1)
        self.assertIn("step: patcheck rc: 2", output)
        self.assertIn("step: developer.texi rc: 0", output)
        self.assertEqual(report.read_text(encoding="utf-8"), output)


class RealToolsTest(FixtureTest):
    """Replay the required planted cases with the actual upstream base tools.

    Pre-commit itself stays stubbed under the implementation lane restriction;
    no hook repositories are fetched and no network-dependent tests are needed.
    The dispatcher runs the real upstream gate, including remote hooks.
    """

    def setUp(self) -> None:
        """Copy upstream files at its pinned HEAD, or name the missing source."""
        if UPSTREAM is None:
            self.skipTest("UPSTREAM_TREE unset; real upstream fixtures require an FFmpeg checkout")
        if not UPSTREAM.is_dir():
            self.skipTest(f"upstream fixture source unavailable: {UPSTREAM}")
        super().setUp()
        self.upstream_head = subprocess.run(  # nosec B603 B607 - read-only revision lookup.
            ["git", "-C", str(UPSTREAM), "rev-parse", "HEAD"],
            env=isolated_git_environment(),
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        ).stdout.strip()
        for name in (upstream_lint.CONFIG, "tools/check_commit_msg.sh", "tools/patcheck"):
            result = subprocess.run(  # nosec B603 B607 - read-only git show, fixed source paths.
                ["git", "-C", str(UPSTREAM), "show", f"{self.upstream_head}:{name}"],
                env=isolated_git_environment(),
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            )
            self.write(name, result.stdout)
        self.commit()
        self.base = self.git("rev-parse", "HEAD").strip()

    def test_added_tab_fails(self):
        """An added C TAB fails using real upstream tools plus mechanical rules."""
        self.write("libavfilter/vf_x.c", SOURCE.replace("    return 1;", "\treturn 2;"))
        self.commit()
        rc, output = self.check()
        self.assertEqual(rc, 1, output)
        self.assertIn("TAB character", output)

    def test_added_trailing_whitespace_fails(self):
        """Added trailing whitespace fails with the actual base's patcheck."""
        self.write("libavfilter/vf_x.c", SOURCE.replace("return 1;", "return 2; "))
        self.commit()
        rc, output = self.check()
        self.assertEqual(rc, 1, output)
        self.assertIn("trailing whitespace", output)

    def test_bad_subject_fails(self):
        """The real upstream subject checker rejects 'fix stuff'."""
        self.write("libavfilter/vf_x.c", SOURCE.replace("return 1", "return 2"))
        self.commit("fix stuff")
        rc, output = self.check()
        self.assertEqual(rc, 1, output)
        self.assertRegex(output, r"step: commit-msg rc: [1-9]")

    def test_clean_commit_passes(self):
        """The requested area-prefixed subject and clean C change pass."""
        self.write("libavfilter/vf_x.c", SOURCE.replace("return 1", "return 2"))
        self.commit("avfilter/vf_x: update y")
        rc, output = self.check()
        self.assertEqual(rc, 0, output)

    def test_inherited_tab_passes_and_is_listed(self):
        """A base TAB outside added lines stays informational with real tools."""
        self.write("libavfilter/vf_x.c", SOURCE.replace("    return 1;", "\treturn 1;"))
        self.commit()
        self.base = self.git("rev-parse", "HEAD").strip()
        self.write("libavfilter/vf_x.c", "\n" + self.git("show", "HEAD:libavfilter/vf_x.c"))
        self.commit()
        rc, output = self.check()
        self.assertEqual(rc, 0, output)
        self.assertIn("pre-existing (untouched)", output)


class RunnerTest(unittest.TestCase):
    """Exercise real subprocess deadlines and byte-preserving Git output."""

    def test_non_allow_listed_executable_refused(self) -> None:
        """Unknown executable basenames return rc 2 without invoking Popen."""
        with unittest.mock.patch.object(upstream_lint.subprocess, "Popen") as popen:
            popen.return_value.__enter__.return_value.communicate.return_value = (b"", None)
            result = upstream_lint._run(["/unused/not-allowed"], Path.cwd())
        self.assertEqual(result.returncode, 2)
        self.assertIn("executable refused", result.stdout)
        popen.assert_not_called()
        self.assertIsInstance(result, upstream_lint.RunResult)
        self.assertEqual(result.args, ("/unused/not-allowed",))

    def test_source_has_no_completed_process_token(self) -> None:
        """The runner uses its own result type throughout its source."""
        source = Path(upstream_lint.__file__).read_text(encoding="utf-8")
        self.assertNotIn("CompletedProcess", source)

    def test_result_is_frozen_and_slotted(self) -> None:
        """Command results cannot mutate or acquire arbitrary attributes."""
        result = upstream_lint.RunResult(("git",), 0, "ok")
        self.assertFalse(hasattr(result, "__dict__"))
        with self.assertRaises(FrozenInstanceError):
            result.returncode = 1

    def test_descendant_pipe_timeout_is_reported(self) -> None:
        """A second timeout retains partial output and returns rc 124."""
        argv = ["git", "--version"]
        for partial, expected in ((b"partial\xff", "partial\ufffd"), (None, "initial")):
            with self.subTest(partial=partial):
                process = unittest.mock.MagicMock(pid=123)
                process.communicate.side_effect = [
                    subprocess.TimeoutExpired(argv, 1, output=b"initial"),
                    subprocess.TimeoutExpired(argv, upstream_lint.REAP_TIMEOUT, output=partial),
                ]
                with (
                    unittest.mock.patch.object(upstream_lint.subprocess, "Popen") as popen,
                    unittest.mock.patch.object(upstream_lint.os, "killpg") as killpg,
                ):
                    popen.return_value.__enter__.return_value = process
                    result = upstream_lint._run(argv, Path.cwd(), timeout=1)
                self.assertIsInstance(result, upstream_lint.RunResult)
                self.assertEqual(result.returncode, 124)
                self.assertEqual(result.args, tuple(argv))
                self.assertIn(expected, result.stdout)
                self.assertIn("command timed out (descendant kept the pipe open)", result.stdout)
                self.assertEqual(process.communicate.call_count, 2)
                self.assertEqual(
                    process.communicate.call_args.kwargs["timeout"], upstream_lint.REAP_TIMEOUT
                )
                killpg.assert_called_once_with(process.pid, upstream_lint.signal.SIGKILL)

    def test_child_environment_excludes_git_overrides(self) -> None:
        """Caller Git overrides cannot redirect subprocess repositories or config."""
        overrides = dict.fromkeys(GIT_ENVIRONMENT_NAMES, "planted repository override")
        overrides |= dict.fromkeys(
            ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM"), "fixture"
        )
        overrides |= {"GIT_CONFIG_KEY_0": "fixture.key", "GIT_CONFIG_VALUE_0": "fixture"}
        process = unittest.mock.MagicMock()
        process.communicate.return_value = (b"ok\n", None)
        process.returncode = 0
        with (
            unittest.mock.patch.dict(os.environ, overrides),
            unittest.mock.patch.object(upstream_lint.subprocess, "Popen") as popen,
        ):
            popen.return_value.__enter__.return_value = process
            result = upstream_lint._run(["git", "--version"], Path.cwd())
            environment = popen.call_args.kwargs["env"]
            self.assertEqual(environment["PATH"], os.environ["PATH"])
        self.assertEqual(result.returncode, 0)
        self.assertTrue(overrides.keys().isdisjoint(environment))
        self.assertEqual(environment["PRE_COMMIT_NO_CONCURRENCY"], "1")

    def test_fixture_git_uses_isolated_environment(self) -> None:
        """Fixture Git receives the shared isolation helper's environment."""
        fixture = FixtureTest()
        fixture.root = Path.cwd()
        environment = {"PATH": os.environ["PATH"]}
        with (
            unittest.mock.patch(
                __name__ + ".isolated_git_environment", return_value=environment
            ) as isolated,
            unittest.mock.patch.object(subprocess, "run") as run,
        ):
            fixture.git("status")
        isolated.assert_called_once_with()
        self.assertEqual(run.call_args.kwargs["env"], environment)

    def test_command_timeout_uses_remaining_overall_budget(self):
        """A later command shares the deadline rather than restarting its budget."""
        process = unittest.mock.MagicMock()
        process.communicate.return_value = (b"ok\n", None)
        process.returncode = 0
        token = upstream_lint._DEADLINE.set(100)
        self.addCleanup(upstream_lint._DEADLINE.reset, token)
        with (
            unittest.mock.patch.object(upstream_lint.subprocess, "Popen") as popen,
            unittest.mock.patch.object(upstream_lint.time, "monotonic", return_value=75),
        ):
            popen.return_value.__enter__.return_value = process
            result = upstream_lint._run(["git", "--version"], Path.cwd(), timeout=60)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(process.communicate.call_args.kwargs["timeout"], 15)

    def test_timeout_exit_race_preserves_real_status(self):
        """A process gone before killpg is reaped with its actual return code."""
        process = unittest.mock.MagicMock(pid=123)
        process.communicate.side_effect = [
            subprocess.TimeoutExpired(["git"], 1),
            (b"finished\n", None),
        ]
        process.returncode = 7
        with (
            unittest.mock.patch.object(upstream_lint.subprocess, "Popen") as popen,
            unittest.mock.patch.object(upstream_lint.os, "killpg", side_effect=ProcessLookupError),
        ):
            popen.return_value.__enter__.return_value = process
            result = upstream_lint._run(["git", "--version"], Path.cwd(), timeout=1)
        self.assertEqual(result.returncode, 7)
        self.assertEqual(result.stdout, "finished\n")
        self.assertEqual(process.communicate.call_count, 2)

    def test_timeout_returns_failure(self):
        """A planted slow subprocess is killed and reported with rc 124."""
        with tempfile.TemporaryDirectory() as directory:
            result = upstream_lint._run(
                ["bash", "-c", "sleep 5"],
                Path(directory),
                timeout=0.05,
            )
        self.assertEqual(result.returncode, 124)
        self.assertIn("command timed out", result.stdout)

    def test_carriage_returns_are_preserved(self):
        """Text-mode newline normalization cannot hide trailing CR violations."""
        with tempfile.TemporaryDirectory() as directory:
            result = upstream_lint._run(
                ["bash", "-c", "printf 'line\\r\\n'"],
                Path(directory),
            )
        self.assertEqual(result.stdout, "line\r\n")

    def test_reap_timeout_pid_guards(self) -> None:
        """Prove _reap_timeout guards against invalid and current-group PIDs."""

        def run_with(pid: int | bool) -> tuple[unittest.mock.Mock, unittest.mock.Mock]:
            mock_killpg = unittest.mock.Mock()
            fake_process = unittest.mock.Mock(
                pid=pid,
                communicate=unittest.mock.Mock(return_value=(b"", None)),
                returncode=0,
            )
            with (
                unittest.mock.patch.object(upstream_lint, "_remaining", return_value=1.0),
                unittest.mock.patch.object(upstream_lint.os, "killpg", mock_killpg),
            ):
                result = upstream_lint._reap_timeout(fake_process, ["x"])
            self.assertEqual(result.returncode, 0)
            return mock_killpg, fake_process.kill

        for bad_pid in (0, 1, True):
            with self.subTest(bad_pid=bad_pid):
                mock_killpg, kill = run_with(bad_pid)
                mock_killpg.assert_not_called()
                kill.assert_not_called()

        with self.subTest(bad_pid=os.getpgrp()):
            mock_killpg, kill = run_with(os.getpgrp())
            mock_killpg.assert_not_called()
            kill.assert_called_once_with()

        good_pid = 4242 if os.getpgrp() != 4242 else 4243
        mock_killpg, kill = run_with(good_pid)
        mock_killpg.assert_called_once_with(good_pid, signal.SIGKILL)
        kill.assert_not_called()

        boundary_pid = 2
        mock_killpg, kill = run_with(boundary_pid)
        mock_killpg.assert_called_once_with(boundary_pid, signal.SIGKILL)
        kill.assert_not_called()


class PreCommitResolutionTest(unittest.TestCase):
    """Resolve portable pre-commit paths without executing hooks."""

    def test_environment_override_is_ignored(self) -> None:
        """An executable override cannot replace PATH discovery."""
        with (
            unittest.mock.patch.dict(
                os.environ, {"UPSTREAM_LINT_PRE_COMMIT": "/fixture/evil/pre-commit"}
            ),
            unittest.mock.patch(
                "shutil.which", return_value="/fixture/pipx/bin/pre-commit"
            ) as which,
        ):
            executable = upstream_lint._pre_commit()
        self.assertEqual(executable, "/fixture/pipx/bin/pre-commit")
        which.assert_called_once_with("pre-commit")

    def test_path_discovery_is_used_without_override(self) -> None:
        """PATH discovery supports user, pipx and virtual environment installs."""
        with unittest.mock.patch(
            "shutil.which", return_value="/fixture/pipx/bin/pre-commit"
        ) as which:
            executable = upstream_lint._pre_commit()
        self.assertEqual(executable, "/fixture/pipx/bin/pre-commit")
        which.assert_called_once_with("pre-commit")

    def test_missing_executable_fails_precommit_step(self) -> None:
        """Missing pre-commit on PATH produces a clear failure before launch."""
        tree = Path.cwd()
        with (
            unittest.mock.patch("shutil.which", return_value=None),
            unittest.mock.patch.object(upstream_lint, "_base_file", return_value=tree / "config"),
            unittest.mock.patch.object(upstream_lint, "_git", return_value=""),
            unittest.mock.patch.object(upstream_lint, "_run") as run,
        ):
            run.return_value.returncode = 0
            run.return_value.stdout = ""
            step = upstream_lint._collect(
                "pre-commit", lambda: upstream_lint._precommit(tree, "base", "head", tree)
            )
        self.assertNotEqual(step.rc, 0)
        self.assertNotIn("UPSTREAM_LINT_PRE_COMMIT", step.output)
        self.assertIn("pre-commit", step.output)
        self.assertIn("PATH", step.output)
        run.assert_not_called()

    def test_resolved_absolute_path_is_allowed(self) -> None:
        """The executable allow-list accepts a discovered absolute path."""
        with unittest.mock.patch("shutil.which", return_value="/fixture/user/bin/pre-commit"):
            executable = upstream_lint._pre_commit()
        upstream_lint._validate_executable([executable, "run"])


if __name__ == "__main__":
    unittest.main()
