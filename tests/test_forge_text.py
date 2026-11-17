# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Check outbound forge prose for listed assistant-chat filler phrases."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class ForgeTextTest(unittest.TestCase):
    """Exercise phrase rejection, Markdown exclusions and file errors through main."""

    def setUp(self) -> None:
        """Load the helper by path and create a disposable body-file directory."""
        spec = importlib.util.spec_from_file_location("forge_text", ROOT / "scripts/forge_text.py")
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.tmp = Path(directory.name)

    def write_fixture(self, name: str, content: str) -> Path:
        """Write one UTF-8 body file inside the fixture directory."""
        path = self.tmp / name
        path.write_text(content, encoding="utf-8")
        return path

    def run_cli(self, *paths: Path) -> tuple[int, str]:
        """Call main with file arguments and capture diagnostic stderr."""
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            rc = self.module.main([str(path) for path in paths])
        return rc, stderr.getvalue()

    def test_each_phrase_is_refused(self) -> None:
        """Every lowercased phrase in the tuple produces its exact diagnostic."""
        self.assertIsInstance(self.module.PHRASES, tuple)
        for phrase in self.module.PHRASES:
            with self.subTest(phrase=phrase):
                self.assertEqual(phrase, phrase.lower())
                path = self.write_fixture("body.md", f"Technical note.\n{phrase}.\n")
                self.assertEqual(self.run_cli(path), (1, f"{path}:2: filler phrase: {phrase}\n"))

    def test_review_opener_is_refused(self) -> None:
        """The planted bot review opener is refused before posting."""
        path = self.write_fixture(
            "body.md", "Hey - I've reviewed your changes and they look great!\n"
        )
        self.assertEqual(
            self.run_cli(path), (1, f"{path}:1: filler phrase: i've reviewed your changes\n")
        )

    def test_reply_offer_is_refused(self) -> None:
        """The planted bot reply offer is refused before posting."""
        path = self.write_fixture("body.md", "Would you like me to add a test?\n")
        self.assertEqual(
            self.run_cli(path), (1, f"{path}:1: filler phrase: would you like me to\n")
        )

    def test_technical_body_passes(self) -> None:
        """A concrete filter change and test command contain no listed filler."""
        path = self.write_fixture(
            "body.md",
            "The filter now preserves the frame timestamp when cloning a lane.\n"
            "An empty input produces no output frame.\n"
            "Validation: `make fate-filter-laneclone` passed with the boundary fixture.\n",
        )
        self.assertEqual(self.run_cli(path), (0, ""))

    def test_aid_to_reviewers_passes(self) -> None:
        """A phrase prefix inside aid is ordinary prose."""
        path = self.write_fixture("body.md", "The table serves as an aid to reviewers.\n")
        self.assertEqual(self.run_cli(path), (0, ""))

    def test_questionnaire_passes(self) -> None:
        """A phrase prefix inside questionnaire is ordinary prose."""
        path = self.write_fixture("body.md", "A great questionnaire follows.\n")
        self.assertEqual(self.run_cli(path), (0, ""))

    def test_aim_passes(self) -> None:
        """A phrase prefix inside aim is ordinary prose."""
        path = self.write_fixture("body.md", "Treat it as an aim, not a rule.\n")
        self.assertEqual(self.run_cli(path), (0, ""))

    def test_phrase_at_line_start_is_refused(self) -> None:
        """Line start and trailing punctuation delimit a listed phrase."""
        path = self.write_fixture("body.md", "Great question!\n")
        self.assertEqual(self.run_cli(path), (1, f"{path}:1: filler phrase: great question\n"))

    def test_parenthesized_phrase_is_refused(self) -> None:
        """Parentheses delimit a listed phrase on both sides."""
        path = self.write_fixture("body.md", "(as an AI)\n")
        self.assertEqual(self.run_cli(path), (1, f"{path}:1: filler phrase: as an ai\n"))

    def test_phrase_ending_in_punctuation_is_refused(self) -> None:
        """A final question mark delimits the reply offer."""
        path = self.write_fixture("body.md", "would you like me to?\n")
        self.assertEqual(
            self.run_cli(path), (1, f"{path}:1: filler phrase: would you like me to\n")
        )

    def test_whole_line_phrase_is_refused(self) -> None:
        """A phrase occupying the entire line needs no surrounding characters."""
        path = self.write_fixture("body.md", "i hope this helps")
        self.assertEqual(self.run_cli(path), (1, f"{path}:1: filler phrase: i hope this helps\n"))

    def test_fenced_phrases_pass(self) -> None:
        """Backtick and tilde fences hide quoted examples using base-claim rules."""
        for opener, closer in (("```text", "```"), ("   ~~~~", "   ~~~~~ ")):
            with self.subTest(opener=opener):
                path = self.write_fixture(
                    "body.md", f"{opener}\nWould you like me to add a test?\n{closer}\n"
                )
                self.assertEqual(self.run_cli(path), (0, ""))

    def test_fence_marker_and_length_are_respected(self) -> None:
        """Shorter or different fence markers cannot close an open fence."""
        path = self.write_fixture(
            "body.md", "````text\n```\n~~~\nWould you like me to add a test?\n````\n"
        )
        self.assertEqual(self.run_cli(path), (0, ""))

    def test_phrase_after_fence_is_refused(self) -> None:
        """The closing fence restores checking and preserves original line numbers."""
        path = self.write_fixture("body.md", "```\nexample\n```\nGreat question!\n")
        self.assertEqual(self.run_cli(path), (1, f"{path}:4: filler phrase: great question\n"))

    def test_invalid_fence_openers_do_not_hide_prose(self) -> None:
        """Indented and backtick-bearing info strings follow the base checker rules."""
        for opener in ("    ```", "``` a`b"):
            with self.subTest(opener=opener):
                path = self.write_fixture("body.md", f"{opener}\nGreat question!\n")
                self.assertEqual(
                    self.run_cli(path), (1, f"{path}:2: filler phrase: great question\n")
                )

    def test_quote_lines_pass(self) -> None:
        """Quote lines may repeat a bot body after optional leading whitespace."""
        path = self.write_fixture(
            "body.md", "> I've reviewed your changes.\n \t> Would you like me to add a test?\n"
        )
        self.assertEqual(self.run_cli(path), (0, ""))

    def test_inline_code_passes(self) -> None:
        """Inline code spans exclude filler with matching single or multiple backticks."""
        for sample in (
            "The rejected text was `Would you like me to add a test?`.\n",
            "Example: ``Would you like me to `add` a test?``.\n",
        ):
            with self.subTest(sample=sample):
                path = self.write_fixture("body.md", sample)
                self.assertEqual(self.run_cli(path), (0, ""))

    def test_inline_code_does_not_hide_surrounding_prose(self) -> None:
        """A code span leaves surrounding prose and unmatched backticks checked."""
        for sample in ("`example` Great question!", "`Great question!"):
            with self.subTest(sample=sample):
                path = self.write_fixture("body.md", sample)
                self.assertEqual(
                    self.run_cli(path), (1, f"{path}:1: filler phrase: great question\n")
                )

    def test_multiline_inline_code_preserves_line_numbers(self) -> None:
        """Multiline inline spans hide their contents without shifting later diagnostics."""
        path = self.write_fixture(
            "body.md",
            "Example: `quoted\nWould you like me to add a test?\ntext`\nGreat question!\n",
        )
        self.assertEqual(self.run_cli(path), (1, f"{path}:4: filler phrase: great question\n"))

    def test_case_and_whitespace_variant_is_refused(self) -> None:
        """Case and repeated whitespace normalize before phrase matching."""
        path = self.write_fixture("body.md", "Would  You LIKE me to\tadd a test?\n")
        self.assertEqual(
            self.run_cli(path), (1, f"{path}:1: filler phrase: would you like me to\n")
        )

    def test_typographic_apostrophe_is_refused(self) -> None:
        """The typographic apostrophe normalizes to the listed ASCII phrase."""
        path = self.write_fixture("body.md", "I\u2019ve reviewed your changes.\n")
        self.assertEqual(
            self.run_cli(path), (1, f"{path}:1: filler phrase: i've reviewed your changes\n")
        )

    def test_one_finding_per_line(self) -> None:
        """Two phrases on one line produce one finding and later lines remain checked."""
        path = self.write_fixture(
            "body.md", "Great question; I hope this helps.\nThanks for your patience.\n"
        )
        rc, err = self.run_cli(path)
        self.assertEqual(rc, 1)
        self.assertEqual(len(err.splitlines()), 2)
        self.assertIn(f"{path}:1: filler phrase:", err)
        self.assertIn(f"{path}:2: filler phrase: thanks for your patience\n", err)

    def test_missing_file_returns_two(self) -> None:
        """An unreadable path returns two and names the failed file."""
        path = self.tmp / "missing.md"
        rc, err = self.run_cli(path)
        self.assertEqual(rc, 2)
        self.assertIn(str(path), err)

    def test_invalid_utf8_returns_two(self) -> None:
        """Invalid UTF-8 is an input error rather than accepted or replaced text."""
        path = self.tmp / "invalid.md"
        path.write_bytes(b"\xff\n")
        rc, err = self.run_cli(path)
        self.assertEqual(rc, 2)
        self.assertIn(str(path), err)

    def test_second_file_finding_names_second_path(self) -> None:
        """All file arguments are checked and a later finding retains its own path."""
        first = self.write_fixture("first.md", "Preserve the filter timestamp.\n")
        second = self.write_fixture("second.md", "Would you like me to add a test?\n")
        self.assertEqual(
            self.run_cli(first, second),
            (1, f"{second}:1: filler phrase: would you like me to\n"),
        )


if __name__ == "__main__":
    unittest.main()
