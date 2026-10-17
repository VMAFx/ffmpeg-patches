# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Parity of FFmpeg tone-map paths against vf_libplacebo (ADR-0002).

Runs one reference filter chain and any number of candidate chains on the
same clip, then reports per candidate:

- mean, 95th percentile and maximum CIEDE2000 to the reference, computed in
  BT.709 primaries, gamma 2.4, D65 on every 30th frame;
- wall time of a full decode-filter run into the null muxer;
- peak VRAM above idle, sampled with nvidia-smi when it is present. Without
  it the column says "not measured"; it is never guessed.

usage:
    python3 scripts/tonemap_parity.py --ffmpeg ./ffmpeg --clip dv5.mp4 \\
        --ref 'libplacebo=...' --cand ocl='format=p010le,hwupload,...' \\
        [--pre ocl='-init_hw_device opencl=ocl -filter_hw_device ocl'] \\
        [--json out.json] [--bar 2.3]

Exit 1 when a candidate's mean is above --bar, so the script works as a gate.
"""

from __future__ import annotations

import argparse
import json
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

STEP = 30
RUN_TIMEOUT_S = 1800
PROBE_TIMEOUT_S = 60
MAX_SAMPLES = 10_000
M709 = np.array([[0.4124, 0.3576, 0.1805], [0.2126, 0.7152, 0.0722], [0.0193, 0.1192, 0.9505]])
D65 = np.array([0.95047, 1.0, 1.08883])
TO_RGB = (
    "setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709:range=tv,"
    "scale=out_color_matrix=bt709:out_range=pc,format=rgb24"
)


def lab(rgb: np.ndarray) -> np.ndarray:
    lin = (rgb.astype(np.float64) / 255.0) ** 2.4
    xyz = lin.reshape(-1, 3) @ M709.T / D65
    eps = (6 / 29) ** 3
    f = np.where(xyz > eps, np.cbrt(xyz), xyz / (3 * (6 / 29) ** 2) + 4 / 29)
    return np.stack([116 * f[:, 1] - 16, 500 * (f[:, 0] - f[:, 1]), 200 * (f[:, 1] - f[:, 2])], 1)


def _hue_terms(h1p, h2p, c1p, c2p):
    dh = h2p - h1p
    dh = np.where(dh > 180, dh - 360, np.where(dh < -180, dh + 360, dh))
    dh = np.where(c1p * c2p == 0, 0, dh)
    hs = h1p + h2p
    near = np.abs(h1p - h2p) <= 180
    hpm = np.where(
        c1p * c2p == 0,
        hs,
        np.where(near, hs / 2, np.where(hs < 360, (hs + 360) / 2, (hs - 360) / 2)),
    )
    return dh, hpm


def de2000(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    l1, a1, b1 = a.T
    l2, a2, b2 = b.T
    cm = (np.hypot(a1, b1) + np.hypot(a2, b2)) / 2
    g = 0.5 * (1 - np.sqrt(cm**7 / (cm**7 + 25.0**7)))
    a1p, a2p = (1 + g) * a1, (1 + g) * a2
    c1p, c2p = np.hypot(a1p, b1), np.hypot(a2p, b2)
    h1p = np.degrees(np.arctan2(b1, a1p)) % 360
    h2p = np.degrees(np.arctan2(b2, a2p)) % 360
    dh, hpm = _hue_terms(h1p, h2p, c1p, c2p)
    dhp = 2 * np.sqrt(c1p * c2p) * np.sin(np.radians(dh / 2))
    lpm, cpm = (l1 + l2) / 2, (c1p + c2p) / 2
    t = (
        1
        - 0.17 * np.cos(np.radians(hpm - 30))
        + 0.24 * np.cos(np.radians(2 * hpm))
        + 0.32 * np.cos(np.radians(3 * hpm + 6))
        - 0.20 * np.cos(np.radians(4 * hpm - 63))
    )
    dth = 30 * np.exp(-(((hpm - 275) / 25) ** 2))
    rc = 2 * np.sqrt(cpm**7 / (cpm**7 + 25.0**7))
    sl = 1 + 0.015 * (lpm - 50) ** 2 / np.sqrt(20 + (lpm - 50) ** 2)
    sc, sh = 1 + 0.045 * cpm, 1 + 0.015 * cpm * t
    rt = -np.sin(np.radians(2 * dth)) * rc
    dl, dc = (l2 - l1) / sl, (c2p - c1p) / sc
    return np.sqrt(dl**2 + dc**2 + (dhp / sh) ** 2 + rt * dc * (dhp / sh))


def self_check() -> None:
    """Sharma, Wu, Dalal 2005, test pairs 1 and 17; refuses a broken formula."""
    pairs = [
        ([50.0, 2.6772, -79.7751], [50.0, 0.0, -82.7485], 2.0425),
        ([50.0, 2.5, 0.0], [73.0, 25.0, -18.0], 27.1492),
    ]
    for p, q, want in pairs:
        got = de2000(np.array([p]), np.array([q]))[0]
        if abs(got - want) > 1e-4:
            raise SystemExit(f"CIEDE2000 self-check failed: {got:.4f} != {want}")


def frame_size(ffprobe: str, clip: str) -> tuple[int, int]:
    out = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "csv=p=0",
            clip,
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=PROBE_TIMEOUT_S,
    ).stdout.strip()
    w, h = out.split(",")[:2]
    return int(w), int(h)


def dump_samples(ffmpeg: str, pre: list[str], clip: str, vf: str, out: Path) -> None:
    chain = f"{vf},select='not(mod(n\\,{STEP}))',{TO_RGB}"
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        *pre,
        "-i",
        clip,
        "-vf",
        chain,
        "-fps_mode",
        "passthrough",
        "-f",
        "rawvideo",
        str(out),
    ]
    subprocess.run(cmd, check=True, timeout=RUN_TIMEOUT_S)


def load(path: Path, w: int, h: int) -> np.ndarray:
    raw = np.fromfile(path, np.uint8)
    n = raw.size // (w * h * 3)
    if n < 1 or n > MAX_SAMPLES or raw.size != n * w * h * 3:
        raise SystemExit(f"{path}: {raw.size} bytes is not a whole number of {w}x{h} rgb24 frames")
    return raw.reshape(n, h, w, 3)


def vram_mib() -> int | None:
    if not shutil.which("nvidia-smi"):
        return None
    out = subprocess.run(
        ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits", "-i", "0"],
        capture_output=True,
        text=True,
        check=True,
        timeout=PROBE_TIMEOUT_S,
    ).stdout
    return int(out.split()[0])


def timed_run(ffmpeg: str, pre: list[str], clip: str, vf: str, work: Path) -> dict:
    """Wall time of a full run; peak VRAM from nvidia-smi sampled every 100 ms."""
    idle = vram_mib()
    log = work / "vram.txt"
    smi = None
    if idle is not None:
        with log.open("w") as fh:
            smi = subprocess.Popen(
                [
                    "nvidia-smi",
                    "--query-gpu=memory.used",
                    "--format=csv,noheader,nounits",
                    "-i",
                    "0",
                    "-lms",
                    "100",
                ],
                stdout=fh,
            )
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-nostats",
        "-loglevel",
        "error",
        *pre,
        "-i",
        clip,
        "-vf",
        vf,
        "-f",
        "null",
        "-",
    ]
    t0 = time.monotonic()
    rc = subprocess.run(cmd, check=False, timeout=RUN_TIMEOUT_S).returncode
    wall = time.monotonic() - t0
    peak = None
    if smi is not None:
        smi.terminate()
        smi.wait(timeout=PROBE_TIMEOUT_S)
        peak = max((int(x) for x in log.read_text().split() if x.isdigit()), default=None)
    above = None if peak is None or idle is None else peak - idle
    return {
        "rc": rc,
        "wall_s": round(wall, 3),
        "vram_above_idle_mib": "not measured" if above is None else above,
    }


def compare(ref: np.ndarray, cand: np.ndarray) -> dict:
    if ref.shape != cand.shape:
        raise SystemExit(f"sample shapes differ: {ref.shape} vs {cand.shape}")
    d = np.concatenate([de2000(lab(ref[i]), lab(cand[i])) for i in range(ref.shape[0])])
    return {
        "frames": int(ref.shape[0]),
        "mean": round(float(d.mean()), 3),
        "p95": round(float(np.percentile(d, 95)), 3),
        "max": round(float(d.max()), 3),
        "share_over_2_3": round(float(np.mean(d > 2.3)), 4),
    }


def pairs(items: list[str], what: str) -> dict[str, str]:
    out = {}
    for item in items:
        name, sep, value = item.partition("=")
        if not sep or not name:
            raise SystemExit(f"--{what} wants name=value, got {item!r}")
        out[name] = value
    return out


def parse(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--ffmpeg", required=True)
    ap.add_argument("--ffprobe", default="ffprobe")
    ap.add_argument("--clip", required=True)
    ap.add_argument("--ref", required=True, help="reference filter chain (vf_libplacebo)")
    ap.add_argument("--cand", action="append", default=[], help="name=filter chain; repeatable")
    ap.add_argument(
        "--pre", action="append", default=[], help="name=input options; 'ref' for the reference"
    )
    ap.add_argument("--bar", type=float, default=2.3)
    ap.add_argument("--no-timing", action="store_true")
    ap.add_argument("--json")
    return ap.parse_args(argv)


@dataclass
class Session:
    """What every path of one run shares: the options, the scratch folder, the frame size."""

    args: argparse.Namespace
    work: Path
    size: tuple[int, int]


def measure(run: Session, name: str, vf: str, pre: list[str]):
    path = run.work / f"{name}.rgb"
    dump_samples(run.args.ffmpeg, pre, run.args.clip, vf, path)
    row: dict = {"vf": vf, "pre": pre}
    if not run.args.no_timing:
        row.update(timed_run(run.args.ffmpeg, pre, run.args.clip, vf, run.work))
    return row, load(path, *run.size)


def candidate(run: Session, name: str, vf: str, pre: list[str], ref: np.ndarray) -> dict:
    """A path FFmpeg refuses to run is a failed path with its exit status, not a crash."""
    try:
        row, got = measure(run, name, vf, pre)
    except subprocess.CalledProcessError as exc:
        return {"vf": vf, "pre": pre, "error": f"ffmpeg exit {exc.returncode}", "pass": False}
    row.update(compare(ref, got))
    row["pass"] = row["mean"] <= run.args.bar
    return row


def main(argv: list[str]) -> int:
    args = parse(argv)
    self_check()
    cands, pres = pairs(args.cand, "cand"), pairs(args.pre, "pre")
    size = frame_size(args.ffprobe, args.clip)
    report: dict = {"clip": args.clip, "step": STEP, "bar": args.bar, "paths": {}}
    with tempfile.TemporaryDirectory(prefix="tonemap-parity-") as tmp:
        run = Session(args, Path(tmp), size)
        ref_row, ref = measure(run, "ref", args.ref, shlex.split(pres.get("ref", "")))
        report["paths"]["ref"] = ref_row
        for name, vf in cands.items():
            pre = shlex.split(pres.get(name, ""))
            report["paths"][name] = candidate(run, name, vf, pre, ref)
    for name, row in report["paths"].items():
        print(name, " ".join(f"{k}={v}" for k, v in row.items() if k not in ("vf", "pre")))
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2) + "\n")
    return 0 if all(r.get("pass", True) for r in report["paths"].values()) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
