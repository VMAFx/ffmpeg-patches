# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Regression tests for patch 0027 (hqdn3d_videotoolbox).

0027: the new hqdn3d_videotoolbox filter runs the integer hqdn3d filter in
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
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.test_hqdn3d_vulkan import OPTIONS, SOURCE, TEMPORAL_ONLY, mismatches  # noqa: E402

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
PATCH = next((Path(__file__).resolve().parent.parent / "patches").glob("0027-*.patch"))


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
    """Run chain on the VideoToolbox device."""
    cmd = [
        FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error",
        "-init_hw_device", "videotoolbox=vt", "-filter_hw_device", "vt",
        *source, "-vf", chain, "-f", "rawvideo", "pipe:1",
    ]  # fmt: skip
    return subprocess.run(cmd, capture_output=True, timeout=300, check=False)


def run(source: list[str], chain: str) -> bytes:
    """Run chain on the VideoToolbox device and return the raw output."""
    done = ffmpeg(source, chain)
    if done.returncode != 0:
        raise AssertionError(f"{chain} exited {done.returncode}: {done.stderr.decode()}")
    return done.stdout


def chains(fmt: str, options: str, prefix: str = "") -> tuple[str, str]:
    """Return the CPU and the Metal chain for one format and option string."""
    planar = FORMATS[fmt][0]
    cpu = f"format={fmt},format={planar},{prefix.format(f='hqdn3d')}hqdn3d{options}"
    metal = (
        f"format={fmt},hwupload,{prefix.format(f='hqdn3d_videotoolbox')}"
        f"hqdn3d_videotoolbox{options},hwdownload,format={fmt},format={planar}"
    )
    return cpu, metal


def lavfi() -> list[str]:
    return ["-f", "lavfi", "-i", SOURCE]


class FormatListTest(unittest.TestCase):
    """0027: the patch accepts and documents exactly the formats under test."""

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
    """0027: the Metal hqdn3d path must match the CPU path."""

    def compare(self, source: list[str], fmt: str, options: str, prefix: str = "") -> int:
        cpu_chain, metal_chain = chains(fmt, options, prefix)
        _, depth, high_bits = FORMATS[fmt]
        return mismatches(run(source, cpu_chain), run(source, metal_chain), depth, high_bits)

    def test_metal_matches_cpu(self) -> None:
        for fmt in FORMATS:
            for options in OPTIONS:
                with self.subTest(fmt=fmt, options=options):
                    self.assertEqual(self.compare(lavfi(), fmt, options), 0)

    def test_temporal_only_after_command(self) -> None:
        for fmt in ("yuv420p", "p010le"):
            with self.subTest(fmt=fmt):
                self.assertEqual(self.compare(lavfi(), fmt, "", TEMPORAL_ONLY), 0)

    def test_state_resets_on_graph_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            parts = []
            for index, size in enumerate(("320x184", "160x96")):
                part = Path(tmp) / f"{index}.nut"
                encode = [
                    FFMPEG, "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i",
                    f"testsrc2=s={size}:d=0.32,noise=alls=20:allf=t,format=yuv420p",
                    "-c:v", "ffv1", str(part),
                ]  # fmt: skip
                subprocess.run(encode, capture_output=True, timeout=120, check=True)
                parts.append(f"file '{part}'\n")
            listing = Path(tmp) / "list.txt"
            listing.write_text("".join(parts), encoding="utf-8")
            source = ["-f", "concat", "-safe", "0", "-i", str(listing)]
            self.assertEqual(self.compare(source, "yuv420p", ""), 0)

    @unittest.skipUnless(RESET_TOOL, "set HQDN3D_RESET_TOOL")
    def test_state_resets_on_in_graph_resize(self) -> None:
        done = subprocess.run([RESET_TOOL, "vt"], capture_output=True, timeout=300, check=False)
        self.assertEqual(done.returncode, 0, done.stdout.decode() + done.stderr.decode())

    def test_unsupported_format_is_refused(self) -> None:
        done = ffmpeg(lavfi(), "format=rgba,hwupload,hqdn3d_videotoolbox,hwdownload,format=rgba")
        self.assertNotEqual(done.returncode, 0)

    def test_planted_wrong_strength_is_refused(self) -> None:
        cpu_chain, _ = chains("yuv420p", "")
        _, metal_chain = chains("yuv420p", "=luma_spatial=4.5")
        self.assertGreater(
            mismatches(run(lavfi(), cpu_chain), run(lavfi(), metal_chain), 8, False), 0
        )


if __name__ == "__main__":
    unittest.main()
