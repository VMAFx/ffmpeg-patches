# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Planted upstream-lint defects; no repository gates or credential tests.

Deterministic fixtures stub the pre-commit and semgrep_scan.sh subprocesses,
never their surrounding Git, isolation or reporting logic. Real-tool fixtures
copy base files with git show from the named upstream checkout and skip
explicitly if unavailable.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import signal
import subprocess  # nosec B404 - fixed commands in disposable Git repositories.
import sys
import tempfile
import unittest
import unittest.mock
from dataclasses import FrozenInstanceError
from pathlib import Path

from tests.helpers import GIT_ENVIRONMENT_NAMES, isolated_git_environment

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import upstream_lint  # noqa: E402

UPSTREAM = Path(os.environ["UPSTREAM_TREE"]) if os.environ.get("UPSTREAM_TREE") else None
SOURCE = "int ff_value(void)\n{\n    return 1;\n}\n"
RTSP_WITH_CLOSE = (
    "int mux_write_header(SessionHandle *s)\n"
    "{\n"
    "    int ret = ff_rtsp_connect(s);\n"
    "    if (ret < 0)\n"
    "        return ret;\n"
    "    ret = start_step(s);\n"
    "    if (ret < 0) {\n"
    "        ff_rtsp_close_connections(s);\n"
    "        ff_network_close();\n"
    "        return ret;\n"
    "    }\n"
    "    return 0;\n"
    "}\n"
)
RTSP_WITHOUT_CLOSE = RTSP_WITH_CLOSE.replace("        ff_network_close();\n", "")
FAKE_CONFIG = "repos: []\n"
FAKE_MESSAGE = '#!/bin/sh\ngit log --format=%s "$1" | grep -Eq "^[^:]+: .+"\n'
FAKE_PATCHECK = "#!/bin/sh\ncat > patcheck.stdout\nprintf 'patcheck fixture\\n'\n"
DOCSTRING_EMPTY_CLEAN = (
    "Docstring coverage: 100.00% (0/0 covered, counted: 0, covered: 0, threshold: 80%)\n"
)
DOCSTRING_FINDING = (
    "Docstring coverage: 0.00% (0/2 covered, counted: 2, covered: 0, threshold: 80%)\n"
    "libavfilter/vf_x.c:5 ff_alpha\n"
    "libavfilter/vf_x.c:12 ff_beta\n"
)
DOCSTRING_ADVICE = "add a doc comment to each listed function, or pass --docstring-reason"


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

    def test_is_c_source_path(self) -> None:
        """Library and fftools C and header paths match; assembly, tests and others do not."""
        for path in (
            "libavformat/rtsp.c",
            "libavformat/rtsp.h",
            "fftools/ffmpeg.c",
            "fftools/ffmpeg.h",
            "libavcodec/tests.c",
        ):
            with self.subTest(path=path):
                self.assertTrue(upstream_lint.is_c_source_path(path))
        for path in (
            "compat/strtod.c",
            "tools/ismindex.c",
            "doc/filters.texi",
            "libavcodec/tests/x.c",
            "libavformat/tests/rtsp.h",
            "libavcodec/arm/x.S",
            "libavcodec/x86/x.asm",
            "rtsp.c",
        ):
            with self.subTest(path=path):
                self.assertFalse(upstream_lint.is_c_source_path(path))


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

    def test_added_defect_words_and_phrases(self) -> None:
        """Every added memory-safety and UB defect word or phrase matches in the body."""
        words = (
            "uninitialised",
            "uninitialized",
            "use after free",
            "use-after-free",
            "double free",
            "double-free",
            "null dereference",
            "null dereferences",
            "null pointer dereference",
            "null pointer dereferences",
            "division by zero",
            "divide by zero",
            "undefined behaviour",
            "undefined behavior",
            "deadlock",
            "deadlocks",
            "deadlocked",
            "segfault",
            "segfaults",
            "segfaulted",
            "segmentation fault",
            "segmentation faults",
            "memory corruption",
            "heap corruption",
            "stack corruption",
            "OOB",
            "oob",
        )
        for word in words:
            with self.subTest(word=word):
                message = f"avformat/rtsp: check write\n\nAvoid {word} on reply.\n"
                self.assertEqual(upstream_lint.fix_word(message), word)

    def test_wrapped_and_hyphenated_added_defect_phrases(self) -> None:
        """Added multi-word phrases match across line breaks, and free phrases across hyphens."""
        phrases = (
            "use\nafter free",
            "use after\nfree",
            "use-after-free",
            "use-after-\nfree",
            "use\n-after-free",
            "double\nfree",
            "double-free",
            "double-\nfree",
            "double\n-free",
            "null\ndereference",
            "null\ndereferences",
            "null\npointer dereference",
            "null pointer\ndereferences",
            "division\nby zero",
            "division by\nzero",
            "divide\nby zero",
            "divide by\nzero",
            "undefined\nbehaviour",
            "undefined\nbehavior",
            "segmentation\nfault",
            "segmentation\nfaults",
            "memory\ncorruption",
            "heap\ncorruption",
            "stack\ncorruption",
        )
        for phrase in phrases:
            with self.subTest(phrase=phrase):
                message = f"avformat/rtsp: check write\n\nAvoid {phrase} on reply.\n"
                self.assertEqual(upstream_lint.fix_word(message), phrase)

    def test_excluded_and_partial_words_return_none(self) -> None:
        """Excluded concurrency words and isolated parts of defect phrases return None."""
        words = (
            "hang",
            "hangs",
            "race",
            "race condition",
            "data race",
            "infinite loop",
            "free",
            "freed",
            "double",
            "zero",
            "undefined",
            "behaviour",
            "behavior",
            "pointer",
            "initialised",
            "initialized",
            "lock",
        )
        for word in words:
            with self.subTest(word=word):
                message = f"avformat/rtsp: check write\n\nHandle {word} in reply.\n"
                self.assertIsNone(upstream_lint.fix_word(message))

    def test_added_defect_word_in_signoff_returns_none(self) -> None:
        """Added defect words inside Signed-off-by trailers are ignored."""
        for trailer in (
            "Signed-off-by: Uninitialised Segfault <x@invalid>",
            "signed-off-by: Double Free <oob@invalid>",
        ):
            with self.subTest(trailer=trailer):
                message = f"avformat/rtsp: check write\n\n{trailer}\n"
                self.assertIsNone(upstream_lint.fix_word(message))

    def test_hyphenated_uninitialised_and_warning_flags(self) -> None:
        """Sanitizer hyphenated forms match while warning flag prefixes return None."""
        for token, expected in (
            ("use-of-uninitialized-value", "uninitialized"),
            ("use-of-uninitialised-value", "uninitialised"),
        ):
            with self.subTest(token=token):
                message = f"avformat/rtsp: check write\n\nHandle {token} in reply.\n"
                self.assertEqual(upstream_lint.fix_word(message), expected)
        for flag in (
            "-Wmaybe-uninitialized",
            "-Wmaybe-uninitialised",
            "maybe-uninitialized",
            "maybe-uninitialised",
            "-Wsometimes-uninitialized",
            "-Wsometimes-uninitialised",
            "sometimes-uninitialized",
            "sometimes-uninitialised",
        ):
            with self.subTest(flag=flag):
                message = f"avformat/rtsp: check write\n\nHandle {flag} in reply.\n"
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
FATE_SOURCES = {"testsrc2": "video", "sine": "audio", "movie": "mixed"}
FILTER_DECLARATIONS = (
    "extern const FFFilter ff_vsrc_testsrc2;\n"
    "extern const FFFilter ff_asrc_sine;\n"
    "extern const FFFilter ff_avsrc_movie;\n"
)


class FateDepsTest(unittest.TestCase):
    """Exercise lavfi dependency parsing without Git or external commands."""

    def check_source(
        self, source: str = FATE_RED, added: set[int] | None = None
    ) -> upstream_lint.Step:
        """Parse one changed FATE file with fixture source declarations."""
        self.assertTrue(hasattr(upstream_lint, "_fate_file"), "fate-deps parser missing")
        if added is None:
            added = set(range(1, len(source.splitlines()) + 1))
        report = upstream_lint._fate_file(
            "tests/fate/filter-video.mak", source, added, FATE_SOURCES
        )
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
        benchmark_claim = (
            "avcodec/x: paint maps once\n\nThe map painter benchmark ran seven rounds, "
            "so encoding is 10x faster.\n"
        )
        cases = (
            (
                benchmark_claim,
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
        expected_finding = (
            '0123456789ab: claims "Encoding is faster" without an end-to-end measurement; '
            "name an ffmpeg -benchmark or fps result, or drop the claim"
        )
        self.assertEqual(
            findings,
            [expected_finding],
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
REFERENCE_CONFIGURE = 'EXTERNAL_LIBRARY_LIST="\nlibaom libsvtav1 vaapi libvpx libx264+ libaom+\n"\n'
REFERENCE_LIBRARIES = frozenset({"libaom", "libsvtav1", "vaapi", "libvpx", "libx264+", "libaom+"})
REFERENCE_TEXT = (
    "@section libaom-av1\n"
    "@table @option\n"
    "@anchor{libaom-av1-qpfile}\n"
    "@item qpfile @var{filename}\n"
    "Read per-frame offsets; libaom reads a missing offset as 0.\n"
    "@end table\n"
    "@section libsvtav1\n"
    "@table @option\n"
    "@item qpfile @var{filename}\n"
    "Read per-frame offsets from @var{filename}, in the format described for\n"
    "the @ref{libaom-av1-qpfile,,libaom-av1 @option{qpfile} option}.\n"
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


class DocReferenceTest(unittest.TestCase):
    """Positive, negative and boundary coverage of added references to another section."""

    def findings(self, text: str, added: set[int] | None = None) -> list[str]:
        """Check Texinfo references against the explicit fixture library names."""
        lines = text.splitlines()
        return upstream_lint.reference_findings(
            "doc/encoders.texi", lines, added or {11}, REFERENCE_LIBRARIES
        )

    def test_configure_library_blocks(self) -> None:
        """Library blocks contribute whitespace-separated lowercase names only."""
        configure = (
            'EXTERNAL_LIBRARY_LIST="\nlibaom LIBSVTAV1\n"\n'
            'HWACCEL_AUTODETECT_LIBRARY_LIST="\nVAAPI\tnvenc\n"\n'
            'EXTERNAL_LIBRARY_NONFREE_LIST="\nlibfdk_aac\n"\n'
            'LIBRARY_LIST="\navcodec\n"\n'
        )
        self.assertEqual(
            upstream_lint.configure_libraries(configure),
            frozenset({"libaom", "libsvtav1", "vaapi", "nvenc", "libfdk_aac"}),
        )

    def test_configure_library_variables_are_skipped(self) -> None:
        """Dollar-prefixed entries are skipped while their own blocks are read independently."""
        configure = (
            'EXTERNAL_LIBRARY_LIST="\n$EXTERNAL_LIBRARY_GPL_LIST ${OTHER} libaom\n"\n'
            'EXTERNAL_LIBRARY_GPL_LIST="\nlibx264\n"\n'
            'HWACCEL_LIBRARY_LIST="\n$UNLISTED libmfx libvpl\n"\n'
        )
        self.assertEqual(
            upstream_lint.configure_libraries(configure),
            frozenset({"libaom", "libx264", "libmfx", "libvpl"}),
        )

    def test_configure_without_library_blocks(self) -> None:
        """Text lacking the nine library blocks yields an empty frozenset."""
        self.assertEqual(
            upstream_lint.configure_libraries('LIBRARY_LIST="\nlibaom\n"\nvaapi\n'), frozenset()
        )

    def test_configure_all_nine_library_blocks(self) -> None:
        """Each declared external or hardware library block contributes its names."""
        blocks = (
            "EXTERNAL_AUTODETECT_LIBRARY_LIST",
            "EXTERNAL_LIBRARY_GPL_LIST",
            "EXTERNAL_LIBRARY_NONFREE_LIST",
            "EXTERNAL_LIBRARY_VERSION3_LIST",
            "EXTERNAL_LIBRARY_GPLV3_LIST",
            "EXTERNAL_LIBRARY_LIST",
            "HWACCEL_AUTODETECT_LIBRARY_LIST",
            "HWACCEL_LIBRARY_NONFREE_LIST",
            "HWACCEL_LIBRARY_LIST",
        )
        for block in blocks:
            with self.subTest(block=block):
                self.assertEqual(
                    upstream_lint.configure_libraries(f'{block}="\nlibaom\n"\n'),
                    frozenset({"libaom"}),
                )

    def test_listed_heading_words_and_prefixes(self) -> None:
        """Only listed words or dash prefixes longer than two characters are admitted."""
        self.assertEqual(
            upstream_lint.section_names(
                "Vaapi-driver libaom-av1 AV libx QSV libre",
                frozenset({"vaapi", "libaom-av1", "av"}),
            ),
            frozenset({"Vaapi", "libaom-av1"}),
        )

    def test_no_doc_change_does_not_read_configure(self) -> None:
        """Boundary control: no changed doc files passes without reading configure."""
        with (
            unittest.mock.patch.object(upstream_lint, "_doc_files", return_value=[]),
            unittest.mock.patch.object(upstream_lint, "_git") as git,
        ):
            step = upstream_lint._doc_reference(Path("fixture"), "base", "head", "")
        self.assertEqual(step.rc, 0)
        git.assert_not_called()

    def test_mixed_case_vaapi_own_library_passes(self) -> None:
        """Mixed-case citing and target VAAPI headings identify the same library."""
        text = REFERENCE_TEXT.replace("@section libaom-av1", "@section VAAPI encoders")
        text = text.replace("@section libsvtav1", "@section Vaapi encoders")
        text = text.replace("libaom reads", "vaapi reads")
        self.assertEqual(self.findings(text), [])

    def test_format_acronym_is_not_a_library(self) -> None:
        """An unlisted AV format acronym cannot name a library in an item body."""
        text = REFERENCE_TEXT.replace("@section libaom-av1", "@section libaom AV formats")
        text = text.replace(
            "Read per-frame offsets; libaom reads a missing offset as 0.",
            "Read offsets from the AV file; a missing offset is zero.",
        )
        self.assertEqual(self.findings(text), [])

    def test_libre_is_not_a_library(self) -> None:
        """An unlisted lib-prefixed ordinary word cannot name a library in an item body."""
        text = REFERENCE_TEXT.replace("@section libaom-av1", "@section libaom libre format")
        text = text.replace(
            "Read per-frame offsets; libaom reads a missing offset as 0.",
            "Read offsets in the libre format; a missing offset is zero.",
        )
        self.assertEqual(self.findings(text), [])

    def test_qsv_heading_known_limit_passes(self) -> None:
        """Known limit of reading configure lists: a QSV-only heading names no library."""
        text = REFERENCE_TEXT.replace("@section libaom-av1", "@section QSV encoders")
        text = text.replace("libaom reads", "QSV reads")
        self.assertEqual(self.findings(text), [])

    def test_metal_heading_known_limit_is_found(self) -> None:
        """Known configure-list limit: a listed ordinary heading word counts as a library."""
        text = REFERENCE_TEXT.replace("@section libaom-av1", "@section Metal formats")
        text = text.replace(
            "Read per-frame offsets; libaom reads a missing offset as 0.",
            "The metal format stores one offset per line; a missing offset is zero.",
        )
        text = text.replace(
            "Read per-frame offsets from @var{filename}, in the format described for\n",
            "Read one offset per line from @var{filename}; a missing offset is zero.\n"
            "The file uses the same format as\n",
        )
        libraries = REFERENCE_LIBRARIES | {"metal"}
        self.assertEqual(
            upstream_lint.reference_findings(
                "doc/encoders.texi", text.splitlines(), {12}, libraries
            ),
            [
                "doc/encoders.texi:12: the libsvtav1 text defers to @ref{libaom-av1-qpfile}, "
                "whose item in the Metal formats section names Metal"
            ],
        )

    def test_deferred_reference_is_found(self) -> None:
        """Boundary control: the baseline reference retains its exact libaom finding."""
        self.assertEqual(
            self.findings(REFERENCE_TEXT),
            [
                "doc/encoders.texi:11: the libsvtav1 text defers to @ref{libaom-av1-qpfile}, "
                "whose item in the libaom-av1 section names libaom"
            ],
        )

    def test_escaped_reference_opener_passes(self) -> None:
        """An escaped at-sign before a reference command makes the opener literal text."""
        for command in ("ref", "xref", "pxref"):
            with self.subTest(command=command):
                text = REFERENCE_TEXT.replace("the @ref{", f"the @@{command}{{")
                self.assertEqual(self.findings(text), [])

    def test_reference_after_escaped_pair_is_found(self) -> None:
        """An escaped at-sign pair before a real reference preserves the finding."""
        for command in ("ref", "xref", "pxref"):
            with self.subTest(command=command):
                text = REFERENCE_TEXT.replace("the @ref{", f"the @@@{command}{{")
                self.assertEqual(
                    self.findings(text),
                    [
                        "doc/encoders.texi:11: the libsvtav1 text defers to "
                        "@ref{libaom-av1-qpfile}, "
                        "whose item in the libaom-av1 section names libaom"
                    ],
                )

    def test_plus_library_name_is_found(self) -> None:
        """A library name ending in plus matches its exact spelling in the item body."""
        text = REFERENCE_TEXT.replace("@section libaom-av1", "@section libaom+")
        text = text.replace("libaom reads", "libaom+ reads")
        self.assertEqual(
            self.findings(text),
            [
                "doc/encoders.texi:11: the libsvtav1 text defers to @ref{libaom-av1-qpfile}, "
                "whose item in the libaom+ section names libaom+"
            ],
        )

    def test_library_name_continuation_passes(self) -> None:
        """A continued library word cannot match the shorter library heading name."""
        text = REFERENCE_TEXT.replace("@section libaom-av1", "@section libaom")
        text = text.replace("libaom reads", "libaomx reads")
        self.assertEqual(self.findings(text), [])

    def test_library_hyphen_family_is_found(self) -> None:
        """A hyphenated family spelling still matches its library heading prefix."""
        text = REFERENCE_TEXT.replace("@section libaom-av1", "@section libaom")
        text = text.replace("libaom reads", "libaom-av1 reads")
        self.assertEqual(
            self.findings(text),
            [
                "doc/encoders.texi:11: the libsvtav1 text defers to @ref{libaom-av1-qpfile}, "
                "whose item in the libaom section names libaom"
            ],
        )

    def test_anchor_without_table_item_passes(self) -> None:
        """An anchor followed by prose without any table item names no item."""
        text = REFERENCE_TEXT.replace("@item qpfile @var{filename}\n", "", 1)
        self.assertEqual(self.findings(text, {10}), [])

    def test_anchor_metadata_before_item_is_found(self) -> None:
        """Blank lines and index entries between an anchor and its item are allowed."""
        text = REFERENCE_TEXT.replace(
            "@anchor{libaom-av1-qpfile}\n",
            "@anchor{libaom-av1-qpfile}\n\n@cindex qpfile\n",
        )
        self.assertEqual(
            self.findings(text, {13}),
            [
                "doc/encoders.texi:13: the libsvtav1 text defers to @ref{libaom-av1-qpfile}, "
                "whose item in the libaom-av1 section names libaom"
            ],
        )

    def test_anchor_inside_table_item_is_found(self) -> None:
        """An anchor on the first line of an existing item's text retains ownership."""
        text = REFERENCE_TEXT.replace(
            "@anchor{libaom-av1-qpfile}\n@item qpfile @var{filename}",
            "@item qpfile @var{filename}\n@anchor{libaom-av1-qpfile}",
        )
        self.assertEqual(
            self.findings(text),
            [
                "doc/encoders.texi:11: the libsvtav1 text defers to @ref{libaom-av1-qpfile}, "
                "whose item in the libaom-av1 section names libaom"
            ],
        )

    def test_adjacent_peer_item_ends_empty_body(self) -> None:
        """A peer item immediately after the anchored item cannot supply its library."""
        text = REFERENCE_TEXT.replace(
            "@item qpfile @var{filename}\n", "@item qpfile @var{filename}\n@item other\n", 1
        )
        self.assertEqual(self.findings(text, {12}), [])

    def test_item_alias_keeps_library_in_body(self) -> None:
        """An item alias immediately after the anchored item preserves its library body."""
        text = REFERENCE_TEXT.replace(
            "@item qpfile @var{filename}\n", "@item qpfile @var{filename}\n@itemx qpf\n", 1
        )
        self.assertEqual(
            self.findings(text, {12}),
            [
                "doc/encoders.texi:12: the libsvtav1 text defers to @ref{libaom-av1-qpfile}, "
                "whose item in the libaom-av1 section names libaom"
            ],
        )

    def test_commented_peer_item_ends_empty_body(self) -> None:
        """A comment before a peer item cannot transfer the peer's library to the anchor."""
        text = REFERENCE_TEXT.replace(
            "@item qpfile @var{filename}\n",
            "@item qpfile @var{filename}\n@c note\n@item other\n",
            1,
        )
        self.assertEqual(self.findings(text, {13}), [])

    def test_conditional_peer_item_ends_body(self) -> None:
        """A peer item inside a conditional cannot supply the referenced item's library."""
        text = REFERENCE_TEXT.replace(
            "Read per-frame offsets; libaom reads a missing offset as 0.",
            "Read per-frame offsets.\n@ifset extra\n@item other\n"
            "libaom reads a missing offset as 0.\n@end ifset",
        )
        self.assertEqual(self.findings(text, {15}), [])

    def test_conditional_library_inside_item_is_found(self) -> None:
        """Conditional prose naming the library remains inside the referenced item."""
        text = REFERENCE_TEXT.replace(
            "Read per-frame offsets; libaom reads a missing offset as 0.",
            "Read per-frame offsets.\n@ifset extra\n"
            "libaom reads a missing offset as 0.\n@end ifset",
        )
        self.assertEqual(
            self.findings(text, {14}),
            [
                "doc/encoders.texi:14: the libsvtav1 text defers to @ref{libaom-av1-qpfile}, "
                "whose item in the libaom-av1 section names libaom"
            ],
        )

    def test_nested_table_item_stays_in_body(self) -> None:
        """An item in a nested table remains part of the referenced outer item."""
        text = REFERENCE_TEXT.replace(
            "Read per-frame offsets; libaom reads a missing offset as 0.",
            "Read per-frame offsets.\n@table @option\n@item nested\n"
            "libaom reads a missing offset as 0.\n@end table",
        )
        self.assertEqual(
            self.findings(text, {15}),
            [
                "doc/encoders.texi:15: the libsvtav1 text defers to @ref{libaom-av1-qpfile}, "
                "whose item in the libaom-av1 section names libaom"
            ],
        )

    def test_uppercase_citing_library_passes(self) -> None:
        """Citing library names match target families regardless of case."""
        text = REFERENCE_TEXT.replace("@section libsvtav1", "@section LIBAOM")
        self.assertEqual(self.findings(text), [])

    def test_uppercase_target_library_is_found(self) -> None:
        """Boundary control: D3 preserves uppercase target spelling in its one finding."""
        text = REFERENCE_TEXT.replace("@section libaom-av1", "@section LIBAOM-AV1")
        self.assertEqual(
            self.findings(text),
            [
                "doc/encoders.texi:11: the libsvtav1 text defers to @ref{libaom-av1-qpfile}, "
                "whose item in the LIBAOM-AV1 section names LIBAOM"
            ],
        )

    def test_citing_library_family_passes(self) -> None:
        """A target variant and its citing library share the first-hyphen family."""
        text = REFERENCE_TEXT.replace("@section libsvtav1", "@section libaom")
        text = text.replace("libaom reads", "libaom-av1 reads")
        self.assertEqual(self.findings(text), [])

    def test_punctuated_target_heading_is_found(self) -> None:
        """Punctuation separates heading words without becoming part of a library name."""
        text = REFERENCE_TEXT.replace("@section libaom-av1", "@section libaom:")
        self.assertEqual(
            self.findings(text),
            [
                "doc/encoders.texi:11: the libsvtav1 text defers to @ref{libaom-av1-qpfile}, "
                "whose item in the libaom: section names libaom"
            ],
        )

    def test_heading_video_word_passes(self) -> None:
        """An ordinary heading word cannot become a library through body repetition."""
        text = REFERENCE_TEXT.replace("@section libaom-av1", "@section libaom video")
        text = text.replace(
            "Read per-frame offsets; libaom reads a missing offset as 0.",
            "Read per-frame offsets; the video offset defaults to zero.",
        )
        self.assertEqual(self.findings(text), [])

    def test_heading_article_passes(self) -> None:
        """An article in a heading cannot become a library through body repetition."""
        text = REFERENCE_TEXT.replace("@section libaom-av1", "@section the libaom")
        text = text.replace(
            "Read per-frame offsets; libaom reads a missing offset as 0.",
            "Read per-frame offsets; the offset defaults to zero.",
        )
        self.assertEqual(self.findings(text), [])

    def test_capitals_target_library_is_found(self) -> None:
        """An uppercase library heading matches lowercase prose and retains its spelling."""
        text = REFERENCE_TEXT.replace("@section libaom-av1", "@section VAAPI encoders")
        text = text.replace("libaom reads", "vaapi reads")
        self.assertEqual(
            self.findings(text),
            [
                "doc/encoders.texi:11: the libsvtav1 text defers to @ref{libaom-av1-qpfile}, "
                "whose item in the VAAPI encoders section names VAAPI"
            ],
        )

    def test_comment_after_escaped_pair_passes(self) -> None:
        """An escaped at-sign pair before a real comment does not expose its reference."""
        for command in ("c", "comment"):
            with self.subTest(command=command):
                lines = REFERENCE_TEXT.splitlines()
                lines[10] = (
                    f"@@@{command} The format is described in "
                    "@ref{libaom-av1-qpfile,,libaom-av1 @option{qpfile} option}."
                )
                self.assertEqual(self.findings("\n".join(lines)), [])

    def test_escaped_comment_command_is_found(self) -> None:
        """One escaped at-sign pair leaves the following reference visible."""
        lines = REFERENCE_TEXT.splitlines()
        lines[10] = "@@c The format is described in @ref{libaom-av1-qpfile}."
        self.assertEqual(
            self.findings("\n".join(lines)),
            [
                "doc/encoders.texi:11: the libsvtav1 text defers to @ref{libaom-av1-qpfile}, "
                "whose item in the libaom-av1 section names libaom"
            ],
        )

    def test_two_escaped_comment_pairs_are_found(self) -> None:
        """Two escaped at-sign pairs leave plain text and its reference visible."""
        lines = REFERENCE_TEXT.splitlines()
        lines[10] = "@@@@c The format is described in @ref{libaom-av1-qpfile}."
        self.assertEqual(
            self.findings("\n".join(lines)),
            [
                "doc/encoders.texi:11: the libsvtav1 text defers to @ref{libaom-av1-qpfile}, "
                "whose item in the libaom-av1 section names libaom"
            ],
        )

    def test_other_defer_wordings_and_commands_are_found(self) -> None:
        """Same-format wording and @xref or @pxref references are found too."""
        for old, new in (
            ("in the format described for", "in the same format as"),
            ("the @ref{", "the @xref{"),
            ("the @ref{", "the @pxref{"),
            ("in the format described for", "in the syntax documented in"),
        ):
            with self.subTest(new=new):
                self.assertEqual(len(self.findings(REFERENCE_TEXT.replace(old, new))), 1)

    def test_prose_anchor_outside_table_passes(self) -> None:
        """Plain prose anchors pass while anchors within table items remain eligible."""
        lines = [
            "@section libaom-av1",
            "@anchor{format-overview}",
            "libaom uses one offset per line.",
            "@section libsvtav1",
            "The format is described in @ref{format-overview}.",
        ]
        self.assertEqual(self.findings("\n".join(lines), {5}), [])

        text = REFERENCE_TEXT.replace(
            "@anchor{libaom-av1-qpfile}\n@item qpfile @var{filename}",
            "@item qpfile @var{filename}\n@anchor{libaom-av1-qpfile}",
        )
        self.assertEqual(len(self.findings(text)), 1)

    def test_anchor_table_depth_skips_conditionals(self) -> None:
        """Conditional wrappers preserve table ownership but other inner blocks do not."""
        for table in ("table", "ftable", "vtable"):
            for block, expected in (("ifset extra", 1), ("example", 0)):
                with self.subTest(table=table, block=block):
                    text = REFERENCE_TEXT.replace("@table", f"@{table}", 1)
                    text = text.replace("@end table", f"@end {table}", 1)
                    text = text.replace(
                        "@anchor{libaom-av1-qpfile}",
                        f"@{block}\n@anchor{{libaom-av1-qpfile}}",
                    )
                    text = text.replace(
                        f"@end {table}", f"@end {block.split()[0]}\n@end {table}", 1
                    )
                    self.assertEqual(len(self.findings(text, {13})), expected)

    def test_reference_in_comment_passes(self) -> None:
        """Whole-line and inline comments hide references while escaped commands do not."""
        for prefix in ("@c ", "@comment ", "Earlier prose. @c ", "Earlier prose. @comment "):
            with self.subTest(prefix=prefix):
                lines = REFERENCE_TEXT.splitlines()
                lines[10] = prefix + "The format is described in @ref{libaom-av1-qpfile}."
                self.assertEqual(self.findings("\n".join(lines)), [])

        for prefix in ("@code{@@c}", "@code{@@comment}", "@caption", "@cword"):
            with self.subTest(prefix=prefix):
                lines = REFERENCE_TEXT.splitlines()
                lines[10] = prefix + " The format is described in @ref{libaom-av1-qpfile}."
                self.assertEqual(len(self.findings("\n".join(lines))), 1)

    def test_reference_in_raw_block_passes(self) -> None:
        """Raw blocks hide references while examples and table item text remain eligible."""
        for block in ("ignore", "verbatim", "macro f", "rmacro f", "linemacro f"):
            with self.subTest(block=block):
                lines = REFERENCE_TEXT.splitlines()
                lines[10:11] = [
                    f"@{block}",
                    f"  @{block}",
                    "@end table",
                    f"@end {block.split()[0]}",
                    "The format is described in @ref{libaom-av1-qpfile}.",
                    f"@end {block.split()[0]}",
                ]
                self.assertEqual(self.findings("\n".join(lines), {15}), [])

        for prefix, suffix, added in (
            ("@example\n", "\n@end example", {12}),
            ("@item ", "", {11}),
        ):
            with self.subTest(prefix=prefix):
                lines = REFERENCE_TEXT.splitlines()
                lines[10] = prefix + "The format is described in @ref{libaom-av1-qpfile}." + suffix
                self.assertEqual(len(self.findings("\n".join(lines), added)), 1)

    def test_reference_with_added_continuation_is_found_once(self) -> None:
        """Only added reference spans are checked once and retain their opening line."""
        text = REFERENCE_TEXT.replace("the @ref{", "the @ref{\n")
        for added in ({12}, {11, 12}, {11}):
            with self.subTest(added=added):
                self.assertEqual(self.findings(text, added), self.findings(REFERENCE_TEXT))

        for added in ({10}, {13}):
            with self.subTest(added=added):
                self.assertEqual(self.findings(text, added), [])

    def test_reference_continuation_lookahead_boundary(self) -> None:
        """The third following line is eligible but the fourth is outside lookahead."""
        for gap, expected in ((3, 1), (4, 0)):
            with self.subTest(gap=gap):
                text = REFERENCE_TEXT.replace("the @ref{", "the @ref{" + "\n" * gap)
                self.assertEqual(len(self.findings(text, {11 + gap})), expected)

    def test_reference_without_defer_wording_passes(self) -> None:
        """A reference not worded as a deferral, or after a sentence end, passes."""
        for old, new in (
            ("in the format described for", "and see also"),
            ("in the format described for", "as described for libaom. See"),
            ("in the format described for", "as described for libaom; see"),
        ):
            with self.subTest(new=new):
                self.assertEqual(self.findings(REFERENCE_TEXT.replace(old, new)), [])

    def test_same_section_or_missing_anchor_passes(self) -> None:
        """A reference inside its own section or to an unknown anchor passes."""
        self.assertEqual(len(self.findings(REFERENCE_TEXT)), 1)
        same = REFERENCE_TEXT.replace("@section libsvtav1\n", "")
        self.assertEqual(self.findings(same, {10}), [])
        missing = REFERENCE_TEXT.replace("@ref{libaom-av1-qpfile", "@ref{other-qpfile")
        self.assertEqual(self.findings(missing), [])

    def test_reference_after_exclamation_passes(self) -> None:
        """An exclamation followed by whitespace ends the earlier deferral sentence."""
        text = REFERENCE_TEXT.replace("in the format described for", "as described for libaom! See")
        self.assertEqual(self.findings(text), [])

    def test_reference_after_question_passes(self) -> None:
        """A question mark followed by whitespace ends the earlier deferral sentence."""
        text = REFERENCE_TEXT.replace("in the format described for", "as described for libaom? See")
        self.assertEqual(self.findings(text), [])

    def test_defer_wording_in_same_sentence_is_found(self) -> None:
        """Deferral wording in the same sentence still flags the reference."""
        for wording in (
            "as described for libaom in",
            "as described for qp!file in",
            "as described for qp?file in",
        ):
            with self.subTest(wording=wording):
                text = REFERENCE_TEXT.replace("in the format described for", wording)
                self.assertEqual(len(self.findings(text)), 1)

    def test_defer_wording_two_lines_before_reference_is_found(self) -> None:
        """Deferral wording survives an earlier continuation of the same sentence."""
        text = REFERENCE_TEXT.replace("the @ref{", "the corresponding option in\nthe @ref{")
        self.assertEqual(len(self.findings(text, {12})), 1)

    def test_defer_context_stops_at_paragraph_sentence_or_command(self) -> None:
        """Paragraphs, sentence endings and block commands end deferral context."""
        for boundary in (
            "",
            "   ",
            "the option ends here. See",
            "the option ends here; see",
            "@item next",
            "@end table",
            "@section libsvtav1",
            "@example",
            "@table @option",
            "@anchor{next}",
        ):
            with self.subTest(boundary=boundary):
                text = REFERENCE_TEXT.replace("the @ref{", f"{boundary}\nthe @ref{{")
                self.assertEqual(self.findings(text, {12}), [])

    def test_defer_context_allows_inline_commands_and_is_bounded(self) -> None:
        """Inline commands preserve context only within six preceding lines."""
        for count, expected in ((5, 1), (6, 0)):
            with self.subTest(count=count):
                text = REFERENCE_TEXT.replace("the @ref{", "@code{option}\n" * count + "the @ref{")
                self.assertEqual(len(self.findings(text, {11 + count})), expected)

    def test_reference_after_chapter_heading_passes(self) -> None:
        """Chapter-level and higher headings clear the earlier section's ownership."""
        for heading in (
            "chapter",
            "unnumbered",
            "appendix",
            "top",
            "part",
            "majorheading",
            "chapheading",
            "centerchap",
        ):
            with self.subTest(heading=heading):
                text = REFERENCE_TEXT.replace(
                    "@section libsvtav1\n", f"@section libsvtav1\n@{heading} Overview\n"
                )
                self.assertEqual(self.findings(text, {12}), [])
                self.assertEqual(upstream_lint.texi_sections(text.splitlines())[7], "")

    def test_reference_under_subsection_is_found(self) -> None:
        """Subsection and subsubsection references retain the containing section."""
        for heading in ("subsection", "subsubsection"):
            with self.subTest(heading=heading):
                text = REFERENCE_TEXT.replace(
                    "@section libsvtav1\n", f"@section libsvtav1\n@{heading} Details\n"
                )
                self.assertEqual(len(self.findings(text, {12})), 1)

    def test_reference_to_external_manual_passes(self) -> None:
        """A non-empty fourth argument makes each reference command external."""
        for command in ("ref", "xref", "pxref"):
            for arguments in (
                ",,,othermanual",
                ",,,othermanual,Printed Manual",
                ",,libaom @option{qpfile}, othermanual ",
            ):
                with self.subTest(command=command, arguments=arguments):
                    lines = REFERENCE_TEXT.splitlines()
                    lines[10] = f"the @{command}{{libaom-av1-qpfile{arguments}}} for details."
                    self.assertEqual(self.findings("\n".join(lines)), [])

    def test_reference_with_empty_manual_is_found(self) -> None:
        """An empty fourth argument or fewer arguments keeps the reference local."""
        for command in ("ref", "xref", "pxref"):
            for arguments in ("", ",,,", ",,, ", ",,,,Printed Manual", ",,libaom @option{qpfile},"):
                with self.subTest(command=command, arguments=arguments):
                    lines = REFERENCE_TEXT.splitlines()
                    lines[10] = f"the @{command}{{libaom-av1-qpfile{arguments}}} for details."
                    self.assertEqual(len(self.findings("\n".join(lines))), 1)

    def test_wrapped_local_reference_is_found(self) -> None:
        """A closing brace on the next line preserves local reference findings."""
        for command in ("ref", "xref", "pxref"):
            for arguments in ("\n", ",,\nTitle", ",,\nTitle,"):
                with self.subTest(command=command, arguments=arguments):
                    lines = REFERENCE_TEXT.splitlines()
                    lines[10] = f"the @{command}{{libaom-av1-qpfile{arguments}}} for details."
                    self.assertEqual(self.findings("\n".join(lines)), self.findings(REFERENCE_TEXT))

    def test_wrapped_reference_to_external_manual_passes(self) -> None:
        """An external manual on any of the three following lines suppresses findings."""
        for command in ("ref", "xref", "pxref"):
            for arguments in (
                ",,,\nothermanual",
                ",,Title,\nothermanual, Printed Manual",
                ",,\nTitle,\n\nothermanual",
            ):
                with self.subTest(command=command, arguments=arguments):
                    lines = REFERENCE_TEXT.splitlines()
                    lines[10] = f"the @{command}{{libaom-av1-qpfile{arguments}}} for details."
                    self.assertEqual(self.findings("\n".join(lines)), [])

    def test_reference_unclosed_within_bound_is_found(self) -> None:
        """Unclosed references without a known external manual keep local findings."""
        for suffix in ("", ",,\nTitle,\n", ",,\nTitle,\n\n\nothermanual}"):
            with self.subTest(suffix=suffix):
                lines = REFERENCE_TEXT.splitlines()[:11]
                lines[10] = f"the @ref{{libaom-av1-qpfile{suffix}"
                self.assertEqual(self.findings("\n".join(lines)), self.findings(REFERENCE_TEXT))

    def test_reference_unclosed_external_manual_with_comma_passes(self) -> None:
        """An external fourth argument before a comma survives a close beyond lookahead."""
        lines = REFERENCE_TEXT.splitlines()[:11]
        lines[10] = "the @ref{libaom-av1-qpfile,,,othermanual,"
        lines.extend(["Printed", "Manual", "Title", "} for details."])
        self.assertEqual(self.findings("\n".join(lines)), [])

    def test_reference_unclosed_external_manual_last_argument_passes(self) -> None:
        """An external fourth argument needs no trailing comma before lookahead ends."""
        lines = REFERENCE_TEXT.splitlines()[:11]
        lines[10] = "the @ref{libaom-av1-qpfile,,,othermanual"
        lines.extend(["", "", "", "} for details."])
        self.assertEqual(self.findings("\n".join(lines)), [])

    def test_reference_unclosed_empty_manual_is_found(self) -> None:
        """An empty or whitespace-only fourth argument keeps an unclosed reference local."""
        for manual in ("", " "):
            with self.subTest(manual=manual):
                lines = REFERENCE_TEXT.splitlines()[:11]
                lines[10] = f"the @ref{{libaom-av1-qpfile,,,{manual},"
                lines.extend(["Printed", "Manual", "Title", "} for details."])
                self.assertEqual(self.findings("\n".join(lines)), self.findings(REFERENCE_TEXT))

    def test_reference_unclosed_first_argument_is_found(self) -> None:
        """A lone first argument keeps local lookup when its close exceeds lookahead."""
        lines = REFERENCE_TEXT.splitlines()[:11]
        lines[10] = "the @ref{libaom-av1-qpfile"
        lines.extend(["continued", "across", "lines", "} for details."])
        self.assertEqual(self.findings("\n".join(lines)), self.findings(REFERENCE_TEXT))

    def test_item_without_other_library_passes(self) -> None:
        """An anchored item that names no library of its section passes."""
        text = REFERENCE_TEXT.replace("libaom reads a missing offset as 0", "a gap reads as 0")
        self.assertEqual(self.findings(text), [])

    def test_item_ends_at_next_item(self) -> None:
        """Library names after the next @item are outside the anchored item."""
        text = REFERENCE_TEXT.replace(
            "Read per-frame offsets; libaom reads a missing offset as 0.\n",
            "Read per-frame offsets.\n@item other\nlibaom reads a missing offset as 0.\n",
        )
        self.assertEqual(self.findings(text, {13}), [])

    def test_item_library_after_example_is_found(self) -> None:
        """Library prose after a closed example remains inside the anchored item."""
        text = REFERENCE_TEXT.replace(
            "Read per-frame offsets;", "@example\n0 1\n@end example\nRead per-frame offsets;"
        )
        self.assertEqual(len(self.findings(text, {14})), 1)

    def test_item_library_after_nested_itemize_is_found(self) -> None:
        """Nested list and table items do not end the enclosing anchored item."""
        for block in ("itemize", "enumerate", "table", "ftable", "vtable", "multitable"):
            with self.subTest(block=block):
                text = REFERENCE_TEXT.replace(
                    "Read per-frame offsets;",
                    f"@{block}\n@item nested\n@itemize\n@item deeper\n@end itemize\n"
                    f"@end {block}\nRead per-frame offsets;",
                )
                self.assertEqual(len(self.findings(text, {17})), 1)

    def test_item_library_after_conditional_is_found(self) -> None:
        """Conditional ends preserve item prose while the enclosing table end stops it."""
        for block in ("ifset extra", "ifclear extra", "ifhtml", "ifnottex", "ifdocbook"):
            with self.subTest(block=block):
                text = REFERENCE_TEXT.replace(
                    "Read per-frame offsets;",
                    f"@{block}\nOptional text.\n@end {block.split()[0]}\nRead per-frame offsets;",
                )
                self.assertEqual(len(self.findings(text, {14})), 1)

        text = REFERENCE_TEXT.replace(
            "Read per-frame offsets; libaom reads a missing offset as 0.\n@end table",
            "Read per-frame offsets.\n@end table\nlibaom reads a missing offset as 0.",
        )
        self.assertEqual(self.findings(text, {12}), [])

    def test_item_library_after_raw_block_is_found(self) -> None:
        """Raw block ends and literal commands preserve the enclosing item."""
        for block in ("ignore", "verbatim", "macro f", "rmacro f", "linemacro f"):
            with self.subTest(block=block):
                raw = [f"@{block}", "@end table", f"@end {block.split()[0]}"]
                body = [*raw, "libaom reads a missing offset as 0."]
                self.assertEqual(upstream_lint._anchor_body(body), body)
                text = REFERENCE_TEXT.replace(
                    "Read per-frame offsets;", "\n".join(raw) + "\nRead per-frame offsets;"
                )
                self.assertEqual(len(self.findings(text, {14})), 1)

    def test_item_unmatched_end_stops_nested_block(self) -> None:
        """An end not opened in the item stops it even within a non-raw block."""
        text = REFERENCE_TEXT.replace(
            "Read per-frame offsets;",
            "@example\n0 1\n@end table\nRead per-frame offsets;",
        )
        self.assertEqual(self.findings(text, {14}), [])

    def test_item_block_ends_remain_in_body(self) -> None:
        """Matching nested ends stay in the body before a peer item or heading."""
        for block in (
            "example",
            "smallexample",
            "verbatim",
            "display",
            "format",
            "quotation",
            "cartouche",
        ):
            for boundary in (
                "@item next",
                "@itemx next",
                "@end table",
                "@section next",
                "@subsection next",
                "@chapter next",
                "@anchor{next}",
            ):
                with self.subTest(block=block, boundary=boundary):
                    body = [f"@{block}", "nested text", f"@end {block}", "prose"]
                    lines = ["@anchor{option}", "@item option", *body, boundary, "libaom"]
                    self.assertEqual(upstream_lint._anchor_item(lines, 0), body)

    def test_item_descriptive_heading_word_passes(self) -> None:
        """Descriptive heading labels cannot stand in for library names."""
        text = REFERENCE_TEXT.replace("@section libaom-av1", "@section libaom-av1 encoder")
        text = text.replace(
            "libaom reads a missing offset as 0", "the encoder reads a missing offset as 0"
        )
        self.assertEqual(self.findings(text), [])

    def test_item_library_under_descriptive_heading_is_found(self) -> None:
        """A descriptive heading preserves findings naming only the actual library."""
        text = REFERENCE_TEXT.replace("@section libaom-av1", "@section libaom-av1 encoder")
        text = text.replace("libaom reads", "the encoder libaom reads")
        self.assertEqual(len(self.findings(text)), 1)
        self.assertTrue(self.findings(text)[0].endswith("section names libaom"))

    def test_citing_line_outside_sections_or_unadded_passes(self) -> None:
        """A citing line before any @section, or one not added, passes."""
        self.assertEqual(self.findings(REFERENCE_TEXT, {10}), [])
        lines = REFERENCE_TEXT.splitlines()
        first = [*lines[9:11], *lines[:6]]
        self.assertEqual(
            upstream_lint.reference_findings("doc/x.texi", first, {2}, REFERENCE_LIBRARIES), []
        )

    def test_section_names(self) -> None:
        """Only configure-listed heading words and dash prefixes longer than two qualify."""
        self.assertEqual(
            upstream_lint.section_names("libaom-av1", REFERENCE_LIBRARIES), frozenset({"libaom"})
        )
        for title, expected in (
            ("libaom-av1 encoder", {"libaom"}),
            ("ac3 and ac3_fixed", set()),
            ("--libaom--/libvpx:libx264+ lib_name", {"libaom", "libvpx", "libx264+"}),
            ("AA-A1 VAAPI2 A1", set()),
            ("VAAPI encoders", {"VAAPI"}),
            ("libaom Decoders", {"libaom"}),
            (
                "and encoder encoders decoder decoders muxer muxers demuxer demuxers "
                "filter filters device devices codec codecs options",
                set(),
            ),
        ):
            with self.subTest(title=title):
                self.assertEqual(
                    upstream_lint.section_names(title, REFERENCE_LIBRARIES), frozenset(expected)
                )
        self.assertEqual(upstream_lint.section_names("qp of x", REFERENCE_LIBRARIES), frozenset())
        self.assertEqual(
            upstream_lint.texi_sections(["a", "@section b c", "d"]), ["", "b c", "b c"]
        )


class TestProgramTest(unittest.TestCase):
    """Positive, negative and boundary coverage of new registered lib*/tests programs."""

    def test_registered_new_program_is_found(self) -> None:
        """A new lib*/tests C file listed by a TESTPROGS assignment of its Makefile is found."""
        multiline_makefile = (
            "TESTPROGS = avcodec                                    \\\n"
            "            aom_film_grain                             \\\n"
            "            golomb\n"
        )
        for makefile in (
            "TESTPROGS-$(CONFIG_ITUT_T35)              += aom_film_grain\n",
            multiline_makefile,
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


class DocstringsStepTest(unittest.TestCase):
    """Positive, negative and boundary coverage of the docstrings step and reader."""

    def run_step(self, returncode: int, stdout: str, waiver: str = "") -> upstream_lint.Step:
        """Run _docstrings with _run stubbed to return the chosen status and output."""
        stub = upstream_lint.RunResult((), returncode, stdout)
        with unittest.mock.patch.object(upstream_lint, "_run", return_value=stub):
            return upstream_lint._docstrings(Path("/fixture/ffmpeg"), "base", "head", waiver)

    def test_coverage_pass_returns_zero(self) -> None:
        """Exit 0 with a valid first-line coverage summary returns step rc 0."""
        covered = (
            "Docstring coverage: 85.71% (6/7 covered, counted: 7, covered: 6, threshold: 80%)\n"
        )
        for stdout in (DOCSTRING_EMPTY_CLEAN, covered):
            with self.subTest(stdout=stdout):
                expected = stdout.rstrip("\n")
                self.assertEqual(
                    upstream_lint.docstring_result(0, stdout),
                    upstream_lint.Step("docstrings", 0, expected),
                )
                step = self.run_step(0, stdout)
                self.assertEqual(step, upstream_lint.Step("docstrings", 0, expected))

    def test_coverage_finding_returns_one_with_functions_and_advice(self) -> None:
        """Exit 1 with a coverage summary keeps the function lines and appends advice."""
        expected = f"{DOCSTRING_FINDING.rstrip()}\n{DOCSTRING_ADVICE}"
        self.assertEqual(
            upstream_lint.docstring_result(1, DOCSTRING_FINDING),
            upstream_lint.Step("docstrings", 1, expected),
        )
        step = self.run_step(1, DOCSTRING_FINDING)
        self.assertEqual(step.rc, 1)
        self.assertEqual(step.output, expected)
        self.assertIn("libavfilter/vf_x.c:5 ff_alpha", step.output)
        self.assertIn("libavfilter/vf_x.c:12 ff_beta", step.output)

    def test_blank_or_whitespace_waiver_returns_one(self) -> None:
        """An empty or whitespace-only --docstring-reason leaves a finding at rc 1."""
        for reason in ("", " ", " \t\n "):
            with self.subTest(reason=reason):
                step = self.run_step(1, DOCSTRING_FINDING, reason)
                self.assertEqual(step.rc, 1)
                self.assertNotIn("waived:", step.output)
                self.assertTrue(step.output.endswith(DOCSTRING_ADVICE))

    def test_non_blank_waiver_waives_finding(self) -> None:
        """A non-blank --docstring-reason returns rc 0 with the waived header line."""
        expected = f"waived: internal wrappers\n{DOCSTRING_FINDING.rstrip()}"
        self.assertEqual(
            upstream_lint.docstring_result(1, DOCSTRING_FINDING, "  internal wrappers  "),
            upstream_lint.Step("docstrings", 0, expected),
        )
        step = self.run_step(1, DOCSTRING_FINDING, "internal wrappers")
        self.assertEqual(step.rc, 0)
        self.assertEqual(step.output, expected)

    def test_hook_refusal_returns_two(self) -> None:
        """Exit 1 starting with a praetor hooks refusal fails closed with rc 2."""
        refusal = "praetor hooks: installing the engine failed\n"
        for reason in ("", "cannot waive refusal"):
            with self.subTest(reason=reason):
                step = self.run_step(1, refusal, reason)
                self.assertEqual(step.rc, 2)
                self.assertIn("1", step.output.splitlines()[0])
                self.assertIn("praetor hooks: installing the engine failed", step.output)
                self.assertNotIn("waived:", step.output)

    def test_engine_error_exit_two_returns_two_even_when_waived(self) -> None:
        """Exit 2 with an [ERROR] line returns rc 2 even when a waiver is supplied."""
        error = "[ERROR] cannot read base revision\n"
        for reason in ("", "not a finding"):
            with self.subTest(reason=reason):
                step = self.run_step(2, error, reason)
                self.assertEqual(step.rc, 2)
                self.assertIn("2", step.output.splitlines()[0])
                self.assertIn("[ERROR] cannot read base revision", step.output)
                self.assertNotIn("waived:", step.output)

    def test_timeout_exit_returns_two(self) -> None:
        """A timed-out engine run with exit 124 fails closed with rc 2."""
        for stdout in ("\ncommand timed out\n", f"{DOCSTRING_EMPTY_CLEAN}command timed out\n"):
            with self.subTest(stdout=stdout):
                step = self.run_step(124, stdout, "not waivable")
                self.assertEqual(step.rc, 2)
                self.assertIn("124", step.output.splitlines()[0])
                self.assertNotIn("waived:", step.output)

    def test_zero_exit_without_coverage_line_returns_two(self) -> None:
        """Exit 0 without the allow-listed coverage line fails closed with rc 2."""
        for stdout in ("", "ok\n", "Docstring coverage: ok\n"):
            with self.subTest(stdout=stdout):
                step = self.run_step(0, stdout)
                self.assertEqual(step.rc, 2)
                self.assertIn("0", step.output.splitlines()[0])

    def test_non_first_coverage_line_returns_two(self) -> None:
        """A coverage summary preceded by another line fails closed with rc 2."""
        cases = (
            (0, f"praetor hooks: running engine on PATH\n{DOCSTRING_EMPTY_CLEAN}"),
            (1, f"\n{DOCSTRING_FINDING}"),
        )
        for returncode, stdout in cases:
            with self.subTest(returncode=returncode):
                step = self.run_step(returncode, stdout, "not a finding")
                self.assertEqual(step.rc, 2)
                self.assertNotIn("waived:", step.output)

    def test_step_passes_expected_argv_and_cwd_to_run(self) -> None:
        """The step runs engine.sh via bash from REPO_ROOT with DOCSTRINGS_TIMEOUT."""
        tree = Path("/fixture/ffmpeg")
        stub = upstream_lint.RunResult((), 0, DOCSTRING_EMPTY_CLEAN)
        with unittest.mock.patch.object(upstream_lint, "_run", return_value=stub) as run:
            step = upstream_lint._docstrings(tree, "base_rev", "head_rev", "")
        self.assertEqual(step.rc, 0)
        self.assertEqual(upstream_lint.DOCSTRINGS_TIMEOUT, 300)
        run.assert_called_once_with(
            [
                "bash",
                str(upstream_lint.REPO_ROOT / ".config" / "lefthook" / "engine.sh"),
                "docstrings",
                "check",
                f"--path={tree}",
                "--base=base_rev",
                "--head=head_rev",
            ],
            upstream_lint.REPO_ROOT,
            timeout=upstream_lint.DOCSTRINGS_TIMEOUT,
        )


class FixtureTest(unittest.TestCase):
    """Shared disposable Git fixture; stub pre-commit, semgrep_scan.sh and the Praetor engine."""

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
        self.write("configure", REFERENCE_CONFIGURE)
        self.commit("avfilter/vf_x: initial fixture")
        self.base = self.git("rev-parse", "HEAD").strip()
        self.real_run = upstream_lint._run
        self.hook_rc = 0
        self.hook_effect = lambda cwd: None
        self.hook_calls = []
        self.c_rules_rc = 0
        self.c_rules_stdout = ""
        self.c_rules_json: str | None = ""
        self.c_rules_calls: list[tuple[list[str], Path, float]] = []
        self.engine_rc = 0
        self.engine_output = DOCSTRING_EMPTY_CLEAN
        self.engine_calls = []

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
        """Stub hooks, scans and the engine with full default path coverage; bound the rest."""
        if Path(argv[0]).name == upstream_lint.PRE_COMMIT:
            self.hook_calls.append((argv, Path(argv[3]).read_text(encoding="utf-8")))
            self.hook_effect(Path(cwd))
            return upstream_lint.RunResult(tuple(argv), self.hook_rc, "fixture pre-commit\n")
        if len(argv) >= 2 and argv[0] == "bash" and Path(argv[1]) == upstream_lint.SEMGREP_SCAN:
            self.c_rules_calls.append((argv, Path(cwd), timeout))
            if self.c_rules_json is not None:
                json_arg = next(item for item in argv if item.startswith("--json-output="))
                Path(json_arg.removeprefix("--json-output=")).write_text(
                    self.c_rules_json
                    or json.dumps({"paths": {"scanned": argv[5:]}, "results": [], "errors": []}),
                    encoding="utf-8",
                )
            return upstream_lint.RunResult(tuple(argv), self.c_rules_rc, self.c_rules_stdout)
        if argv[:2] == ["bash", str(upstream_lint.REPO_ROOT / upstream_lint.ENGINE_SCRIPT)]:
            self.engine_calls.append((argv, Path(cwd)))
            return upstream_lint.RunResult(tuple(argv), self.engine_rc, self.engine_output)
        return self.real_run(argv, cwd, data, timeout)

    def check(self, *args: str) -> tuple[int, str]:
        """Capture the complete gate report with controlled hook and engine results."""
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
    """Run Git and base tools in a throwaway repository; stub external hooks and engine."""

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
            "doc-reference",
            "test-program",
            "c-rules",
            "docstrings",
        ):
            self.assertIn(f"step: {name} rc: 0", output)

    def test_c_rules_finding_on_added_line_fails(self) -> None:
        """A c-rules finding touching an added source line fails the step and pipeline."""
        self.write("libavfilter/vf_x.c", SOURCE.replace("return 1", "return 2"))
        self.commit()
        self.c_rules_rc = 1
        self.c_rules_json = json.dumps(
            {
                "paths": {"scanned": ["libavfilter/vf_x.c"]},
                "results": [
                    {
                        "check_id": "ffmpeg-rtsp-connect-failure-without-network-close",
                        "path": "libavfilter/vf_x.c",
                        "start": {"line": 3},
                        "end": {"line": 3},
                        "extra": {"message": "call ff_network_close() before returning"},
                    }
                ],
                "errors": [],
            }
        )
        rc, output = self.check()
        self.assertEqual(rc, 1, output)
        self.assertIn("step: c-rules rc: 1", output)
        self.assertIn(
            "finding: libavfilter/vf_x.c:3: "
            "ffmpeg-rtsp-connect-failure-without-network-close: "
            "call ff_network_close() before returning",
            output,
        )
        self.assertIn("fix the flagged path, or pass --c-rule-reason", output)
        self.assertIn("step: test-program rc: 0", output)

    def test_c_rules_waiver_passes(self) -> None:
        """A non-blank --c-rule-reason waives a c-rules finding and prints the reason."""
        self.write("libavfilter/vf_x.c", SOURCE.replace("return 1", "return 2"))
        self.commit()
        self.c_rules_rc = 1
        self.c_rules_json = json.dumps(
            {
                "paths": {"scanned": ["libavfilter/vf_x.c"]},
                "results": [
                    {
                        "check_id": "ffmpeg-rtsp-connect-failure-without-network-close",
                        "path": "libavfilter/vf_x.c",
                        "start": {"line": 3},
                        "end": {"line": 3},
                        "extra": {"message": "call ff_network_close() before returning"},
                    }
                ],
                "errors": [],
            }
        )
        rc, output = self.check("--c-rule-reason", "caller owns network teardown")
        self.assertEqual(rc, 0, output)
        self.assertIn("step: c-rules rc: 0\nwaived: caller owns network teardown", output)

    def test_c_rules_renamed_source_tracks_only_changed_lines(self) -> None:
        """A pure rename leaves pre-existing matches untouched; editing inside one fails."""
        self.write("libavformat/rtsp_old.c", RTSP_WITHOUT_CLOSE)
        self.commit("avformat/rtsp: add mux helper")
        self.base = self.git("rev-parse", "HEAD").strip()
        self.c_rules_rc = 1
        self.c_rules_json = json.dumps(
            {
                "paths": {"scanned": ["libavformat/rtsp_new.c"]},
                "results": [
                    {
                        "check_id": "ffmpeg-rtsp-connect-failure-without-network-close",
                        "path": "libavformat/rtsp_new.c",
                        "start": {"line": 8},
                        "end": {"line": 9},
                        "extra": {"message": "call ff_network_close() before returning"},
                    }
                ],
                "errors": [],
            }
        )
        self.git("mv", "libavformat/rtsp_old.c", "libavformat/rtsp_new.c")
        self.commit("avformat/rtsp: rename mux helper")
        rc, output = self.check()
        self.assertEqual(rc, 0, output)
        self.assertEqual(self.c_rules_calls[0][0][5:], ["libavformat/rtsp_new.c"])
        self.assertIn("pre-existing (untouched): libavformat/rtsp_new.c:8:", output)

        self.git("reset", "--hard", self.base)
        self.c_rules_calls.clear()
        self.git("mv", "libavformat/rtsp_old.c", "libavformat/rtsp_new.c")
        self.write(
            "libavformat/rtsp_new.c",
            RTSP_WITHOUT_CLOSE.replace(
                "        return ret;\n    }\n", "        return -1;\n    }\n"
            ),
        )
        self.commit("avformat/rtsp: rename and adjust return")
        rc, output = self.check()
        self.assertEqual(rc, 1, output)
        self.assertEqual(self.c_rules_calls[0][0][5:], ["libavformat/rtsp_new.c"])
        self.assertIn("step: c-rules rc: 1", output)
        self.assertIn("finding: libavformat/rtsp_new.c:8:", output)

    def test_c_rules_deletion_only_hunks_and_span_boundaries(self) -> None:
        """Deletions inside or directly after a match span fail; before start_line passes."""
        self.c_rules_rc = 1
        self.c_rules_json = json.dumps(
            {
                "paths": {"scanned": ["libavformat/rtsp.c"]},
                "results": [
                    {
                        "check_id": "ffmpeg-rtsp-connect-failure-without-network-close",
                        "path": "libavformat/rtsp.c",
                        "start": {"line": 8},
                        "end": {"line": 9},
                        "extra": {"message": "call ff_network_close() before returning"},
                    }
                ],
                "errors": [],
            }
        )
        after_end = RTSP_WITHOUT_CLOSE.replace(
            "        return ret;\n", "        return ret;\n        /* after match */\n"
        )
        before_start = RTSP_WITHOUT_CLOSE.replace(
            "        ff_rtsp_close_connections(s);\n",
            "        /* before match */\n        ff_rtsp_close_connections(s);\n",
        )
        saved_base = self.base
        for label, base_source, expected_rc, marker in (
            ("inside", RTSP_WITH_CLOSE, 1, "finding: libavformat/rtsp.c:8:"),
            ("after-end", after_end, 1, "finding: libavformat/rtsp.c:8:"),
            ("before-start", before_start, 0, "pre-existing (untouched): libavformat/rtsp.c:8:"),
        ):
            with self.subTest(case=label):
                self.git("reset", "--hard", saved_base)
                self.write("libavformat/rtsp.c", base_source)
                self.commit("avformat/rtsp: add mux helper")
                self.base = self.git("rev-parse", "HEAD").strip()
                self.write("libavformat/rtsp.c", RTSP_WITHOUT_CLOSE)
                self.commit("avformat/rtsp: trim mux helper")
                rc, output = self.check()
                self.assertEqual(rc, expected_rc, output)
                self.assertIn(f"step: c-rules rc: {expected_rc}", output)
                self.assertIn(marker, output)

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

    def test_body_uninitialised_defect_word_requires_test_or_waiver(self) -> None:
        """An uninitialised-reply body fails without a test and passes when waived or neutral."""
        subject = "avformat/rtsp: check command write before using replies"
        self.write("libavfilter/vf_x.c", SOURCE.replace("return 1", "return 2"))
        self.commit(
            f"{subject}\n\nIgnoring that error can make callers inspect an uninitialised reply."
        )
        rc, output = self.check()
        self.assertEqual(rc, 1, output)
        self.assertIn("step: regression-test rc: 1", output)
        rc, output = self.check("--no-test-reason", "needs a live RTSP server")
        self.assertEqual(rc, 0, output)
        self.assertIn("step: regression-test rc: 0", output)
        self.assertIn("waived: needs a live RTSP server", output)
        self.git("commit", "--amend", "-qm", f"{subject}\n\nRename the reply helper.")
        rc, output = self.check()
        self.assertEqual(rc, 0, output)
        self.assertIn("step: regression-test rc: 0", output)

    def test_hyphenated_uninitialized_subject_requires_test_or_waiver(self) -> None:
        """A use-of-uninitialized-value subject fails without a test unless waived."""
        self.write("libavfilter/vf_x.c", SOURCE.replace("return 1", "return 2"))
        self.commit("avformat/x: use-of-uninitialized-value in keybuffer")
        rc, output = self.check()
        self.assertEqual(rc, 1, output)
        self.assertIn("step: regression-test rc: 1", output)
        rc, output = self.check("--no-test-reason", "reproducer requires MemorySanitizer")
        self.assertEqual(rc, 0, output)
        self.assertIn("step: regression-test rc: 0", output)
        self.assertIn("waived: reproducer requires MemorySanitizer", output)
        self.git("commit", "--amend", "-qm", "avformat/x: clear keybuffer")
        rc, output = self.check()
        self.assertEqual(rc, 0, output)
        self.assertIn("step: regression-test rc: 0", output)

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

    def test_doc_reference_fails(self) -> None:
        """An added reference deferring to another library's item fails doc-reference."""
        self.write("doc/encoders.texi", REFERENCE_TEXT)
        self.commit("doc/encoders: describe qpfile")
        rc, output = self.check()
        self.assertEqual(rc, 1, output)
        self.assertIn("step: doc-reference rc: 1", output)
        self.assertIn("the libsvtav1 text defers to @ref{libaom-av1-qpfile}", output)

    def test_doc_reference_waiver_passes(self) -> None:
        """A non-blank --doc-reason waives the doc-reference step and prints the reason."""
        self.write("doc/encoders.texi", REFERENCE_TEXT)
        self.commit("doc/encoders: describe qpfile")
        rc, output = self.check("--doc-reason", "both encoders parse the file alike")
        self.assertEqual(rc, 0, output)
        self.assertIn(
            "step: doc-reference rc: 0\nwaived: both encoders parse the file alike", output
        )

    def test_doc_reference_own_description_passes(self) -> None:
        """Docs that describe the format in their own section pass doc-reference."""
        text = REFERENCE_TEXT.replace(
            "in the format described for\n", "one offset per line; a gap reads as 0, like\n"
        )
        self.write("doc/encoders.texi", text)
        self.commit("doc/encoders: describe qpfile")
        rc, output = self.check()
        self.assertEqual(rc, 0, output)
        self.assertIn("step: doc-reference rc: 0", output)

    def test_doc_reference_missing_configure_fails(self) -> None:
        """Missing head configure fails doc-reference even with a doc-reason waiver."""
        self.git("rm", "configure")
        self.write("doc/encoders.texi", REFERENCE_TEXT)
        self.commit("doc/encoders: describe qpfile")
        for args in ((), ("--doc-reason", "cannot waive missing library lists")):
            with self.subTest(args=args):
                rc, output = self.check(*args)
                self.assertEqual(rc, 1, output)
                self.assertIn(
                    "step: doc-reference rc: 1\nconfigure is absent at checked head", output
                )

    def test_doc_reference_empty_library_lists_fail(self) -> None:
        """Empty head configure library lists fail doc-reference even with a waiver."""
        self.write("configure", 'EXTERNAL_LIBRARY_LIST="\n$UNLISTED\n"\n')
        self.write("doc/encoders.texi", REFERENCE_TEXT)
        self.commit("doc/encoders: describe qpfile")
        for args in ((), ("--doc-reason", "cannot waive empty library lists")):
            with self.subTest(args=args):
                rc, output = self.check(*args)
                self.assertEqual(rc, 1, output)
                self.assertIn(
                    "step: doc-reference rc: 1\n"
                    "configure library lists give no names at checked head",
                    output,
                )

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

    def test_docstrings_finding_fails(self) -> None:
        """An unwaived docstrings engine finding fails its step and the overall run."""
        self.engine_rc = 1
        self.engine_output = DOCSTRING_FINDING
        self.write("libavfilter/vf_x.c", SOURCE.replace("return 1", "return 2"))
        self.commit()
        rc, output = self.check()
        self.assertEqual(rc, 1, output)
        self.assertIn("step: docstrings rc: 1", output)
        self.assertIn("libavfilter/vf_x.c:5 ff_alpha", output)
        self.assertIn(DOCSTRING_ADVICE, output)

    def test_docstrings_waiver_passes(self) -> None:
        """A non-blank --docstring-reason waives a docstrings finding in the report."""
        self.engine_rc = 1
        self.engine_output = DOCSTRING_FINDING
        self.write("libavfilter/vf_x.c", SOURCE.replace("return 1", "return 2"))
        self.commit()
        rc, output = self.check("--docstring-reason", "internal static wrappers")
        self.assertEqual(rc, 0, output)
        self.assertIn("step: docstrings rc: 0\nwaived: internal static wrappers", output)
        self.assertIn("libavfilter/vf_x.c:5 ff_alpha", output)

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


class CRulesTest(unittest.TestCase):
    """Unit coverage of the c-rules allow-list, argv, working directory and waiver."""

    def setUp(self) -> None:
        """Set default stub state for one changed C source with added lines 11 and 12."""
        self.tree = Path("/fixture/ffmpeg")
        self.diff_names = "libavformat/rtspenc.c\0"
        self.patches = {"libavformat/rtspenc.c": "@@ -10,0 +11,2 @@\n+a\n+b\n"}
        self.stub_rc = 0
        self.stub_stdout = ""
        self.stub_json: str | None = json.dumps(
            {"paths": {"scanned": ["libavformat/rtspenc.c"]}, "results": [], "errors": []}
        )
        self.calls: list[tuple[list[str], Path, float]] = []

    def fake_git(self, tree: Path, *args: str) -> str:
        """Return stubbed changed-path or zero-context diff output for _c_rules."""
        if "--name-only" in args:
            return self.diff_names
        if args[-1] in self.patches:
            return self.patches[args[-1]]
        names = [name for name in self.diff_names.split("\0") if name]
        sections = [
            f"diff --git a/{name} b/{name}\n{self.patches.get(name, '')}".rstrip("\n")
            for name in names
        ]
        return "\n".join(sections) + ("\n" if sections else "")

    def fake_run(
        self, argv: list[str], cwd: Path, data: str | None = None, timeout: float = 60
    ) -> upstream_lint.RunResult:
        """Write stubbed JSON when configured and return the chosen RunResult."""
        self.calls.append((argv, cwd, timeout))
        if self.stub_json is not None:
            json_arg = next(item for item in argv if item.startswith("--json-output="))
            Path(json_arg.removeprefix("--json-output=")).write_text(
                self.stub_json, encoding="utf-8"
            )
        return upstream_lint.RunResult(tuple(argv), self.stub_rc, self.stub_stdout)

    def run_c_rules(self, waiver: str = "") -> upstream_lint.Step:
        """Execute _c_rules through _collect with _git and _run stubbed."""
        with (
            unittest.mock.patch.object(upstream_lint, "_git", side_effect=self.fake_git),
            unittest.mock.patch.object(upstream_lint, "_run", side_effect=self.fake_run),
        ):
            return upstream_lint._collect(
                "c-rules",
                lambda: upstream_lint._c_rules(self.tree, "base", "head", waiver),
            )

    def inter_hunk_fixture(self) -> FixtureTest:
        """Create real Git hunks around an unchanged defect with context fusion enabled."""
        fixture = FixtureTest()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        fixture.git("config", "diff.interHunkContext", "2")
        source = (
            "int ff_value(void)\n{\n    ff_first();\n    ff_second();\n"
            "    ff_third();\n    ff_fourth();\n    ff_before(1);\n"
            "\tff_old();\n    ff_between();\n    return 1;\n}\n"
        )
        fixture.write("libavfilter/vf_x.c", source)
        fixture.commit("avfilter/vf_x: add inherited defect")
        fixture.base = fixture.git("rev-parse", "HEAD").strip()
        fixture.write(
            "libavfilter/vf_x.c",
            source.replace("ff_before(1)", "ff_before(2)").replace("return 1", "return 2"),
        )
        fixture.commit()
        return fixture

    def test_inter_hunk_context_keeps_c_rule_match_untouched(self) -> None:
        """Configured context fusion cannot turn an unchanged Semgrep match into a finding."""
        fixture = self.inter_hunk_fixture()
        fixture.c_rules_rc = 1
        fixture.c_rules_json = json.dumps(
            {
                "paths": {"scanned": ["libavfilter/vf_x.c"]},
                "results": [
                    {
                        "check_id": "inherited-defect",
                        "path": "libavfilter/vf_x.c",
                        "start": {"line": 8},
                        "end": {"line": 8},
                        "extra": {"message": "inherited match"},
                    }
                ],
                "errors": [],
            }
        )
        with unittest.mock.patch.object(upstream_lint, "_run", side_effect=fixture.command):
            step = upstream_lint._c_rules(fixture.root, fixture.base, "HEAD", "")
        self.assertEqual(step.rc, 0, step.output)
        self.assertIn("pre-existing (untouched): libavfilter/vf_x.c:8:", step.output)

    def test_inter_hunk_context_keeps_line_rule_defect_untouched(self) -> None:
        """Configured context fusion cannot mark an unchanged TAB line as added."""
        fixture = self.inter_hunk_fixture()
        step = upstream_lint._mechanical(fixture.root, fixture.base, "HEAD")
        self.assertEqual(step.rc, 0, step.output)
        self.assertIn("pre-existing (untouched): libavfilter/vf_x.c:8: TAB character", step.output)

    def test_requested_path_missing_from_scanned_is_unwaivable(self) -> None:
        """Every requested path must be scanned even when a waiver is supplied."""
        self.diff_names += "libavformat/rtsp.h\0"
        self.patches["libavformat/rtsp.h"] = "@@ -1,0 +2 @@\n+x\n"
        self.stub_json = json.dumps(
            {"paths": {"scanned": ["libavformat/rtspenc.c"]}, "results": [], "errors": []}
        )
        for waiver in ("", "caller closes network"):
            with self.subTest(waiver=waiver):
                step = self.run_c_rules(waiver)
                self.assertEqual(step.rc, 2, step.output)
                self.assertNotIn("waived:", step.output)

    def test_invalid_scanned_paths_is_unwaivable(self) -> None:
        """Missing or malformed scan coverage gives rc 2 with or without a waiver."""
        for coverage in (
            {},
            {"paths": None},
            {"paths": []},
            {"paths": {}},
            {"paths": {"scanned": None}},
            {"paths": {"scanned": "libavformat/rtspenc.c"}},
            {"paths": {"scanned": {"libavformat/rtspenc.c": True}}},
            {"paths": {"scanned": ["libavformat/rtspenc.c", 7]}},
        ):
            self.stub_json = json.dumps({"results": [], "errors": [], **coverage})
            for waiver in ("", "caller closes network"):
                with self.subTest(coverage=coverage, waiver=waiver):
                    step = self.run_c_rules(waiver)
                    self.assertEqual(step.rc, 2, step.output)
                    self.assertNotIn("waived:", step.output)

    def test_no_changed_source_makes_no_run_call(self) -> None:
        """No changed library or fftools C or header source returns rc 0 without _run."""
        for names in ("", "doc/x.texi\0libavcodec/tests/x.c\0libavcodec/x86/x.asm\0"):
            with self.subTest(names=names):
                self.diff_names = names
                step = self.run_c_rules()
                self.assertEqual(step.rc, 0, step.output)
                self.assertIn("no added or modified C or header source", step.output)
                self.assertEqual(self.calls, [])

    def test_argv_working_directory_and_timeout(self) -> None:
        """Scan argv disables the target size limit and preserves tree and timeout settings."""
        self.diff_names = "libavformat/rtspenc.c\0libavformat/rtsp.h\0"
        self.patches["libavformat/rtsp.h"] = "@@ -1,0 +2,1 @@\n+x\n"
        self.stub_json = json.dumps(
            {
                "paths": {"scanned": ["libavformat/rtspenc.c", "libavformat/rtsp.h"]},
                "results": [],
                "errors": [],
            }
        )
        step = self.run_c_rules()
        self.assertEqual(step.rc, 0, step.output)
        self.assertEqual(len(self.calls), 1)
        argv, cwd, timeout = self.calls[0]
        self.assertEqual(cwd, self.tree)
        self.assertEqual(timeout, 300)
        self.assertEqual(timeout, upstream_lint.C_RULES_TIMEOUT)
        self.assertEqual(
            argv[:3],
            [
                "bash",
                str(upstream_lint.REPO_ROOT / "scripts" / "semgrep_scan.sh"),
                f"--rules={upstream_lint.REPO_ROOT / '.semgrep-ffmpeg.yml'}",
            ],
        )
        self.assertTrue(argv[3].startswith("--json-output="))
        self.assertFalse(Path(argv[3].removeprefix("--json-output=")).parent.exists())
        self.assertEqual(argv[4], "--max-target-bytes=0")
        self.assertEqual(argv[5:], ["libavformat/rtspenc.c", "libavformat/rtsp.h"])

    def test_exit_zero_empty_results_passes(self) -> None:
        """Exit status 0 with empty results and errors lists returns step rc 0."""
        step = self.run_c_rules()
        self.assertEqual(step.rc, 0, step.output)
        self.assertIn("0 finding(s)", step.output)

    def test_exit_one_result_on_added_line_fails(self) -> None:
        """A result whose line range meets an added line gives a finding and step rc 1."""
        self.stub_rc = 1
        self.stub_json = json.dumps(
            {
                "paths": {"scanned": ["libavformat/rtspenc.c"]},
                "results": [
                    {
                        "check_id": "ffmpeg-rtsp-connect-failure-without-network-close",
                        "path": "libavformat/rtspenc.c",
                        "start": {"line": 10},
                        "end": {"line": 11},
                        "extra": {"message": "first message line\nsecond detail line"},
                    }
                ],
                "errors": [],
            }
        )
        step = self.run_c_rules()
        self.assertEqual(step.rc, 1, step.output)
        self.assertIn(
            "finding: libavformat/rtspenc.c:10: "
            "ffmpeg-rtsp-connect-failure-without-network-close: first message line",
            step.output,
        )
        self.assertNotIn("second detail line", step.output)
        self.assertIn("fix the flagged path, or pass --c-rule-reason", step.output)

    def test_rules_path_prefix_stripped_from_check_id(self) -> None:
        """A check_id prefixed with the dotted rules path reports the bare rule id."""
        rule = "ffmpeg-rtsp-connect-failure-without-network-close"
        self.stub_rc = 1
        self.stub_json = json.dumps(
            {
                "paths": {"scanned": ["libavformat/rtspenc.c"]},
                "results": [
                    {
                        "check_id": f"home.user.checkout.{rule}",
                        "path": "libavformat/rtspenc.c",
                        "start": {"line": 11},
                        "end": {"line": 11},
                        "extra": {"message": "missing ff_network_close()"},
                    }
                ],
                "errors": [],
            }
        )
        step = self.run_c_rules()
        self.assertEqual(step.rc, 1, step.output)
        self.assertIn(
            f"finding: libavformat/rtspenc.c:11: {rule}: missing ff_network_close()", step.output
        )
        self.assertNotIn("home.user", step.output)

    def test_result_on_untouched_line_passes_and_lists_preexisting(self) -> None:
        """A result outside added lines is listed as pre-existing without failing."""
        self.stub_rc = 1
        self.stub_json = json.dumps(
            {
                "paths": {"scanned": ["libavformat/rtspenc.c"]},
                "results": [
                    {
                        "check_id": "ffmpeg-rtsp-connect-failure-without-network-close",
                        "path": "libavformat/rtspenc.c",
                        "start": {"line": 3},
                        "end": {"line": 5},
                        "extra": {"message": "missing ff_network_close()"},
                    }
                ],
                "errors": [],
            }
        )
        step = self.run_c_rules()
        self.assertEqual(step.rc, 0, step.output)
        self.assertIn(
            "pre-existing (untouched): libavformat/rtspenc.c:3: "
            "ffmpeg-rtsp-connect-failure-without-network-close: missing ff_network_close()",
            step.output,
        )
        self.assertNotIn("finding:", step.output)

    def test_partial_parsing_error_is_informational(self) -> None:
        """PartialParsing errors produce informational file lines without failing."""
        for error_type in ("PartialParsing", ["PartialParsing", [{"path": "libavformat/rtsp.c"}]]):
            with self.subTest(error_type=error_type):
                self.stub_json = json.dumps(
                    {
                        "paths": {"scanned": ["libavformat/rtspenc.c"]},
                        "results": [],
                        "errors": [{"type": error_type, "path": "libavformat/rtsp.c"}],
                    }
                )
                step = self.run_c_rules()
                self.assertEqual(step.rc, 0, step.output)
                self.assertIn("informational: libavformat/rtsp.c: PartialParsing", step.output)

    def test_other_error_type_gives_rc_two(self) -> None:
        """Any error type other than PartialParsing gives step rc 2 with status and output."""
        self.stub_stdout = "rule parse error\n"
        self.stub_json = json.dumps(
            {
                "paths": {"scanned": ["libavformat/rtspenc.c"]},
                "results": [],
                "errors": [{"type": "Syntax error", "path": "libavformat/rtsp.c"}],
            }
        )
        step = self.run_c_rules()
        self.assertEqual(step.rc, 2, step.output)
        self.assertTrue(step.output.startswith("command rc: 0\nrule parse error"))

    def test_unexpected_exit_statuses_give_rc_two(self) -> None:
        """Exit statuses 2, 127 and 124 give step rc 2 naming the exit status and output."""
        for status, text in (
            (2, "fatal config error\n"),
            (127, "semgrep: command not found\n"),
            (124, "command timed out\n"),
        ):
            with self.subTest(status=status):
                self.stub_rc = status
                self.stub_stdout = text
                step = self.run_c_rules()
                self.assertEqual(step.rc, 2, step.output)
                self.assertTrue(step.output.startswith(f"command rc: {status}\n{text}"))

    def test_warning_line_gives_rc_two(self) -> None:
        """A semgrep_scan: warning line in output gives step rc 2 even on exit 0 or 1."""
        for status in (0, 1):
            with self.subTest(status=status):
                self.stub_rc = status
                self.stub_stdout = (
                    "Rule demo contains warning\n"
                    "semgrep_scan: a rule in .semgrep-ffmpeg.yml draws the warning above\n"
                )
                step = self.run_c_rules()
                self.assertEqual(step.rc, 2, step.output)
                self.assertTrue(step.output.startswith(f"command rc: {status}\n"))
                self.assertIn("semgrep_scan:", step.output)

    def test_missing_json_file_gives_rc_two(self) -> None:
        """A missing JSON output file gives step rc 2 naming the exit status and output."""
        self.stub_json = None
        self.stub_stdout = "no json written\n"
        step = self.run_c_rules()
        self.assertEqual(step.rc, 2, step.output)
        self.assertTrue(step.output.startswith("command rc: 0\nno json written"))

    def test_unparseable_json_gives_rc_two(self) -> None:
        """Unparseable or structurally invalid JSON payloads give step rc 2."""
        self.stub_stdout = "scan finished\n"
        for payload in (
            "{not-json",
            "[]",
            json.dumps({"paths": {"scanned": ["libavformat/rtspenc.c"]}, "results": []}),
            json.dumps(
                {"paths": {"scanned": ["libavformat/rtspenc.c"]}, "results": [{}], "errors": []}
            ),
        ):
            with self.subTest(payload=payload):
                self.stub_json = payload
                step = self.run_c_rules()
                self.assertEqual(step.rc, 2, step.output)
                self.assertTrue(step.output.startswith("command rc: 0\nscan finished"))

    def test_waiver_on_rc_one_and_rc_two(self) -> None:
        """A non-blank reason waives rc 1 with waived: first; blank fails; rc 2 stays 2."""
        self.stub_rc = 1
        self.stub_json = json.dumps(
            {
                "paths": {"scanned": ["libavformat/rtspenc.c"]},
                "results": [
                    {
                        "check_id": "ffmpeg-rtsp-connect-failure-without-network-close",
                        "path": "libavformat/rtspenc.c",
                        "start": {"line": 11},
                        "end": {"line": 12},
                        "extra": {"message": "call ff_network_close()"},
                    }
                ],
                "errors": [],
            }
        )
        waived = self.run_c_rules("caller closes network")
        self.assertEqual(waived.rc, 0, waived.output)
        self.assertEqual(waived.output.splitlines()[0], "waived: caller closes network")
        self.assertIn("finding: libavformat/rtspenc.c:11:", waived.output)
        self.assertNotIn("pass --c-rule-reason", waived.output)

        blank = self.run_c_rules("   ")
        self.assertEqual(blank.rc, 1, blank.output)
        self.assertNotIn("waived:", blank.output)

        self.stub_rc = 127
        self.stub_stdout = "semgrep: not found\n"
        unwaivable = self.run_c_rules("caller closes network")
        self.assertEqual(unwaivable.rc, 2, unwaivable.output)
        self.assertNotIn("waived:", unwaivable.output)

    def test_empty_rule_id_gives_unwaivable_rc_two(self) -> None:
        """A check_id whose final dotted segment is empty gives non-waivable rc 2."""
        self.stub_rc = 1
        self.stub_stdout = "scan finished\n"
        for check_id in ("rules.", "."):
            with self.subTest(check_id=check_id):
                self.stub_json = json.dumps(
                    {
                        "paths": {"scanned": ["libavformat/rtspenc.c"]},
                        "results": [
                            {
                                "check_id": check_id,
                                "path": "libavformat/rtspenc.c",
                                "start": {"line": 11},
                                "end": {"line": 12},
                                "extra": {"message": "call ff_network_close()"},
                            }
                        ],
                        "errors": [],
                    }
                )
                step = self.run_c_rules()
                self.assertEqual(step.rc, 2, step.output)
                waived = self.run_c_rules("caller closes network")
                self.assertEqual(waived.rc, 2, waived.output)
                self.assertNotIn("waived:", waived.output)

    def test_unscanned_result_path_gives_unwaivable_rc_two(self) -> None:
        """A result path absent from the scanned path list gives non-waivable rc 2."""
        self.stub_rc = 1
        self.stub_stdout = "scan finished\n"
        for result_path in (
            "libavformat/rtsp.c",
            "./libavformat/rtspenc.c",
            "/fixture/ffmpeg/libavformat/rtspenc.c",
        ):
            with self.subTest(result_path=result_path):
                self.stub_json = json.dumps(
                    {
                        "paths": {"scanned": ["libavformat/rtspenc.c"]},
                        "results": [
                            {
                                "check_id": "ffmpeg-rtsp-connect-failure-without-network-close",
                                "path": result_path,
                                "start": {"line": 11},
                                "end": {"line": 12},
                                "extra": {"message": "call ff_network_close()"},
                            }
                        ],
                        "errors": [],
                    }
                )
                step = self.run_c_rules()
                self.assertEqual(step.rc, 2, step.output)
                waived = self.run_c_rules("caller closes network")
                self.assertEqual(waived.rc, 2, waived.output)
                self.assertNotIn("waived:", waived.output)

    def test_semgrep_span_boundaries_give_rc_two(self) -> None:
        """Boolean, zero and inverted start/end line numbers give step rc 2."""
        self.stub_rc = 1
        for start_line, end_line in ((True, 11), (11, True), (0, 11), (12, 11)):
            with self.subTest(start=start_line, end=end_line):
                self.stub_json = json.dumps(
                    {
                        "paths": {"scanned": ["libavformat/rtspenc.c"]},
                        "results": [
                            {
                                "check_id": "ffmpeg-rtsp-connect-failure-without-network-close",
                                "path": "libavformat/rtspenc.c",
                                "start": {"line": start_line},
                                "end": {"line": end_line},
                                "extra": {"message": "call ff_network_close()"},
                            }
                        ],
                        "errors": [],
                    }
                )
                step = self.run_c_rules()
                self.assertEqual(step.rc, 2, step.output)

    def test_partial_parsing_boundaries_give_rc_two(self) -> None:
        """Empty error paths, non-matching list types and string errors give rc 2."""
        for error_entry in (
            {"type": "PartialParsing", "path": ""},
            {"type": [], "path": "libavformat/rtspenc.c"},
            {"type": ["Syntax error"], "path": "libavformat/rtspenc.c"},
            {"type": "PartialParsing", "path": ["libavformat/rtspenc.c"]},
            "PartialParsing",
        ):
            with self.subTest(error=error_entry):
                self.stub_json = json.dumps(
                    {
                        "paths": {"scanned": ["libavformat/rtspenc.c"]},
                        "results": [],
                        "errors": [error_entry],
                    }
                )
                step = self.run_c_rules()
                self.assertEqual(step.rc, 2, step.output)


if __name__ == "__main__":
    unittest.main()
