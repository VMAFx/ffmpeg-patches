# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Reject imprecise comparison assertions and premature unittest main guards."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

from tests.helpers import python_sources

ROOT = Path(__file__).resolve().parent.parent


def imprecise_asserts(source: str, *, filename: str) -> list[str]:
    """Return single-comparison assertTrue and assertFalse calls without positional messages."""
    findings = []
    nodes = list(ast.walk(ast.parse(source, filename=filename)))
    for node in nodes:
        match node:
            case ast.Call(
                func=ast.Attribute(attr=("assertTrue" | "assertFalse") as method),
                args=[ast.Compare(ops=[_]) as comparison],
            ):
                findings.append(f"{node.lineno}: {method}({ast.unparse(comparison)})")
    return findings


def statements_after_main_guard(source: str, *, filename: str) -> list[int]:
    """Return top-level statement lines following the last exact main guard."""
    body = ast.parse(source, filename=filename).body
    last_guard = -1
    for index, node in enumerate(body):
        match node:
            case (
                ast.If(
                    test=ast.Compare(
                        left=ast.Name(id="__name__"),
                        ops=[ast.Eq()],
                        comparators=[ast.Constant(value="__main__")],
                    )
                )
                | ast.If(
                    test=ast.Compare(
                        left=ast.Constant(value="__main__"),
                        ops=[ast.Eq()],
                        comparators=[ast.Name(id="__name__")],
                    )
                )
            ):
                last_guard = index
    return [node.lineno for node in body[last_guard + 1 :]] if last_guard >= 0 else []


class UnittestShapeTest(unittest.TestCase):
    def test_repository_comparison_assertions_are_precise(self) -> None:
        """Tracked Python sources avoid the exact imprecise assertion query shape."""
        offenders = []
        for path in python_sources(ROOT):
            relative = path.relative_to(ROOT)
            offenders.extend(
                f"{relative}:{finding}"
                for finding in imprecise_asserts(
                    path.read_text(encoding="utf-8"), filename=str(relative)
                )
            )
        self.assertEqual(offenders, [], "imprecise assertions:\n" + "\n".join(offenders))

    def test_repository_main_guards_are_last(self) -> None:
        """Tracked Python sources define every top-level statement before their last guard."""
        offenders = []
        for path in python_sources(ROOT):
            relative = path.relative_to(ROOT)
            offenders.extend(
                f"{relative}:{line}"
                for line in statements_after_main_guard(
                    path.read_text(encoding="utf-8"), filename=str(relative)
                )
            )
        self.assertEqual(offenders, [], "statements after main guard:\n" + "\n".join(offenders))

    def test_imprecise_asserts_report_single_comparisons(self) -> None:
        """Both assertion methods report every single comparison operator."""
        for operator in ("==", "!=", "<", "<=", ">", ">=", "in", "not in", "is", "is not"):
            for method in ("assertTrue", "assertFalse"):
                with self.subTest(operator=operator, method=method):
                    source = f"self.{method}(a {operator} b)"
                    self.assertEqual(
                        imprecise_asserts(source, filename="planted.py"),
                        [f"1: {method}(a {operator} b)"],
                    )

    def test_keyword_messages_do_not_exempt_comparisons(self) -> None:
        """Keyword messages leave a comparison inside the matched query shape."""
        source = "self.assertTrue(a == b, msg=None)\nother.assertFalse(x in y, msg='message')"
        self.assertEqual(
            imprecise_asserts(source, filename="planted.py"),
            ["1: assertTrue(a == b)", "2: assertFalse(x in y)"],
        )

    def test_other_assertion_shapes_are_not_reported(self) -> None:
        """Positional messages, chains, and other calls remain outside the query."""
        sources = (
            'self.assertTrue(a == b, "msg")',
            'self.assertFalse(a != b, "msg")',
            "self.assertTrue(a < b < c)",
            "self.assertTrue(flag)",
            "self.assertIs(a == b, True)",
            "self.assertEqual(a, b)",
            "self.check(a == b)",
            "assertTrue(a == b)",
            "self.assertTrue(first=a == b)",
            "self.assertTrue()",
            "self.assertTrue(x.__eq__(y))",
        )
        for source in sources:
            with self.subTest(source=source):
                self.assertEqual(imprecise_asserts(source, filename="planted.py"), [])

    def test_statements_and_classes_after_guard_are_reported(self) -> None:
        """A statement and class after a guard identify their top-level lines."""
        source = 'if __name__ == "__main__":\n    unittest.main()\npass\nclass Late: pass\n'
        self.assertEqual(statements_after_main_guard(source, filename="planted.py"), [3, 4])

    def test_final_guard_and_no_guard_are_allowed(self) -> None:
        """A final guard, preceding function, and module without guard report nothing."""
        sources = (
            'if __name__ == "__main__":\n    unittest.main()\n',
            'def earlier(): pass\nif __name__ == "__main__":\n    unittest.main()\n',
            "class Test: pass\n",
            "unittest.main()\nclass Late: pass\n",
        )
        for source in sources:
            with self.subTest(source=source):
                self.assertEqual(statements_after_main_guard(source, filename="planted.py"), [])

    def test_reversed_guard_is_recognized(self) -> None:
        """A reversed equality guard reports later statements and permits a final guard."""
        source = 'if "__main__" == __name__:\n    unittest.main()\n'
        self.assertEqual(statements_after_main_guard(source, filename="planted.py"), [])
        self.assertEqual(statements_after_main_guard(source + "pass\n", filename="planted.py"), [3])

    def test_only_last_top_level_guard_counts(self) -> None:
        """Earlier guards and nested guards do not change the last top-level boundary."""
        source = (
            'if __name__ == "__main__":\n    unittest.main()\n'
            "class Between: pass\n"
            'if "__main__" == __name__:\n    unittest.main()\n'
        )
        self.assertEqual(statements_after_main_guard(source, filename="planted.py"), [])
        self.assertEqual(statements_after_main_guard(source + "pass\n", filename="planted.py"), [6])
        nested = 'def wrapper():\n    if __name__ == "__main__":\n        pass\npass\n'
        self.assertEqual(statements_after_main_guard(nested, filename="planted.py"), [])

    def test_other_guard_conditions_are_not_reported(self) -> None:
        """Only the exact name, equality, and string condition establishes a guard."""
        for condition in (
            '__name__ != "__main__"',
            '__name__ == "other"',
            'other == "__main__"',
            'module.__name__ == "__main__"',
            '__name__ == "__main__" == other',
            '__name__ == "__main__" and flag',
        ):
            with self.subTest(condition=condition):
                source = f"if {condition}:\n    pass\npass\n"
                self.assertEqual(statements_after_main_guard(source, filename="planted.py"), [])

    def test_empty_source_and_trailing_comments_are_allowed(self) -> None:
        """Empty text and non-statement lines after a final guard report nothing."""
        self.assertEqual(imprecise_asserts("", filename="empty.py"), [])
        self.assertEqual(statements_after_main_guard("", filename="empty.py"), [])
        source = 'if __name__ == "__main__":\n    unittest.main()\n\n# trailing comment\n\n'
        self.assertEqual(statements_after_main_guard(source, filename="planted.py"), [])


if __name__ == "__main__":
    unittest.main()
