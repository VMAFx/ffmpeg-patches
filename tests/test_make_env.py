# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Check Make environment isolation and require its reuse by Make fixtures."""

from __future__ import annotations

import ast
import os
import unittest
import unittest.mock
from pathlib import Path

from tests.make_env import make_environment


def _missing_make_env_import(source: str) -> bool:
    """Flag GNU Make fixture sources without an import of the shared isolation module."""
    if 'shutil.which("make")' not in source:
        return False
    return not any(
        isinstance(node, ast.ImportFrom) and node.module == "tests.make_env"
        for node in ast.walk(ast.parse(source))
    )


class MakeEnvironmentTest(unittest.TestCase):
    """Exercise environment copying and source-level adoption checks."""

    def test_make_environment_copies_and_scrubs_channels_and_extra_names(self) -> None:
        """Every Make channel and extra name is removed without mutating the parent."""
        channels = ("MAKEFLAGS", "MFLAGS", "GNUMAKEFLAGS", "MAKELEVEL", "MAKEOVERRIDES")
        planted = dict.fromkeys((*channels, "FIXTURE_TREE"), "parent")
        planted["KEPT"] = "literal value"
        with unittest.mock.patch.dict(os.environ, planted):
            environment = make_environment("FIXTURE_TREE")
            self.assertEqual(set(environment), set(os.environ) - set(channels) - {"FIXTURE_TREE"})
            self.assertEqual(environment["KEPT"], "literal value")
            for name in (*channels, "FIXTURE_TREE"):
                self.assertEqual(os.environ[name], "parent")
            environment["KEPT"] = "child"
            self.assertEqual(os.environ["KEPT"], "literal value")

    def test_make_fixtures_import_shared_environment(self) -> None:
        """Every GNU Make fixture names the shared module, including planted sources."""
        planted = 'import shutil\nmake = shutil.which("make")\n'
        self.assertTrue(_missing_make_env_import(planted))
        self.assertFalse(
            _missing_make_env_import("from tests.make_env import make_environment\n" + planted)
        )
        self.assertFalse(_missing_make_env_import("pass\n"))
        offenders = [
            str(path.relative_to(Path(__file__).parent.parent))
            for path in sorted(Path(__file__).parent.glob("test_*.py"))
            if _missing_make_env_import(path.read_text(encoding="utf-8"))
        ]
        self.assertEqual(offenders, [], "Make fixtures missing tests.make_env: " + repr(offenders))
