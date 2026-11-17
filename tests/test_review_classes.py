# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Exercise the canonical review class list and fail-closed YAML reader."""

from __future__ import annotations

import ast
import copy
import tempfile
import unittest
from pathlib import Path

import yaml

from scripts import lane_brief, review_classes, variant_sweep

ROOT = Path(__file__).resolve().parents[1]
ITEM = {
    "id": "example-class",
    "hits": 2,
    "pull_requests": [1],
    "definition": "A recurring review issue.",
    "question": "Has this issue been checked?",
}


class ReviewClassesTest(unittest.TestCase):
    """Check real check references and small temporary malformed class files."""

    def setUp(self) -> None:
        """Create a temporary path for each bounded class document."""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "classes.yaml"

    def load(self, document: object) -> list[dict]:
        """Write one safe YAML document and load it through the production reader."""
        self.path.write_text(yaml.safe_dump(document), encoding="utf-8")
        return review_classes.load_classes(self.path)

    def invalid_item(self, field: str, value: object, message: str) -> None:
        """Require refusal of one malformed field in an otherwise valid class."""
        item = copy.deepcopy(ITEM)
        item[field] = value
        with self.assertRaisesRegex(ValueError, message):
            self.load({"classes": [item]})

    def invalid_text(self, text: str, message: str) -> None:
        """Require refusal of a literal malformed YAML file."""
        self.path.write_text(text, encoding="utf-8")
        with self.assertRaisesRegex(ValueError, message):
            review_classes.load_classes(self.path)

    def test_real_classes_and_check_references(self) -> None:
        """Load 39 unique classes, confirm question endings, and resolve declared checks."""
        classes = review_classes.load_classes()
        self.assertEqual(len(classes), 39)
        self.assertEqual(len({item["id"] for item in classes}), 39)
        tree = ast.parse((ROOT / "scripts/upstream_lint.py").read_text(encoding="utf-8"))
        steps = {
            node.args[0].value
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_collect"
            and node.args
            and isinstance(node.args[0], ast.Constant)
        }
        for item in classes:
            self.assertTrue(item["question"].endswith("?"), item["id"])
            if "check" not in item:
                continue
            kind, target = item["check"].split(":", 1)
            with self.subTest(check=item["check"]):
                if kind == "upstream-lint":
                    self.assertIn(target, steps)
                elif kind == "variant-sweep":
                    self.assertIn(target, variant_sweep.REPORT_TABLES)
                else:
                    self.assertEqual(kind, "script")
                    self.assertTrue((ROOT / target).is_file())

    def test_one_class_and_hits_two(self) -> None:
        """Accept a single class with exactly the minimum recurring hit count."""
        self.assertEqual(self.load({"classes": [ITEM]}), [ITEM])

    def test_missing_file(self) -> None:
        """Refuse a missing class file with its path in the diagnostic."""
        with self.assertRaisesRegex(ValueError, str(self.path)):
            review_classes.load_classes(self.path)

    def test_nonregular_file(self) -> None:
        """Refuse a directory instead of a regular class file."""
        self.path.mkdir()
        with self.assertRaises(ValueError):
            review_classes.load_classes(self.path)

    def test_invalid_yaml(self) -> None:
        """Refuse malformed YAML rather than treating it as an empty list."""
        self.invalid_text("classes: [", "invalid YAML")

    def test_unsafe_yaml(self) -> None:
        """Refuse Python object tags through the shared safe YAML loader."""
        self.invalid_text("classes: !!python/tuple []", "invalid YAML")

    def test_duplicate_mapping_key(self) -> None:
        """Refuse duplicate class field keys before schema validation."""
        self.invalid_text("classes:\n  - id: first\n    id: second\n", "duplicate key id")

    def test_duplicate_root_key(self) -> None:
        """Refuse a second classes mapping instead of silently replacing the first."""
        self.invalid_text("classes: []\nclasses: []\n", "duplicate key classes")

    def test_unhashable_mapping_key(self) -> None:
        """Refuse non-scalar YAML mapping keys with a controlled error."""
        self.invalid_text("? [classes]\n: []\n", "unhashable key")

    def test_invalid_root(self) -> None:
        """Refuse absent, nonmapping, missing, or unknown top-level fields."""
        for document in (None, [], {}, {"other": []}, {"classes": [ITEM], "other": []}):
            with self.subTest(document=document), self.assertRaisesRegex(ValueError, "classes"):
                self.load(document)

    def test_invalid_class_list(self) -> None:
        """Refuse an empty or nonlist class collection."""
        for value in ([], None, "example-class", {}):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "nonempty"):
                self.load({"classes": value})

    def test_invalid_class_fields(self) -> None:
        """Refuse nonmapping classes, missing fields, and unknown fields."""
        for item in (None, "example-class", {}, {**ITEM, "unexpected": True}):
            with self.subTest(item=item), self.assertRaisesRegex(ValueError, "fields"):
                self.load({"classes": [item]})
        for field in ITEM:
            item = dict(ITEM)
            del item[field]
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "fields"):
                self.load({"classes": [item]})

    def test_invalid_id(self) -> None:
        """Refuse ids that cannot name one stable Markdown class row."""
        for value in (None, 1, "", "Upper", "bad id", "bad|id", "bad--id"):
            with self.subTest(value=value):
                self.invalid_item("id", value, "class id")

    def test_duplicate_id(self) -> None:
        """Refuse repeated class ids even when their mappings differ."""
        with self.assertRaisesRegex(ValueError, "duplicate class id example-class"):
            self.load({"classes": [ITEM, {**ITEM, "hits": 3}]})

    def test_invalid_hits(self) -> None:
        """Refuse nonrecurring counts and values that only coerce to integers."""
        for value in (None, True, "2", 2.0, 1, 0, -1):
            with self.subTest(value=value):
                self.invalid_item("hits", value, "hits")

    def test_invalid_pull_request_list(self) -> None:
        """Refuse missing, empty, or nonlist pull request evidence."""
        for value in (None, [], "1", {}):
            with self.subTest(value=value):
                self.invalid_item("pull_requests", value, "pull_requests")

    def test_invalid_pull_request_number(self) -> None:
        """Refuse zero, negative, noninteger, and boolean pull request numbers."""
        for value in (None, True, "1", 1.0, 0, -1):
            with self.subTest(value=value):
                self.invalid_item("pull_requests", [value], "pull request number")

    def test_duplicate_pull_request_number(self) -> None:
        """Refuse duplicate pull request evidence within one class."""
        self.invalid_item("pull_requests", [1, 1], "duplicate pull request")

    def test_descending_pull_requests(self) -> None:
        """Refuse descending pull requests with the file path and ordering problem."""
        self.invalid_item("pull_requests", [2, 1], rf"{self.path}:.*strictly ascending")

    def test_unsorted_three_pull_requests(self) -> None:
        """Refuse a three-number list that starts ascending but ends out of order."""
        self.invalid_item("pull_requests", [1, 3, 2], rf"{self.path}:.*strictly ascending")

    def test_invalid_definition(self) -> None:
        """Refuse empty, nontext, non-ASCII, or table-breaking definitions."""
        for value in (None, "", " padded ", "line\nbreak", "bad|cell", "\u00e9", "bad\tcell"):
            with self.subTest(value=value):
                self.invalid_item("definition", value, "definition")

    def test_invalid_question(self) -> None:
        """Refuse questions missing a final ? or unable to fit exactly in a table cell."""
        for value in (
            None,
            "",
            " padded ",
            "line\nbreak",
            "bad|cell",
            "\u00e9",
            "bad\rcell",
            "Missing question mark",
        ):
            with self.subTest(value=value):
                self.invalid_item("question", value, "question")

    def test_invalid_check(self) -> None:
        """Refuse unknown check kinds, empty targets, and nontext check values."""
        for value in (None, 1, "none", "other:step", "upstream-lint:", "script:bad path"):
            with self.subTest(value=value):
                self.invalid_item("check", value, "check")

    def test_invalid_encoding(self) -> None:
        """Refuse bytes that cannot be decoded as the required UTF-8 YAML."""
        self.path.write_bytes(b"classes: \xff\n")
        with self.assertRaises(ValueError):
            review_classes.load_classes(self.path)

    def test_line_size_bound(self) -> None:
        """Refuse class input with one line beyond the shared reader limit."""
        self.invalid_text("#" * (lane_brief.MAX_LINE_SIZE + 1), "line exceeds")

    def test_line_count_bound(self) -> None:
        """Refuse input with one more than the bounded line count."""
        self.invalid_text("\n" * (lane_brief.MAX_LINES + 1), "exceeds 10000 lines")

    def test_size_boundary(self) -> None:
        """Accept the size limit and refuse a class file exactly one byte beyond it."""
        self.load({"classes": [ITEM]})
        data = self.path.read_bytes()
        remaining = lane_brief.MAX_SIZE - len(data)
        padding = (b"#" + b"x" * 4094 + b"\n") * (remaining // 4096)
        padding += b"#" * (remaining % 4096)
        self.path.write_bytes(data + padding)
        self.assertEqual(review_classes.load_classes(self.path), [ITEM])
        self.path.write_bytes(data + padding + b"x")
        with self.assertRaisesRegex(ValueError, "size exceeds"):
            review_classes.load_classes(self.path)
