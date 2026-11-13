# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Exercise full sweep reports against committed fixtures and optional planted heads."""

from __future__ import annotations

import os
import shutil
import subprocess  # nosec B404 - fixed local interpreter argv, bounded timeout.
import sys
import tempfile
import unittest
from pathlib import Path

from scripts import variant_sweep
from tests import test_variant_sweep as fixtures

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "variant_sweep.py"
PLANTED_BASE = "681025faf1add6567b1f97d2f1037d0e764afd0f"
REFUSED_HEAD = "b48c431d335e7dfab72b1b5fc2ca5d84f441b681"
FIXED_HEAD = "0f802749f015b7b1786e1778cb2bbf8ff1352e82"


def section(report: str, name: str) -> str:
    """Return one named class section without neighboring class rows."""
    return report.split(f"class: {name}\n", 1)[1].split("class: ", 1)[0]


class SweepClassesTest(unittest.TestCase):
    """Cover state updates, documentation claims, and isolated CLI loading."""

    setUp = fixtures.VariantSweepTest.setUp
    commit_head = fixtures.VariantSweepTest.commit_head
    sweep = fixtures.VariantSweepTest.sweep

    def full_report(self, source: str) -> str:
        """Commit a changed function and run all classes through the CLI."""
        head = self.commit_head(source)
        result = self.sweep(head, "all")
        self.assertEqual(result.returncode, 0, result.stderr)
        return self.output.read_text(encoding="utf-8")

    def test_state_after_error(self) -> None:
        """An increment after an earlier error return produces one state row."""
        report = self.full_report("int f(void) { if (r < 0) return r; s->n++; return 0; }\n")
        state = section(report, "state-advance")
        self.assertIn("| fix.c:1 | s->n++ | 1 | TODO |", state)
        self.assertEqual(state.count("| TODO |"), 1)

    def test_state_before_error(self) -> None:
        """An increment before the first error return produces no state row."""
        report = self.full_report("int f(void) { s->n++; if (r < 0) return r; return 0; }\n")
        self.assertIn("no hits", section(report, "state-advance"))

    def test_state_after_success(self) -> None:
        """Only an earlier literal-zero return does not flag a later increment."""
        report = self.full_report("int f(void) { if (eof) return 0; s->n++; return 0; }\n")
        self.assertIn("no hits", section(report, "state-advance"))

    def test_state_masks_noncode(self) -> None:
        """Comments, strings, and directives cannot plant member updates."""
        report = self.full_report(
            'int f(void) { if (r < 0) return r; /* s->n++; */ puts("s->n++;");\n'
            "#define UPDATE s->n++\nreturn 0; }\n"
        )
        self.assertIn("no hits", section(report, "state-advance"))

    def test_state_update_forms(self) -> None:
        """Pre/post increments, decrements, and compound member updates are listed."""
        report = self.full_report(
            "int f(void) { if (r < 0) return r;\n"
            "++s->n; --s.n; s.n--; s->n += 2; s->n -= amount; return 0; }\n"
        )
        state = section(report, "state-advance")
        for expression in ("++s->n", "--s.n", "s.n--", "s->n += 2", "s->n -= amount"):
            with self.subTest(expression=expression):
                self.assertIn(f"| fix.c:2 | {expression} | 1 | TODO |", state)
        self.assertEqual(state.count("| TODO |"), 5)

    def test_state_ignores_untouched_function(self) -> None:
        """An error in another function and untouched bodies cannot seed findings."""
        source = (
            "int old(void) { if (r < 0) return r; s->n++; return 0; }\nint f(void) { return 0; }\n"
        )
        self.base = self.commit_head(source)
        report = self.full_report(source.replace("int f(void) {", "int f(void) { s->n++;"))
        self.assertIn("no hits", section(report, "state-advance"))

    def test_state_separate_unary_operators(self) -> None:
        """Separate unary signs cannot masquerade as pre/post increment tokens."""
        report = self.full_report(
            "int f(void) { if (r < 0) return r;\n"
            "consume(s->n + +value); consume(value + +s->n); consume(value+++s->n); return 0; }\n"
        )
        self.assertIn("no hits", section(report, "state-advance"))

    def test_state_deletion_touches_body(self) -> None:
        """A deletion-only hunk still selects its enclosing head function body."""
        source = "int f(void) {\nif (r < 0) return r;\nconsume();\ns->n++;\nreturn 0;\n}\n"
        self.base = self.commit_head(source)
        report = self.full_report(source.replace("consume();\n", ""))
        self.assertIn("s->n++", section(report, "state-advance"))

    def doc_report(
        self,
        added: str,
        *,
        unchanged: str = "",
        name: str = "libfoo",
        configure: str = (
            'enabled libfoo && require_pkg_config libfoo "foo >= 1.2.0" foo.h foo_init\n'
        ),
        source_files: dict[str, str] | None = None,
    ) -> str:
        """Build committed Texi, configure, Makefile, and encoder source fixtures."""
        (self.repo / "doc").mkdir()
        (self.repo / "libavcodec").mkdir()
        (self.repo / "configure").write_text(configure, encoding="utf-8")
        if source_files is None:
            source_files = {"libfoo.c": "#if FOO_CHECK_VERSION(1, 6, 0)\n#endif\n"}
        filenames = tuple(source_files)
        for index in range(len(filenames)):
            filename = filenames[index]
            (self.repo / "libavcodec" / filename).write_text(
                source_files[filename], encoding="utf-8"
            )
        texi = self.repo / "doc/encoders.texi"
        content = f"@section {name}\n@subsection Options\n" + unchanged
        texi.write_text(content, encoding="utf-8")
        self.base = self.commit_head("int f(void) { return 0; }\n")
        texi.write_text(content + added, encoding="utf-8")
        return self.full_report("int f(void) { return 0; }\n")

    def test_doc_claim_floor_and_section(self) -> None:
        """A size claim uses the enclosing section plus configure and guard versions."""
        report = self.doc_report("one value per 64x64 superblock\n")
        claims = section(report, "doc-claims")
        self.assertIn(
            "| doc/encoders.texi:3 | one value per 64x64 superblock | libfoo | "
            "1.2.0, 1.6.0 | TODO | TODO |",
            claims,
        )
        self.assertEqual(claims.count("| TODO | TODO |"), 1)

    def test_doc_plain_prose(self) -> None:
        """Added prose without an allowed claim shape produces no row."""
        report = self.doc_report("This encoder supports useful options.\n")
        self.assertIn("no hits", section(report, "doc-claims"))

    def test_doc_context_is_ignored(self) -> None:
        """An unchanged size claim in diff context produces no row."""
        report = self.doc_report("Useful options.\n", unchanged="one value per 64x64 superblock\n")
        self.assertIn("no hits", section(report, "doc-claims"))

    def test_doc_claim_allowlist(self) -> None:
        """Bounded counts and approximation stems match while unrelated prose does not."""
        report = self.doc_report(
            "up to sixteen segments\nat most a total of 3 segments\n"
            "The smallest segment is selected.\nRound values before encoding.\n"
            "The encoder supports values.\n"
        )
        claims = section(report, "doc-claims")
        self.assertEqual(claims.count("| TODO | TODO |"), 4)
        self.assertNotIn("supports values", claims)

    def test_doc_unknown_library_and_floor(self) -> None:
        """Unresolved sections and missing sections retain explicit unknown values."""
        report = self.doc_report("@section libnone\n64x64 cells | claim\n")
        self.assertIn(r"64x64 cells \| claim | unknown | unknown | TODO | TODO |", report)
        (self.repo / "doc/encoders.texi").write_text("64x64 cells\n", encoding="utf-8")
        report = self.full_report("int f(void) { return 0; }\n")
        self.assertIn("64x64 cells | unknown | unknown | TODO | TODO |", report)

    def test_doc_encoder_dependency_and_objects(self) -> None:
        """Encoder dependencies and all existing Makefile objects resolve head versions."""
        report = self.doc_report(
            "64x64 cells\n",
            name="libfoo-av1",
            configure=(
                'libfoo_av1_encoder_deps="libfoo"\n'
                'enabled libfoo && require_pkg_config libfoo "foo >= 2.0.0" foo.h foo_init\n'
            ),
            source_files={
                "Makefile": "OBJS-$(CONFIG_LIBFOO_AV1_ENCODER)         += libfooenc.o libfoo.o\n",
                "libfooenc.c": "#if FOO_CHECK_VERSION(3, 1, 0)\n#endif\n",
                "libfoo.c": "#if FOO_CHECK_VERSION(3, 1, 0)\n#endif\n",
                "unrelated.c": "#if FOO_CHECK_VERSION(9, 9, 9)\n#endif\n",
            },
        )
        self.assertIn(
            "| libfoo (libfoo-av1) | 2.0.0, 3.1.0 | TODO | TODO |",
            section(report, "doc-claims"),
        )

    def test_doc_comma_section_and_build_guards(self) -> None:
        """Comma sections resolve their first encoder and combine configure/build floors."""
        report = self.doc_report(
            "64x64 cells\n",
            name="libbar, libbarrgb",
            configure=(
                'libbar_encoder_deps="libbar"\n'
                'require_cpp_condition libbar bar.h "BAR_BUILD >= 155"\n'
            ),
            source_files={
                "Makefile": "OBJS-$(CONFIG_LIBBAR_ENCODER) += libbar.o\n",
                "libbar.c": "#if BAR_BUILD >= 161\n#endif\n",
            },
        )
        self.assertIn(
            "| libbar | BAR_BUILD >= 155, BAR_BUILD >= 161 | TODO | TODO |",
            section(report, "doc-claims"),
        )

    def test_doc_missing_object_and_disallowed_macro(self) -> None:
        """Missing head objects and macros outside the version allow-list add no values."""
        report = self.doc_report(
            "64x64 cells\n",
            configure="require_pkg_config libfoo foo foo.h foo_init\n",
            source_files={
                "Makefile": "OBJS-$(CONFIG_LIBFOO_ENCODER) += missing.o libfooenc.o\n",
                "libfooenc.c": "#if HAVE_FOO >= 1\n#endif\n",
                "libfoo.c": "#if FOO_CHECK_VERSION(9, 9, 9)\n#endif\n",
            },
        )
        self.assertIn("| libfoo | unknown | TODO | TODO |", report)

    def test_doc_cpp_requirement_fallback(self) -> None:
        """A CPP requirement alone resolves a library without a dependency or pkg floor."""
        report = self.doc_report(
            "64x64 cells\n",
            configure='require_cpp_condition libfoo foo.h "FOO_BUILD >= 155"\n',
            source_files={"libfoo.c": "#if FOO_BUILD >= 161\n#endif\n"},
        )
        self.assertIn("| libfoo | FOO_BUILD >= 155, FOO_BUILD >= 161 | TODO | TODO |", report)

    def test_doc_ordered_version_shapes(self) -> None:
        """All floor and guard shapes retain distinct values in the required order."""
        report = self.doc_report(
            "64x64 cells\n",
            configure=(
                'require_pkg_config libfoo "foo >= 2.0.0" foo.h foo_init\n'
                'require_pkg_config libfoo "foo >= 2.1.0" foo.h foo_init\n'
                'require_cpp_condition libfoo foo.h "FOO_BUILD >= 155"\n'
                'require_pkg_config libother "other >= 9.0.0" other.h init\n'
            ),
            source_files={
                "Makefile": "OBJS-$(CONFIG_LIBFOO_ENCODER) += first.o second.o\n",
                "first.c": "#if FOO_BUILD >= 161\n#elif FOO_CHECK_VERSION(3, 1, 0)\n#endif\n",
                "second.c": (
                    "#if FOO_CHECK_VERSION(3, 2, 0)\n#elif FOO_BUILD < 158\n"
                    "#elif FOO_VERSION >= 4 && FOO_VERSION_MAJOR < 5\n"
                    "#elif FOO_API_VERSION >= 6\n#elif HAVE_FOO >= 1\n#endif\n"
                    "int value = FOO_BUILD >= 999;\n"
                ),
            },
        )
        self.assertIn(
            "| 2.0.0, 2.1.0, FOO_BUILD >= 155, 3.1.0, 3.2.0, FOO_BUILD >= 161, "
            "FOO_BUILD < 158, FOO_VERSION >= 4, FOO_VERSION_MAJOR < 5, "
            "FOO_API_VERSION >= 6 | TODO | TODO |",
            report,
        )

    def test_doc_comparison_claim(self) -> None:
        """One added comparison claim produces exactly one row."""
        claims = section(self.doc_report("Speeds above 8 can be set.\n"), "doc-claims")
        self.assertEqual(claims.count("| TODO | TODO |"), 1)
        self.assertIn("| Speeds above 8 can be set. |", claims)

    def test_doc_version_bound_claim(self) -> None:
        """One added version-bound claim produces exactly one row."""
        claims = section(self.doc_report("Needs 1.2 or newer.\n"), "doc-claims")
        self.assertEqual(claims.count("| TODO | TODO |"), 1)
        self.assertIn("| Needs 1.2 or newer. |", claims)

    def test_doc_range_claim(self) -> None:
        """One added signed-range claim produces exactly one row."""
        claims = section(self.doc_report("Offsets are clipped to -12..12.\n"), "doc-claims")
        self.assertEqual(claims.count("| TODO | TODO |"), 1)
        self.assertIn("| Offsets are clipped to -12..12. |", claims)

    def test_doc_size_claim(self) -> None:
        """One added size claim produces exactly one row."""
        claims = section(self.doc_report("A line longer than 1 MiB is refused.\n"), "doc-claims")
        self.assertEqual(claims.count("| TODO | TODO |"), 1)
        self.assertIn("| A line longer than 1 MiB is refused. |", claims)

    def test_doc_claim_shape_allowlist_boundaries(self) -> None:
        """Allowed variants match while numberless and overly distant comparisons fail."""
        examples = (
            "below two",
            "over 2",
            "under 2",
            "exceed 2",
            "exceeds 2",
            "greater than 2",
            "less than 2",
            "more than 2",
            "fewer than 2",
            "higher than 2",
            "lower than a total of two",
            "1.2 or older",
            "1.2 or later",
            "1.2 or earlier",
            "1.2 or above",
            "1.2 or below",
            "newer than 1",
            "older than 1.2",
            "-12 to 12",
            "-12 .. -1",
            "1KiB",
            "1 GiB",
            "1 KB",
            "1 MB",
            "1 GB",
            "1 byte",
            "2 bytes",
            "1 bit",
            "2 bits",
            "ABOVE EIGHT",
        )
        claims = section(self.doc_report("\n".join(examples) + "\n"), "doc-claims")
        self.assertEqual(claims.count("| TODO | TODO |"), len(examples))

    def test_doc_claim_shape_negatives(self) -> None:
        """Numberless comparisons, distant numbers, and unlisted shapes produce no hits."""
        report = self.doc_report(
            "above the threshold\nunder the hood\nabove a total of exactly 8\n"
            "1 widget\n1.2 or compatible\nFOO_BUILD >= 161\n"
        )
        self.assertIn("no hits", section(report, "doc-claims"))

    def test_doc_distinct_guards(self) -> None:
        """Repeated version guards are deduplicated in the oldest-version list."""
        self.doc_report("64x64 cells\n")
        (self.repo / "libavcodec/libfoo.c").write_text(
            "#if FOO_CHECK_VERSION(1, 6, 0)\n#endif\n"
            "#if FOO_CHECK_VERSION(1, 6, 0)\n#endif\n"
            "#if FOO_CHECK_VERSION(2, 0, 0)\n#endif\n",
            encoding="utf-8",
        )
        texi = self.repo / "doc/encoders.texi"
        texi.write_text(texi.read_text(encoding="utf-8") + "128x128 cells\n", encoding="utf-8")
        report = self.full_report("int f(void) { return 0; }\n")
        self.assertIn("| 1.2.0, 1.6.0, 2.0.0 | TODO | TODO |", section(report, "doc-claims"))

    def test_clean_control(self) -> None:
        """An empty diff emits exactly three no-hits sections."""
        result = self.sweep(self.base, "all")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.output.read_text(encoding="utf-8").count("no hits"), 3)
        self.assertTrue(variant_sweep.validate_full_report(self.output, self.base))


class FullReportTest(unittest.TestCase):
    """Reject incomplete or unreviewed full reports without importing sweep modules."""

    def reviewed_report(self) -> str:
        """Return a reviewed example containing all three table schemas."""
        sha = "0" * 40
        return (
            f"class: unchecked-return\nhead: {sha}\n{variant_sweep.TABLE}\n"
            "| --- | --- | --- | --- | --- |\n"
            "| a.c:1 | f | discarded | no | fold |\n"
            f"class: state-advance\nhead: {sha}\n"
            "| file:line | expression | first earlier error return line | disposition |\n"
            "| --- | --- | --- | --- |\n"
            "| a.c:2 | s->n++ | 1 | no-variant: intentional |\n"
            f"class: doc-claims\nhead: {sha}\n"
            "| file:line | claim text | library | oldest | newest | disposition |\n"
            "| --- | --- | --- | --- | --- | --- |\n"
            "| doc/a.texi:3 | 64x64 | libfoo | 1.2.0 | 2.0.0 | "
            "checked: libfoo 1.2.0 2.0.0 upstream/file.c:7; upstream/other.c:9 |\n"
        )

    def valid(self, report: str) -> bool:
        """Validate supplied text using the exact-head full-report API."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sweep.md"
            path.write_text(report, encoding="utf-8")
            return variant_sweep.validate_full_report(path, "0" * 40)

    def test_reviewed_report(self) -> None:
        """All three sections with reviewed rows satisfy the full validator."""
        self.assertTrue(self.valid(self.reviewed_report()))

    def test_missing_class(self) -> None:
        """A missing doc-claims section fails closed."""
        self.assertFalse(self.valid(self.reviewed_report().split("class: doc-claims")[0]))

    def test_duplicate_class(self) -> None:
        """A duplicated class cannot stand in for required coverage."""
        report = self.reviewed_report()
        self.assertFalse(
            self.valid(report + "class: doc-claims\nhead: " + "0" * 40 + "\nno hits\n")
        )

    def test_doc_todo(self) -> None:
        """An unresolved documentation disposition fails validation."""
        report = self.reviewed_report()
        self.assertFalse(
            self.valid(
                report.replace(
                    "checked: libfoo 1.2.0 2.0.0 upstream/file.c:7; upstream/other.c:9", "TODO"
                )
            )
        )

    def test_checked_missing_newest(self) -> None:
        """A checked disposition must name both oldest and newest versions."""
        self.assertFalse(
            self.valid(
                self.reviewed_report().replace(
                    "libfoo 1.2.0 2.0.0 upstream", "libfoo 1.2.0 upstream"
                )
            )
        )

    def test_wrong_section_head(self) -> None:
        """Every section must name the exact requested head."""
        self.assertFalse(
            self.valid(self.reviewed_report().replace("head: " + "0" * 40, "head: " + "1" * 40, 1))
        )

    def test_escaped_pipe(self) -> None:
        """Escaped claim pipes preserve table columns during validation."""
        self.assertTrue(self.valid(self.reviewed_report().replace("64x64", r"64x64 \| cells")))

    def test_class_dispositions(self) -> None:
        """State dispositions and documentation reasons use separate allow-lists."""
        for disposition in ("fold", "follow-up: separate patch", "no-variant: intentional"):
            with self.subTest(disposition=disposition):
                self.assertTrue(
                    self.valid(
                        self.reviewed_report().replace("no-variant: intentional", disposition)
                    )
                )
        self.assertTrue(
            self.valid(
                self.reviewed_report().replace(
                    "checked: libfoo 1.2.0 2.0.0 upstream/file.c:7; upstream/other.c:9",
                    "no-claim: prose only",
                )
            )
        )
        for disposition in ("fold", "TODO", "no-claim:", "checked: libfoo 1.2.0 2.0.0"):
            with self.subTest(disposition=disposition):
                self.assertFalse(
                    self.valid(
                        self.reviewed_report().replace(
                            "checked: libfoo 1.2.0 2.0.0 upstream/file.c:7; upstream/other.c:9",
                            disposition,
                        )
                    )
                )

    def test_isolated_validator_load(self) -> None:
        """The full validator loads under isolated Python without sibling sweep modules."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copyfile(SCRIPT, root / "variant_sweep.py")
            (root / "report.md").write_text(self.reviewed_report(), encoding="utf-8")
            code = (
                "import importlib.util,pathlib,sys; "
                "spec=importlib.util.spec_from_file_location('validator',sys.argv[1]); "
                "module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module); "
                "valid=module.validate_full_report(pathlib.Path(sys.argv[2]),'0'*40); "
                "sys.exit(0 if valid else 1)"
            )
            result = subprocess.run(  # nosec B603 - local copied helper, isolated interpreter.
                [
                    sys.executable,
                    "-I",
                    "-B",
                    "-c",
                    code,
                    str(root / "variant_sweep.py"),
                    str(root / "report.md"),
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)


class PlantedReplayTest(unittest.TestCase):
    """Replay supplied FFmpeg objects only when the dispatcher provides a tree."""

    def replay(self, head: str) -> str:
        """Run all classes when the external tree and both objects are available."""
        tree = os.environ.get("FFMPEG_QPFILE_TREE")
        if not tree:
            self.skipTest("FFMPEG_QPFILE_TREE unset; dispatcher supplies the FFmpeg tree")
        for revision in (PLANTED_BASE, head):
            try:
                variant_sweep.git(tree, "cat-file", "-e", f"{revision}^{{commit}}")
            except (RuntimeError, OSError, subprocess.TimeoutExpired):
                self.skipTest(f"FFMPEG_QPFILE_TREE lacks object {revision}")
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "sweep.md"
            result = subprocess.run(  # nosec B603 - fixed local CLI, bounded deadline.
                [
                    sys.executable,
                    "-B",
                    str(SCRIPT),
                    "--class",
                    "all",
                    PLANTED_BASE,
                    head,
                    "-o",
                    str(output),
                ],
                cwd=tree,
                capture_output=True,
                text=True,
                timeout=180,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            return output.read_text(encoding="utf-8")

    def test_refused_frame(self) -> None:
        """The refused-frame head flags both delayed ROI frame counters."""
        state = section(self.replay(REFUSED_HEAD), "state-advance")
        for path in ("libavcodec/libaomenc.c", "libavcodec/libsvtav1.c"):
            self.assertRegex(state, rf"\| {path}:\d+ \| [^\n]*roi_input_frame[^\n]*\| TODO \|")

    def test_fixed_counter_and_doc_claim(self) -> None:
        """The fixed head clears ROI counters and lists the SVT superblock claim."""
        report = self.replay(FIXED_HEAD)
        self.assertRegex(
            section(report, "doc-claims"),
            r"doc/encoders\.texi:\d+ \| [^\n]*64x64[^\n]*\| libsvtav1 \|",
        )
        for row in section(report, "state-advance").splitlines():
            if "libavcodec/libaomenc.c:" in row or "libavcodec/libsvtav1.c:" in row:
                self.assertNotIn("roi_input_frame", row)

    def test_planted_clean_control(self) -> None:
        """The planted base compared with itself produces no hits in every class."""
        report = self.replay(PLANTED_BASE)
        self.assertEqual(report.count("no hits"), 3)

    def test_libaom_comparison_claim(self) -> None:
        """The fixed head resolves the above-eight claim to libaom and its 2.0.0 floor."""
        self.assertRegex(
            section(self.replay(FIXED_HEAD), "doc-claims"),
            r"\| doc/encoders\.texi:\d+ \| [^\n|]*above 8[^\n|]* \| "
            r"libaom[^|]* \| [^|]*2\.0\.0[^|]* \|",
        )


if __name__ == "__main__":
    unittest.main()
