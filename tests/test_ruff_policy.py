# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Tests for repository Ruff policy enforcement."""

from __future__ import annotations

import subprocess  # nosec B404 - fixture runner invokes the pinned local Ruff module.
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def run_ruff_fixture(source: str) -> subprocess.CompletedProcess[str]:
    """Return the Ruff result for one fixture source passed on stdin."""
    return subprocess.run(  # nosec B603 - local module; source goes on stdin, no shell.
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--no-cache",
            "--config",
            str(ROOT / "ruff.toml"),
            "--output-format",
            "concise",
            "--statistics",
            "--stdin-filename",
            "fixture.py",
            "-",
        ],
        input=source,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


class RuffPolicyTest(unittest.TestCase):
    """Verify that Ruff rejects empty exception handlers."""

    def test_typed_except_pass_fails(self) -> None:
        """A typed exception handler containing only pass must fail with S110."""
        result = run_ruff_fixture(
            'def candidate() -> bool:\n    """Exercise a typed exception handler."""\n'
            "    try:\n        raise OSError\n    except OSError:\n        pass\n"
        )
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("S110", result.stdout)

    def test_typed_except_pass_with_comment_fails(self) -> None:
        """A typed pass with a trailing comment must still fail with S110."""
        result = run_ruff_fixture(
            'def candidate() -> bool:\n    """Exercise a commented empty handler."""\n'
            "    try:\n        raise OSError\n"
            "    except OSError:\n        pass  # Deliberately empty.\n"
        )
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("S110", result.stdout)

    def test_typed_except_return_passes(self) -> None:
        """A typed exception handler returning a value must pass without S110."""
        result = run_ruff_fixture(
            'def candidate() -> bool:\n    """Handle a typed exception explicitly."""\n'
            "    try:\n        raise OSError\n    except OSError:\n        return False\n"
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("S110", result.stdout)

    def test_exception_pass_fails(self) -> None:
        """An Exception handler containing only pass must retain default S110 behavior."""
        result = run_ruff_fixture(
            'def candidate() -> bool:\n    """Exercise the default broad exception check."""\n'
            "    try:\n        raise OSError\n    except Exception:\n        pass\n"
        )
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("S110", result.stdout)
