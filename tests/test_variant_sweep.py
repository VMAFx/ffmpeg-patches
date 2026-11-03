# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Exercise the sweep against committed C fixtures without network access."""

from __future__ import annotations

import subprocess
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

        return subprocess.run(
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
        """Assignment lvalues, return and direct conditions seed checked callees."""

        source = """int changed(int s)
{
    ctx->status = assign(s);
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
        self.assertIn(
            "targets: assign, changed, different, less, returned, sibling, wrapped", report
        )
        self.assertEqual(report.count("| discarded |"), 5)
        self.assertEqual(report.count("| normalized |"), 2)

    def test_git_failure(self) -> None:
        """An invalid revision fails with a diagnostic rather than an empty report."""

        result = self.sweep("missing-revision")
        self.assertEqual(result.returncode, 1)
        self.assertIn("git", result.stderr)


if __name__ == "__main__":
    unittest.main()
