# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Regression tests for patch 0023 (hqdn3d_vulkan).

0023: the new hqdn3d_vulkan filter runs the integer hqdn3d filter in two
compute shaders and must give the same output samples as the CPU hqdn3d
filter (ADR-0004). Planar formats must match bit for bit. The semi-planar
P010, P012 and P016 formats keep samples in the high bits, so a 12-bit CPU
result above 4095 (the CPU filter can write 65535 into a 12-bit plane when an
intermediate value turns negative) cannot be stored; there the Vulkan path
must give the format maximum and every other sample must match.

Temporal state must reset where the CPU filter resets it: on a graph rebuild
(a resolution switch in the input) and on an in-graph frame size change,
which the tools/hqdn3d_reset_test driver of the patch exercises. The rebuild
source joins a 320x184 and a 160x96 part with the concat demuxer. The parts
are MJPEG, whose frame header carries the frame size: concat keeps the stream
parameters of the first part, so a codec without an in-band size (FFV1)
would decode the second part at 320x184, no rebuild would happen, and the
output would depend on decoder threading. -noautoscale keeps the CLI from
scaling the second part back to 320x184 for the encoder, so the output length
proves the rebuild.

The planted wrong cases are a luma strength of 4.5 against the CPU default of
4, which the comparison must refuse, and an FFV1 rebuild source, whose output
length the rebuild check must refuse.

The device cases need an FFmpeg built with the series and a Vulkan device:

    VK_FFMPEG=/path/to/ffmpeg VK_DEVICE=0 python3 -m unittest tests.test_hqdn3d_vulkan

The in-graph reset case also needs HQDN3D_RESET_TOOL=/path/to/tools/hqdn3d_reset_test
(make tools/hqdn3d_reset_test in the FFmpeg build). Without the variables
the cases are skipped.
"""

from __future__ import annotations

import unittest
from array import array

from tests import helpers
from tests.helpers import mismatches

FFMPEG, DEVICE = helpers.device_environment("VK")
# format under test -> (planar reference format, bit depth, high-bit packed)
FORMATS = {
    "yuv420p": ("yuv420p", 8, False),
    "yuv422p": ("yuv422p", 8, False),
    "yuv444p10le": ("yuv444p10le", 10, False),
    "yuv420p12le": ("yuv420p12le", 12, False),
    "yuv444p16le": ("yuv444p16le", 16, False),
    **helpers.SEMIPLANAR_FORMATS,
}


runner = helpers.Hqdn3dRunner(
    FFMPEG, (f"vulkan=vk:{DEVICE}", "vk"), "vulkan", FORMATS, noautoscale=True
)
ffmpeg = runner.ffmpeg
run = runner.run
chains = runner.chains


@unittest.skipUnless(FFMPEG and DEVICE, "set VK_FFMPEG and VK_DEVICE")
class Hqdn3dVulkanTest(helpers.Hqdn3dSizedRebuildMixin, unittest.TestCase):
    """0023: the Vulkan hqdn3d path must match the CPU path."""

    runner = runner
    reset_device = DEVICE

    def test_vulkan_matches_cpu(self) -> None:
        helpers.assert_formats_match(self, self.compare, FORMATS)


class MismatchRuleTest(unittest.TestCase):
    """The comparison rule itself, without a device."""

    def test_equal_planar_samples_pass(self) -> None:
        self.assertEqual(
            mismatches(array("H", [1, 4095]).tobytes(), array("H", [1, 4095]).tobytes(), 12, False),
            0,
        )

    def test_out_of_range_maps_to_maximum_only_when_high_bits(self) -> None:
        cpu = array("H", [65535, 7]).tobytes()
        self.assertEqual(mismatches(cpu, array("H", [4095, 7]).tobytes(), 12, True), 0)
        self.assertEqual(mismatches(cpu, array("H", [4095, 7]).tobytes(), 12, False), 1)

    def test_wrong_in_range_sample_fails(self) -> None:
        self.assertEqual(
            mismatches(array("H", [9, 7]).tobytes(), array("H", [8, 7]).tobytes(), 12, True), 1
        )

    def test_empty_or_short_output_fails(self) -> None:
        self.assertGreater(mismatches(b"", b"", 8, False), 0)
        self.assertGreater(mismatches(b"ab", b"a", 8, False), 0)


if __name__ == "__main__":
    unittest.main()
