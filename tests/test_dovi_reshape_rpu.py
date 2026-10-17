# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Regression tests for the Dolby Vision metadata after patch 0012 (dovi_reshape).

dovi_reshape turns the profile 5 IPT base layer into BT.2020 PQ pixels. The
profile 5 RPU describes the IPT layer, so it must not reach an encoder with
the reshaped pixels: a player that applies it to them shows a green, purple or
pink picture. The test encodes the reshaped profile 5 clip with libx265 and
checks that the output carries neither a Dolby Vision configuration record nor
an RPU.

The planted wrong case encodes the clip without dovi_reshape and with
dolbyvision=1, which keeps the profile 5 RPU; the same check must find it.

The cases need an FFmpeg built with the series and libx265, and its ffprobe
next to it:

    DV_FFMPEG=/path/to/ffmpeg python3 -m unittest tests.test_dovi_reshape_rpu

Without the variable they are skipped.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

FFMPEG = os.environ.get("DV_FFMPEG", "")
CLIP = Path(__file__).resolve().parent / "fixtures" / "dv5" / "dv5.mp4"
FRAMES = 6
RESHAPE = "dovi_reshape,scale=out_color_matrix=bt2020:out_range=tv,format=yuv420p10le"
X265 = [
    "-c:v", "libx265", "-preset", "ultrafast",
    "-x265-params", "log-level=error:vbv-maxrate=20000:vbv-bufsize=20000",
    "-strict", "unofficial",
]  # fmt: skip
# libx265 opens a Dolby Vision encode only with mastering display data.
MASTER = (
    "log-level=error:vbv-maxrate=20000:vbv-bufsize=20000:"
    "master-display=G(8500,39850)B(6550,2300)R(35400,14600)WP(15635,16450)L(40000000,50)"
)


def encode(out: Path, extra: list[str]) -> None:
    """Encode FRAMES frames of the profile 5 clip with libx265 into out."""
    cmd = [
        FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
        "-i", str(CLIP), "-frames:v", str(FRAMES), *X265, *extra, str(out),
    ]  # fmt: skip
    done = subprocess.run(cmd, capture_output=True, timeout=300, check=False)
    if done.returncode != 0:
        raise AssertionError(f"encode exited {done.returncode}: {done.stderr.decode()}")


def dovi_metadata(path: Path) -> list[str]:
    """Return every Dolby Vision record ffprobe finds in the stream and its frames."""
    ffprobe = str(Path(FFMPEG).with_name("ffprobe"))
    entries = "stream_side_data=side_data_type,dv_profile:frame_side_data=side_data_type"
    cmd = [
        ffprobe, "-v", "error", "-select_streams", "v:0", "-of", "json",
        "-show_entries", entries, "-show_frames", str(path),
    ]  # fmt: skip
    done = subprocess.run(cmd, capture_output=True, timeout=120, check=True)
    probe = json.loads(done.stdout)
    found = []
    for stream in probe.get("streams", []):
        for sd in stream.get("side_data_list", []):
            if "DOVI" in sd.get("side_data_type", ""):
                found.append(f"stream: {sd['side_data_type']} profile {sd.get('dv_profile')}")
    for index, frame in enumerate(probe.get("frames", [])):
        for sd in frame.get("side_data_list", []):
            if "Dolby Vision" in sd.get("side_data_type", ""):
                found.append(f"frame {index}: {sd['side_data_type']}")
    return found


@unittest.skipUnless(FFMPEG, "set DV_FFMPEG")
class ReshapedRpuTest(unittest.TestCase):
    """0012: reshaped pixels never leave with the profile 5 RPU."""

    def test_reshaped_encode_carries_no_dolby_vision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "reshaped.mp4"
            encode(out, ["-vf", RESHAPE])
            self.assertEqual(dovi_metadata(out), [])

    def test_forced_dolby_vision_on_reshaped_frames_is_refused(self) -> None:
        # With dolbyvision=1 libx265 writes the RPU of every frame; a reshaped
        # frame has none, so the encoder must stop instead of writing one.
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "forced.mp4"
            with self.assertRaisesRegex(AssertionError, "without AV_FRAME_DATA_DOVI_METADATA"):
                encode(out, ["-vf", RESHAPE, "-dolbyvision", "1", "-x265-params", MASTER])

    def test_planted_profile5_rpu_is_found(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "planted.mp4"
            encode(out, ["-dolbyvision", "1"])
            found = dovi_metadata(out)
            self.assertIn("stream: DOVI configuration record profile 5", found)
            self.assertIn("frame 0: Dolby Vision RPU Data", found)


if __name__ == "__main__":
    unittest.main()
