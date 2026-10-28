# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Regression tests for patch 0025 (hqdn3d_opencl).

0025: the new hqdn3d_opencl filter runs the integer hqdn3d filter in two
OpenCL kernels and must give the same output samples as the CPU hqdn3d
filter (ADR-0004), by the comparison rule of tests/test_hqdn3d_vulkan.py.
The OpenCL frames context keeps no LSB-aligned 10- or 12-bit planar format,
so the high-depth planar cases are 16-bit; a format the filter does not take
(rgba) must be refused.

Temporal state must reset where the CPU filter resets it: on a graph rebuild
and on an in-graph frame size change, which tools/hqdn3d_reset_test checks
with an ocl:<platform>.<device> argument.

The planted wrong case is a luma strength of 4.5 against the CPU default of
4, which the comparison must refuse.

The device cases need an FFmpeg built with the series and an OpenCL device:

    OCL_FFMPEG=/path/to/ffmpeg OCL_DEVICE=2.0 python3 -m unittest tests.test_hqdn3d_opencl

The in-graph reset case also needs HQDN3D_RESET_TOOL=/path/to/tools/hqdn3d_reset_test.
Without the variables the cases are skipped.
"""

from __future__ import annotations

import unittest

from tests import helpers

FFMPEG, DEVICE = helpers.device_environment("OCL")
# format under test -> (planar reference format, bit depth, high-bit packed)
FORMATS = {
    "yuv420p": ("yuv420p", 8, False),
    "yuv422p": ("yuv422p", 8, False),
    "yuv444p": ("yuv444p", 8, False),
    "yuv420p16le": ("yuv420p16le", 16, False),
    "yuv444p16le": ("yuv444p16le", 16, False),
    **helpers.SEMIPLANAR_FORMATS,
}


runner = helpers.Hqdn3dRunner(
    FFMPEG, (f"opencl=ocl:{DEVICE}", "ocl"), "opencl", FORMATS, noautoscale=True
)
ffmpeg = runner.ffmpeg
run = runner.run
chains = runner.chains


@unittest.skipUnless(FFMPEG and DEVICE, "set OCL_FFMPEG and OCL_DEVICE")
class Hqdn3dOpenCLTest(helpers.Hqdn3dSizedRebuildMixin, unittest.TestCase):
    """0025: the OpenCL hqdn3d path must match the CPU path."""

    runner = runner
    reset_device = f"ocl:{DEVICE}"

    def test_opencl_matches_cpu(self) -> None:
        helpers.assert_formats_match(self, self.compare, FORMATS)

    def test_unsupported_format_is_refused(self) -> None:
        done = ffmpeg(helpers.lavfi(), "format=rgba,hwupload,hqdn3d_opencl,hwdownload,format=rgba")
        self.assertNotEqual(done.returncode, 0)


if __name__ == "__main__":
    unittest.main()
