# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Tests for scripts/semgrep_scan.sh, the semgrep run of make semgrep and the scanners workflow."""

from __future__ import annotations

import os
import shutil
import subprocess  # nosec B404 - runs the scan script against a stub semgrep with a timeout.
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "semgrep_scan.sh"
STUB = """#!/bin/sh
printf '%s\\n' "$@" > "$STUB_ARGV"
cat "$STUB_STDERR" >&2
exit "$STUB_RC"
"""
RULE_WARNING = (
    "Rule negated-if-status-read contains an include pattern 'scripts/ffmpeg-forgejo' "
    "that will soon be interpreted as '/scripts/ffmpeg-forgejo'.\n"
)
SCAN_ARGS = [
    "scan",
    "--config",
    ".semgrep.yml",
    "--error",
    "--metrics",
    "off",
    "--disable-version-check",
]
CAUSE = "semgrep_scan: a rule in .semgrep.yml draws the warning above"


def _run_scan(
    scratch: Path, stderr_text: str, rc: int, extra: tuple[str, ...] = ()
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """Run semgrep_scan.sh against a stub semgrep; return the result and the stub's argv."""
    stub = scratch / "semgrep"
    stub.write_text(STUB, encoding="utf-8")
    stub.chmod(0o755)
    stub_stderr = scratch / "stub-stderr.txt"
    stub_stderr.write_text(stderr_text, encoding="utf-8")
    argv_file = scratch / "stub-argv.txt"
    env = os.environ.copy()
    env.update(
        {
            "SEMGREP": str(stub),
            "STUB_ARGV": str(argv_file),
            "STUB_STDERR": str(stub_stderr),
            "STUB_RC": str(rc),
            "TMPDIR": str(scratch),
        }
    )
    proc = subprocess.run(  # nosec B603 B607 - fixed sh argv, no shell string, bounded timeout.
        ["sh", str(SCRIPT), *extra],
        cwd=scratch,
        env=env,
        timeout=60,
        capture_output=True,
        text=True,
        check=False,
    )
    return proc, argv_file.read_text(encoding="utf-8").splitlines()


@unittest.skipUnless(shutil.which("sh"), "sh not installed; make semgrep runs the script with it")
class SemgrepScanTest(unittest.TestCase):
    """Exercise scripts/semgrep_scan.sh with a stub semgrep."""

    def setUp(self) -> None:
        """Create a scratch directory for the stub and its files."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.scratch = Path(tmp.name)

    def test_clean_scan_passes_arguments_through(self) -> None:
        """A clean scan exits 0 and semgrep gets the repository arguments plus the extra ones."""
        proc, argv = _run_scan(self.scratch, "0 findings.\n", 0, ("--sarif-output=x",))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(argv, [*SCAN_ARGS, "--sarif-output=x"])
        self.assertNotIn(CAUSE, proc.stderr)

    def test_rule_warning_fails_clean_scan(self) -> None:
        """A rule warning on stderr turns a clean scan into exit 1 and names the cause."""
        proc, _ = _run_scan(self.scratch, RULE_WARNING, 0)
        self.assertEqual(proc.returncode, 1)
        self.assertIn(RULE_WARNING.strip(), proc.stderr)
        self.assertIn(CAUSE, proc.stderr)

    def test_finding_exit_code_is_kept(self) -> None:
        """A finding keeps semgrep's exit code 1 without the rule-warning message."""
        proc, _ = _run_scan(self.scratch, "Ran 10 rules: 1 finding.\n", 1)
        self.assertEqual(proc.returncode, 1)
        self.assertNotIn(CAUSE, proc.stderr)

    def test_error_exit_code_is_kept_with_rule_warning(self) -> None:
        """A semgrep error code survives a rule warning instead of becoming 1."""
        proc, _ = _run_scan(self.scratch, RULE_WARNING, 2)
        self.assertEqual(proc.returncode, 2)
        self.assertIn(CAUSE, proc.stderr)

    def test_indented_rule_text_is_no_warning(self) -> None:
        """Text that starts with Rule only after spaces is no rule warning."""
        proc, _ = _run_scan(self.scratch, "  Rule demo contains text\n", 0)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn(CAUSE, proc.stderr)

    def test_stderr_file_is_removed(self) -> None:
        """The script removes the file it keeps semgrep's stderr in once it exits."""
        _run_scan(self.scratch, RULE_WARNING, 0)
        self.assertEqual(sorted(p.name for p in self.scratch.glob("tmp.*")), [])


if __name__ == "__main__":
    unittest.main()
