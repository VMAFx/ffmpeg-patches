# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Regression tests for patches 0015, 0017 and 0034 (tonemap_opencl).

0015: tonemap_opencl declares the matrix and range of its output on the
link. Without it, a software conversion after hwdownload took BT.709 frames
as the input's BT.2020 matrix and converted them again; a pure red HDR10
frame came out with Y 73 instead of 63 after format=yuv420p.

0017: tonemap_opencl=tonemap=bt2408_hable runs the CPU tonemap=bt2408_hable
math on the GPU. On flat frames both paths give the same 8-bit code values;
the test allows one code value of difference.

0034: tonemap_opencl=tonemap=bt2408_hable:peak_detect=1 measures the peak
on the GPU like tonemap=bt2408_hable:peak_detect=1 does on the CPU. A raw
frame carries no light level side data, so its static peak is the PQ
maximum of 10000 cd/m^2, far above the flat test colours.

Each check has a planted wrong case that the comparison must refuse: a
frame mislabelled as BT.2020 before the conversion, a GPU run at four
times the peak, and a GPU run without detection on the static peak.

The device cases need an FFmpeg built with the series, OpenCL and zimg, and
an OpenCL device:

    OCL_FFMPEG=/path/to/ffmpeg OCL_DEVICE=0.0 \\
        python3 -m unittest tests.test_tonemap_opencl

OCL_DEVICE is the platform.device index of -init_hw_device opencl. Without
both variables they are skipped.
"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.helpers import (  # noqa: E402
    COLOURS,
    CPU,
    PQ_INPUT,
    SIZE,
    centre_yuv,
    flat_frame,
    worst_difference,
)

FFMPEG = os.environ.get("OCL_FFMPEG", "")
DEVICE = os.environ.get("OCL_DEVICE", "")
GPU = (
    "format=p010,hwupload,tonemap_opencl=tonemap={curve}:m=bt709:r=tv:p=bt709:t=bt709:"
    "peak={peak},hwdownload,format=nv12"
)
CPU_DETECT = CPU.replace("peak={peak},", "peak={peak}:peak_detect=1,")
GPU_DETECT = GPU.replace("peak={peak},", "peak={peak}:peak_detect=1,")


def run(frame: bytes, chain: str) -> bytes:
    """Filter one raw frame through PQ_INPUT and chain; return raw output."""
    cmd = [
        FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error",
        "-init_hw_device", f"opencl=ocl:{DEVICE}", "-filter_hw_device", "ocl",
        "-f", "rawvideo", "-pix_fmt", "yuv420p10le", "-s", f"{SIZE}x{SIZE}", "-i", "pipe:0",
        "-vf", f"{PQ_INPUT},{chain}", "-f", "rawvideo", "pipe:1",
    ]  # fmt: skip
    done = subprocess.run(cmd, input=frame, capture_output=True, timeout=120, check=False)
    if done.returncode != 0:
        raise AssertionError(f"{cmd} exited {done.returncode}: {done.stderr.decode()}")
    return done.stdout


class HelperTest(unittest.TestCase):
    def test_flat_frame_size_and_values(self) -> None:
        frame = flat_frame(292, 388, 956)
        self.assertEqual(len(frame), SIZE * SIZE * 3)
        self.assertEqual(frame[:2], (292).to_bytes(2, "little"))
        self.assertEqual(frame[-2:], (956).to_bytes(2, "little"))

    def test_centre_reads_each_plane(self) -> None:
        luma = SIZE * SIZE
        frame = bytes([10]) * luma + bytes([20]) * (luma // 4) + bytes([30]) * (luma // 4)
        self.assertEqual(centre_yuv(frame), (10, 20, 30))

    def test_centre_refuses_a_short_frame(self) -> None:
        with self.assertRaises(ValueError):
            centre_yuv(bytes(SIZE * SIZE))

    def test_detect_chains_add_the_option(self) -> None:
        for plain, detect in ((CPU, CPU_DETECT), (GPU, GPU_DETECT)):
            self.assertNotIn("peak_detect", plain)
            self.assertIn("peak={peak}:peak_detect=1,", detect)

    def test_worst_difference(self) -> None:
        self.assertEqual(worst_difference((63, 102, 240), (63, 102, 240)), 0)
        self.assertEqual(worst_difference((63, 102, 240), (73, 100, 241)), 10)


@unittest.skipUnless(FFMPEG and DEVICE, "set OCL_FFMPEG and OCL_DEVICE")
class OutputLinkTest(unittest.TestCase):
    """0015: a software conversion after tonemap_opencl must not re-matrix."""

    def test_conversion_keeps_the_output_matrix(self) -> None:
        frame = flat_frame(*COLOURS[0])
        for curve in ("hable", "bt2408_hable"):
            with self.subTest(curve=curve):
                chain = GPU.format(curve=curve, peak=10)
                nv12 = run(frame, f"{chain},format=nv12")
                via_sws = run(frame, f"{chain},format=yuv420p,format=nv12")
                self.assertEqual(nv12, via_sws)

    def test_planted_wrong_matrix_is_refused(self) -> None:
        frame = flat_frame(*COLOURS[0])
        chain = GPU.format(curve="bt2408_hable", peak=10)
        right = run(frame, f"{chain},format=yuv420p")
        wrong = run(frame, f"{chain},setparams=colorspace=bt2020nc,"
                           "scale=out_color_matrix=bt709,format=yuv420p")  # fmt: skip
        self.assertGreater(worst_difference(centre_yuv(right), centre_yuv(wrong)), 1)


@unittest.skipUnless(FFMPEG and DEVICE, "set OCL_FFMPEG and OCL_DEVICE")
class Bt2408HableTest(unittest.TestCase):
    """0017: the GPU bt2408_hable path must match the CPU path."""

    def test_gpu_matches_cpu(self) -> None:
        for colour in COLOURS:
            with self.subTest(colour=colour):
                frame = flat_frame(*colour)
                cpu = centre_yuv(run(frame, CPU.format(peak=10)))
                gpu = centre_yuv(run(frame, GPU.format(curve="bt2408_hable", peak=10)
                                     + ",format=yuv420p"))  # fmt: skip
                self.assertLessEqual(worst_difference(cpu, gpu), 1, f"cpu {cpu} gpu {gpu}")

    def test_planted_wrong_peak_is_refused(self) -> None:
        worst = 0
        for colour in COLOURS:
            frame = flat_frame(*colour)
            cpu = centre_yuv(run(frame, CPU.format(peak=10)))
            gpu = centre_yuv(run(frame, GPU.format(curve="bt2408_hable", peak=40)
                                 + ",format=yuv420p"))  # fmt: skip
            worst = max(worst, worst_difference(cpu, gpu))
        self.assertGreater(worst, 1)


@unittest.skipUnless(FFMPEG and DEVICE, "set OCL_FFMPEG and OCL_DEVICE")
class PeakDetectTest(unittest.TestCase):
    """0034: GPU peak detection must match CPU peak detection."""

    def worst_against_cpu_detection(self, gpu_chain: str) -> int:
        worst = 0
        for colour in COLOURS:
            frame = flat_frame(*colour)
            cpu = centre_yuv(run(frame, CPU_DETECT.format(peak=0)))
            gpu = centre_yuv(run(frame, gpu_chain.format(curve="bt2408_hable", peak=0)
                                 + ",format=yuv420p"))  # fmt: skip
            worst = max(worst, worst_difference(cpu, gpu))
        return worst

    def test_gpu_detection_matches_cpu_detection(self) -> None:
        self.assertLessEqual(self.worst_against_cpu_detection(GPU_DETECT), 1)

    def test_planted_static_peak_is_refused(self) -> None:
        self.assertGreater(self.worst_against_cpu_detection(GPU), 1)


if __name__ == "__main__":
    unittest.main()
