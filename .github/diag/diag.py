# SPDX-FileCopyrightText: 2026 lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Temporary CI diagnostic for the software-device parity step."""

import subprocess
import tempfile
from pathlib import Path

from tests import test_hqdn3d_vulkan as vk


def per_frame(cpu: bytes, gpu: bytes, sizes: list[int]) -> None:
    print("len cpu", len(cpu), "gpu", len(gpu))
    off = 0
    for i, size in enumerate(sizes):
        a, b = cpu[off : off + size], gpu[off : off + size]
        print("frame", i, "size", size, "wrong", sum(1 for x, y in zip(a, b, strict=False) if x != y))
        off += size


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        for size in ("320x184", "160x96"):
            src = ["-f", "lavfi", "-i", f"testsrc2=s={size}:d=0.32,noise=alls=20:allf=t"]
            cpu, gpu = vk.chains("yuv420p", "")
            w, h = (int(v) for v in size.split("x"))
            per_frame(vk.run(src, cpu), vk.run(src, gpu), [w * h * 3 // 2] * 8)
        parts = []
        for index, size in enumerate(("320x184", "160x96")):
            part = Path(tmp) / f"{index}.nut"
            enc = [vk.FFMPEG, "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i",
                   f"testsrc2=s={size}:d=0.32,noise=alls=20:allf=t,format=yuv420p", "-c:v", "ffv1", str(part)]
            subprocess.run(enc, capture_output=True, timeout=120, check=True)
            parts.append(f"file '{part}'\n")
        listing = Path(tmp) / "list.txt"
        listing.write_text("".join(parts), encoding="utf-8")
        src = ["-f", "concat", "-safe", "0", "-i", str(listing)]
        cpu, gpu = vk.chains("yuv420p", "")
        per_frame(vk.run(src, cpu), vk.run(src, gpu), [88320] * 8 + [23040] * 8)


main()
