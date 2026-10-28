# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Regression tests for patch 0018 (tonemap_vaapi).

0018: tonemap_vaapi with tonemap=bt2408_hable fills a VAProcFilter3DLUT table
from the CPU tonemap=bt2408_hable math. On flat frames the VAAPI path gives
the same 8-bit code values as the CPU path; the test allows one code value
of difference.

The filter writes 8-bit full range RGB. With a YUV output the Intel media
driver converts the table output again, so the filter refuses YUV output
formats; the test checks that refusal and its message.

The planted wrong case is a run at four times the peak, which the
comparison must refuse.

The device cases need an FFmpeg built with the series and zimg, and a VAAPI
driver with the 3DLUT filter (iHD on an Intel GPU):

    VA_FFMPEG=/path/to/ffmpeg VA_DEVICE=/dev/dri/renderD128 \\
        LIBVA_DRIVER_NAME=iHD python3 -m unittest tests.test_tonemap_vaapi

Without both variables they are skipped.
"""

from __future__ import annotations

import unittest

from tests import helpers

FFMPEG, DEVICE = helpers.device_environment("VA")
VAAPI = (
    "format=p010,hwupload,tonemap_vaapi=format=rgba:tonemap=bt2408_hable:peak={peak},"
    "hwdownload,format=rgba,scale=out_color_matrix=bt709:out_range=tv,format=yuv420p"
)
RGB_ONLY = "needs an 8-bit RGB output format"


runner = helpers.TonemapRunner(FFMPEG, (f"vaapi=va:{DEVICE}", "va"), VAAPI, "vaapi")
run = runner.run
output = runner.output


@unittest.skipUnless(FFMPEG and DEVICE, "set VA_FFMPEG and VA_DEVICE")
class Bt2408HableTest(helpers.TonemapDeviceMixin, unittest.TestCase):
    """0018: the VAAPI bt2408_hable path must match the CPU path."""

    runner = runner

    def test_vaapi_matches_cpu(self) -> None:
        self.assert_matches_cpu()

    def test_yuv_output_is_refused(self) -> None:
        chain = "format=p010,hwupload,tonemap_vaapi=format=nv12:tonemap=bt2408_hable,hwdownload"
        done = run(helpers.flat_frame(*helpers.COLOURS[0]), chain)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn(RGB_ONLY, done.stderr.decode())


if __name__ == "__main__":
    unittest.main()
