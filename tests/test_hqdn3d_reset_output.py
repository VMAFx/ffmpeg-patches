# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Check the in-graph reset output check on every host, without a device."""

from __future__ import annotations

import subprocess  # nosec B404 - CompletedProcess fixtures only, no commands run.
import unittest

from tests import helpers

VULKAN_LINES = (
    "14 160 96 first same",
    "12 160 96 yuv420p10 refused EINVAL",
    "13 160 96 yuv444p refused EINVAL",
    "f1 160 96 yuv444p refused EINVAL",
    "p012 0 min 0 max 65520 low 0",
    "p012 1 min 65520 max 65520 low 0",
    "p012 2 min 65520 max 65520 low 0",
    "neg 0 2 2 same luma 0 0 0 0",
    "neg 1 2 2 same luma 4095 4095 65535 0",
)


class ResetOutputTest(unittest.TestCase):
    """Require the reset frames and reject mismatches regardless of exit status."""

    def test_reset_frames_pass(self) -> None:
        """Accept the twelve expected frames with exit status zero."""
        stdout = "\n".join(helpers.RESET_LINES) + "\n"
        self.assertEqual(helpers.reset_output_problems(0, stdout), [])

    def test_vulkan_extra_lines_pass(self) -> None:
        """Accept Vulkan checks following the twelve expected frames."""
        stdout = "\n".join(helpers.RESET_LINES + VULKAN_LINES) + "\n"
        self.assertEqual(helpers.reset_output_problems(0, stdout), [])

    def test_nonzero_exit_fails(self) -> None:
        """Reject a nonzero exit status even with matching frames."""
        stdout = "\n".join(helpers.RESET_LINES) + "\n"
        problems = helpers.reset_output_problems(1, stdout)
        self.assertEqual(len(problems), 1)
        self.assertIn("exit status 1", problems[0])

    def test_reset_diff_fails(self) -> None:
        """Reject a DIFF in the required reset frames."""
        lines = list(helpers.RESET_LINES)
        lines[5] = "5 96 56 DIFF"
        problems = helpers.reset_output_problems(0, "\n".join(lines) + "\n")
        self.assertEqual(len(problems), 2)
        self.assertIn("line 5", problems[0])
        self.assertIn(helpers.RESET_LINES[5], problems[0])
        self.assertIn(lines[5], problems[0])
        self.assertIn(lines[5], problems[1])

    def test_vulkan_extra_diff_fails(self) -> None:
        """Reject a DIFF in the extra Vulkan checks."""
        lines = helpers.RESET_LINES + VULKAN_LINES[:-1] + ("neg 1 2 2 DIFF luma 4095 4095 65535 0",)
        problems = helpers.reset_output_problems(0, "\n".join(lines) + "\n")
        self.assertEqual(len(problems), 1)
        self.assertIn("line 20", problems[0])
        self.assertIn(lines[-1], problems[0])

    def test_wrong_size_fails(self) -> None:
        """Reject a frame with the previous group's size."""
        lines = list(helpers.RESET_LINES)
        lines[8] = "8 96 56 same"
        problems = helpers.reset_output_problems(0, "\n".join(lines) + "\n")
        self.assertEqual(len(problems), 1)
        self.assertIn("line 8", problems[0])
        self.assertIn(helpers.RESET_LINES[8], problems[0])
        self.assertIn(lines[8], problems[0])

    def test_swapped_frames_fail(self) -> None:
        """Reject frames printed in the wrong order."""
        lines = list(helpers.RESET_LINES)
        lines[4], lines[5] = lines[5], lines[4]
        problems = helpers.reset_output_problems(0, "\n".join(lines) + "\n")
        self.assertEqual(len(problems), 1)
        self.assertIn("line 4", problems[0])
        self.assertIn(helpers.RESET_LINES[4], problems[0])
        self.assertIn(lines[4], problems[0])

    def test_missing_last_frame_fails(self) -> None:
        """Reject output missing the twelfth reset frame."""
        stdout = "\n".join(helpers.RESET_LINES[:11]) + "\n"
        problems = helpers.reset_output_problems(0, stdout)
        self.assertEqual(len(problems), 1)
        self.assertIn("line 11", problems[0])
        self.assertIn(helpers.RESET_LINES[11], problems[0])

    def test_empty_output_fails(self) -> None:
        """Reject empty output even with exit status zero."""
        problems = helpers.reset_output_problems(0, "")
        self.assertEqual(len(problems), 1)
        self.assertIn("line 0", problems[0])
        self.assertIn(helpers.RESET_LINES[0], problems[0])

    def test_different_word_passes(self) -> None:
        """Match DIFF as a whitespace-separated word, not a prefix."""
        stdout = "\n".join(helpers.RESET_LINES + ("12 160 96 DIFFERENT",)) + "\n"
        self.assertEqual(helpers.reset_output_problems(0, stdout), [])

    def test_assert_reset_output(self) -> None:
        """Raise on a DIFF and accept the twelve matching reset frames."""
        stdout = "\n".join(helpers.RESET_LINES) + "\n"
        done = subprocess.CompletedProcess(
            args=["x"], returncode=0, stdout=stdout.encode(), stderr=b""
        )
        helpers.assert_reset_output(self, done)
        done = subprocess.CompletedProcess(
            args=["x"],
            returncode=0,
            stdout=stdout.replace("5 96 56 same", "5 96 56 DIFF").encode(),
            stderr=b"",
        )
        with self.assertRaises(AssertionError):
            helpers.assert_reset_output(self, done)


if __name__ == "__main__":
    unittest.main()
