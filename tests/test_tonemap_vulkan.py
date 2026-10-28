# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Regression tests for patches 0019 and 0021 (tonemap_vulkan).

0019: the new tonemap_vulkan filter runs the CPU tonemap=bt2408_hable math in
a compute shader. On flat frames the Vulkan path gives the same 8-bit code
values as the CPU path; the test allows one code value of difference.

The shader decodes PQ and BT.2020 limited range only, so the filter refuses
other input; the test checks that refusal on an HLG frame.

The planted wrong case is a run at four times the peak, which the
comparison must refuse.

0021: with peak_detect=1 the Vulkan path measures the peak of the frame in a
compute shader and gives the same output as the CPU detector. The detected
peak of a flat frame is far below the fixed peak of 1000 cd/m^2, so the test
also checks that detection changes the output; the planted wrong case runs
the Vulkan path at four times the peak against the CPU detector.

The device cases need an FFmpeg built with the series and zimg, and a Vulkan
device:

    VK_FFMPEG=/path/to/ffmpeg VK_DEVICE=0 python3 -m unittest tests.test_tonemap_vulkan

Without both variables they are skipped.
"""

from __future__ import annotations

import unittest

from tests import helpers

FFMPEG, DEVICE = helpers.device_environment("VK")
VULKAN = (
    "format=p010,hwupload,tonemap_vulkan=tonemap=bt2408_hable:peak={peak},"
    "hwdownload,format=nv12,format=yuv420p"
)
DETECT = "0:peak_detect=1"
HLG_INPUT = "setparams=color_trc=arib-std-b67"
PQ_ONLY = "Unsupported input color properties"


runner = helpers.TonemapRunner(FFMPEG, (f"vulkan=vk:{DEVICE}", "vk"), VULKAN, "vulkan")
run = runner.run
output = runner.output


@unittest.skipUnless(FFMPEG and DEVICE, "set VK_FFMPEG and VK_DEVICE")
class Bt2408HableTest(helpers.TonemapDeviceMixin, unittest.TestCase):
    """0019, 0021: the Vulkan bt2408_hable path must match the CPU path."""

    runner = runner

    def test_vulkan_matches_cpu(self) -> None:
        self.assert_matches_cpu()

    def test_detected_peak_matches_cpu(self) -> None:
        self.assert_matches_cpu(DETECT)

    def test_detection_changes_the_output(self) -> None:
        self.assertGreater(
            self.worst_between(VULKAN.format(peak=10), VULKAN.format(peak=DETECT)), 1
        )

    def test_planted_wrong_detected_peak_is_refused(self) -> None:
        self.assertGreater(
            self.worst_between(helpers.CPU.format(peak=DETECT), VULKAN.format(peak=40)), 1
        )

    def test_hlg_input_is_refused(self) -> None:
        done = run(helpers.flat_frame(*helpers.COLOURS[0]), f"{HLG_INPUT},{VULKAN.format(peak=10)}")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn(PQ_ONLY, done.stderr.decode())


if __name__ == "__main__":
    unittest.main()
