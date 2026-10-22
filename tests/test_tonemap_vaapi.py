# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Regression tests for patch 0019 (tonemap_vaapi).

0019: tonemap_vaapi with tonemap=bt2408_hable fills a VAProcFilter3DLUT table
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

FFMPEG = os.environ.get("VA_FFMPEG", "")
DEVICE = os.environ.get("VA_DEVICE", "")
VAAPI = (
    "format=p010,hwupload,tonemap_vaapi=format=rgba:tonemap=bt2408_hable:peak={peak},"
    "hwdownload,format=rgba,scale=out_color_matrix=bt709:out_range=tv,format=yuv420p"
)
RGB_ONLY = "needs an 8-bit RGB output format"


def run(frame: bytes, chain: str) -> subprocess.CompletedProcess[bytes]:
    """Filter one raw frame through PQ_INPUT and chain on the VAAPI device."""
    cmd = [
        FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error",
        "-init_hw_device", f"vaapi=va:{DEVICE}", "-filter_hw_device", "va",
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


@unittest.skipUnless(FFMPEG and DEVICE, "set VA_FFMPEG and VA_DEVICE")
class Bt2408HableTest(unittest.TestCase):
    """0019: the VAAPI bt2408_hable path must match the CPU path."""

    def test_vaapi_matches_cpu(self) -> None:
        for colour in COLOURS:
            with self.subTest(colour=colour):
                frame = flat_frame(*colour)
                cpu = centre_yuv(output(frame, CPU.format(peak=10)))
                vaapi = centre_yuv(output(frame, VAAPI.format(peak=10)))
                self.assertLessEqual(worst_difference(cpu, vaapi), 1, f"cpu {cpu} vaapi {vaapi}")

    def test_planted_wrong_peak_is_refused(self) -> None:
        worst = 0
        for colour in COLOURS:
            frame = flat_frame(*colour)
            cpu = centre_yuv(output(frame, CPU.format(peak=10)))
            vaapi = centre_yuv(output(frame, VAAPI.format(peak=40)))
            worst = max(worst, worst_difference(cpu, vaapi))
        self.assertGreater(worst, 1)

    def test_yuv_output_is_refused(self) -> None:
        chain = "format=p010,hwupload,tonemap_vaapi=format=nv12:tonemap=bt2408_hable,hwdownload"
        done = run(flat_frame(*COLOURS[0]), chain)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn(RGB_ONLY, done.stderr.decode())


if __name__ == "__main__":
    unittest.main()
