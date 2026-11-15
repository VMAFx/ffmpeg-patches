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


if __name__ == "__main__":
    unittest.main()
