# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Require explicit text encodings across scripts and regression tests."""

from __future__ import annotations

import ast
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _has_positional_encoding(node: ast.Call) -> bool:
    """Identify an encoding slot only when earlier arguments have known positions."""
    index = 0 if node.func.attr == "read_text" else 1
    return len(node.args) > index and not any(
        isinstance(argument, ast.Starred) for argument in node.args[: index + 1]
    )


def missing_encodings(path: Path) -> list[str]:
    """List text calls lacking a keyword or unambiguous positional encoding."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return sorted(
        f"{path}:{node.lineno}: {node.func.attr} lacks encoding"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"read_text", "write_text"}
        and not any(keyword.arg == "encoding" for keyword in node.keywords)
        and not _has_positional_encoding(node)
    )


def duplicate_encodings(path: Path) -> list[str]:
    """List text calls supplying both positional and keyword encodings."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return sorted(
        f"{path}:{node.lineno}: {node.func.attr} duplicate encoding"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"read_text", "write_text"}
        and _has_positional_encoding(node)
        and any(keyword.arg == "encoding" for keyword in node.keywords)
    )


class TextEncodingTest(unittest.TestCase):
    """Check repository text calls and planted examples in both directions."""

    def test_repository_text_calls_have_encoding(self) -> None:
        """Reject missing or duplicate encodings in script and test Python sources."""
        for check in (missing_encodings, duplicate_encodings):
            with self.subTest(check=check.__name__):
                findings = [
                    finding
                    for folder in ("scripts", "tests")
                    for path in sorted((ROOT / folder).rglob("*.py"))
                    for finding in check(path)
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
                'p.read_text("utf-8")\np.write_text("x", "utf-8")\n'
                'p.read_text("utf-8", *a)\np.write_text("x", "utf-8", *a)\n'
                'path.read_bytes()\ntext = "path.read_text()"\n',
                encoding="utf-8",
            )
            self.assertEqual(missing_encodings(path), [])

    def test_planted_starred_encodings_are_reported(self) -> None:
        """Reject missing encodings and starred arguments obscuring encoding slots."""
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / "starred.py"
            path.write_text(
                'p.write_text("x")\np.read_text(*a)\np.read_text(*a, "utf-8")\n'
                'p.write_text(*a, "utf-8")\np.write_text("x", *a, "utf-8")\n',
                encoding="utf-8",
            )
            self.assertEqual(
                missing_encodings(path),
                [
                    f"{path}:1: write_text lacks encoding",
                    f"{path}:2: read_text lacks encoding",
                    f"{path}:3: read_text lacks encoding",
                    f"{path}:4: write_text lacks encoding",
                    f"{path}:5: write_text lacks encoding",
                ],
            )

    def test_planted_duplicate_encodings_are_reported(self) -> None:
        """Detect duplicate read and write encodings, including nested calls."""
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / "duplicates.py"
            path.write_text(
                'p.read_text("utf-8", encoding="utf-8")\n'
                'p.write_text("x", "utf-8", encoding="utf-8")\n'
                'p.write_text(p.read_text("utf-8", encoding="utf-8"), encoding="utf-8")\n',
                encoding="utf-8",
            )
            self.assertEqual(
                duplicate_encodings(path),
                [
                    f"{path}:1: read_text duplicate encoding",
                    f"{path}:2: write_text duplicate encoding",
                    f"{path}:3: read_text duplicate encoding",
                ],
            )

    def test_single_encodings_have_no_duplicates(self) -> None:
        """Accept single encodings and avoid assuming starred argument contents."""
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / "singles.py"
            path.write_text(
                'p.read_text("utf-8")\np.write_text("x", "utf-8")\n'
                'p.read_text(encoding="utf-8")\np.write_text("x", encoding="utf-8")\n'
                'p.write_text("x")\np.read_text(*a, encoding="utf-8")\n'
                'p.write_text(*a, "utf-8", encoding="utf-8")\n',
                encoding="utf-8",
            )
            self.assertEqual(duplicate_encodings(path), [])
