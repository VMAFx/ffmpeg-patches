# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Check upstream-lint Make arguments through a disposable stub recipe."""

from __future__ import annotations

import os
import shutil
import subprocess  # nosec B404 - bounded make calls in a disposable fixture only.
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from scripts import upstream_lint
from tests.make_env import make_environment


class MakefileUpstreamLintTest(unittest.TestCase):
    """Exercise only upstream-lint in a copied Makefile with a harmless argument sink."""

    def make_arguments(self, records: str | None, reason: str, command_line: bool) -> list[str]:
        """Capture literal recipe arguments with parent Make flags and fixture inputs isolated."""
        make = shutil.which("make")
        if make is None:
            self.skipTest("make is not installed")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copyfile(Path(__file__).resolve().parents[1] / "Makefile", root / "Makefile")
            stub = root / ".config/lefthook/python.sh"
            stub.parent.mkdir(parents=True)
            stub.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > arguments.txt\n', encoding="ascii")
            environment = {
                name: value
                for name, value in make_environment(
                    "UPSTREAM_TREE",
                    "UPSTREAM_BASE",
                    "UPSTREAM_REVERT_PROOF",
                    "UPSTREAM_NO_REVERT_PROOF_REASON",
                ).items()
                if not name.startswith("GIT_")
            }
            environment.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
            values = {"UPSTREAM_TREE": "/fixture/tree", "UPSTREAM_BASE": "fixture-base"}
            if records is not None:
                values["UPSTREAM_REVERT_PROOF"] = records
            values["UPSTREAM_NO_REVERT_PROOF_REASON"] = reason
            arguments = [make, "-s", "upstream-lint"]
            if command_line:
                arguments.extend(f"{name}={value}" for name, value in values.items())
            else:
                environment.update(values)
            done = subprocess.run(  # nosec B603 - fixed make, stub-only target, bounded fixture values.
                arguments,
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            return (root / "arguments.txt").read_text(encoding="utf-8").splitlines()

    def test_record_and_waiver_arguments_are_literal(self) -> None:
        """Record lists split once and nonblank waivers preserve quotes, dollars and backticks."""
        reason = "double \" single ' dollar $VALUE backtick `literal`"
        base = ["scripts/upstream_lint.py", "--tree=/fixture/tree", "--base=fixture-base"]
        proofs = ["--revert-proof=one.json", "--revert-proof=two.json"]
        for command_line in (False, True):
            for records, waiver, expected in (
                (
                    "one.json two.json",
                    reason,
                    base + proofs + ["--no-revert-proof-reason=" + reason],
                ),
                ("one.json two.json", " \t ", base + proofs),
                (None, reason, base + ["--no-revert-proof-reason=" + reason]),
                (None, "", base),
            ):
                with self.subTest(records=records, waiver=waiver, command_line=command_line):
                    self.assertEqual(self.make_arguments(records, waiver, command_line), expected)
        padded = "  " + reason + "  "
        self.assertEqual(
            self.make_arguments(None, padded, False), base + ["--no-revert-proof-reason=" + padded]
        )

    def test_parent_makeflags_cannot_override_fixture_values(self) -> None:
        """Parent command-line assignments cannot override either fixture input form."""
        flags = " -- UPSTREAM_TREE=/parent/tree UPSTREAM_BASE=parent-base"
        with unittest.mock.patch.dict(os.environ, {"MAKEFLAGS": flags}):
            for command_line in (False, True):
                with self.subTest(command_line=command_line):
                    arguments = self.make_arguments(None, "", command_line)
                    self.assertIn("/fixture/tree", " ".join(arguments))
                    self.assertIn("fixture-base", " ".join(arguments))
                    self.assertNotIn("/parent/tree", " ".join(arguments))
                    self.assertNotIn("parent-base", " ".join(arguments))

    def test_leading_dashes_are_literal_parser_values(self) -> None:
        """Dash-prefixed records and waivers remain values in the real argument parser."""
        for command_line in (False, True):
            with self.subTest(command_line=command_line):
                arguments = self.make_arguments("-proof.json", "--manual", command_line)
                self.assertIn("--revert-proof=-proof.json", arguments)
                self.assertIn("--no-revert-proof-reason=--manual", arguments)
                self.assertIn("--tree=/fixture/tree", arguments)
                self.assertIn("--base=fixture-base", arguments)
                parsed = upstream_lint._parser().parse_args(arguments[1:])
                self.assertEqual(parsed.revert_proof, [Path("-proof.json")])
                self.assertEqual(parsed.no_revert_proof_reason, "--manual")
