# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Reject mixed import forms for the same Python module."""

from __future__ import annotations

import ast
import os
import subprocess
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from tests.helpers import python_sources, run_git

ROOT = Path(__file__).resolve().parent.parent


def find_import_and_import_from(source: str, *, filename: str = "<unknown>") -> list[str]:
    """Return modules imported with both direct and from-import forms."""

    tree = ast.parse(source, filename=filename)
    direct_modules = set()
    from_modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for name in node.names:
                direct_modules.add(name.name)
                direct_modules.add(name.name.partition(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            from_modules.add(node.module)
    return sorted(direct_modules & from_modules)


class ImportStyleTest(unittest.TestCase):
    def test_python_sources_and_run_git_ignore_checkout_environment(self) -> None:
        """Source discovery and fixture Git use the requested repository."""

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_git(root, "init", "-q")
            source = root / "scripts" / "tracked.py"
            source.parent.mkdir()
            source.write_text("pass\n", encoding="utf-8")
            run_git(root, "add", "scripts/tracked.py")
            bogus = root / "bogus"
            overrides = {"GIT_DIR": str(bogus / "git"), "GIT_INDEX_FILE": str(bogus / "index")}

            with unittest.mock.patch.dict(os.environ, overrides):
                self.assertEqual(run_git(root, "ls-files"), "scripts/tracked.py")
                self.assertEqual(python_sources(root), [source])

    def test_python_sources_names_repository_when_git_fails(self) -> None:
        """Source discovery identifies a directory that Git cannot inspect."""

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(RuntimeError) as caught:
                python_sources(root)
            self.assertIn(str(root), str(caught.exception))
            self.assertIsInstance(caught.exception.__cause__, subprocess.CalledProcessError)

    def test_python_sources_skips_deleted_tracked_files(self) -> None:
        """Tracked paths missing from the worktree are outside the scan."""

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_git(root, "init", "-q")
            scripts = root / "scripts"
            scripts.mkdir()
            sources = [scripts / "x.py", scripts / "y"]
            sources[0].write_text("pass\n", encoding="utf-8")
            sources[1].write_text("#!/usr/bin/env python3\n", encoding="utf-8")
            run_git(root, "add", "scripts/x.py", "scripts/y")
            for source in sources:
                source.unlink()

            self.assertEqual(python_sources(root), [])

    def test_python_sources_excludes_tracked_python_outside_scanned_directories(self) -> None:
        """Tracked Python outside tests and scripts remains outside scanner scope."""

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_git(root, "init", "-q")
            outside = root / "config" / "hook.py"
            outside.parent.mkdir()
            outside.write_text("pass\n", encoding="utf-8")
            run_git(root, "add", "config/hook.py")

            self.assertNotIn(outside, python_sources(root))

    def test_import_and_import_from_reports_module(self) -> None:
        """Exact direct and from-import forms report their shared module."""

        source = "import unittest\nfrom unittest import mock\n"
        self.assertEqual(find_import_and_import_from(source), ["unittest"])

    def test_plain_and_dotted_imports_do_not_conflict(self) -> None:
        """Two direct imports do not create a mixed-form finding."""

        source = "import unittest\nimport unittest.mock\n"
        self.assertEqual(find_import_and_import_from(source), [])

    def test_dotted_import_and_parent_from_import_reports_parent(self) -> None:
        """A dotted direct import also imports its top-level module."""

        source = "import package.sub\nfrom package import value\n"
        self.assertEqual(find_import_and_import_from(source), ["package"])

    def test_relative_from_import_does_not_conflict(self) -> None:
        """Relative imports remain outside the absolute-module comparison."""

        source = "import x\nfrom . import x\n"
        self.assertEqual(find_import_and_import_from(source), [])

    def test_syntax_error_names_scanned_file(self) -> None:
        """A syntax error identifies the repository file being scanned."""

        with self.assertRaises(SyntaxError) as caught:
            find_import_and_import_from("if:\n", filename="planted.py")
        self.assertEqual(caught.exception.filename, "planted.py")

    def test_repository_uses_one_import_form_per_module(self) -> None:
        """Tracked Python sources avoid mixed direct and from-import forms."""

        offenders = []
        for path in python_sources(ROOT):
            relative = path.relative_to(ROOT)
            source = path.read_text(encoding="utf-8")
            offenders.extend(
                f"{relative}:{module}"
                for module in find_import_and_import_from(source, filename=str(relative))
            )
        self.assertEqual(offenders, [], "mixed import forms:\n" + "\n".join(offenders))


if __name__ == "__main__":
    unittest.main()
