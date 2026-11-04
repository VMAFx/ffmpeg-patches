# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Require explicit text encodings across scripts and regression tests."""

from __future__ import annotations

import ast
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def missing_encodings(path: Path) -> list[str]:
    """List text method calls lacking an encoding keyword with their locations."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return sorted(
        f"{path}:{node.lineno}: {node.func.attr} lacks encoding"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"read_text", "write_text"}
        and not any(keyword.arg == "encoding" for keyword in node.keywords)
    )


class TextEncodingTest(unittest.TestCase):
    """Check repository text calls and planted examples in both directions."""

    def test_repository_text_calls_have_encoding(self) -> None:
        """Reject implicit encodings in every script and test Python source."""
        findings = [
            finding
            for folder in ("scripts", "tests")
            for path in sorted((ROOT / folder).rglob("*.py"))
            for finding in missing_encodings(path)
        ]
        self.assertEqual(findings, [], "\n".join(findings))

    def test_planted_missing_encodings_are_reported(self) -> None:
        """Detect implicit reads, writes and nested calls with file and line."""
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / "planted.py"
            path.write_text(
                'path.read_text()\npath.write_text("text")\n'
                'path.write_text(path.read_text(errors="replace"), encoding="utf-8")\n',
                encoding="utf-8",
            )
            self.assertEqual(
                missing_encodings(path),
                [
                    f"{path}:1: read_text lacks encoding",
                    f"{path}:2: write_text lacks encoding",
                    f"{path}:3: read_text lacks encoding",
                ],
            )

    def test_explicit_encodings_pass(self) -> None:
        """Accept explicit encodings while ignoring other methods and string literals."""
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / "explicit.py"
            path.write_text(
                'path.read_text(encoding="utf-8", errors="replace")\n'
                'path.write_text("text", encoding="utf-8")\n'
                'path.read_bytes()\ntext = "path.read_text()"\n',
                encoding="utf-8",
            )
            self.assertEqual(missing_encodings(path), [])
