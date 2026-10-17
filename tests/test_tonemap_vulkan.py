# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Regression tests for patch 0019 (tonemap_vulkan).

0019: the new tonemap_vulkan filter runs the CPU tonemap=bt2408_hable math in
a compute shader. On flat frames the Vulkan path gives the same 8-bit code
values as the CPU path; the test allows one code value of difference.

The shader decodes PQ and BT.2020 limited range only, so the filter refuses
other input; the test checks that refusal on an HLG frame.

The planted wrong case is a run at four times the peak, which the
comparison must refuse.

The device cases need an FFmpeg built with the series and zimg, and a Vulkan
device:

    VK_FFMPEG=/path/to/ffmpeg VK_DEVICE=0 python3 -m unittest tests.test_tonemap_vulkan

Without both variables they are skipped.
"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.test_tonemap_opencl import (  # noqa: E402
    COLOURS,
    CPU,
    PQ_INPUT,
    SIZE,
    centre_yuv,
    flat_frame,
    worst_difference,
)

FFMPEG = os.environ.get("VK_FFMPEG", "")
DEVICE = os.environ.get("VK_DEVICE", "")
VULKAN = (
    "format=p010,hwupload,tonemap_vulkan=tonemap=bt2408_hable:peak={peak},"
    "hwdownload,format=nv12,format=yuv420p"
)
HLG_INPUT = "setparams=color_trc=arib-std-b67"
PQ_ONLY = "Unsupported input color properties"


def run(frame: bytes, chain: str) -> subprocess.CompletedProcess[bytes]:
    """Filter one raw frame through PQ_INPUT and chain on the Vulkan device."""
    cmd = [
        FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error",
        "-init_hw_device", f"vulkan=vk:{DEVICE}", "-filter_hw_device", "vk",
        "-f", "rawvideo", "-pix_fmt", "yuv420p10le", "-s", f"{SIZE}x{SIZE}", "-i", "pipe:0",
        "-vf", f"{PQ_INPUT},{chain}", "-f", "rawvideo", "pipe:1",
    ]  # fmt: skip
    return subprocess.run(cmd, input=frame, capture_output=True, timeout=120, check=False)


def output(frame: bytes, chain: str) -> bytes:
    """Return the raw output of a run that must succeed."""
    done = run(frame, chain)
    if done.returncode != 0:
        raise AssertionError(f"{chain} exited {done.returncode}: {done.stderr.decode()}")
    return done.stdout


@unittest.skipUnless(FFMPEG and DEVICE, "set VK_FFMPEG and VK_DEVICE")
class Bt2408HableTest(unittest.TestCase):
    """0019: the Vulkan bt2408_hable path must match the CPU path."""

    def test_vulkan_matches_cpu(self) -> None:
        for colour in COLOURS:
            with self.subTest(colour=colour):
                frame = flat_frame(*colour)
                cpu = centre_yuv(output(frame, CPU.format(peak=10)))
                vulkan = centre_yuv(output(frame, VULKAN.format(peak=10)))
                self.assertLessEqual(worst_difference(cpu, vulkan), 1, f"cpu {cpu} vulkan {vulkan}")

    def test_planted_wrong_peak_is_refused(self) -> None:
        worst = 0
        for colour in COLOURS:
            frame = flat_frame(*colour)
            cpu = centre_yuv(output(frame, CPU.format(peak=10)))
            vulkan = centre_yuv(output(frame, VULKAN.format(peak=40)))
            worst = max(worst, worst_difference(cpu, vulkan))
        self.assertGreater(worst, 1)

    def test_hlg_input_is_refused(self) -> None:
        done = run(flat_frame(*COLOURS[0]), f"{HLG_INPUT},{VULKAN.format(peak=10)}")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn(PQ_ONLY, done.stderr.decode())


if __name__ == "__main__":
    unittest.main()
