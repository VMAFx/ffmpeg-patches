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

import os
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests import helpers  # noqa: E402

FFMPEG = os.environ.get("CUDA_FFMPEG", "")
DEVICE = os.environ.get("CUDA_DEVICE", "")
RESET_TOOL = os.environ.get("HQDN3D_RESET_TOOL", "")
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


def ffmpeg(source: list[str], chain: str) -> subprocess.CompletedProcess[bytes]:
    return helpers.filter_frames(FFMPEG, (f"cuda=cu:{DEVICE}", "cu"), source, chain)


def run(source: list[str], chain: str) -> bytes:
    return helpers.raw_output(ffmpeg(source, chain), chain)


def chains(fmt: str, options: str, prefix: str = "") -> tuple[str, str]:
    return helpers.hqdn3d_chains(fmt, FORMATS[fmt][0], options, prefix, "cuda")


@unittest.skipUnless(FFMPEG and DEVICE, "set CUDA_FFMPEG and CUDA_DEVICE")
class Hqdn3dCudaTest(unittest.TestCase):
    """0024: the CUDA hqdn3d path must match the CPU path."""

    def compare(self, source: list[str], fmt: str, options: str, prefix: str = "") -> int:
        return helpers.compare_hqdn3d(source, chains(fmt, options, prefix), run, FORMATS[fmt])

    def test_cuda_matches_cpu(self) -> None:
        helpers.assert_formats_match(self, self.compare, FORMATS)

    def test_temporal_only_after_command(self) -> None:
        helpers.assert_temporal_match(self, self.compare, ("yuv420p", "p010le", "yuv444p16le"))

    def test_state_resets_on_graph_rebuild(self) -> None:
        helpers.assert_ffv1_rebuild_matches(self, FFMPEG, self.compare)

    @unittest.skipUnless(RESET_TOOL, "set HQDN3D_RESET_TOOL")
    def test_state_resets_on_in_graph_resize(self) -> None:
        done = helpers.run_reset(RESET_TOOL, f"cuda:{DEVICE}")
        self.assertEqual(done.returncode, 0, done.stdout.decode() + done.stderr.decode())

    def test_p012_is_refused(self) -> None:
        done = ffmpeg(
            helpers.lavfi(), "format=p012le,hwupload_cuda,hqdn3d_cuda,hwdownload,format=p012le"
        )
        self.assertNotEqual(done.returncode, 0)

    def test_planted_wrong_strength_is_refused(self) -> None:
        cpu_chain, _ = chains("yuv420p", "")
        _, cuda_chain = chains("yuv420p", "=luma_spatial=4.5")
        self.assertGreater(
            helpers.mismatches(
                run(helpers.lavfi(), cpu_chain), run(helpers.lavfi(), cuda_chain), 8, False
            ),
            0,
        )


if __name__ == "__main__":
    unittest.main()
