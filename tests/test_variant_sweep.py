# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Exercise the sweep against committed C fixtures without network access."""

from __future__ import annotations

import contextlib
import io
import os
import subprocess  # nosec B404 - fixed interpreter argv in a temporary test tree.
import sys
import tempfile
import unittest
from functools import partial
from pathlib import Path

from scripts import variant_sweep
from tests.helpers import run_git as fixture_git

run_git = partial(fixture_git, preserve_config=True)

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "variant_sweep.py"
BASE = """int write(int s);
int send_cmd(int s)
{
    write(s);
    return 0;
}
int play(int s)
{
    return send_cmd(s);
}
"""
CALLERS = """int play(int s);
int read_packet(int s)
{
    play (s);
    return 0;
}
int fallback(int s)
{
    if (play(s) != 0) return -1;
    return 0;
}
int intentional(int s)
{
    (void)play(s);
    return 0;
}
"""


class VariantSweepTest(unittest.TestCase):
    """Cover targets, sibling callers and intentional consumption."""

    def setUp(self) -> None:
        """Commit a base whose command write is unchecked."""

        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.repo = Path(self.directory.name)
        run_git(self.repo, "init", "-q")
        (self.repo / "fix.c").write_text(BASE)
        (self.repo / "callers.c").write_text(CALLERS)
        run_git(self.repo, "add", ".")
        run_git(self.repo, "commit", "-qm", "base")
        self.base = run_git(self.repo, "rev-parse", "HEAD")
        self.output = self.repo / "sweep.md"

    def commit_head(self, source: str) -> str:
        """Commit the changed fix file and return its full object ID."""

        (self.repo / "fix.c").write_text(source)
        run_git(self.repo, "add", ".")
        run_git(self.repo, "commit", "--allow-empty", "-qm", "head")
        return run_git(self.repo, "rev-parse", "HEAD")

    def sweep(self, head: str, bug_class: str = "unchecked-return") -> subprocess.CompletedProcess:
        """Invoke the public CLI against the fixture history."""

        return subprocess.run(  # nosec B603 - fixed interpreter argv in a temporary test tree.
            [
                sys.executable,
                str(SCRIPT),
                "-C",
                str(self.repo),
                "--class",
                bug_class,
                self.base,
                head,
                "-o",
                str(self.output),
            ],
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )

    def test_sibling_callers(self) -> None:
        """Checked write seeds touched send_cmd and changed play callers."""

        source = BASE.replace(
            "    write(s);", "    int status = write(s);\n    if (status < 0) return status;"
        )
        source = source.replace("    return send_cmd(s);", "    return send_cmd(s) + 0;")
        head = self.commit_head(source)
        result = self.sweep(head)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = self.output.read_text()
        self.assertIn(f"base: {self.base}\nhead: {head}\n", report)
        self.assertIn("targets: play, send_cmd, write", report)
        self.assertIn("| callers.c:4 | play | discarded | no | TODO |", report)
        self.assertIn("| callers.c:9 | play | normalized | no | TODO |", report)
        self.assertEqual(report.count("| TODO |"), 2)

    def test_no_hits(self) -> None:
        """An unchanged head with no added checks has no sweep targets."""

        head = self.commit_head(BASE)
        self.assertEqual(self.sweep(head).returncode, 0)
        self.assertIn("no hits\n", self.output.read_text())

    def test_multiline_and_same_file(self) -> None:
        """Multiline statements report their first line and touched-file membership."""

        source = BASE.replace("    write(s);", "    return write(s);")
        source += "int sibling(int s)\n{\n    send_cmd (\n        s\n    );\n    return 0;\n}\n"
        head = self.commit_head(source)
        self.assertEqual(self.sweep(head).returncode, 0)
        self.assertIn("| fix.c:13 | send_cmd | discarded | yes | TODO |", self.output.read_text())

    def test_unknown_class(self) -> None:
        """Class parsing rejects every value outside the allow-list."""

        self.assertEqual(self.sweep(self.base, "anything-else").returncode, 2)

    def test_touched_leaf_without_added_check(self) -> None:
        """A touched leaf with no callers and no consumed call yields no hits."""

        (self.repo / "callers.c").write_text("int unrelated(void) { return 0; }\n")
        head = self.commit_head("int leaf(void) { return 1; }\n")
        self.assertEqual(self.sweep(head).returncode, 0)
        self.assertIn("no hits\n", self.output.read_text())

    def test_consumed_forms_and_lexical_noise(self) -> None:
        """Error-tested assignments and direct conditions seed checked callees."""

        source = """int changed(int s)
{
    ctx->status = assign(s);
    if (ctx->status < 0) return ctx->status;
    if ((status = wrapped(s)) < 0) return status;
    if (less(s) < 0) return -1;
    if (different(s) != 0) return -1;
    return returned(s);
}
int sibling(int s)
{
    assign(s);
    wrapped(s);
    less(s);
    different(s);
    returned(s);
    (void)assign(s);
    /* assign(s); { } */
    const char *text = "returned(s); { }";
    return assign(s);
}
"""
        head = self.commit_head(source)
        result = self.sweep(head)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = self.output.read_text()
        self.assertIn("targets: assign, changed, different, less, sibling, wrapped", report)
        self.assertEqual(report.count("| discarded |"), 4)
        self.assertEqual(report.count("| normalized |"), 2)

    def test_ternary_averror_not_discarded(self) -> None:
        """Ternary branches consume calls, including macro-style error constructors."""

        source = (
            BASE.replace("    write(s);", "    return write(s);")
            + """int fallback(int ret)
{
    if (!ret) return AVERROR(EINVAL);
    return ret < 0 ? ret : AVERROR(EIO);
}
int consume(int ret)
{
    ret = (ret < 0) ? ret : AVERROR(EIO);
    return ret < 0 ? ret : send_cmd(ret);
}
"""
        )
        head = self.commit_head(source)
        result = self.sweep(head)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("no hits\n", self.output.read_text())

    def test_macro_not_target(self) -> None:
        """Neither returns nor checked assignments promote uppercase macro names."""

        source = """int changed(int ret)
{
    ret = AVERROR(EIO);
    if (ret < 0) return ret;
    if (CHECK_2(ret) != 0) return -1;
    return AVERROR(EINVAL);
}
"""
        head = self.commit_head(source)
        self.assertEqual(self.sweep(head).returncode, 0)
        self.assertIn("targets: changed\n", self.output.read_text())

    def test_offset_assignment_not_target(self) -> None:
        """Arithmetic expressions remain consumed even when their result is tested."""

        source = """int changed(int ret)
{
    t = now() + 5;
    rt->teardown_deadline = av_gettime_relative() + 10;
    ret = offset(ret) + 5;
    if (ret < 0) return ret;
    if ((ret = wrapped_offset(ret) + 5) < 0) return ret;
    return forwarded(ret);
}
"""
        head = self.commit_head(source)
        self.assertEqual(self.sweep(head).returncode, 0)
        self.assertIn("targets: changed\n", self.output.read_text())

    def test_assignment_checked_across_added_lines(self) -> None:
        """Checks within three added lines promote only the tested variable's callee."""

        source = """int changed(int x)
{
    ret = f(x);
    if (ret < 0) return ret;
    unchecked = ignored(x);
    different = other(x);
    if (ret != 0) return ret;
    status = boundary(x);
    /* first intervening added line */
    /* second intervening added line */
    if (status != 0) return status;
    late = too_late(x);
    /* first intervening added line */
    /* second intervening added line */
    /* third intervening added line */
    if (late < 0) return late;
    same = same_line(x); if (same) return same;
    negated = false_check(x);
    if (!negated) return -1;
    if ((inline_ret = inline_check(x)) < 0) return inline_ret;
    return 0;
}
int sibling(int x)
{
    f(x);
    boundary(x);
    same_line(x);
    false_check(x);
    inline_check(x);
    ignored(x);
    other(x);
    too_late(x);
    return 0;
}
"""
        head = self.commit_head(source)
        self.assertEqual(self.sweep(head).returncode, 0)
        report = self.output.read_text()
        self.assertIn(
            "targets: boundary, changed, f, false_check, inline_check, same_line, sibling\n",
            report,
        )
        self.assertEqual(report.count("| discarded |"), 5)

    def test_discarded_statement_boundaries(self) -> None:
        """Control headers start statements; casts and expression operands do not."""

        source = (
            BASE.replace("    write(s);", "    return write(s);")
            + """int sibling(int s)
{
    if (s) send_cmd(s);
    else send_cmd(s);
    for (s = 0; s < 3; s++) send_cmd(s);
    while (s) send_cmd(s);
    switch (s) send_cmd(s);
    { } send_cmd(s);
    (int)send_cmd(s);
    s ? send_cmd(s) : send_cmd(s);
    s = send_cmd(s);
    s, send_cmd(s);
    s + send_cmd(s);
    consume(send_cmd(s));
    (void)send_cmd(s);
    return send_cmd(s);
}
"""
        )
        head = self.commit_head(source)
        self.assertEqual(self.sweep(head).returncode, 0)
        report = self.output.read_text()
        self.assertEqual(report.count("| discarded |"), 6)
        self.assertEqual(report.count("| TODO |"), 6)

    def test_static_target_stays_in_its_translation_unit(self) -> None:
        """A changed private function does not seed same-named foreign callers."""

        (self.repo / "callers.c").write_text(
            "static int send_cmd(int s) { return s; }\n"
            "int other(int s) { send_cmd(s); return s; }\n"
        )
        self.base = self.commit_head(
            BASE.replace("int send_cmd(int s)", "static int send_cmd(int s)")
        )
        head = self.commit_head(
            BASE.replace("int send_cmd(int s)", "static int send_cmd(int s)").replace(
                "    write(s);", "    return write(s);"
            )
            + "int sibling(int s) { send_cmd(s); return s; }\n"
        )
        result = self.sweep(head)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = self.output.read_text()
        self.assertIn("| fix.c:11 | send_cmd | discarded | yes | TODO |", report)
        self.assertNotIn("| callers.c:2 | send_cmd |", report)

    def test_global_target_does_not_match_a_foreign_static(self) -> None:
        """A global target excludes a shadowing private definition in another unit."""

        (self.repo / "callers.c").write_text(
            "static int send_cmd(int s) { return s; }\n"
            "int other(int s) { send_cmd(s); return s; }\n"
        )
        (self.repo / "external.c").write_text(
            "int send_cmd(int s);\nint other(int s) { send_cmd(s); return s; }\n"
        )
        self.base = self.commit_head(BASE)
        head = self.commit_head(BASE.replace("    write(s);", "    return write(s);"))
        result = self.sweep(head)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = self.output.read_text()
        self.assertIn("| external.c:2 | send_cmd | discarded | no | TODO |", report)
        self.assertNotIn("| callers.c:2 | send_cmd |", report)

    def test_git_ignores_repository_location_environment(self) -> None:
        """A real foreign Git directory cannot redirect symbolic revisions."""

        head = self.commit_head(BASE.replace("    write(s);", "    return write(s);"))
        with tempfile.TemporaryDirectory() as other:
            foreign = Path(other)
            run_git(foreign, "init", "-q")
            run_git(foreign, "commit", "--allow-empty", "-qm", "foreign")
            with unittest.mock.patch.dict(os.environ, {"GIT_DIR": str(foreign / ".git")}):
                result = self.sweep("HEAD")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"head: {head}\n", self.output.read_text())

    def test_git_decodes_invalid_source_bytes(self) -> None:
        """Non-UTF-8 comments do not cause a traceback or hide a caller."""

        (self.repo / "callers.c").write_bytes(
            b"/* non-UTF-8: \xff */\nint sibling(int s) { send_cmd(s); return s; }\n"
        )
        head = self.commit_head(BASE.replace("    write(s);", "    return write(s);"))
        result = self.sweep(head)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "| callers.c:2 | send_cmd | discarded | yes | TODO |", self.output.read_text()
        )

    def test_git_failure(self) -> None:
        """An invalid revision fails with a diagnostic rather than an empty report."""

        result = self.sweep("missing-revision")
        self.assertEqual(result.returncode, 1)
        self.assertIn("git", result.stderr)


class SweepRegressionTest(unittest.TestCase):
    def test_labels_and_do_start_discarded_statements(self) -> None:
        """Labels and do bodies start statements; ternary colons do not."""

        source = """int sibling(int s) {
    switch (s) {
    case 1: target(s); break;
    default: target(s); break;
    }
    named: target(s);
    do target(s); while (s);
    s ? s : target(s);
    return 0;
}
"""
        self.assertEqual(
            variant_sweep.find_hits(source, {"target"}),
            [
                (3, "target", "discarded"),
                (4, "target", "discarded"),
                (6, "target", "discarded"),
                (7, "target", "discarded"),
            ],
        )

    def test_function_like_averror_is_normalized(self) -> None:
        """Error constructors count for both braced and unbraced branches."""

        for branch in ("return AVERROR(EIO);", "{ return AVERROR(ENOMEM); }"):
            with self.subTest(branch=branch):
                source = f"int sibling(int s) {{ if (target(s) < 0) {branch} return s; }}"
                self.assertEqual(
                    variant_sweep.find_hits(source, {"target"}), [(1, "target", "normalized")]
                )

    def test_attribute_does_not_hide_function_declarator(self) -> None:
        """Leading and trailing attributes leave the definition's name intact."""

        for attribute in (
            "__attribute__((noinline))",
            "__declspec(noinline)",
            "AV_ATTRIBUTE(noinline)",
            '[[nodiscard("reason")]]',
        ):
            with self.subTest(attribute=attribute):
                source = (
                    f"{attribute} int target(int (*callback)(int)) "
                    "__attribute__((warn_unused_result)) { return 0; }"
                )
                self.assertEqual(variant_sweep.touched_targets(source, {1: source}), {"target"})
        self.assertEqual(
            variant_sweep.find_hits("int CALLER(void) { target(); return 0; }", {"target"}),
            [(1, "target", "discarded")],
        )

    def test_unrelated_comparison_does_not_seed_target(self) -> None:
        """Only complete bare truthiness and recognized zero tests seed calls."""

        for condition in (
            "((status = target()) == 1)",
            "((status = target()) > 0)",
            "((status = target()) + 1)",
            "((status = target()) < 0) == 1",
        ):
            with self.subTest(condition=condition):
                source = f"int changed(void) {{ if ({condition}) return -1; return 0; }}"
                self.assertEqual(variant_sweep.touched_targets(source, {1: source}), {"changed"})
        for condition in (
            "(status = target())",
            "!(status = target())",
            "((status = target()) < 0)",
            "((status = target()) != 0)",
        ):
            with self.subTest(condition=condition):
                source = f"int changed(void) {{ if ({condition}) return -1; return 0; }}"
                self.assertEqual(
                    variant_sweep.touched_targets(source, {1: source}), {"changed", "target"}
                )

    def test_assigned_errors_are_normalized(self) -> None:
        """Simple and member assignments carry their result into an immediate check."""

        for variable in ("ret", "ctx->status", "ctx.status"):
            for result in ("-1", "AVERROR_EOF", "AVERROR(EIO)"):
                with self.subTest(variable=variable, result=result):
                    source = (
                        f"int sibling(void) {{ {variable} = target(); "
                        f"if ({variable} < 0) return {result}; return 0; }}"
                    )
                    self.assertEqual(
                        variant_sweep.find_hits(source, {"target"}), [(1, "target", "normalized")]
                    )
        for tail in (
            "ret = 0; if (ret < 0) return -1;",
            "if (other < 0) return -1;",
            "if (ret == 1) return -1;",
            "if (ret < 0) return ret;",
            "if (ret < 0) { consume(); return -1; }",
        ):
            with self.subTest(tail=tail):
                source = f"int sibling(void) {{ ret = target(); {tail} return 0; }}"
                self.assertEqual(variant_sweep.find_hits(source, {"target"}), [])

    def test_preprocessor_directives_preserve_call_boundaries(self) -> None:
        """Directive continuations and unmatched macro braces cannot alter C bodies."""

        source = """#define OPEN { \\
    target(bogus); }
#define UNBALANCED }
int sibling(int s) {
#if CONFIG_X
    target(s);
#endif
    target(s);
    return 0;
}
"""
        self.assertEqual(
            variant_sweep.find_hits(source, {"target"}),
            [(6, "target", "discarded"), (8, "target", "discarded")],
        )
        self.assertEqual(len(variant_sweep.mask_literals(source)), len(source))

    def test_atomic_report_survives_partial_write(self) -> None:
        """A failed write preserves the reviewed report and removes the staging file."""

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "sweep.md"
            output.write_text("old complete report\n")
            new = "class: unchecked-return\nhead: new\n| unresolved |\n"
            real_open = Path.open

            class BrokenWriter:
                def __init__(self, stream):
                    self.stream = stream

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    self.stream.close()

                def write(self, text):
                    self.stream.write(text[:24])
                    self.stream.flush()
                    raise OSError("planted write failure")

            def open_file(path, mode="r", *args, **kwargs):
                stream = real_open(path, mode, *args, **kwargs)
                return BrokenWriter(stream) if "w" in mode else stream

            with (
                unittest.mock.patch.object(variant_sweep, "sweep", return_value=new),
                unittest.mock.patch.object(Path, "open", open_file),
                contextlib.redirect_stderr(io.StringIO()) as errors,
            ):
                status = variant_sweep.main(
                    ["--class", "unchecked-return", "base", "head", "-o", str(output)]
                )
            self.assertEqual(status, 1)
            self.assertIn("planted write failure", errors.getvalue())
            self.assertEqual(output.read_text(), "old complete report\n")
            self.assertEqual(list(Path(directory).iterdir()), [output])

    def test_atomic_report_is_published_only_when_complete(self) -> None:
        """The destination remains unchanged until a same-directory atomic replace."""

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "sweep.md"
            output.write_text("old complete report\n")
            new = "new complete report\n"
            replace = os.replace
            seen = []

            def replace_file(source, destination):
                self.assertEqual(Path(source).parent, output.parent)
                self.assertEqual(output.read_text(), "old complete report\n")
                self.assertEqual(Path(source).read_text(), new)
                seen.append(source)
                return replace(source, destination)

            with (
                unittest.mock.patch.object(variant_sweep, "sweep", return_value=new),
                unittest.mock.patch("os.replace", side_effect=replace_file),
            ):
                status = variant_sweep.main(
                    ["--class", "unchecked-return", "base", "head", "-o", str(output)]
                )
            self.assertEqual(status, 0)
            self.assertEqual(len(seen), 1)
            self.assertEqual(output.read_text(), new)


class SweepLimitTest(unittest.TestCase):
    """Use tiny bounds rather than allocate oversized fixtures."""

    setUp = VariantSweepTest.setUp
    commit_head = VariantSweepTest.commit_head

    def test_deadline_is_shared_across_git_calls(self) -> None:
        """Later Git calls receive only the remainder of the total sweep budget."""

        head = self.commit_head(BASE.replace("    write(s);", "    return write(s);"))
        with (
            unittest.mock.patch.object(variant_sweep, "MAX_SWEEP_SECONDS", 1, create=True),
            unittest.mock.patch("time.monotonic", side_effect=[0, 0, 0, 0.75, 1.1]),
            self.assertRaisesRegex(RuntimeError, "sweep deadline"),
        ):
            variant_sweep.sweep(str(self.repo), self.base, head)

    def test_exact_sweep_limits_are_allowed(self) -> None:
        """Equality at each deterministic input and report bound remains accepted."""

        source = BASE.replace("    write(s);", "    return write(s);")
        head = self.commit_head(source)
        expected = variant_sweep.sweep(str(self.repo), self.base, head)
        for name, bound in (
            ("MAX_SOURCE_COUNT", 1),
            ("MAX_SOURCE_BYTES", len(source.encode())),
            ("MAX_TOTAL_SOURCE_BYTES", len(source.encode())),
            ("MAX_TARGET_COUNT", 1),
            ("MAX_REPORT_BYTES", len(expected.encode())),
        ):
            with (
                self.subTest(limit=name),
                unittest.mock.patch.object(variant_sweep, name, bound, create=True),
            ):
                self.assertEqual(variant_sweep.sweep(str(self.repo), self.base, head), expected)

    def test_sweep_limits_fail_by_name(self) -> None:
        """Every sweep bound, once exceeded, fails with a diagnostic naming that bound."""

        head = self.commit_head(BASE.replace("    write(s);", "    return write(s);"))
        for name, value, diagnostic in (
            ("MAX_SOURCE_COUNT", 0, "source count limit"),
            ("MAX_SOURCE_BYTES", 1, "source size limit"),
            ("MAX_TOTAL_SOURCE_BYTES", 1, "total source size limit"),
            ("MAX_TARGET_COUNT", 0, "target count limit"),
            ("MAX_REPORT_BYTES", 1, "report size limit"),
            ("MAX_SWEEP_SECONDS", 0, "sweep deadline"),
        ):
            with (
                self.subTest(limit=name),
                unittest.mock.patch.object(variant_sweep, name, value, create=True),
                self.assertRaisesRegex(RuntimeError, diagnostic),
            ):
                variant_sweep.sweep(str(self.repo), self.base, head)

    def test_source_limit_rejects_before_source_read(self) -> None:
        """Source count and size bounds reject a sweep before any source blob is read."""

        head = self.commit_head(BASE.replace("    write(s);", "    return write(s);"))
        original = variant_sweep.git
        reads = []

        def observe(directory, *args, **kwargs):
            if args[0] == "show":
                reads.append(args)
            return original(directory, *args, **kwargs)

        for name, bound, diagnostic in (
            ("MAX_SOURCE_BYTES", 1, "source size limit"),
            ("MAX_SOURCE_COUNT", 0, "source count limit"),
            ("MAX_TOTAL_SOURCE_BYTES", 1, "total source size limit"),
        ):
            with (
                self.subTest(limit=name),
                unittest.mock.patch.object(variant_sweep, name, bound, create=True),
                unittest.mock.patch.object(variant_sweep, "git", side_effect=observe),
                self.assertRaisesRegex(RuntimeError, diagnostic),
            ):
                variant_sweep.sweep(str(self.repo), self.base, head)
        self.assertEqual(reads, [])


if __name__ == "__main__":
    unittest.main()
