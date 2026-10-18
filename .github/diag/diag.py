# SPDX-FileCopyrightText: 2026 lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Temporary CI diagnostic for the software-device parity step."""

import subprocess
import tempfile
from pathlib import Path

from tests import test_hqdn3d_vulkan as vk


def per_frame(tag: str, cpu: bytes, gpu: bytes) -> None:
    size = 88320
    wrong = [
        sum(1 for x, y in zip(cpu[o : o + size], gpu[o : o + size], strict=False) if x != y)
        for o in range(0, len(cpu), size)
    ]
    print(tag, "len", len(cpu), len(gpu), "wrong per frame", wrong)


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        parts = []
        for index, size in enumerate(("320x184", "160x96")):
            part = Path(tmp) / f"{index}.nut"
            enc = [
                vk.FFMPEG,
                "-nostdin",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"testsrc2=s={size}:d=0.32,noise=alls=20:allf=t,format=yuv420p",
                "-c:v",
                "ffv1",
                str(part),
            ]
            subprocess.run(enc, capture_output=True, timeout=120, check=True)
            parts.append(f"file '{part}'\n")
        listing = Path(tmp) / "list.txt"
        listing.write_text("".join(parts), encoding="utf-8")
        src = ["-f", "concat", "-safe", "0", "-i", str(listing)]
        plain = vk.run(src, "format=yuv420p")
        per_frame(
            "roundtrip", plain, vk.run(src, "format=yuv420p,hwupload,hwdownload,format=yuv420p")
        )
        cpu, gpu = vk.chains("yuv420p", "")
        ref = vk.run(src, cpu)
        for i in range(3):
            per_frame(f"hqdn3d#{i}", ref, vk.run(src, gpu))
        cpu, gpu = vk.chains("yuv420p", "=0:0:6:4")
        per_frame("temporal-only", vk.run(src, cpu), vk.run(src, gpu))
        cpu, gpu = vk.chains("yuv420p", "=4:3:0.001:0.001")
        per_frame("spatial-mostly", vk.run(src, cpu), vk.run(src, gpu))


main()
