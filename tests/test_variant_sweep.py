# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Exercise the sweep against committed C fixtures without network access."""

from __future__ import annotations

import subprocess  # nosec B404 - fixed interpreter argv in a temporary test tree.
import sys
import tempfile
import unittest
from pathlib import Path

from tests.helpers import run_git

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

    def test_git_failure(self) -> None:
        """An invalid revision fails with a diagnostic rather than an empty report."""

        result = self.sweep("missing-revision")
        self.assertEqual(result.returncode, 1)
        self.assertIn("git", result.stderr)


if __name__ == "__main__":
    unittest.main()
