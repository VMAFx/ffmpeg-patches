# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Regression tests for patch 0026 (hqdn3d_videotoolbox).

0026: the new hqdn3d_videotoolbox filter runs the integer hqdn3d filter in
two Metal kernels and must give the same output samples as the CPU hqdn3d
filter (ADR-0004), by the comparison rule of tests/test_hqdn3d_vulkan.py.
The filter takes yuv420p and the two-plane nv12, nv16, nv24, p010, p210,
p410, p216 and p416; a format the filter does not take (rgba) must be
refused. No Apple host has run these cases yet. The format list in the
patch (validate_format and doc/filters.texi) must equal FORMATS; that case
runs on every host, and a patch text with one format dropped must fail it.

Temporal state must reset where the CPU filter resets it: on a graph rebuild
and on an in-graph frame size change, which tools/hqdn3d_reset_test checks
with a vt argument.

The planted wrong case is a luma strength of 4.5 against the CPU default of
4, which the comparison must refuse.

The device cases need an FFmpeg built with the series on macOS with Metal:

    VT_FFMPEG=/path/to/ffmpeg python3 -m unittest tests.test_hqdn3d_videotoolbox

The in-graph reset case also needs HQDN3D_RESET_TOOL=/path/to/tools/hqdn3d_reset_test.
Without the variables the cases are skipped.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests import helpers  # noqa: E402

FFMPEG = os.environ.get("VT_FFMPEG", "")
RESET_TOOL = os.environ.get("HQDN3D_RESET_TOOL", "")
# format under test -> (planar reference format, bit depth, high-bit packed)
FORMATS = {
    "yuv420p": ("yuv420p", 8, False),
    "nv12": ("yuv420p", 8, False),
    "nv16": ("yuv422p", 8, False),
    "nv24": ("yuv444p", 8, False),
    "p010le": ("yuv420p10le", 10, True),
    "p210le": ("yuv422p10le", 10, True),
    "p410le": ("yuv444p10le", 10, True),
    "p216le": ("yuv422p16le", 16, False),
    "p416le": ("yuv444p16le", 16, False),
}
PATCH = next((Path(__file__).resolve().parent.parent / "patches").glob("0026-*.patch"))


def patch_formats(text: str) -> tuple[set[str], set[str]]:
    """Return the formats validate_format and the filter doc accept in a patch."""
    code = text.split("+static int validate_format(", 1)[1].split("\n+}\n", 1)[0]
    doc = text.split("+Supported input hardware frame software formats:", 1)[1]
    doc = doc.split("+@end itemize", 1)[0]
    return (
        {name.lower() for name in re.findall(r"case AV_PIX_FMT_(\w+):", code)},
        set(re.findall(r"@item @samp\{(\w+)\}", doc)),
    )


def expected_formats() -> set[str]:
    return {fmt.removesuffix("le") for fmt in FORMATS}


def ffmpeg(source: list[str], chain: str) -> subprocess.CompletedProcess[bytes]:
    return helpers.filter_frames(FFMPEG, ("videotoolbox=vt", "vt"), source, chain)


def run(source: list[str], chain: str) -> bytes:
    return helpers.raw_output(ffmpeg(source, chain), chain)


def chains(fmt: str, options: str, prefix: str = "") -> tuple[str, str]:
    return helpers.hqdn3d_chains(fmt, FORMATS[fmt][0], options, prefix, "videotoolbox")


class FormatListTest(unittest.TestCase):
    """0026: the patch accepts and documents exactly the formats under test."""

    def test_patch_formats_match(self) -> None:
        code, doc = patch_formats(PATCH.read_text(encoding="utf-8"))
        self.assertEqual(code, expected_formats())
        self.assertEqual(doc, expected_formats())

    def test_planted_dropped_format_is_refused(self) -> None:
        text = PATCH.read_text(encoding="utf-8").replace("+    case AV_PIX_FMT_P416:\n", "", 1)
        code, _ = patch_formats(text)
        self.assertNotEqual(code, expected_formats())


@unittest.skipUnless(FFMPEG, "set VT_FFMPEG")
class Hqdn3dVideoToolboxTest(unittest.TestCase):
    """0026: the Metal hqdn3d path must match the CPU path."""

    def compare(self, source: list[str], fmt: str, options: str, prefix: str = "") -> int:
        return helpers.compare_hqdn3d(source, chains(fmt, options, prefix), run, FORMATS[fmt])

    def test_metal_matches_cpu(self) -> None:
        helpers.assert_formats_match(self, self.compare, FORMATS)

    def test_temporal_only_after_command(self) -> None:
        helpers.assert_temporal_match(self, self.compare, ("yuv420p", "p010le"))

    def test_state_resets_on_graph_rebuild(self) -> None:
        helpers.assert_ffv1_rebuild_matches(self, FFMPEG, self.compare)

    @unittest.skipUnless(RESET_TOOL, "set HQDN3D_RESET_TOOL")
    def test_state_resets_on_in_graph_resize(self) -> None:
        done = helpers.run_reset(RESET_TOOL, "vt")
        self.assertEqual(done.returncode, 0, done.stdout.decode() + done.stderr.decode())

    def test_unsupported_format_is_refused(self) -> None:
        done = ffmpeg(
            helpers.lavfi(), "format=rgba,hwupload,hqdn3d_videotoolbox,hwdownload,format=rgba"
        )
        self.assertNotEqual(done.returncode, 0)

    def test_planted_wrong_strength_is_refused(self) -> None:
        cpu_chain, _ = chains("yuv420p", "")
        _, metal_chain = chains("yuv420p", "=luma_spatial=4.5")
        self.assertGreater(
            helpers.mismatches(
                run(helpers.lavfi(), cpu_chain), run(helpers.lavfi(), metal_chain), 8, False
            ),
            0,
        )


if __name__ == "__main__":
    unittest.main()
