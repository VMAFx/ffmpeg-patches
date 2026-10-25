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

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.test_hqdn3d_vulkan import (  # noqa: E402
    OPTIONS,
    REBUILD_BYTES,
    SOURCE,
    TEMPORAL_ONLY,
    mismatches,
    rebuild_source,
)

FFMPEG = os.environ.get("OCL_FFMPEG", "")
DEVICE = os.environ.get("OCL_DEVICE", "")
RESET_TOOL = os.environ.get("HQDN3D_RESET_TOOL", "")
# format under test -> (planar reference format, bit depth, high-bit packed)
FORMATS = {
    "yuv420p": ("yuv420p", 8, False),
    "yuv422p": ("yuv422p", 8, False),
    "yuv444p": ("yuv444p", 8, False),
    "yuv420p16le": ("yuv420p16le", 16, False),
    "yuv444p16le": ("yuv444p16le", 16, False),
    "nv12": ("yuv420p", 8, False),
    "p010le": ("yuv420p10le", 10, True),
    "p012le": ("yuv420p12le", 12, True),
    "p016le": ("yuv420p16le", 16, True),
}


def ffmpeg(source: list[str], chain: str) -> subprocess.CompletedProcess[bytes]:
    """Run chain on the OpenCL device."""
    cmd = [
        FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error",
        "-init_hw_device", f"opencl=ocl:{DEVICE}", "-filter_hw_device", "ocl",
        *source, "-vf", chain, "-noautoscale", "-f", "rawvideo", "pipe:1",
    ]  # fmt: skip
    return subprocess.run(cmd, capture_output=True, timeout=300, check=False)


def run(source: list[str], chain: str) -> bytes:
    """Run chain on the OpenCL device and return the raw output."""
    done = ffmpeg(source, chain)
    if done.returncode != 0:
        raise AssertionError(f"{chain} exited {done.returncode}: {done.stderr.decode()}")
    return done.stdout


def chains(fmt: str, options: str, prefix: str = "") -> tuple[str, str]:
    """Return the CPU and the OpenCL chain for one format and option string."""
    planar = FORMATS[fmt][0]
    cpu = f"format={fmt},format={planar},{prefix.format(f='hqdn3d')}hqdn3d{options}"
    opencl = (
        f"format={fmt},hwupload,{prefix.format(f='hqdn3d_opencl')}hqdn3d_opencl{options},"
        f"hwdownload,format={fmt},format={planar}"
    )
    return cpu, opencl


def lavfi() -> list[str]:
    return ["-f", "lavfi", "-i", SOURCE]


@unittest.skipUnless(FFMPEG and DEVICE, "set OCL_FFMPEG and OCL_DEVICE")
class Hqdn3dOpenCLTest(unittest.TestCase):
    """0025: the OpenCL hqdn3d path must match the CPU path."""

    def compare(self, source: list[str], fmt: str, options: str, prefix: str = "") -> int:
        cpu_chain, opencl_chain = chains(fmt, options, prefix)
        _, depth, high_bits = FORMATS[fmt]
        return mismatches(run(source, cpu_chain), run(source, opencl_chain), depth, high_bits)

    def test_opencl_matches_cpu(self) -> None:
        for fmt in FORMATS:
            for options in OPTIONS:
                with self.subTest(fmt=fmt, options=options):
                    self.assertEqual(self.compare(lavfi(), fmt, options), 0)

    def test_temporal_only_after_command(self) -> None:
        for fmt in ("yuv420p", "p010le", "yuv444p16le"):
            with self.subTest(fmt=fmt):
                self.assertEqual(self.compare(lavfi(), fmt, "", TEMPORAL_ONLY), 0)

    def test_state_resets_on_graph_rebuild(self) -> None:
        cpu_chain, opencl_chain = chains("yuv420p", "")
        with tempfile.TemporaryDirectory() as tmp:
            source = rebuild_source(FFMPEG, tmp, "mjpeg")
            cpu = run(source, cpu_chain)
            self.assertEqual(len(cpu), REBUILD_BYTES)
            self.assertEqual(mismatches(cpu, run(source, opencl_chain), 8, False), 0)

    def test_planted_sizeless_rebuild_source_is_refused(self) -> None:
        cpu_chain, _ = chains("yuv420p", "")
        with tempfile.TemporaryDirectory() as tmp:
            source = rebuild_source(FFMPEG, tmp, "ffv1")
            self.assertNotEqual(len(run(source, cpu_chain)), REBUILD_BYTES)

    @unittest.skipUnless(RESET_TOOL, "set HQDN3D_RESET_TOOL")
    def test_state_resets_on_in_graph_resize(self) -> None:
        done = subprocess.run(
            [RESET_TOOL, f"ocl:{DEVICE}"], capture_output=True, timeout=300, check=False
        )
        self.assertEqual(done.returncode, 0, done.stdout.decode() + done.stderr.decode())

    def test_unsupported_format_is_refused(self) -> None:
        done = ffmpeg(lavfi(), "format=rgba,hwupload,hqdn3d_opencl,hwdownload,format=rgba")
        self.assertNotEqual(done.returncode, 0)

    def test_planted_wrong_strength_is_refused(self) -> None:
        cpu_chain, _ = chains("yuv420p", "")
        _, opencl_chain = chains("yuv420p", "=luma_spatial=4.5")
        self.assertGreater(
            mismatches(run(lavfi(), cpu_chain), run(lavfi(), opencl_chain), 8, False), 0
        )


if __name__ == "__main__":
    unittest.main()
