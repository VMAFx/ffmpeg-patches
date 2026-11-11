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

from tests.helpers import GIT_ENVIRONMENT_NAMES, isolated_git_environment

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
        self.assertEqual(step.output.count("finding:"), 2)

    def test_options_between_format_and_input(self) -> None:
        """Input options and quoted option values preserve source classification."""
        source = FATE_RED.replace(
            "-f lavfi -i", '-f lavfi -thread_queue_size 8 -dumpgraph "a b" -i'
        )
        self.assertEqual(self.check_source(source).rc, 1)

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

    def commit(self, subject="avfilter/vf_x: fix y"):
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
        """A correctly named clean commit passes all five collected checks."""
        self.write("libavfilter/vf_x.c", SOURCE.replace("return 1", "return 2"))
        self.commit()
        rc, output = self.check()
        self.assertEqual(rc, 0, output)
        for name in ("pre-commit", "commit-msg", "patcheck", "developer.texi", "fate-deps"):
            self.assertIn(f"step: {name} rc: 0", output)

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
        self.commit("avfilter/vf_x: fix y")
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
