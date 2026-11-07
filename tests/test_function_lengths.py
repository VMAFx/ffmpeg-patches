# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Tests for the HISS-04 function length gate in scripts/series.py."""

from __future__ import annotations

import contextlib
import io
import json
import subprocess  # nosec B404 - fixed Python argv for dependency-isolation tests.
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

from scripts import series
from tests.helpers import capture_main, init_upstream, run_git, write_base


def build_c_function(
    name: str,
    total_lines: int,
    multiline: bool = False,
    nested: bool = False,
) -> list[str]:
    lines = [f"int {name}(int a,", "      int b)", "{"] if multiline else [f"int {name}(void)", "{"]
    body_target = total_lines - len(lines) - 1
    if nested and body_target >= 3:
        lines.extend(["    if (1) {", "        int x = 0;", "    }"])
        body_target -= 3
    for i in range(body_target):
        lines.append(f"    int v_{i} = {i};")
    lines.append("}")
    return lines


class LengthFixture:
    def __init__(self, scratch: Path) -> None:
        self.tree = scratch / "upstream"
        self.root = scratch / "series"
        self.base = init_upstream(
            self.tree, self.root, lambda: self.write_file("lib.c", build_c_function("f", 58))
        )
        (self.root / "series.txt").write_text("# header\n", encoding="utf-8")

    def write_file(self, relpath: str, lines: list[str]) -> None:
        """Write the supplied source lines to the requested fixture path."""
        path = self.tree / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def commit_file(self, relpath: str, lines: list[str], msg: str) -> None:
        self.write_file(relpath, lines)
        run_git(self.tree, "add", relpath)
        run_git(self.tree, "commit", "-q", "-s", "-m", msg)

    def export(self) -> list[str]:
        """Export fixture patches and return their names."""
        with contextlib.redirect_stderr(io.StringIO()):
            names = series.export_series(self.root, self.tree)
        for name in names:
            sidecar = (self.root / "patches" / name).with_suffix(".toml")
            if not sidecar.exists():
                sidecar.write_text('status = "unsubmitted"\nconsumers = ["a"]\n', encoding="utf-8")
        return names

    def set_exception(self, patch_name: str, lines: int, **overrides: str) -> None:
        """Write the requested function-length exception into fixture metadata."""
        path = overrides.get("path", "lib.c")
        function = overrides.get("function", "f")
        category = overrides.get("category", "B")
        entered = overrides.get("entered", "2026-10-18")
        expires = overrides.get("expires", "2026-10-21")
        reason = overrides.get("reason", "upstream function bloat")
        sidecar = (self.root / "patches" / patch_name).with_suffix(".toml")
        content = (
            'status = "unsubmitted"\n'
            'consumers = ["a"]\n\n'
            "[[exceptions]]\n"
            'rule = "HISS-04"\n'
            f'path = "{path}"\n'
            f'function = "{function}"\n'
            f"lines = {lines}\n"
            f'category = "{category}"\n'
            f'entered = "{entered}"\n'
            f'expires = "{expires}"\n'
            f'reason = "{reason}"\n'
        )
        sidecar.write_text(content, encoding="utf-8")


def run_series_cli(root: Path, *args: str) -> tuple[int, str, str]:
    return capture_main(series.main, "--root", str(root), *args)


def check_lengths(fixture: LengthFixture) -> tuple[int, str, str]:
    fixture.export()
    return run_series_cli(fixture.root, "lengths", str(fixture.tree), "--today", "2026-10-18")


def run_without_tree_sitter(source: str) -> subprocess.CompletedProcess[str]:
    """Run Python source in a subprocess with tree-sitter imports blocked."""
    blocked_imports = (
        "import sys\nsys.modules['tree_sitter'] = None\nsys.modules['tree_sitter_c'] = None\n"
    )
    return subprocess.run(  # nosec B603 - fixed interpreter and test-controlled source.
        [sys.executable, "-c", blocked_imports + source],
        cwd=series.ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


class TestFunctionLengths(unittest.TestCase):
    def setUp(self) -> None:
        self._scratch = tempfile.TemporaryDirectory()
        self.fx = LengthFixture(Path(self._scratch.name))

    def tearDown(self) -> None:
        self._scratch.cleanup()

    def test_planted_growth_without_exception_fails(self) -> None:
        self.fx.commit_file("lib.c", build_c_function("f", 61), "grow f to 61")
        rc, out, _ = check_lengths(self.fx)
        self.assertEqual(rc, 1)
        msg = "58 -> 61 lines, over the 60-line cap (HISS-04) without a live exception"
        self.assertIn(msg, out)
        self.assertIn("1 function(s) over 60 lines, 0 excused, 1 failing", out)

    def test_boundary_growth_passes(self) -> None:
        self.fx.commit_file("lib.c", build_c_function("f", 60), "grow f to 60")
        rc, out, _ = check_lengths(self.fx)
        self.assertEqual(rc, 0)
        self.assertIn("0 function(s) over 60 lines, 0 excused, 0 failing", out)

    def test_lengths_without_tree_sitter_exits_two(self) -> None:
        """Missing tree-sitter exits two with an install hint and no traceback."""
        self.fx.commit_file("lib.c", build_c_function("f", 61), "grow f to 61")
        self.fx.export()
        script = str(series.ROOT / "scripts" / "series.py")
        argv = [script, "--root", str(self.fx.root), "lengths", str(self.fx.tree)]
        result = run_without_tree_sitter(
            f"import runpy\nsys.argv = {argv!r}\nrunpy.run_path({script!r}, run_name='__main__')\n"
        )
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertTrue(result.stderr.startswith("error:"), result.stderr)
        self.assertIn("requirements-dev.txt", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_live_exception_calibration_and_boundaries(self) -> None:
        self.fx.commit_file("lib.c", build_c_function("f", 61), "grow f to 61")
        names = self.fx.export()
        patch_name = names[0]

        self.fx.set_exception(patch_name, lines=61)
        rc, out, _ = run_series_cli(
            self.fx.root, "lengths", str(self.fx.tree), "--today", "2026-10-18"
        )
        self.assertEqual(rc, 0)
        self.assertIn("58 -> 61 lines (excused)", out)
        self.assertIn("1 function(s) over 60 lines, 1 excused, 0 failing", out)

        self.fx.set_exception(patch_name, lines=60)
        rc, out, _ = run_series_cli(
            self.fx.root, "lengths", str(self.fx.tree), "--today", "2026-10-18"
        )
        self.assertEqual(rc, 1)
        self.assertIn("without a live exception", out)

        self.fx.set_exception(patch_name, lines=62)
        rc, out, err = run_series_cli(
            self.fx.root, "lengths", str(self.fx.tree), "--today", "2026-10-18"
        )
        self.assertEqual(rc, 1)
        self.assertIn("lines=62 > measured 61", err)

    def test_expired_exception_fails(self) -> None:
        self.fx.commit_file("lib.c", build_c_function("f", 61), "grow f to 61")
        names = self.fx.export()
        self.fx.set_exception(names[0], lines=61, entered="2026-01-01", expires="2026-01-04")
        rc, out, _ = run_series_cli(
            self.fx.root, "lengths", str(self.fx.tree), "--today", "2026-01-05"
        )
        self.assertEqual(rc, 1)
        self.assertIn("without a live exception", out)

    def test_unreadable_metadata_fails(self) -> None:
        """Verify unreadable metadata fails."""
        self.fx.commit_file("lib.c", build_c_function("f", 59), "grow f to 59")
        names = self.fx.export()
        sidecar = (self.fx.root / "patches" / names[0]).with_suffix(".toml")
        sidecar.write_text("[[exceptions]\n", encoding="utf-8")
        rc, _, err = run_series_cli(
            self.fx.root, "lengths", str(self.fx.tree), "--today", "2026-10-18"
        )
        self.assertEqual(rc, 1)
        self.assertIn(sidecar.name, err)

    def test_stale_exception_fails(self) -> None:
        lines = build_c_function("f", 58)
        lines[1] = "    /* touched without growing */"
        self.fx.commit_file("lib.c", lines, "touch without growing")
        names = self.fx.export()
        self.fx.set_exception(names[0], lines=65)
        rc, _, err = run_series_cli(
            self.fx.root, "lengths", str(self.fx.tree), "--today", "2026-10-18"
        )
        self.assertEqual(rc, 1)
        self.assertIn("stale exception for lib.c f", err)

    def test_new_function_over_cap_fails(self) -> None:
        lines = build_c_function("f", 58) + [""] + build_c_function("new_f", 61)
        self.fx.commit_file("lib.c", lines, "add new function 61 lines")
        rc, out, _ = check_lengths(self.fx)
        self.assertEqual(rc, 1)
        msg = "new_f 0 -> 61 lines, over the 60-line cap (HISS-04) without a live exception"
        self.assertIn(msg, out)

    def test_untouched_and_shortened_functions_not_flagged(self) -> None:
        base_lines = build_c_function("f", 58) + [""] + build_c_function("long_fn", 80)
        self.fx.commit_file("lib.c", base_lines, "base with long_fn")
        run_git(self.fx.tree, "commit", "--amend", "-q", "-m", "base with long_fn")
        self.fx.base = run_git(self.fx.tree, "rev-parse", "HEAD")
        write_base(self.fx.root, self.fx.base)

        lines_untouched = build_c_function("f", 59) + [""] + build_c_function("long_fn", 80)
        self.fx.commit_file("lib.c", lines_untouched, "grow f, long_fn untouched")
        rc, out, _ = check_lengths(self.fx)
        self.assertEqual(rc, 0)
        self.assertIn("0 function(s) over 60 lines, 0 excused, 0 failing", out)

        # Shortened, with one changed line inside, so the patch adds a line to it.
        long_fn = build_c_function("long_fn", 75)
        long_fn[2] = "    int changed = 1;"
        lines_shortened = build_c_function("f", 58) + [""] + long_fn
        run_git(self.fx.tree, "reset", "--hard", self.fx.base)
        self.fx.commit_file("lib.c", lines_shortened, "shorten long_fn")
        rc, out, _ = check_lengths(self.fx)
        self.assertEqual(rc, 0)
        self.assertIn("0 function(s) over 60 lines, 0 excused, 0 failing", out)

    def test_multiline_signature_counts_from_first_line(self) -> None:
        lines = build_c_function("multi_f", 61, multiline=True, nested=True)
        self.fx.commit_file("lib.c", lines, "add multi_f 61 lines")
        names = self.fx.export()

        rc, out, _ = run_series_cli(
            self.fx.root, "lengths", str(self.fx.tree), "--today", "2026-10-18"
        )
        self.assertEqual(rc, 1)
        self.assertIn("multi_f 0 -> 61 lines", out)

        self.fx.set_exception(names[0], lines=61, function="multi_f")
        rc, out, _ = run_series_cli(
            self.fx.root, "lengths", str(self.fx.tree), "--today", "2026-10-18"
        )
        self.assertEqual(rc, 0)
        self.assertIn("multi_f 0 -> 61 lines (excused)", out)

    def test_commit_count_mismatch_exits_two(self) -> None:
        self.fx.commit_file("lib.c", build_c_function("f", 59), "commit one")
        self.fx.export()
        self.fx.commit_file("lib.c", build_c_function("f", 60), "commit two unexported")
        rc, _, err = run_series_cli(self.fx.root, "lengths", str(self.fx.tree))
        self.assertEqual(rc, 2)
        self.assertIn("commit count 2 differs from series length 1", err)

    def test_metadata_validation_errors(self) -> None:
        """Verify metadata validation errors."""
        sidecar = self.fx.root / "test.toml"

        def write_sidecar(exc_table: str) -> series.Findings:
            f = series.Findings()
            content = f'status = "unsubmitted"\nconsumers = ["a"]\n\n[[exceptions]]\n{exc_table}\n'
            sidecar.write_text(content, encoding="utf-8")
            series.check_metadata(sidecar, f, today=date(2026, 10, 18))
            return f

        # Category D with entered + 119 days
        f = write_sidecar(
            'rule = "HISS-04"\npath = "lib.c"\nfunction = "f"\nlines = 70\n'
            'category = "D"\nentered = "2026-01-01"\nexpires = "2026-04-30"\nreason = "r"'
        )
        self.assertTrue(any("interval mismatch: category D" in e for e in f.errors))

        # Category A with entered + 91 days
        f = write_sidecar(
            'rule = "HISS-04"\npath = "lib.c"\nfunction = "f"\nlines = 70\n'
            'category = "A"\nentered = "2026-01-01"\nexpires = "2026-04-03"\nreason = "r"'
        )
        self.assertTrue(any("interval mismatch: category A" in e for e in f.errors))

        # Unknown key
        f = write_sidecar(
            'rule = "HISS-04"\npath = "lib.c"\nfunction = "f"\nlines = 70\n'
            'category = "B"\nentered = "2026-10-18"\nexpires = "2026-10-21"\n'
            'reason = "r"\nextra = "bad"'
        )
        self.assertTrue(any("unknown key 'extra'" in e for e in f.errors))

        # lines <= 60
        f = write_sidecar(
            'rule = "HISS-04"\npath = "lib.c"\nfunction = "f"\nlines = 60\n'
            'category = "B"\nentered = "2026-10-18"\nexpires = "2026-10-21"\nreason = "r"'
        )
        self.assertTrue(any("'lines' must be > 60" in e for e in f.errors))

    def test_report_file_appends_summary(self) -> None:
        """Verify report file appends summary."""
        self.fx.commit_file("lib.c", build_c_function("f", 60), "grow f to 60")
        self.fx.export()
        report_file = self.fx.root / "report.md"
        report_file.write_text("# Initial Report\n\n", encoding="utf-8")
        rc, _, _ = run_series_cli(
            self.fx.root,
            "lengths",
            str(self.fx.tree),
            "--report",
            str(report_file),
            "--today",
            "2026-10-18",
        )
        self.assertEqual(rc, 0)
        content = report_file.read_text(encoding="utf-8")
        expected = "# Initial Report\n\n0 function(s) over 60 lines, 0 excused, 0 failing\n"
        self.assertIn(expected, content)


class TestExceptionFunctionNames(unittest.TestCase):
    """Exception names match identifiers and parser-produced macro calls."""

    def check_name(self, name: object) -> series.Findings:
        """Validate a function name with otherwise valid exception fields."""
        entry = {
            "rule": "HISS-04",
            "path": "lib.c",
            "function": name,
            "lines": 61,
            "category": "B",
            "reason": "upstream function bloat",
        }
        findings = series.Findings()
        series.check_exception_fields(entry, "test.toml", findings)
        return findings

    def test_accepted_function_names(self) -> None:
        """Plain identifiers and macro-generated names validate."""
        for name in ("render_charset", "fn(filter_plane)", "FUNC(put)", "DEF(a, 8)"):
            with self.subTest(name=name):
                self.assertEqual(self.check_name(name).errors, [])

    def test_rejected_function_names(self) -> None:
        """Malformed, multiline, non-ASCII and non-string names fail."""
        error = "function must be a C identifier or a macro call such as FUNC(name)"
        names = (
            "",
            "1fn",
            "fn(",
            "fn(a))",
            "fn(a)(b)",
            "fn((a))",
            "fn(a)x",
            "fn\n",
            "fünction",
            "fn()",
            "fn(a,)",
            "fn(a + b)",
            "fn(1a)",
            "fn(a\n)",
            "fn(é)",
            None,
            1,
        )
        for name in names:
            with self.subTest(name=name):
                self.assertEqual(
                    self.check_name(name).errors,
                    [f"test.toml: {error}"],
                )

    def test_parsed_function_names_round_trip(self) -> None:
        """Every fixture and macro function name validates as an exception."""
        fixture = Path(__file__).parent / "fixtures" / "function_spans" / "ffmpeg_style.c.txt"
        sources = (fixture.read_text(encoding="utf-8"), "static void FUNC(put)(int x)\n{\n}\n")
        for source in sources:
            longest, all_spans = series.parse_function_spans(source)
            self.assertTrue(longest)
            self.assertEqual(longest.keys(), all_spans.keys())
            for name in all_spans:
                with self.subTest(name=name):
                    self.assertEqual(self.check_name(name).errors, [])

    def test_macro_spacing_round_trip(self) -> None:
        """Whitespace collapses without adding spaces around punctuation."""
        for name, expected in (
            ("DEF(a,8)", "DEF(a,8)"),
            ("DEF(a,  8)", "DEF(a, 8)"),
            ("DEF ( a ,\n\t8 )", "DEF ( a , 8 )"),
        ):
            with self.subTest(name=name):
                longest, _ = series.parse_function_spans(f"static void {name}(int x)\n{{\n}}\n")
                self.assertEqual(list(longest), [expected])
                self.assertEqual(self.check_name(expected).errors, [])


class TestParseFunctionSpans(unittest.TestCase):
    """Direct tests for tree-sitter parse_function_spans and HISS-04 spans."""

    def test_large_source_repeated_parses_in_subprocess(self) -> None:
        """Repeated large parses preserve exact spans without crashing Python."""
        source = (
            "import unittest\n"
            "from scripts import series\n"
            "text = ''.join(f'int f{i}(void)\\n{{\\n    return {i};\\n}}\\n' "
            "for i in range(2000))\n"
            "case = unittest.TestCase()\n"
            "for iteration in range(5):\n"
            "    longest, all_spans = series.parse_function_spans(text)\n"
            "    case.assertEqual(len(longest), 2000)\n"
            "    case.assertEqual(len(all_spans), 2000)\n"
            "    for i in range(2000):\n"
            "        expected = series.FunctionSpan(f'f{i}', 4 * i + 1, 4 * i + 4, 4)\n"
            "        case.assertEqual(longest[f'f{i}'], expected)\n"
            "        case.assertEqual(all_spans[f'f{i}'], [expected])\n"
            "    print(f'parse {iteration + 1}: 2000 exact spans', flush=True)\n"
        )
        result = subprocess.run(  # nosec B603 - fixed interpreter and test-controlled source.
            [sys.executable, "-c", source],
            cwd=series.ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        self.assertEqual(
            result.returncode, 0, f"rc {result.returncode}\n{result.stdout}\n{result.stderr}"
        )

    def test_parser_avoids_point_accessors(self) -> None:
        """Parser source never accesses the unsafe coordinate properties."""
        source = (series.ROOT / "scripts" / "series.py").read_text(encoding="utf-8")
        self.assertNotIn("start_point", source)
        self.assertNotIn("end_point", source)

    def test_closing_brace_at_end_of_file(self) -> None:
        """A final closing brace needs no trailing newline for its span."""
        longest, all_spans = series.parse_function_spans("int f(void)\n{\n    return 0;\n}")
        expected = series.FunctionSpan("f", 1, 4, 4)
        self.assertEqual(longest, {"f": expected})
        self.assertEqual(all_spans, {"f": [expected]})

    def test_crlf_line_endings(self) -> None:
        """CRLF input preserves function start and closing brace lines."""
        text = "// café\r\n\r\nint f(void)\r\n{\r\n    return 0;\r\n}\r\n"
        longest, all_spans = series.parse_function_spans(text)
        expected = series.FunctionSpan("f", 3, 6, 4)
        self.assertEqual(longest, {"f": expected})
        self.assertEqual(all_spans, {"f": [expected]})

    def test_incomplete_function_ending_after_newline(self) -> None:
        """A missing brace after a directive retains the exclusive end row."""
        text = "int f(void)\n{\n#define X 1\n"
        tree = series._get_c_parser().parse(text.encode("utf-8"))
        node = series._collect_function_definitions(tree.root_node)[0]
        self.assertTrue(node.has_error)
        self.assertEqual(node.end_byte, len(text.encode("utf-8")))
        longest, all_spans = series.parse_function_spans(text)
        expected = series.FunctionSpan("f", 1, 4, 4)
        self.assertEqual(longest, {"f": expected})
        self.assertEqual(all_spans, {"f": [expected]})

    def test_module_import_without_tree_sitter(self) -> None:
        """scripts.series imports when tree-sitter is not installed."""
        result = run_without_tree_sitter("import scripts.series\n")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_parse_without_tree_sitter_reports_install_command(self) -> None:
        """Parsing without tree-sitter names the requirements-dev.txt install."""
        result = run_without_tree_sitter(
            "import unittest\n"
            "from scripts import series\n"
            "with unittest.TestCase().assertRaisesRegex(\n"
            "    series.SeriesError, 'requirements-dev.txt'\n"
            "):\n"
            "    series.parse_function_spans('int f(void) {\\n}\\n')\n"
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_traversal_limit_raises_series_error(self) -> None:
        """Exhausting the traversal limit raises a tool error with the node bound."""
        tree = series._get_c_parser().parse(b"int f(void) { return 0; }\n")
        with self.assertRaisesRegex(series.SeriesError, "tree traversal exceeded limit of 1 nodes"):
            series._collect_function_definitions(tree.root_node, max_steps=1)

    def test_ffmpeg_style_spans_fixture_reproduced(self) -> None:
        """New parser reproduces ffmpeg_style.spans.json exactly."""
        base_dir = Path(__file__).resolve().parent
        fixture_c = base_dir / "fixtures" / "function_spans" / "ffmpeg_style.c.txt"
        fixture_json = base_dir / "fixtures" / "function_spans" / "ffmpeg_style.spans.json"
        text = fixture_c.read_text(encoding="utf-8")
        expected = json.loads(fixture_json.read_text(encoding="utf-8"))
        _, all_spans = series.parse_function_spans(text)
        measured = {
            name: [[s.start, s.end, s.length] for s in spans] for name, spans in all_spans.items()
        }
        self.assertEqual(measured, expected)

    def test_same_line_brace(self) -> None:
        """Same-line opening brace parses to the expected span."""
        snippet = "int f(void) {\n    return 0;\n}\n"
        longest, all_spans = series.parse_function_spans(snippet)
        expected = series.FunctionSpan("f", 1, 3, 3)
        self.assertEqual(longest, {"f": expected})
        self.assertEqual(all_spans, {"f": [expected]})

    def test_indented_function_body_inside_if(self) -> None:
        """Indented function body inside #if with { not at column 0."""
        snippet = (
            "#if CONFIG_FOO\n    int indented_fn(void)\n    {\n        return 42;\n    }\n#endif\n"
        )
        longest, all_spans = series.parse_function_spans(snippet)
        expected = series.FunctionSpan("indented_fn", 2, 5, 4)
        self.assertEqual(longest, {"indented_fn": expected})
        self.assertEqual(all_spans, {"indented_fn": [expected]})

    def test_macro_name_verbatim_whitespace_collapsed(self) -> None:
        """Macro-generated signature FUNC(put) extracts as FUNC(put)."""
        snippet = "static void FUNC(put)(int x)\n{\n    x = 1;\n}\n"
        longest, all_spans = series.parse_function_spans(snippet)
        expected = series.FunctionSpan("FUNC(put)", 1, 4, 4)
        self.assertEqual(longest, {"FUNC(put)": expected})
        self.assertEqual(all_spans, {"FUNC(put)": [expected]})

    def test_two_macro_functions_stay_distinct(self) -> None:
        """Two macro-named functions FUNC(a) and FUNC(b) stay distinct."""
        snippet = (
            "static void FUNC(a)(int x)\n"
            "{\n"
            "    return;\n"
            "}\n\n"
            "static void FUNC(b)(int x)\n"
            "{\n"
            "    return;\n"
            "}\n"
        )
        longest, all_spans = series.parse_function_spans(snippet)
        span_a = series.FunctionSpan("FUNC(a)", 1, 4, 4)
        span_b = series.FunctionSpan("FUNC(b)", 6, 9, 4)
        self.assertIn("FUNC(a)", longest)
        self.assertIn("FUNC(b)", longest)
        self.assertEqual(longest["FUNC(a)"], span_a)
        self.assertEqual(longest["FUNC(b)"], span_b)
        self.assertEqual(all_spans, {"FUNC(a)": [span_a], "FUNC(b)": [span_b]})

    def test_return_type_on_own_line_starts_at_type_line(self) -> None:
        """Return type on its own line measures start from the type line."""
        snippet = "static int\nsplit_sig_fn(void)\n{\n    return 1;\n}\n"
        longest, all_spans = series.parse_function_spans(snippet)
        expected = series.FunctionSpan("split_sig_fn", 1, 5, 5)
        self.assertEqual(longest, {"split_sig_fn": expected})
        self.assertEqual(all_spans, {"split_sig_fn": [expected]})

    def test_pointer_return_type(self) -> None:
        """Pointer return type extracts name and full span."""
        snippet = 'static char *g(void)\n{\n    return "ok";\n}\n'
        longest, all_spans = series.parse_function_spans(snippet)
        expected = series.FunctionSpan("g", 1, 4, 4)
        self.assertEqual(longest, {"g": expected})
        self.assertEqual(all_spans, {"g": [expected]})

    def test_function_returning_function_pointer(self) -> None:
        """A function returning a function pointer resolves to the inner function name."""
        snippet = "int (*get_cb(void))(int) { return 0; }\n"
        longest, _ = series.parse_function_spans(snippet)
        self.assertIn("get_cb", longest)

    def test_nested_function_pointer_declarator(self) -> None:
        """Deeply nested function pointer declarators resolve to the function identifier."""
        snippet = "static void (*(*nested(void))(void))(int) { return 0; }\n"
        longest, _ = series.parse_function_spans(snippet)
        self.assertIn("nested", longest)

    def test_two_macro_function_pointers_stay_distinct(self) -> None:
        """Two macro-named functions returning function pointers stay distinct keys."""
        snippet = (
            "void (*FUNC(a)(int x))(void)\n"
            "{\n"
            "    return;\n"
            "}\n\n"
            "void (*FUNC(b)(int x))(void)\n"
            "{\n"
            "    return;\n"
            "}\n"
        )
        longest, all_spans = series.parse_function_spans(snippet)
        self.assertIn("FUNC(a)", longest)
        self.assertIn("FUNC(b)", longest)
        self.assertNotIn("FUNC", longest)
        self.assertEqual(longest["FUNC(a)"].length, 4)
        self.assertEqual(longest["FUNC(b)"].length, 4)
        self.assertIn("FUNC(a)", all_spans)
        self.assertIn("FUNC(b)", all_spans)

    def test_macro_function_pointer_length_increase_reported_under_own_key(self) -> None:
        """A length increase in one macro-named function pointer is reported under its own key."""
        snippet = (
            "void (*FUNC(a)(int x))(void)\n"
            "{\n"
            "    return;\n"
            "}\n\n"
            "void (*FUNC(b)(int x))(void)\n"
            "{\n"
            "    int y = 1;\n"
            "    int z = 2;\n"
            "    return;\n"
            "}\n"
        )
        longest, all_spans = series.parse_function_spans(snippet)
        self.assertIn("FUNC(a)", longest)
        self.assertIn("FUNC(b)", longest)
        self.assertEqual(longest["FUNC(a)"].length, 4)
        self.assertEqual(longest["FUNC(b)"].length, 6)

    def test_pointer_return_boundary(self) -> None:
        """Single and multiple pointer return types preserve the function name."""
        for snippet, expected in (
            ("char *foo(int) { return 0; }\n", "foo"),
            ("char **bar(void) { return 0; }\n", "bar"),
        ):
            with self.subTest(expected=expected):
                longest, _ = series.parse_function_spans(snippet)
                self.assertIn(expected, longest)

    def test_negative_non_functions_give_no_spans(self) -> None:
        """Prototypes, struct initialisers, and macros give no spans."""
        snippet = (
            "int proto1(int x);\n"
            "void proto2(void);\n"
            "static const struct { int a; } s = { .a = 1 };\n"
            "#define F(x) { x }\n"
        )
        longest, all_spans = series.parse_function_spans(snippet)
        self.assertEqual(longest, {})
        self.assertEqual(all_spans, {})

    def test_empty_text_gives_empty_spans(self) -> None:
        """Empty text input yields empty longest and all_spans dictionaries."""
        longest, all_spans = series.parse_function_spans("")
        self.assertEqual(longest, {})
        self.assertEqual(all_spans, {})
