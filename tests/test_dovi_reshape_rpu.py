# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Regression tests for the Dolby Vision metadata after patches 0011 and 0022 (dovi_reshape).

dovi_reshape turns the profile 5 IPT base layer into BT.2020 PQ pixels. The
profile 5 RPU describes the IPT layer, so it must not reach an encoder with
the reshaped pixels: a player that applies it to them shows a green, purple or
pink picture. The test encodes the reshaped profile 5 clip with libx265 and
checks that the output carries neither a Dolby Vision configuration record nor
an RPU.

The planted wrong case encodes the clip without dovi_reshape and with
dolbyvision=1, which keeps the profile 5 RPU; the same check must find it.

With rpu=p81 (0022) the filter rewrites the RPU to profile 8.1 for the
reshaped pixels and drops the level 5 active area. Those cases give the clip
a level 5 block with dovi_tool, encode it and read the output RPU back with
dovi_tool: profile 8, no level 5, and a profile 8.1 configuration record.
Their planted wrong case encodes the same input without dovi_reshape; the same
checks must find profile 5 and the level 5 block.

The cases need an FFmpeg built with the series and libx265, its ffprobe next
to it, and dovi_tool 2.3.4 for the 0022 cases:

    DV_FFMPEG=/path/to/ffmpeg DV_DOVI_TOOL=/path/to/dovi_tool \
        python3 -m unittest tests.test_dovi_reshape_rpu

Without the variables they are skipped.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

FFMPEG = os.environ.get("DV_FFMPEG", "")
DOVI_TOOL = os.environ.get("DV_DOVI_TOOL", "")
CLIP = Path(__file__).resolve().parent / "fixtures" / "dv5" / "dv5.mp4"
FRAMES = 6
RESHAPE = "dovi_reshape,scale=out_color_matrix=bt2020:out_range=tv,format=yuv420p10le"
P81 = RESHAPE.replace("dovi_reshape", "dovi_reshape=rpu=p81", 1)
# dovi_tool editor input: a 40 row active area offset at top and bottom of every frame.
L5_EDIT = {
    "active_area": {
        "presets": [{"id": 0, "left": 0, "right": 0, "top": 40, "bottom": 40}],
        "edits": {"all": 0},
    }
}
L5_SET = "top=40, bottom=40, left=0, right=0"
L5_NONE = "top=N/A, bottom=N/A, left=N/A, right=N/A"
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


def run(cmd: list[str]) -> str:
    """Run cmd and return its stdout; a non-zero exit raises with its stderr."""
    done = subprocess.run(  # nosec B603 - test argv uses explicit fixture tool paths and values.
        cmd, capture_output=True, timeout=300, check=False
    )
    if done.returncode != 0:
        tool = Path(cmd[0]).name
        raise AssertionError(f"{tool} exited {done.returncode}: {done.stderr.decode()}")
    return done.stdout.decode()


def encode(out: Path, extra: list[str], src: Path = CLIP) -> None:
    """Encode FRAMES frames of the profile 5 clip with libx265 into out."""
    run([
        FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
        "-i", str(src), "-frames:v", str(FRAMES), *X265, *extra, str(out),
    ])  # fmt: skip


def annexb(src: Path, out: Path) -> Path:
    """Copy the HEVC stream of src into a raw Annex B file, the form dovi_tool reads."""
    run([
        FFMPEG, "-nostdin", "-loglevel", "error", "-y", "-i", str(src),
        "-c", "copy", "-bsf:v", "hevc_mp4toannexb", "-f", "hevc", str(out),
    ])  # fmt: skip
    return out


def level5_clip(tmp: Path) -> Path:
    """Write the profile 5 clip as raw HEVC whose RPUs carry the L5_EDIT active area."""
    src = annexb(CLIP, tmp / "src.hevc")
    edit = tmp / "l5.json"
    edit.write_text(json.dumps(L5_EDIT))
    rpu, edited, out = tmp / "src.bin", tmp / "l5.bin", tmp / "l5.hevc"
    run([DOVI_TOOL, "extract-rpu", str(src), "-o", str(rpu)])
    run([DOVI_TOOL, "editor", "-i", str(rpu), "-j", str(edit), "-o", str(edited)])
    run([DOVI_TOOL, "inject-rpu", "-i", str(src), "--rpu-in", str(edited), "-o", str(out)])
    return out


def rpu_summary(stream: Path) -> dict[str, str]:
    """Return dovi_tool's summary of the RPUs in a raw HEVC stream as key -> value."""
    rpu = stream.with_suffix(".rpu")
    run([DOVI_TOOL, "extract-rpu", str(stream), "-o", str(rpu)])
    summary = {}
    for line in run([DOVI_TOOL, "info", "-s", "-i", str(rpu)]).splitlines():
        key, _, value = line.strip().partition(": ")
        summary[key] = value
    return summary


def dovi_metadata(path: Path) -> list[str]:
    """Return every Dolby Vision record ffprobe finds in the stream and its frames."""
    ffprobe = str(Path(FFMPEG).with_name("ffprobe"))
    entries = (
        "stream_side_data=side_data_type,dv_profile,dv_bl_signal_compatibility_id"
        ":frame_side_data=side_data_type"
    )
    cmd = [
        ffprobe, "-v", "error", "-select_streams", "v:0", "-of", "json",
        "-show_entries", entries, "-show_frames", str(path),
    ]  # fmt: skip
    done = subprocess.run(  # nosec B603 - test argv uses explicit fixture tool paths and values.
        cmd, capture_output=True, timeout=120, check=True
    )
    probe = json.loads(done.stdout)
    found = []
    for stream in probe.get("streams", []):
        for sd in stream.get("side_data_list", []):
            if "DOVI" in sd.get("side_data_type", ""):
                profile = f"{sd.get('dv_profile')}.{sd.get('dv_bl_signal_compatibility_id')}"
                found.append(f"stream: {sd['side_data_type']} profile {profile}")
    for index, frame in enumerate(probe.get("frames", [])):
        for sd in frame.get("side_data_list", []):
            if "Dolby Vision" in sd.get("side_data_type", ""):
                found.append(f"frame {index}: {sd['side_data_type']}")
    return found


@unittest.skipUnless(FFMPEG, "set DV_FFMPEG")
class ReshapedRpuTest(unittest.TestCase):
    """0011: reshaped pixels never leave with the profile 5 RPU."""

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
            self.assertIn("stream: DOVI configuration record profile 5.0", found)
            self.assertIn("frame 0: Dolby Vision RPU Data", found)


@unittest.skipUnless(FFMPEG and DOVI_TOOL, "set DV_FFMPEG and DV_DOVI_TOOL")
class Profile81RpuTest(unittest.TestCase):
    """0022: rpu=p81 writes a profile 8.1 RPU without the level 5 active area."""

    def test_p81_encode_is_profile_81_without_level5(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src = level5_clip(Path(tmp))
            self.assertEqual(rpu_summary(src)["L5 offsets"], L5_SET)
            out = Path(tmp) / "p81.mp4"
            encode(out, ["-vf", P81, "-dolbyvision", "1", "-x265-params", MASTER], src)
            self.assertIn("stream: DOVI configuration record profile 8.1", dovi_metadata(out))
            summary = rpu_summary(annexb(out, Path(tmp) / "p81.hevc"))
            self.assertEqual(summary["Profile"], "8")
            self.assertEqual(summary["L5 offsets"], L5_NONE)

    def test_p81_without_mastering_display_is_refused(self) -> None:
        # The clip carries no mastering display data, so libx265 cannot open.
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "bare.mp4"
            with self.assertRaisesRegex(AssertionError, "8.1 requires Mastering display"):
                encode(out, ["-vf", P81, "-dolbyvision", "1"])

    def test_planted_profile5_with_level5_is_found(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "planted.mp4"
            encode(out, ["-dolbyvision", "1", "-x265-params", MASTER], level5_clip(Path(tmp)))
            self.assertIn("stream: DOVI configuration record profile 5.0", dovi_metadata(out))
            summary = rpu_summary(annexb(out, Path(tmp) / "planted.hevc"))
            self.assertEqual(summary["Profile"], "5")
            self.assertEqual(summary["L5 offsets"], L5_SET)


if __name__ == "__main__":
    unittest.main()
