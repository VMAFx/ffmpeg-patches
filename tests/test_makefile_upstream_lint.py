# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Check upstream-lint Make arguments through a disposable stub recipe."""

from __future__ import annotations

import os
import shutil
import subprocess  # nosec B404 - bounded make calls in a disposable fixture only.
import tempfile
import unittest
from pathlib import Path


class MakefileUpstreamLintTest(unittest.TestCase):
    """Exercise only upstream-lint in a copied Makefile with a harmless argument sink."""

    def make_arguments(self, records: str | None, reason: str, command_line: bool) -> list[str]:
        """Capture the exact recipe arguments with literal environment or command-line values."""
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
                name: value for name, value in os.environ.items() if not name.startswith("GIT_")
            }
            environment.pop("UPSTREAM_REVERT_PROOF", None)
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
        base = ["scripts/upstream_lint.py", "--tree", "/fixture/tree", "--base", "fixture-base"]
        proofs = ["--revert-proof", "one.json", "--revert-proof", "two.json"]
        for command_line in (False, True):
            for records, waiver, expected in (
                ("one.json two.json", reason, base + proofs + ["--no-revert-proof-reason", reason]),
                ("one.json two.json", " \t ", base + proofs),
                (None, reason, base + ["--no-revert-proof-reason", reason]),
                (None, "", base),
            ):
                with self.subTest(records=records, waiver=waiver, command_line=command_line):
                    self.assertEqual(self.make_arguments(records, waiver, command_line), expected)
        padded = "  " + reason + "  "
        self.assertEqual(
            self.make_arguments(None, padded, False), base + ["--no-revert-proof-reason", padded]
        )
