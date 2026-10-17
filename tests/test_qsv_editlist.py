# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Regression test for patch 0010 (h264_qsv and hevc_qsv on MP4 edit lists).

The fixture is an H.264 MP4 whose edit list starts 1.5 s into a 120-frame
GOP, so its first 90 packets carry AV_PKT_FLAG_DISCARD. FFmpeg n9.0.2 wrote
it:

    ffmpeg -f lavfi -i testsrc2=s=128x96:r=60:d=3 -c:v libx264 \
        -preset veryfast -crf 45 -bf 3 -g 120 -keyint_min 120 \
        -sc_threshold 0 -pix_fmt yuv420p -threads 1 -bitexact \
        -fflags +bitexact -map_metadata -1 src.mp4
    ffmpeg -ss 1.5 -i src.mp4 -c copy -an -bitexact -fflags +bitexact \
        -map_metadata -1 editlist.mp4

The software decoder returns 90 frames. Without the patch, h264_qsv in system
memory returns 88 frames whose first two are pre-roll pictures, and the
hardware-frame path with async_depth 1 returns 91.

The hardware cases need an FFmpeg built with the series and an Intel GPU:

    QSV_FFMPEG=/path/to/ffmpeg QSV_DEVICE=/dev/dri/renderD128 \
        python3 -m unittest tests.test_qsv_editlist

Without both variables they are skipped. LIBVA_DRIVER_NAME is passed through.
"""

from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "qsv" / "h264-editlist.mp4"
FFMPEG = os.environ.get("QSV_FFMPEG", "")
DEVICE = os.environ.get("QSV_DEVICE", "")


def framecrc_lines(text: str) -> list[str]:
    """Return the frame lines of framecrc output, without the header."""
    return [line for line in text.splitlines() if line and not line.startswith("#")]


def first_difference(want: list[str], got: list[str]) -> str:
    """Describe where two framecrc outputs part, or return "" when equal."""
    if want == got:
        return ""
    for index, (left, right) in enumerate(zip(want, got, strict=False)):
        if left != right:
            return f"frame {index}: want {left!r}, got {right!r}"
    return f"want {len(want)} frames, got {len(got)}"


def framecrc(*args: str) -> list[str]:
    """Decode the fixture with FFMPEG and return its framecrc frame lines."""
    cmd = [FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error", *args]
    done = subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=False)
    if done.returncode != 0:
        raise AssertionError(f"{cmd} exited {done.returncode}: {done.stderr}")
    return framecrc_lines(done.stdout)


class CompareTest(unittest.TestCase):
    def test_equal_outputs_have_no_difference(self) -> None:
        lines = ["0, 0, 0, 1, 18432, 0x1", "0, 1, 1, 1, 18432, 0x2"]
        self.assertEqual(first_difference(lines, list(lines)), "")

    def test_shifted_content_is_reported(self) -> None:
        want = ["0, 0, 0, 1, 18432, 0x1", "0, 1, 1, 1, 18432, 0x2"]
        got = ["0, 0, 0, 1, 18432, 0x9", "0, 1, 1, 1, 18432, 0x1"]
        self.assertEqual(
            first_difference(want, got),
            "frame 0: want '0, 0, 0, 1, 18432, 0x1', got '0, 0, 0, 1, 18432, 0x9'",
        )

    def test_missing_frames_are_reported(self) -> None:
        want = ["0, 0, 0, 1, 18432, 0x1", "0, 1, 1, 1, 18432, 0x2"]
        self.assertEqual(first_difference(want, want[:1]), "want 2 frames, got 1")

    def test_header_lines_are_ignored(self) -> None:
        text = "#tb 0: 1/60\n#media_type 0: video\n0, 0, 0, 1, 18432, 0x1\n"
        self.assertEqual(framecrc_lines(text), ["0, 0, 0, 1, 18432, 0x1"])


@unittest.skipUnless(FFMPEG and DEVICE, "set QSV_FFMPEG and QSV_DEVICE")
class QsvEditListTest(unittest.TestCase):
    """h264_qsv output must equal the software decoder frame for frame."""

    reference: list[str] = []

    @classmethod
    def setUpClass(cls) -> None:
        cls.reference = framecrc(
            "-c:v", "h264", "-i", str(FIXTURE), "-pix_fmt", "nv12", "-f", "framecrc", "-"
        )  # fmt: skip

    def assert_matches_software(self, got: list[str]) -> None:
        self.assertEqual(len(self.reference), 90)
        self.assertEqual(first_difference(self.reference, got), "")

    def test_system_memory(self) -> None:
        self.assert_matches_software(
            framecrc(
                "-init_hw_device", f"qsv=hw:{DEVICE}", "-c:v", "h264_qsv",
                "-i", str(FIXTURE), "-pix_fmt", "nv12", "-f", "framecrc", "-",
            )
        )  # fmt: skip

    def test_hardware_frames(self) -> None:
        for depth in ("1", "4"):
            with self.subTest(async_depth=depth):
                self.assert_matches_software(
                    framecrc(
                        "-init_hw_device", f"qsv=hw:{DEVICE}", "-hwaccel", "qsv",
                        "-hwaccel_output_format", "qsv", "-async_depth", depth,
                        "-c:v", "h264_qsv", "-i", str(FIXTURE),
                        "-vf", "hwdownload,format=nv12", "-f", "framecrc", "-",
                    )
                )  # fmt: skip


if __name__ == "__main__":
    unittest.main()
