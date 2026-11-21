# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Unit tests for base-commit claim parsing and live merge-base checks."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from scripts import base_claims

MERGE_BASE = "c" * 40


class BaseClaimsTest(unittest.TestCase):
    """Verify base-claim extraction, filtering, staleness checks and CLI exit codes."""

    def setUp(self) -> None:
        """Create a temporary fixture directory registered for cleanup."""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.tmp = Path(directory.name)

    def write_fixture(self, name: str, content: str) -> Path:
        """Write UTF-8 fixture text inside the temporary directory."""
        path = self.tmp / name
        path.write_text(content, encoding="utf-8")
        return path

    def run_cli(self, *argv: str) -> tuple[int, str]:
        """Execute base_claims.main with captured stderr."""
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            rc = base_claims.main(list(argv))
        return rc, stderr.getvalue()

    def test_find_base_claims_matches_supported_forms(self) -> None:
        """Supported base-claim phrasings return lowercased hex hashes in order."""
        forty_a = "a" * 40
        self.assertEqual(
            base_claims.find_base_claims("Base: FFmpeg master 46f23be8b197."),
            ["46f23be8b197"],
        )
        self.assertEqual(base_claims.find_base_claims(f"base: {forty_a}"), [forty_a])
        self.assertEqual(
            base_claims.find_base_claims("proposed base master abc1234"),
            ["abc1234"],
        )
        self.assertEqual(
            base_claims.find_base_claims("Base: FFmpeg commit 46F23BE8B197"),
            ["46f23be8b197"],
        )

    def test_find_base_claims_ignores_non_claims_and_boundaries(self) -> None:
        """Unrelated words, quotes, fences, history markers and 6-hex values are ignored."""
        fenced = "```text\nBase: master 46f23be8b197\n```\n"
        for sample in (
            "database 1234567",
            "base64 abcdef1",
            "parent a88838af4821",
            "> Base: master 46f23be8b197",
            fenced,
            "Previous base: 46f23be8b197",
            "base abc123",
        ):
            with self.subTest(sample=sample):
                self.assertEqual(base_claims.find_base_claims(sample), [])

    def test_stale_claims_checks_merge_base_prefixes(self) -> None:
        """Prefixes of merge_base pass while non-matching hashes are returned."""
        self.assertEqual(
            base_claims.stale_claims(f"Base: FFmpeg master {MERGE_BASE[:12]}.", MERGE_BASE),
            [],
        )
        self.assertEqual(
            base_claims.stale_claims("Base: FFmpeg master 46f23be8b197.", MERGE_BASE),
            ["46f23be8b197"],
        )
        self.assertEqual(
            base_claims.stale_claims(f"base: {MERGE_BASE[:7]}", MERGE_BASE.upper()),
            [],
        )

    def test_main_with_merge_base_and_body_file(self) -> None:
        """CLI --merge-base rejects stale claims and accepts current or absent claims."""
        stale_file = self.write_fixture("stale.md", "Base: FFmpeg master 46f23be8b197.\n")
        rc, err = self.run_cli("--merge-base", MERGE_BASE, str(stale_file))
        self.assertEqual(rc, 1)
        self.assertIn(
            f"{stale_file}: names base 46f23be8b197, the live merge base is {MERGE_BASE}",
            err,
        )

        current_file = self.write_fixture("current.md", f"Base: {MERGE_BASE[:12]}\n")
        rc, err = self.run_cli("--merge-base", MERGE_BASE, str(current_file))
        self.assertEqual(rc, 0)
        self.assertEqual(err, "")

        plain_file = self.write_fixture("plain.md", "No base commit mentioned here.\n")
        rc, err = self.run_cli("--merge-base", MERGE_BASE, str(plain_file))
        self.assertEqual(rc, 0)
        self.assertEqual(err, "")

    def test_main_with_pull_json_and_live_body(self) -> None:
        """CLI --pull-json validates merge_base presence and optional live PR body claims."""
        missing_base = self.write_fixture("missing.json", json.dumps({"body": ""}))
        claim_file = self.write_fixture("claim.md", "Base: FFmpeg master 46f23be8b197.\n")
        plain_file = self.write_fixture("plain.md", "Unrelated text.\n")

        rc, err = self.run_cli("--pull-json", str(missing_base), str(claim_file))
        self.assertEqual(rc, 2)
        self.assertNotEqual(err, "")

        rc, err = self.run_cli("--pull-json", str(missing_base), str(plain_file))
        self.assertEqual(rc, 0)
        self.assertEqual(err, "")

        stale_pull = self.write_fixture(
            "stale.json",
            json.dumps({"merge_base": MERGE_BASE, "body": "Base: FFmpeg master 46f23be8b197.\n"}),
        )
        rc, err = self.run_cli("--pull-json", str(stale_pull), "--live-body")
        self.assertEqual(rc, 1)
        self.assertIn("live PR body", err)

        rc, err = self.run_cli("--pull-json", str(stale_pull))
        self.assertEqual(rc, 0)
        self.assertEqual(err, "")

    def test_main_stdin_and_input_errors(self) -> None:
        """CLI stdin reading works and unreadable inputs or bad merge bases return 2."""
        payload = json.dumps(
            {"merge_base": MERGE_BASE, "body": "Base: FFmpeg master 46f23be8b197.\n"}
        )
        with unittest.mock.patch("sys.stdin", io.StringIO(payload)):
            rc, err = self.run_cli("--pull-json", "-", "--live-body")
        self.assertEqual(rc, 1)
        self.assertIn("live PR body: names base 46f23be8b197", err)

        rc, err = self.run_cli("--merge-base", MERGE_BASE, str(self.tmp / "nonexistent.md"))
        self.assertEqual(rc, 2)
        self.assertNotEqual(err, "")

        claim_file = self.write_fixture("claim.md", "Base: FFmpeg master 46f23be8b197.\n")
        rc, err = self.run_cli("--merge-base", "short", str(claim_file))
        self.assertEqual(rc, 2)
        self.assertNotEqual(err, "")

    def test_find_base_claims_commonmark_fences(self) -> None:
        """CommonMark backtick and tilde fences track marker, length, indent and info strings."""
        cases = (
            ("````text\n```\n````\nBase: aaaaaaa.\n", ["aaaaaaa"]),
            ("~~~\nBase: aaaaaaa.\n~~~\n", []),
            ("```\nBase: aaaaaaa.\n````  \nBase: bbbbbbb.\n", ["bbbbbbb"]),
            ("```\n``` text\nBase: aaaaaaa.\n```\n", []),
            ("~~~\n```\nBase: aaaaaaa.\n~~~\nBase: bbbbbbb.\n", ["bbbbbbb"]),
            ("    ```\nBase: aaaaaaa.\n", ["aaaaaaa"]),
            ("``` a`b\nBase: aaaaaaa.\n", ["aaaaaaa"]),
        )
        for sample, expected in cases:
            with self.subTest(sample=sample):
                self.assertEqual(base_claims.find_base_claims(sample), expected)

    def test_find_base_claims_clause_scoped_history_words(self) -> None:
        """History markers drop only claims in their own clause and keep later current claims."""
        compound = "Previous base: ccccccc; current base: aaaaaaa."
        self.assertEqual(base_claims.find_base_claims(compound), ["aaaaaaa"])
        self.assertEqual(base_claims.stale_claims(compound, "c" * 40), ["aaaaaaa"])
        self.assertEqual(
            base_claims.find_base_claims("Previous base: ccccccc, now base aaaaaaa"),
            ["aaaaaaa"],
        )
        self.assertEqual(
            base_claims.find_base_claims("rebased from the previous base abc1234"),
            [],
        )

    def test_main_head_option(self) -> None:
        """CLI --head checks exact 40-hex pull head.sha and rejects short or invalid heads."""
        pull_file = self.write_fixture(
            "pull.json",
            json.dumps({"head": {"sha": "a" * 40}, "merge_base": "c" * 40}),
        )
        rc, err = self.run_cli("--pull-json", str(pull_file), "--head", "a" * 40)
        self.assertEqual(rc, 0)
        self.assertEqual(err, "")

        rc, err = self.run_cli("--pull-json", str(pull_file), "--head", "A" * 40)
        self.assertEqual(rc, 0)
        self.assertEqual(err, "")

        rc, err = self.run_cli("--pull-json", str(pull_file), "--head", "b" * 40)
        self.assertEqual(rc, 3)
        self.assertIn("pull head", err)

        no_head = self.write_fixture("no_head.json", json.dumps({"merge_base": "c" * 40}))
        rc, err = self.run_cli("--pull-json", str(no_head), "--head", "a" * 40)
        self.assertEqual(rc, 2)
        self.assertNotEqual(err, "")

        rc, err = self.run_cli("--merge-base", "c" * 40, "--head", "a" * 40)
        self.assertEqual(rc, 2)
        self.assertNotEqual(err, "")

        rc, err = self.run_cli("--pull-json", str(pull_file), "--head", "xyz")
        self.assertEqual(rc, 2)
        self.assertIn("full 40-hex", err)

        rc, err = self.run_cli("--pull-json", str(pull_file), "--head", "aaaaaaa")
        self.assertEqual(rc, 2)
        self.assertIn("full 40-hex", err)

    def test_main_label_option(self) -> None:
        """CLI --label replaces the file path in stale-claim diagnostic messages."""
        stale_file = self.write_fixture("stale.md", "Base: FFmpeg master 46f23be8b197.\n")
        rc, err = self.run_cli("--merge-base", MERGE_BASE, "--label", "shown-name", str(stale_file))
        self.assertEqual(rc, 1)
        self.assertTrue(err.startswith("shown-name:"), err)

    def test_dependency_examples(self) -> None:
        """Review examples and uppercase heads yield normalized dependency claims."""
        cases = (
            (
                (
                    "Depends on #25664: the first three commits are that PR's series, "
                    "currently ending at c35b0e1fce."
                ),
                [(25664, "c35b0e1fce")],
            ),
            (
                (
                    "This branch is stacked on pull request 25726 (head edd7328845e3); "
                    "only the last three commits are new here."
                ),
                [(25726, "edd7328845e3")],
            ),
            ("on top of PR #12 (head: ABCDEF1)", [(12, "abcdef1")]),
        )
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(base_claims.find_dependency_claims(text), expected)

    def test_dependency_list_pairs_each_members_own_head(self) -> None:
        """Each list reference owns only the head inside its member span."""
        for verb in ("depends on", "stacked on", "on top of", "builds on", "based on"):
            for reference in ("#13", "PR #13", "PR 13", "pull request #13", "pull request 13"):
                text = f"{verb.upper()} #12 (head AAAAAAA) and {reference.upper()} (tip bbbbbbb)"
                with self.subTest(text=text):
                    self.assertEqual(
                        base_claims.find_dependency_claims(text), [(12, "aaaaaaa"), (13, "bbbbbbb")]
                    )

    def test_dependency_comma_list_pairs_each_members_own_head(self) -> None:
        """Comma and Oxford-comma lists pair each member with its own head."""
        for separator in (", ", ", and "):
            with self.subTest(separator=separator):
                self.assertEqual(
                    base_claims.find_dependency_claims(
                        f"Depends on #12 (head aaaaaaa){separator}PR #13 (head bbbbbbb)"
                    ),
                    [(12, "aaaaaaa"), (13, "bbbbbbb")],
                )

    def test_dependency_ampersand_list_pairs_each_members_own_head(self) -> None:
        """Ampersand lists pair each member with its own head."""
        self.assertEqual(
            base_claims.find_dependency_claims(
                "Depends on #12 (head aaaaaaa) & #13 (head bbbbbbb)"
            ),
            [(12, "aaaaaaa"), (13, "bbbbbbb")],
        )

    def test_dependency_list_headless_member_is_listed_without_claim(self) -> None:
        """A headless final member is listed without borrowing another member's head."""
        text = "Depends on #12 (head aaaaaaa) and #13"
        self.assertEqual(base_claims.find_dependency_claims(text), [(12, "aaaaaaa")])
        body = self.write_fixture("body.md", text)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            rc, err = self.run_cli("--merge-base", MERGE_BASE, "--list-dependencies", str(body))
        self.assertEqual((rc, err, stdout.getvalue()), (0, "", "12\n13\n"))

    def test_dependency_list_trailing_head_is_ambiguous(self) -> None:
        """A lone trailing list head fails closed with numbers and repair instructions."""
        message = (
            r"ambiguous.*#12.*#13.*bbbbbbb.*head right after each reference"
            r".*one dependency per sentence"
        )
        for separator in (" and ", ", ", ", and ", " & "):
            with self.subTest(separator=separator), self.assertRaisesRegex(ValueError, message):
                base_claims.find_dependency_claims(f"Depends on #12{separator}#13 (head bbbbbbb)")

    def test_dependency_cli_ambiguous_list_refuses_check_and_listing(self) -> None:
        """Both CLI modes return two and explain an ambiguous trailing list head."""
        body = self.write_fixture("body.md", "Depends on #12 and #13 (head bbbbbbb)")
        dependency = self.dependency_json({"head": {"sha": "b" * 40}})
        for flags in ((), ("--list-dependencies",)):
            with self.subTest(flags=flags):
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    rc, err = self.run_cli(
                        "--merge-base",
                        MERGE_BASE,
                        "--dependency-json",
                        f"12={dependency}",
                        *flags,
                        str(body),
                    )
                self.assertEqual(rc, 2)
                for detail in (
                    "ambiguous",
                    "#12",
                    "#13",
                    "bbbbbbb",
                    "head right after each reference",
                    "one dependency per sentence",
                ):
                    self.assertIn(detail, err)
                self.assertEqual(stdout.getvalue(), "")

    def test_dependency_list_headless_members_count_toward_limit(self) -> None:
        """Every list member counts toward the shared distinct dependency limit."""
        text = "Depends on " + ", ".join(
            f"#{number}" for number in range(1, base_claims.MAX_DEPENDENCIES + 2)
        )
        message = f"more than {base_claims.MAX_DEPENDENCIES} distinct dependency PR numbers"
        with self.assertRaisesRegex(ValueError, message):
            base_claims.find_dependency_claims(text)
        body = self.write_fixture("body.md", text)
        for flags in ((), ("--list-dependencies",)):
            with self.subTest(flags=flags):
                rc, err = self.run_cli("--merge-base", MERGE_BASE, *flags, str(body))
                self.assertEqual(rc, 2)
                self.assertIn(message, err)

    def test_dependency_list_lead_history_applies_to_every_member(self) -> None:
        """Lead-clause history suppresses every member's head across list commas."""
        for history in ("Previous", "prior", "earlier", "former", "old", "previously"):
            for lead in (f"{history} dependency: depends on #12", f"Depends on #12 {history}"):
                with self.subTest(lead=lead):
                    self.assertEqual(
                        base_claims.find_dependency_claims(
                            f"{lead} (head aaaaaaa), #13 (head bbbbbbb)"
                        ),
                        [],
                    )

    def test_dependency_list_history_is_local_to_each_members_clauses(self) -> None:
        """Member history stays local and ordinary clause breaks retain their meaning."""
        cases = (
            ("Depends on #12 (old head aaaaaaa), #13 (head bbbbbbb)", [(13, "bbbbbbb")]),
            (
                "Depends on #12 (head aaaaaaa), #13 from an earlier series (head bbbbbbb)",
                [(12, "aaaaaaa")],
            ),
            (
                "Depends on #12 (head aaaaaaa), #13 (previous head bbbbbbb, now head ccccccc)",
                [(12, "aaaaaaa"), (13, "ccccccc")],
            ),
            (
                "Previous dependency, depends on #12 (head aaaaaaa), #13 (head bbbbbbb)",
                [(12, "aaaaaaa"), (13, "bbbbbbb")],
            ),
        )
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(base_claims.find_dependency_claims(text), expected)

    def test_dependency_cli_second_list_members_stale_head_names_its_number(self) -> None:
        """A matching first head cannot hide a stale head belonging to the second member."""
        body = self.write_fixture("body.md", "Depends on #12 (head bbbbbbb) and #13 (head aaaaaaa)")
        dependency = self.dependency_json({"head": {"sha": "b" * 40}})
        rc, err = self.run_cli(
            "--merge-base",
            MERGE_BASE,
            "--dependency-json",
            f"12={dependency}",
            "--dependency-json",
            f"13={dependency}",
            str(body),
        )
        self.assertEqual(rc, 1)
        self.assertEqual(
            err, f"{body}: names head aaaaaaa of #13, the live head of #13 is {'b' * 40}\n"
        )

    def test_dependency_list_stops_before_unrelated_text(self) -> None:
        """Unrelated prose terminates list membership and its members' own head spans."""
        text = "Depends on #12 (head aaaaaaa) and #13 (head bbbbbbb), unrelated #14 head ccccccc"
        self.assertEqual(
            base_claims.find_dependency_claims(text), [(12, "aaaaaaa"), (13, "bbbbbbb")]
        )

    def test_dependency_list_nonmember_owns_unsupported_head(self) -> None:
        """A nearest nonmember reference owns its head even with an unsupported opener."""
        for reference in ("#14", "PR 14", "pull request 14"):
            for annotation in ("head ccccccc", "~ head ccccccc", "{head ccccccc}"):
                text = (
                    "Depends on #12 (head aaaaaaa) and #13 (head bbbbbbb), "
                    f"see {reference} {annotation}"
                )
                with self.subTest(text=text):
                    self.assertEqual(
                        base_claims.find_dependency_claims(text), [(12, "aaaaaaa"), (13, "bbbbbbb")]
                    )

    def test_dependency_list_unpaired_head_names_nearest_member_and_hex(self) -> None:
        """Unsupported member annotations fail closed and identify their reference and head."""
        for reference in ("#13", "PR 13", "pull request 13"):
            for annotation in ("~ head bbbbbbb", "{head bbbbbbb}"):
                text = f"Depends on #12 (head aaaaaaa) and {reference} {annotation}"
                with self.subTest(text=text), self.assertRaisesRegex(ValueError, r"#13.*bbbbbbb"):
                    base_claims.find_dependency_claims(text)

    def test_dependency_cli_unpaired_head_names_nearest_member_and_hex(self) -> None:
        """Both CLI modes identify unpaired member heads even when both live heads match."""
        first = self.dependency_json()
        second = self.write_fixture("second.json", json.dumps({"head": {"sha": "b" * 40}}))
        for annotation in ("~ head bbbbbbb", "{head bbbbbbb}"):
            body = self.write_fixture(
                "body.md", f"Depends on #12 (head aaaaaaa) and #13 {annotation}"
            )
            for flags in ((), ("--list-dependencies",)):
                with self.subTest(annotation=annotation, flags=flags):
                    rc, err = self.run_cli(
                        "--merge-base",
                        MERGE_BASE,
                        "--dependency-json",
                        f"12={first}",
                        "--dependency-json",
                        f"13={second}",
                        *flags,
                        str(body),
                    )
                    self.assertEqual(rc, 2)
                    self.assertRegex(err, r"#13.*bbbbbbb")

    def test_dependency_list_colon_heads_belong_to_their_own_members(self) -> None:
        """A colon on either list member preserves both reference and head pairs."""
        for text in (
            "Depends on #12: head aaaaaaa and #13 (head bbbbbbb)",
            "Depends on #12 (head aaaaaaa) and #13: head bbbbbbb",
        ):
            with self.subTest(text=text):
                self.assertEqual(
                    base_claims.find_dependency_claims(text), [(12, "aaaaaaa"), (13, "bbbbbbb")]
                )

    def test_dependency_cli_colon_heads_list_both_members(self) -> None:
        """Listing prints both dependency numbers with a colon on either member."""
        for text in (
            "Depends on #12: head aaaaaaa and #13 (head bbbbbbb)",
            "Depends on #12 (head aaaaaaa) and #13: head bbbbbbb",
        ):
            with self.subTest(text=text):
                body = self.write_fixture("body.md", text)
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    rc, err = self.run_cli(
                        "--merge-base", MERGE_BASE, "--list-dependencies", str(body)
                    )
                self.assertEqual((rc, err, stdout.getvalue()), (0, "", "12\n13\n"))

    def test_dependency_cli_colon_heads_reject_stale_second_member(self) -> None:
        """A matching first head cannot hide stale member thirteen across a colon."""
        dependency = self.dependency_json()
        for text in (
            "Depends on #12: head aaaaaaa and #13 (head bbbbbbb)",
            "Depends on #12 (head aaaaaaa) and #13: head bbbbbbb",
        ):
            with self.subTest(text=text):
                body = self.write_fixture("body.md", text)
                rc, err = self.run_cli(
                    "--merge-base",
                    MERGE_BASE,
                    "--dependency-json",
                    f"12={dependency}",
                    "--dependency-json",
                    f"13={dependency}",
                    str(body),
                )
                self.assertEqual(rc, 1)
                self.assertEqual(
                    err, f"{body}: names head bbbbbbb of #13, the live head of #13 is {'a' * 40}\n"
                )

    def test_dependency_single_colon_head_is_preserved(self) -> None:
        """A single colon-separated dependency head retains its original pairing."""
        self.assertEqual(
            base_claims.find_dependency_claims("Depends on #12: head aaaaaaa"), [(12, "aaaaaaa")]
        )

    def test_dependency_single_dash_and_bracket_heads_are_preserved(self) -> None:
        """Single dash and bracket heads retain their existing reference pairing."""
        for annotation in ("- head bbbbbbb", "[head bbbbbbb]"):
            with self.subTest(annotation=annotation):
                self.assertEqual(
                    base_claims.find_dependency_claims(f"Depends on #13 {annotation}"),
                    [(13, "bbbbbbb")],
                )

    def test_dependency_list_member_openers_pair_both_positions(self) -> None:
        """Every supported opener attributes heads to its own member in either position."""
        for annotation in (
            ": head {}",
            "- head {}",
            "\u2013 head {}",
            "\u2014 head {}",
            "[head {}]",
        ):
            for text in (
                "Depends on #12 (head aaaaaaa) and #13 " + annotation.format("bbbbbbb"),
                "Depends on #12 " + annotation.format("aaaaaaa") + " and #13 (head bbbbbbb)",
            ):
                with self.subTest(text=text):
                    self.assertEqual(
                        base_claims._dependency_records(text),
                        [(12, None), (12, "aaaaaaa"), (13, None), (13, "bbbbbbb")],
                    )

    def test_dependency_list_dash_first_does_not_borrow_second_head(self) -> None:
        """A first-member dash cannot consume the following member's distinct head."""
        text = "Depends on #12 - head aaaaaaa and #13 (head ccccccc)"
        self.assertEqual(
            base_claims.find_dependency_claims(text), [(12, "aaaaaaa"), (13, "ccccccc")]
        )
        self.assertEqual(base_claims._dependency_numbers([("body", text)]), [12, 13])

    def test_dependency_cli_member_openers_list_both_members(self) -> None:
        """Listing preserves both members with dash or bracket annotations."""
        for annotation in ("- head {}", "\u2013 head {}", "\u2014 head {}", "[head {}]"):
            for text in (
                "Depends on #12 (head aaaaaaa) and #13 " + annotation.format("bbbbbbb"),
                "Depends on #12 " + annotation.format("aaaaaaa") + " and #13 (head ccccccc)",
            ):
                with self.subTest(text=text):
                    body = self.write_fixture("body.md", text)
                    stdout = io.StringIO()
                    with contextlib.redirect_stdout(stdout):
                        rc, err = self.run_cli(
                            "--merge-base", MERGE_BASE, "--list-dependencies", str(body)
                        )
                    self.assertEqual((rc, err, stdout.getvalue()), (0, "", "12\n13\n"))

    def test_dependency_cli_member_openers_reject_stale_second_head(self) -> None:
        """A current first member cannot hide a stale dash or bracket second member."""
        dependency = self.dependency_json()
        for annotation in ("- head bbbbbbb", "\u2013 head bbbbbbb", "[head bbbbbbb]"):
            with self.subTest(annotation=annotation):
                body = self.write_fixture(
                    "body.md", f"Depends on #12 (head aaaaaaa) and #13 {annotation}"
                )
                rc, err = self.run_cli(
                    "--merge-base",
                    MERGE_BASE,
                    "--dependency-json",
                    f"12={dependency}",
                    "--dependency-json",
                    f"13={dependency}",
                    str(body),
                )
                self.assertEqual(rc, 1)
                self.assertIn("names head bbbbbbb of #13", err)

    def test_dependency_list_dash_members_count_toward_limit(self) -> None:
        """A dash on the first member cannot hide eight members from the bound."""
        members = "Depends on #1 - head aaaaaaa, " + ", ".join(f"#{n}" for n in range(2, 9))
        self.assertEqual(base_claims._dependency_numbers([("body", members)]), list(range(1, 9)))
        text = members + ", #9"
        message = "more than 8 distinct dependency PR numbers"
        with self.assertRaisesRegex(ValueError, message):
            base_claims.find_dependency_claims(text)
        body = self.write_fixture("body.md", text)
        for flags in ((), ("--list-dependencies",)):
            with self.subTest(flags=flags):
                rc, err = self.run_cli("--merge-base", MERGE_BASE, *flags, str(body))
                self.assertEqual(rc, 2)
                self.assertIn(message, err)

    def test_dependency_list_unpaired_current_heads_fail_closed(self) -> None:
        """Unknown tilde and brace openers cannot silently discard current list heads."""
        for annotation in ("~ head bbbbbbb", "{head bbbbbbb}"):
            text = f"Depends on #12 (head aaaaaaa) and #13 {annotation}"
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, r"#12.*#13"):
                base_claims.find_dependency_claims(text)

    def test_dependency_cli_unpaired_current_heads_fail_closed(self) -> None:
        """Both CLI modes refuse unpaired current heads even with matching live heads."""
        dependency = self.dependency_json()
        for annotation in ("~ head aaaaaaa", "{head aaaaaaa}"):
            body = self.write_fixture(
                "body.md", f"Depends on #12 (head aaaaaaa) and #13 {annotation}"
            )
            for flags in ((), ("--list-dependencies",)):
                with self.subTest(annotation=annotation, flags=flags):
                    stdout = io.StringIO()
                    with contextlib.redirect_stdout(stdout):
                        rc, err = self.run_cli(
                            "--merge-base",
                            MERGE_BASE,
                            "--dependency-json",
                            f"12={dependency}",
                            "--dependency-json",
                            f"13={dependency}",
                            *flags,
                            str(body),
                        )
                    self.assertEqual(rc, 2)
                    self.assertIn("#12", err)
                    self.assertIn("#13", err)
                    self.assertEqual(stdout.getvalue(), "")

    def test_dependency_list_hidden_members_fail_closed(self) -> None:
        """A single-member fallback rejects separator-led references hidden by an opener."""
        text = "Depends on #12 ~ head aaaaaaa, #13"
        with self.assertRaisesRegex(ValueError, r"#12.*#13"):
            base_claims.find_dependency_claims(text)

    def test_dependency_cli_hidden_members_fail_closed(self) -> None:
        """Hidden fallback members return two in both CLI modes with matching live heads."""
        body = self.write_fixture("body.md", "Depends on #12 ~ head aaaaaaa, #13")
        dependency = self.dependency_json()
        for flags in ((), ("--list-dependencies",)):
            with self.subTest(flags=flags):
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    rc, err = self.run_cli(
                        "--merge-base",
                        MERGE_BASE,
                        "--dependency-json",
                        f"12={dependency}",
                        *flags,
                        str(body),
                    )
                self.assertEqual(rc, 2)
                self.assertIn("#12", err)
                self.assertIn("#13", err)
                self.assertEqual(stdout.getvalue(), "")

    def test_dependency_list_first_annotation_rejects_hidden_word_members(self) -> None:
        """Word-form members inside the first annotation cannot be silently discarded."""
        for annotation in (
            "(head aaaaaaa, and PR 13 head bbbbbbb)",
            "(head aaaaaaa, PR 13 head bbbbbbb)",
            "[head aaaaaaa, and pull request 13 head bbbbbbb]",
            "(head aaaaaaa, and PR 13)",
        ):
            with (
                self.subTest(annotation=annotation),
                self.assertRaisesRegex(ValueError, r"#12.*#13"),
            ):
                base_claims.find_dependency_claims(f"Depends on #12 {annotation}.")

    def test_dependency_cli_first_annotation_rejects_hidden_word_members(self) -> None:
        """Both modes reject hidden first-annotation members with empty stdout."""
        dependency = self.dependency_json()
        options = [
            arg for number in (12, 13) for arg in ("--dependency-json", f"{number}={dependency}")
        ]
        for annotation in (
            "(head aaaaaaa, and PR 13 head bbbbbbb)",
            "(head aaaaaaa, PR 13 head bbbbbbb)",
            "[head aaaaaaa, and pull request 13 head bbbbbbb]",
            "(head aaaaaaa, and PR 13)",
        ):
            body = self.write_fixture("body.md", f"Depends on #12 {annotation}.")
            for flags in ((), ("--list-dependencies",)):
                with self.subTest(annotation=annotation, flags=flags):
                    stdout = io.StringIO()
                    with contextlib.redirect_stdout(stdout):
                        rc, err = self.run_cli(
                            "--merge-base", MERGE_BASE, *options, *flags, str(body)
                        )
                    self.assertEqual(rc, 2)
                    self.assertRegex(err, r"#12.*#13")
                    self.assertEqual(stdout.getvalue(), "")

    def test_dependency_list_later_annotation_rejects_hidden_word_member(self) -> None:
        """A word-form member hidden in a later annotation names all window references."""
        text = "Depends on #12 (head aaaaaaa), #13 (head bbbbbbb, and PR 14 head ccccccc)."
        with self.assertRaisesRegex(ValueError, r"#12.*#13.*#14"):
            base_claims.find_dependency_claims(text)

    def test_dependency_cli_later_annotation_rejects_hidden_word_member(self) -> None:
        """Both modes reject hidden later-annotation members with empty stdout."""
        body = self.write_fixture(
            "body.md",
            "Depends on #12 (head aaaaaaa), #13 (head bbbbbbb, and PR 14 head ccccccc).",
        )
        dependency = self.dependency_json()
        options = [
            arg
            for number in (12, 13, 14)
            for arg in ("--dependency-json", f"{number}={dependency}")
        ]
        for flags in ((), ("--list-dependencies",)):
            with self.subTest(flags=flags):
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    rc, err = self.run_cli("--merge-base", MERGE_BASE, *options, *flags, str(body))
                self.assertEqual(rc, 2)
                self.assertRegex(err, r"#12.*#13.*#14")
                self.assertEqual(stdout.getvalue(), "")

    def test_dependency_list_truncated_middle_member_rejects_hidden_tail(self) -> None:
        """A truncated multi-member list rejects later members with their full attribution."""
        for annotation in (": aaaaaaa", " ~ previous head aaaaaaa"):
            text = f"Depends on #12 (head aaaaaaa), #13{annotation}, #14 (head bbbbbbb)"
            with (
                self.subTest(annotation=annotation),
                self.assertRaisesRegex(ValueError, r"#12.*#13.*#14"),
            ):
                base_claims.find_dependency_claims(text)

    def test_dependency_cli_truncated_middle_member_rejects_hidden_tail(self) -> None:
        """Both modes reject hidden tails even when all supplied live heads match."""
        dependency = self.dependency_json()
        options = [
            arg
            for number in (12, 13, 14)
            for arg in ("--dependency-json", f"{number}={dependency}")
        ]
        for annotation in (": aaaaaaa", " ~ previous head aaaaaaa"):
            for head in ("aaaaaaa", "bbbbbbb"):
                body = self.write_fixture(
                    "body.md", f"Depends on #12 (head aaaaaaa), #13{annotation}, #14 (head {head})"
                )
                for flags in ((), ("--list-dependencies",)):
                    with self.subTest(annotation=annotation, head=head, flags=flags):
                        stdout = io.StringIO()
                        with contextlib.redirect_stdout(stdout):
                            rc, err = self.run_cli(
                                "--merge-base", MERGE_BASE, *options, *flags, str(body)
                            )
                        self.assertEqual(rc, 2)
                        self.assertRegex(err, r"#12.*#13.*#14")
                        self.assertEqual(stdout.getvalue(), "")

    def test_dependency_list_truncated_nine_members_fail_closed(self) -> None:
        """A truncated second member cannot hide a list beyond the eight-member bound."""
        text = "Depends on #1 (head aaaaaaa), #2: aaaaaaa, " + ", ".join(
            f"#{number} (head bbbbbbb)" for number in range(3, 10)
        )
        with self.assertRaisesRegex(ValueError, r"#1.*#2.*#9"):
            base_claims.find_dependency_claims(text)

    def test_dependency_cli_truncated_nine_members_fail_closed(self) -> None:
        """Both modes reject hidden excess members instead of listing only the first two."""
        text = "Depends on #1 (head aaaaaaa), #2: aaaaaaa, " + ", ".join(
            f"#{number} (head bbbbbbb)" for number in range(3, 10)
        )
        body = self.write_fixture("body.md", text)
        dependency = self.dependency_json()
        options = [
            arg for number in (1, 2) for arg in ("--dependency-json", f"{number}={dependency}")
        ]
        for flags in ((), ("--list-dependencies",)):
            with self.subTest(flags=flags):
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    rc, err = self.run_cli("--merge-base", MERGE_BASE, *options, *flags, str(body))
                self.assertEqual(rc, 2)
                self.assertRegex(err, r"#1.*#2.*#9")
                self.assertEqual(stdout.getvalue(), "")

    def test_dependency_list_marked_middle_member_preserves_third_claim(self) -> None:
        """A supported middle annotation keeps all three members paired with their heads."""
        text = "Depends on #12 (head aaaaaaa), #13: head aaaaaaa, #14 (head bbbbbbb)"
        self.assertEqual(
            base_claims.find_dependency_claims(text),
            [(12, "aaaaaaa"), (13, "aaaaaaa"), (14, "bbbbbbb")],
        )

    def test_dependency_cli_marked_middle_member_checks_stale_third(self) -> None:
        """Check mode still names the stale third member after a supported middle annotation."""
        body = self.write_fixture(
            "body.md", "Depends on #12 (head aaaaaaa), #13: head aaaaaaa, #14 (head bbbbbbb)"
        )
        dependency = self.dependency_json()
        options = [
            arg
            for number in (12, 13, 14)
            for arg in ("--dependency-json", f"{number}={dependency}")
        ]
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            rc, err = self.run_cli("--merge-base", MERGE_BASE, *options, str(body))
        self.assertEqual(rc, 1)
        self.assertIn("head bbbbbbb of #14", err)
        self.assertEqual(stdout.getvalue(), "")

    def test_dependency_list_nonmember_prose_keeps_two_claims(self) -> None:
        """Separator-led prose without a leading member remains a nonmember boundary."""
        for tail in ("unrelated #14 head ccccccc", "see #14 ~ head ccccccc", "unrelated text"):
            text = f"Depends on #12 (head aaaaaaa) and #13 (head bbbbbbb), {tail}"
            with self.subTest(tail=tail):
                self.assertEqual(
                    base_claims.find_dependency_claims(text), [(12, "aaaaaaa"), (13, "bbbbbbb")]
                )

    def test_dependency_list_history_precedes_trailing_head_ambiguity(self) -> None:
        """Historical reference and head clauses are ignored before ambiguity checks."""
        for text in (
            "Previously depends on #12 and #13 (head bbbbbbb)",
            "Depends on #12 and #13 (previous head bbbbbbb)",
        ):
            with self.subTest(text=text):
                self.assertEqual(base_claims.find_dependency_claims(text), [])

    def test_dependency_cli_historical_lists_need_no_dependency_json(self) -> None:
        """Historical lists pass check mode without any live dependency JSON mappings."""
        for text in (
            "Previously depends on #12 and #13 (head bbbbbbb)",
            "Depends on #12 and #13 (previous head bbbbbbb)",
        ):
            with self.subTest(text=text):
                body = self.write_fixture("body.md", text)
                rc, err = self.run_cli("--merge-base", MERGE_BASE, str(body))
                self.assertEqual((rc, err), (0, ""))

    def test_dependency_list_limit_boundary_and_duplicate_members(self) -> None:
        """Exactly the distinct limit and repeated list members remain accepted."""
        references = [f"#{number}" for number in range(1, base_claims.MAX_DEPENDENCIES + 1)]
        text = "Depends on " + ", ".join([*references, "#1"])
        body = self.write_fixture("body.md", text)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            rc, err = self.run_cli("--merge-base", MERGE_BASE, "--list-dependencies", str(body))
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(stdout.getvalue(), "".join(f"{n}\n" for n in range(1, 9)))

    def test_dependency_list_head_markers_and_parenthesized_hashes(self) -> None:
        """List annotations accept every head marker and parenthesized uppercase hashes."""
        for marker in ("head", "ending at", "ends at", "tip"):
            for span in (f"{marker}: (AAAAAAA)", f"({marker}: (AAAAAAA))"):
                with self.subTest(span=span):
                    self.assertEqual(
                        base_claims.find_dependency_claims(
                            f"Depends on #12 {span}, #13 (head bbbbbbb)"
                        ),
                        [(12, "aaaaaaa"), (13, "bbbbbbb")],
                    )

    def test_dependency_exclamation_sentence_break(self) -> None:
        """An exclamation sentence break prevents associating a later unrelated head."""
        self.assertEqual(
            base_claims.find_dependency_claims("Depends on #12! This PR has head bbbbbbb."), []
        )

    def test_dependency_question_sentence_break(self) -> None:
        """A question sentence break prevents associating a later unrelated head."""
        self.assertEqual(
            base_claims.find_dependency_claims("Depends on #12? This PR has head bbbbbbb."), []
        )

    def test_dependency_head_before_exclamation_break(self) -> None:
        """A head inside the dependency sentence survives a later exclamation break."""
        self.assertEqual(
            base_claims.find_dependency_claims("Depends on #12 (head abcdef1)! Next sentence."),
            [(12, "abcdef1")],
        )

    def test_dependency_current_head_after_previous_clause(self) -> None:
        """A previous head clause does not suppress a current head after a comma."""
        self.assertEqual(
            base_claims.find_dependency_claims(
                "Depends on #12 (previous head aaaaaaa, now head bbbbbbb)"
            ),
            [(12, "bbbbbbb")],
        )

    def test_dependency_current_head_after_old_parenthesis(self) -> None:
        """An old parenthesized head does not suppress a later current head."""
        self.assertEqual(
            base_claims.find_dependency_claims("Depends on #12 (old head aaaaaaa) at head bbbbbbb"),
            [(12, "bbbbbbb")],
        )

    def test_dependency_historical_reference(self) -> None:
        """A historical reference suppresses a head in the same clause."""
        self.assertEqual(
            base_claims.find_dependency_claims("previously stacked on #12 at head abcdef1"), []
        )

    def test_dependency_history_after_reference_before_parenthesis(self) -> None:
        """History after the reference suppresses a head in a later parenthesized clause."""
        self.assertEqual(
            base_claims.find_dependency_claims(
                "Depends on #12 from an earlier series (head abcdef1)"
            ),
            [],
        )

    def test_dependency_current_head_after_previously_clause(self) -> None:
        """Previously in an intermediate head clause does not suppress the current head."""
        self.assertEqual(
            base_claims.find_dependency_claims(
                "Depends on #12, previously at head aaaaaaa, now head bbbbbbb"
            ),
            [(12, "bbbbbbb")],
        )

    def test_dependency_cli_current_head_after_history(self) -> None:
        """The CLI checks the current head after history against the live dependency head."""
        body = self.write_fixture(
            "body.md", "Depends on #12 (previous head aaaaaaa, now head bbbbbbb)."
        )
        for live_head, expected_rc in (("a" * 40, 1), ("b" * 40, 0)):
            with self.subTest(live_head=live_head):
                dependency = self.dependency_json({"head": {"sha": live_head}})
                rc, err = self.run_cli(
                    "--merge-base", MERGE_BASE, "--dependency-json", f"12={dependency}", str(body)
                )
                self.assertEqual(rc, expected_rc)
                if expected_rc:
                    self.assertIn("names head bbbbbbb of #12", err)
                else:
                    self.assertEqual(err, "")

    def test_dependency_reference_and_head_forms(self) -> None:
        """All dependency verbs, reference forms and head markers are recognized."""
        for verb in ("depends on", "stacked on", "on top of", "builds on", "based on"):
            for reference in ("#12", "PR 12", "PR #12", "pull request 12", "pull request #12"):
                for marker in ("head", "ending at", "ends at", "tip"):
                    text = f"{verb.upper()} {reference} {marker}: (ABCDEF1)"
                    with self.subTest(text=text):
                        self.assertEqual(
                            base_claims.find_dependency_claims(text), [(12, "abcdef1")]
                        )

    def test_dependency_exclusions(self) -> None:
        """Sentence boundaries, missing markers, fences, quotes and history exclude claims."""
        cases = (
            "Depends on #12. Head abcdef1",
            "Depends on #12; head abcdef1",
            "Depends on #12\nhead abcdef1",
            "Depends on #12 at abcdef1",
            "```text\nDepends on #12 head abcdef1\n```",
            "~~~\nDepends on #12 head abcdef1\n~~~",
            "> Depends on #12 head abcdef1",
            "previously stacked on #12 at head abcdef1",
            "Previous dependency: stacked on #12 (head abcdef1)",
            "depends on #12 at old head abcdef1",
            "depends on #12345678 head abcdef1",
            "depends on #0 head abcdef1",
            "depends on #12 head abcdef",
            "depends on #12 head " + "a" * 41,
        )
        for text in cases:
            with self.subTest(text=text):
                self.assertEqual(base_claims.find_dependency_claims(text), [])

    def test_dependency_and_base_extraction_share_filtering(self) -> None:
        """Both claim kinds share fence tracking without changing base extraction."""
        text = (
            "````text\n```\nDepends on #1 head aaaaaaa; base aaaaaaa\n````\n"
            "> Depends on #2 head bbbbbbb; base bbbbbbb\n"
            "Depends on #12 head abcdef1; base ccccccc\n"
            "Previous base aaaaaaa, now base ddddddd\n"
        )
        self.assertEqual(base_claims.find_dependency_claims(text), [(12, "abcdef1")])
        self.assertEqual(base_claims.find_base_claims(text), ["ccccccc", "ddddddd"])

    def dependency_json(self, payload: object | None = None) -> Path:
        """Write a dependency pull fixture with a full head unless overridden."""
        return self.write_fixture(
            "dependency.json",
            json.dumps({"head": {"sha": "a" * 40}} if payload is None else payload),
        )

    def test_dependency_cli_stale_head(self) -> None:
        """A stale dependency prefix reports both claimed and live heads with exit one."""
        body = self.write_fixture("body.md", "Depends on #12 head bbbbbbb")
        dependency = self.dependency_json()
        rc, err = self.run_cli(
            "--merge-base", MERGE_BASE, "--dependency-json", f"12={dependency}", str(body)
        )
        self.assertEqual(rc, 1)
        self.assertEqual(
            err, f"{body}: names head bbbbbbb of #12, the live head of #12 is {'a' * 40}\n"
        )

    def test_dependency_cli_current_prefix(self) -> None:
        """A dependency prefix matches a full uppercase live head with exit zero."""
        body = self.write_fixture("body.md", "stacked on pull request #12 (head AAAAAAA)")
        dependency = self.dependency_json({"head": {"sha": "A" * 40}})
        rc, err = self.run_cli(
            "--merge-base", MERGE_BASE, "--dependency-json", f"12={dependency}", str(body)
        )
        self.assertEqual((rc, err), (0, ""))

    def test_dependency_cli_missing_pull(self) -> None:
        """A dependency claim without supplied pull JSON fails closed and names its PR."""
        body = self.write_fixture("body.md", "Depends on #12 head aaaaaaa")
        rc, err = self.run_cli("--merge-base", MERGE_BASE, str(body))
        self.assertEqual(rc, 2)
        self.assertIn("#12", err)

    def test_dependency_cli_missing_json_file_names_path(self) -> None:
        """A supplied dependency JSON path that does not exist returns two with its path."""
        body = self.write_fixture("body.md", "Depends on #12 head aaaaaaa")
        missing = self.tmp / "missing-dependency.json"
        rc, err = self.run_cli(
            "--merge-base", MERGE_BASE, "--dependency-json", f"12={missing}", str(body)
        )
        self.assertEqual(rc, 2)
        self.assertIn(str(missing), err)

    def test_dependency_cli_json_directory_names_path(self) -> None:
        """A directory supplied as dependency JSON returns two and identifies the path."""
        body = self.write_fixture("body.md", "Depends on #12 head aaaaaaa")
        rc, err = self.run_cli(
            "--merge-base", MERGE_BASE, "--dependency-json", f"12={self.tmp}", str(body)
        )
        self.assertEqual(rc, 2)
        self.assertIn(str(self.tmp), err)

    def test_dependency_cli_malformed_option(self) -> None:
        """Malformed dependency mappings and stdin paths return input errors."""
        for value in ("12", "x=file", "0=file", "-1=file", "12345678=file", "12=", "12=-"):
            with self.subTest(value=value):
                rc, err = self.run_cli("--merge-base", MERGE_BASE, "--dependency-json", value)
                self.assertEqual(rc, 2)
                self.assertIn("N=PATH", err)

    def test_dependency_cli_repeated_number(self) -> None:
        """Repeated dependency PR options are rejected even for the same file."""
        dependency = self.dependency_json()
        rc, err = self.run_cli(
            "--merge-base",
            MERGE_BASE,
            "--dependency-json",
            f"12={dependency}",
            "--dependency-json",
            f"012={dependency}",
        )
        self.assertEqual(rc, 2)
        self.assertIn("repeated", err)
        self.assertIn("#12", err)

    def test_dependency_cli_bad_head(self) -> None:
        """Missing, nonstring, short and nonhex dependency heads fail closed."""
        payloads = [{}, {"head": []}, {"head": {}}] + [
            {"head": {"sha": value}} for value in (None, 123, "a" * 7, "g" * 40, "a" * 41)
        ]
        for payload in payloads:
            with self.subTest(payload=payload):
                dependency = self.dependency_json(payload)
                rc, err = self.run_cli(
                    "--merge-base", MERGE_BASE, "--dependency-json", f"12={dependency}"
                )
                self.assertEqual(rc, 2)
                self.assertIn("#12", err)

    def test_dependency_cli_non_object_json(self) -> None:
        """A dependency pull JSON array is an input error even without claims."""
        dependency = self.dependency_json([])
        rc, err = self.run_cli("--merge-base", MERGE_BASE, "--dependency-json", f"12={dependency}")
        self.assertEqual(rc, 2)
        self.assertIn("object", err)

    def test_dependency_cli_list_sorted_unique(self) -> None:
        """Dependency listing merges live and file bodies in sorted unique order."""
        body = self.write_fixture(
            "body.md", "Depends on #12 head aaaaaaa\nBuilds on PR 2 tip bbbbbbb"
        )
        pull = self.write_fixture(
            "pull.json",
            json.dumps(
                {
                    "head": {"sha": "d" * 40},
                    "body": "Based on #3 ends at ccccccc\nDepends on #12 head aaaaaaa",
                }
            ),
        )
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            rc, err = self.run_cli(
                "--pull-json",
                str(pull),
                "--head",
                "d" * 40,
                "--live-body",
                "--list-dependencies",
                str(body),
            )
        self.assertEqual((rc, err, stdout.getvalue()), (0, "", "2\n3\n12\n"))

    def test_dependency_cli_list_head_mismatch(self) -> None:
        """Dependency listing still refuses a mismatched tested pull head."""
        pull = self.write_fixture("pull.json", json.dumps({"head": {"sha": "d" * 40}}))
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            rc, err = self.run_cli(
                "--pull-json", str(pull), "--head", "a" * 40, "--list-dependencies"
            )
        self.assertEqual(rc, 3)
        self.assertIn("pull head", err)
        self.assertEqual(stdout.getvalue(), "")

    def test_dependency_cli_list_input_error(self) -> None:
        """Dependency listing validates live body input before emitting numbers."""
        pull = self.write_fixture("pull.json", json.dumps({"body": []}))
        rc, err = self.run_cli("--pull-json", str(pull), "--live-body", "--list-dependencies")
        self.assertEqual(rc, 2)
        self.assertIn("string body", err)

    def test_dependency_cli_combined_stale_claims(self) -> None:
        """One validation reports stale base and dependency claims together."""
        body = self.write_fixture("body.md", "Base bbbbbbb; depends on #12 head ddddddd")
        dependency = self.dependency_json()
        rc, err = self.run_cli(
            "--merge-base",
            MERGE_BASE,
            "--label",
            "body",
            "--dependency-json",
            f"12={dependency}",
            str(body),
        )
        self.assertEqual(rc, 1)
        self.assertIn(f"body: names base bbbbbbb, the live merge base is {MERGE_BASE}\n", err)
        self.assertIn(f"body: names head ddddddd of #12, the live head of #12 is {'a' * 40}\n", err)

    def test_dependency_cli_distinct_limit(self) -> None:
        """Nine distinct dependency claims exceed the bound during checking and listing."""
        body = self.write_fixture(
            "body.md", "\n".join(f"Depends on #{number} head aaaaaaa" for number in range(1, 10))
        )
        for flags in ((), ("--list-dependencies",)):
            with self.subTest(flags=flags):
                rc, err = self.run_cli("--merge-base", MERGE_BASE, *flags, str(body))
                self.assertEqual(rc, 2)
                self.assertIn("8", err)

    def test_dependency_cli_option_limit(self) -> None:
        """Nine dependency JSON options exceed the shared bound."""
        dependency = self.dependency_json()
        options = [
            arg
            for number in range(1, 10)
            for arg in ("--dependency-json", f"{number}={dependency}")
        ]
        rc, err = self.run_cli("--merge-base", MERGE_BASE, *options)
        self.assertEqual(rc, 2)
        self.assertIn("8", err)

    def test_dependency_cli_limit_boundary(self) -> None:
        """Exactly eight dependency options and claims are accepted."""
        dependency = self.dependency_json()
        body = self.write_fixture(
            "body.md", "\n".join(f"Depends on #{number} head aaaaaaa" for number in range(1, 9))
        )
        options = [
            arg for number in range(1, 9) for arg in ("--dependency-json", f"{number}={dependency}")
        ]
        rc, err = self.run_cli("--merge-base", MERGE_BASE, *options, str(body))
        self.assertEqual((rc, err), (0, ""))


if __name__ == "__main__":
    unittest.main()
