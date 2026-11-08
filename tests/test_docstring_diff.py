# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Exercise the diff-scoped public-function docstring gate in fixture repositories."""

from __future__ import annotations

import subprocess  # nosec B404 - fixed Git and Python commands in disposable fixtures.
import sys
import tempfile
import unittest
from pathlib import Path

from tests.helpers import isolated_git_environment

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "docstring_diff.py"


class DocstringDiffTest(unittest.TestCase):
    """Check staged and working-tree diffs without changing this repository."""

    def setUp(self) -> None:
        """Create a committed fixture with one undocumented public function."""
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source = self.root / "module.py"
        self.git("init", "-q")
        self.source.write_text("def old():\n    return 1\n", encoding="utf-8")
        self.git("add", "module.py")
        self.git("commit", "-qm", "base")

    def git(self, *args: str) -> str:
        """Run bounded Git commands with fixture-only author and signing settings."""
        result = subprocess.run(  # nosec B603 B607 - fixed Git executable and fixture arguments.
            [
                "git",
                "-C",
                str(self.root),
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@invalid",
                "-c",
                "commit.gpgsign=false",
                *args,
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
            env=isolated_git_environment(),
        )
        return result.stdout.strip()

    def run_check(self, *args: str) -> subprocess.CompletedProcess[str]:
        """Run the new gate against the temporary repository."""
        return subprocess.run(  # nosec B603 - fixed Python executable and test script.
            [sys.executable, str(SCRIPT), *args],
            cwd=self.root,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )

    def write_and_stage(self, text: str) -> None:
        """Replace and stage the fixture module."""
        self.source.write_text(text, encoding="utf-8")
        self.git("add", "module.py")

    def assert_finding(
        self, result: subprocess.CompletedProcess[str], line: int, name: str
    ) -> None:
        """Require a precise missing-docstring diagnostic."""
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(f"module.py:{line}: {name} has no docstring", result.stderr)

    def test_documented_added_function_passes_both_modes(self) -> None:
        """A new documented public function passes staged and base comparisons."""
        self.write_and_stage(
            'def old():\n    return 1\n\ndef added():\n    """Documented."""\n    return 2\n'
        )
        self.assertEqual(self.run_check("--cached").returncode, 0)
        self.assertEqual(self.run_check("--base", "HEAD").returncode, 0)

    def test_planted_undocumented_added_function_fails(self) -> None:
        """The planted missing docstring is rejected in both modes."""
        self.write_and_stage("def old():\n    return 1\n\ndef planted():\n    return 2\n")
        self.assert_finding(self.run_check("--cached"), 4, "planted")
        self.assert_finding(self.run_check("--base", "HEAD"), 4, "planted")

    def test_changed_undocumented_function_fails(self) -> None:
        """An edit inside an existing undocumented function enters scope."""
        self.write_and_stage("def old():\n    return 2\n")
        self.assert_finding(self.run_check("--cached"), 1, "old")

    def test_public_method_fails(self) -> None:
        """A public method directly in a public class enters scope."""
        self.write_and_stage(
            "def old():\n    return 1\n\nclass Public:\n    def method(self):\n        return 2\n"
        )
        self.assert_finding(self.run_check("--cached"), 5, "method")

    def test_untouched_undocumented_function_passes(self) -> None:
        """The old function remains outside the added-line span."""
        self.write_and_stage(
            'def old():\n    return 1\n\ndef fresh():\n    """Documented."""\n    return 2\n'
        )
        self.assertEqual(self.run_check("--cached").returncode, 0)

    def test_private_function_and_private_class_pass(self) -> None:
        """Private names are outside the public-function rule."""
        self.write_and_stage(
            "def old():\n    return 1\n\ndef _helper():\n    return 2\n\n"
            "class _Hidden:\n    def method(self):\n        return 3\n"
        )
        self.assertEqual(self.run_check("--cached").returncode, 0)

    def test_nested_undocumented_function_passes(self) -> None:
        """A nested function is outside the direct module and class scope."""
        self.write_and_stage(
            'def old():\n    return 1\n\ndef outer():\n    """Documented."""\n'
            "    def nested():\n        return 2\n    return nested()\n"
        )
        self.assertEqual(self.run_check("--cached").returncode, 0)

    def test_decorator_only_change_fails(self) -> None:
        """A changed decorator intersects the function's full AST span."""
        self.write_and_stage(
            "def identity(value):\n    return value\n\n@identity\ndef old():\n    return 1\n"
        )
        self.git("commit", "-qm", "decorated base")
        self.write_and_stage(
            "def identity(value):\n    return value\n\n"
            "@identity  # edited\ndef old():\n    return 1\n"
        )
        self.assert_finding(self.run_check("--cached"), 5, "old")

    def test_deletion_only_hunk_passes(self) -> None:
        """A hunk with zero new lines does not select an old function."""
        self.write_and_stage("def old():\n    return 1\n\n# removed\n")
        self.git("commit", "-qm", "comment base")
        self.write_and_stage("def old():\n    return 1\n")
        self.assertEqual(self.run_check("--cached").returncode, 0)

    def test_deleted_public_docstring_fails_both_modes(self) -> None:
        """Deleting only a public function's docstring selects that function."""
        self.write_and_stage('def f():\n    """Doc."""\n    return 1\n')
        self.git("commit", "-qm", "documented base")
        self.write_and_stage("def f():\n    return 1\n")
        self.assert_finding(self.run_check("--cached"), 1, "f")
        self.assert_finding(self.run_check("--base", "HEAD"), 1, "f")

    def test_deleted_line_inside_documented_function_passes(self) -> None:
        """Deleting a body line selects the function but preserves its passing result."""
        self.write_and_stage('def f():\n    """Doc."""\n    value = 1\n    return value\n')
        self.git("commit", "-qm", "body base")
        self.write_and_stage('def f():\n    """Doc."""\n    return 1\n')
        self.assertEqual(self.run_check("--cached").returncode, 0)

    def test_deletion_between_undocumented_functions_passes(self) -> None:
        """Deleting a separator after one function does not select either neighbor."""
        self.write_and_stage(
            "def first():\n    return 1\n\n# removed\n\ndef second():\n    return 2\n"
        )
        self.git("commit", "-qm", "separator base")
        self.write_and_stage("def first():\n    return 1\n\ndef second():\n    return 2\n")
        self.assertEqual(self.run_check("--cached").returncode, 0)

    def test_unresolvable_base_fails_closed(self) -> None:
        """An unknown ref is a tool error, not a clean scan."""
        result = self.run_check("--base", "no-such-ref")
        self.assertEqual(result.returncode, 2)
        self.assertIn("no-such-ref", result.stderr)

    def test_syntax_error_fails_closed(self) -> None:
        """Changed Python with invalid syntax returns a named error."""
        self.write_and_stage("def old(:\n    return 1\n")
        result = self.run_check("--cached")
        self.assertEqual(result.returncode, 2)
        self.assertIn("module.py", result.stderr)
        self.assertIn("syntax", result.stderr.lower())

    def test_deletion_only_syntax_error_fails_closed(self) -> None:
        """A deleted function body still requires parsing the new file."""
        self.write_and_stage("def old():\n")
        result = self.run_check("--cached")
        self.assertEqual(result.returncode, 2)
        self.assertIn("module.py", result.stderr)
        self.assertIn("syntax", result.stderr.lower())

    def test_base_reads_worktree_text(self) -> None:
        """Base mode sees unstaged edits after a staged documented version."""
        self.write_and_stage('def old():\n    """Documented."""\n    return 1\n')
        self.source.write_text("def old():\n    return 2\n", encoding="utf-8")
        self.assert_finding(self.run_check("--base", "HEAD"), 1, "old")

    def test_cached_reads_index_text(self) -> None:
        """Cached mode uses staged text despite a clean working-tree edit."""
        self.write_and_stage("def old():\n    return 2\n")
        self.source.write_text(
            'def old():\n    """Documented."""\n    return 2\n', encoding="utf-8"
        )
        self.assert_finding(self.run_check("--cached"), 1, "old")

    def test_rename_reports_new_path(self) -> None:
        """A renamed and edited module is reported under its new path."""
        renamed = self.root / "renamed.py"
        self.source.rename(renamed)
        renamed.write_text("def old():\n    return 2\n", encoding="utf-8")
        self.git("add", "-A")
        result = self.run_check("--cached")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("renamed.py:1: old has no docstring", result.stderr)


if __name__ == "__main__":
    unittest.main()
