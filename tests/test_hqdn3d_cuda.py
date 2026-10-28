# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Regression tests for patch 0024 (hqdn3d_cuda).

0024: the new hqdn3d_cuda filter runs the integer hqdn3d filter in two CUDA
kernels and must give the same output samples as the CPU hqdn3d filter
(ADR-0004), by the comparison rule of tests/test_hqdn3d_vulkan.py. P012 is
not accepted, because hwupload_cuda converts it to P016; the refusal is a
case here.

Temporal state must reset where the CPU filter resets it: on a graph rebuild
and on an in-graph frame size change, which tools/hqdn3d_reset_test checks
with a cuda:<index> device argument.

The planted wrong case is a luma strength of 4.5 against the CPU default of
4, which the comparison must refuse.

The device cases need an FFmpeg built with the series and a CUDA device:

    CUDA_FFMPEG=/path/to/ffmpeg CUDA_DEVICE=0 python3 -m unittest tests.test_hqdn3d_cuda

The in-graph reset case also needs HQDN3D_RESET_TOOL=/path/to/tools/hqdn3d_reset_test.
Without the variables the cases are skipped.
"""

from __future__ import annotations

import unittest

from tests import helpers

FFMPEG, DEVICE = helpers.device_environment("CUDA")
# format under test -> (planar reference format, bit depth, high-bit packed)
FORMATS = {
    "yuv420p": ("yuv420p", 8, False),
    "yuv422p": ("yuv422p", 8, False),
    "yuv444p": ("yuv444p", 8, False),
    "yuv420p10le": ("yuv420p10le", 10, False),
    "yuv444p10le": ("yuv444p10le", 10, False),
    "yuv444p16le": ("yuv444p16le", 16, False),
    "nv12": ("yuv420p", 8, False),
    "p010le": ("yuv420p10le", 10, True),
    "p016le": ("yuv420p16le", 16, True),
}


runner = helpers.Hqdn3dRunner(
    FFMPEG, (f"cuda=cu:{DEVICE}", "cu"), "cuda", FORMATS, noautoscale=False
)
ffmpeg = runner.ffmpeg
run = runner.run
chains = runner.chains


@unittest.skipUnless(FFMPEG and DEVICE, "set CUDA_FFMPEG and CUDA_DEVICE")
class Hqdn3dCudaTest(helpers.Hqdn3dDeviceMixin, unittest.TestCase):
    """0024: the CUDA hqdn3d path must match the CPU path."""

    runner = runner
    reset_device = f"cuda:{DEVICE}"

    def test_cuda_matches_cpu(self) -> None:
        helpers.assert_formats_match(self, self.compare, FORMATS)

    def test_p012_is_refused(self) -> None:
        done = ffmpeg(
            helpers.lavfi(), "format=p012le,hwupload_cuda,hqdn3d_cuda,hwdownload,format=p012le"
        )
        self.assertNotEqual(done.returncode, 0)


if __name__ == "__main__":
    unittest.main()
