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

import os
import subprocess
import tempfile
import unittest
from array import array
from pathlib import Path

FFMPEG = os.environ.get("VK_FFMPEG", "")
DEVICE = os.environ.get("VK_DEVICE", "")
RESET_TOOL = os.environ.get("HQDN3D_RESET_TOOL", "")
SOURCE = "testsrc2=s=320x184:d=0.32,noise=alls=20:allf=t"
# format under test -> (planar reference format, bit depth, high-bit packed)
FORMATS = {
    "yuv420p": ("yuv420p", 8, False),
    "yuv422p": ("yuv422p", 8, False),
    "yuv444p10le": ("yuv444p10le", 10, False),
    "yuv420p12le": ("yuv420p12le", 12, False),
    "yuv444p16le": ("yuv444p16le", 16, False),
    "nv12": ("yuv420p", 8, False),
    "p010le": ("yuv420p10le", 10, True),
    "p012le": ("yuv420p12le", 12, True),
    "p016le": ("yuv420p16le", 16, True),
}
OPTIONS = ("", "=254:254:254:254", "=0.5:0.5:0.5:0.5", "=luma_spatial=60:luma_tmp=0:chroma_tmp=200")
TEMPORAL_ONLY = "sendcmd=c='0.12 {f} luma_spatial 0; 0.12 {f} chroma_spatial 0',"
REBUILD_SIZES = ((320, 184), (160, 96))
# eight frames of each part as yuv420p
REBUILD_BYTES = sum(8 * w * h * 3 // 2 for w, h in REBUILD_SIZES)


def run(source: list[str], chain: str) -> bytes:
    """Run chain on the Vulkan device and return the raw output."""
    cmd = [
        FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error",
        "-init_hw_device", f"vulkan=vk:{DEVICE}", "-filter_hw_device", "vk",
        *source, "-vf", chain, "-noautoscale", "-f", "rawvideo", "pipe:1",
    ]  # fmt: skip
    done = subprocess.run(cmd, capture_output=True, timeout=300, check=False)
    if done.returncode != 0:
        raise AssertionError(f"{chain} exited {done.returncode}: {done.stderr.decode()}")
    return done.stdout


def chains(fmt: str, options: str, prefix: str = "") -> tuple[str, str]:
    """Return the CPU and the Vulkan chain for one format and option string."""
    planar = FORMATS[fmt][0]
    cpu = f"format={fmt},format={planar},{prefix.format(f='hqdn3d')}hqdn3d{options}"
    vulkan = (
        f"format={fmt},hwupload,{prefix.format(f='hqdn3d_vulkan')}hqdn3d_vulkan{options},"
        f"hwdownload,format={fmt},format={planar}"
    )
    return cpu, vulkan


def mismatches(cpu: bytes, vulkan: bytes, depth: int, high_bits: bool) -> int:
    """Count samples the Vulkan output gets wrong by the ADR-0004 rule."""
    if len(cpu) != len(vulkan) or not cpu:
        return max(len(cpu), len(vulkan), 1)
    if depth == 8:
        return sum(1 for a, b in zip(cpu, vulkan, strict=True) if a != b)
    top = (1 << depth) - 1
    wrong = 0
    for a, b in zip(array("H", cpu), array("H", vulkan), strict=True):
        want = top if high_bits and a > top else a
        wrong += b != want
    return wrong


def lavfi() -> list[str]:
    return ["-f", "lavfi", "-i", SOURCE]


def rebuild_source(ffmpeg: str, tmp: str, codec: str) -> list[str]:
    """Write the two parts of the rebuild source in codec and return the input options."""
    parts = []
    for index, (width, height) in enumerate(REBUILD_SIZES):
        part = Path(tmp) / f"{index}.nut"
        encode = [
            ffmpeg, "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i",
            f"testsrc2=s={width}x{height}:d=0.32,noise=alls=20:allf=t",
            "-c:v", codec, str(part),
        ]  # fmt: skip
        subprocess.run(encode, capture_output=True, timeout=120, check=True)
        parts.append(f"file '{part}'\n")
    listing = Path(tmp) / "list.txt"
    listing.write_text("".join(parts), encoding="utf-8")
    return ["-f", "concat", "-safe", "0", "-i", str(listing)]


@unittest.skipUnless(FFMPEG and DEVICE, "set VK_FFMPEG and VK_DEVICE")
class Hqdn3dVulkanTest(unittest.TestCase):
    """0023: the Vulkan hqdn3d path must match the CPU path."""

    def compare(self, source: list[str], fmt: str, options: str, prefix: str = "") -> int:
        cpu_chain, vulkan_chain = chains(fmt, options, prefix)
        _, depth, high_bits = FORMATS[fmt]
        return mismatches(run(source, cpu_chain), run(source, vulkan_chain), depth, high_bits)

    def test_vulkan_matches_cpu(self) -> None:
        for fmt in FORMATS:
            for options in OPTIONS:
                with self.subTest(fmt=fmt, options=options):
                    self.assertEqual(self.compare(lavfi(), fmt, options), 0)

    def test_temporal_only_after_command(self) -> None:
        for fmt in ("yuv420p", "p010le", "yuv444p16le"):
            with self.subTest(fmt=fmt):
                self.assertEqual(self.compare(lavfi(), fmt, "", TEMPORAL_ONLY), 0)

    def test_state_resets_on_graph_rebuild(self) -> None:
        cpu_chain, vulkan_chain = chains("yuv420p", "")
        with tempfile.TemporaryDirectory() as tmp:
            source = rebuild_source(FFMPEG, tmp, "mjpeg")
            cpu = run(source, cpu_chain)
            self.assertEqual(len(cpu), REBUILD_BYTES)
            self.assertEqual(mismatches(cpu, run(source, vulkan_chain), 8, False), 0)

    def test_planted_sizeless_rebuild_source_is_refused(self) -> None:
        cpu_chain, _ = chains("yuv420p", "")
        with tempfile.TemporaryDirectory() as tmp:
            source = rebuild_source(FFMPEG, tmp, "ffv1")
            self.assertNotEqual(len(run(source, cpu_chain)), REBUILD_BYTES)

    @unittest.skipUnless(RESET_TOOL, "set HQDN3D_RESET_TOOL")
    def test_state_resets_on_in_graph_resize(self) -> None:
        done = subprocess.run([RESET_TOOL, DEVICE], capture_output=True, timeout=300, check=False)
        self.assertEqual(done.returncode, 0, done.stdout.decode() + done.stderr.decode())

    def test_planted_wrong_strength_is_refused(self) -> None:
        cpu_chain, _ = chains("yuv420p", "")
        _, vulkan_chain = chains("yuv420p", "=luma_spatial=4.5")
        self.assertGreater(
            mismatches(run(lavfi(), cpu_chain), run(lavfi(), vulkan_chain), 8, False), 0
        )


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
