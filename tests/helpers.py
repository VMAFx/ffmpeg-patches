# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Shared fixtures and runners for the regression tests."""

from __future__ import annotations

import contextlib
import io
import subprocess
import tempfile
import unittest
from array import array
from collections.abc import Callable
from pathlib import Path

from scripts import series

SOURCE = "testsrc2=s=320x184:d=0.32,noise=alls=20:allf=t"
OPTIONS = ("", "=254:254:254:254", "=0.5:0.5:0.5:0.5", "=luma_spatial=60:luma_tmp=0:chroma_tmp=200")
TEMPORAL_ONLY = "sendcmd=c='0.12 {f} luma_spatial 0; 0.12 {f} chroma_spatial 0',"
REBUILD_SIZES = ((320, 184), (160, 96))
# eight frames of each part as yuv420p
REBUILD_BYTES = sum(8 * w * h * 3 // 2 for w, h in REBUILD_SIZES)
SEMIPLANAR_FORMATS = {
    "nv12": ("yuv420p", 8, False),
    "p010le": ("yuv420p10le", 10, True),
    "p012le": ("yuv420p12le", 12, True),
    "p016le": ("yuv420p16le", 16, True),
}


SIZE = 64
# PQ BT.2020 limited range 10-bit code values (Y, Cb, Cr); the first is pure red.
COLOURS = [(292, 388, 956), (600, 512, 512), (500, 600, 400), (700, 450, 580), (350, 700, 480)]
PQ_INPUT = (
    "setparams=color_primaries=bt2020:color_trc=smpte2084:colorspace=bt2020nc:range=tv"
)  # fmt: skip
CPU = (
    "zscale=t=linear:npl=100,format=gbrpf32le,tonemap=bt2408_hable:peak={peak},"
    "zscale=t=bt709:m=bt709:r=tv,format=yuv420p"
)


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


def rebuild_source(ffmpeg: str, tmp: str, codec: str, pixel_format: str = "") -> list[str]:
    """Write the two parts of the rebuild source in codec and return the input options."""
    parts = []
    for index, (width, height) in enumerate(REBUILD_SIZES):
        part = Path(tmp) / f"{index}.nut"
        encode = [
            ffmpeg, "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i",
            f"testsrc2=s={width}x{height}:d=0.32,noise=alls=20:allf=t{pixel_format}",
            "-c:v", codec, str(part),
        ]  # fmt: skip
        subprocess.run(encode, capture_output=True, timeout=120, check=True)
        parts.append(f"file '{part}'\n")
    listing = Path(tmp) / "list.txt"
    listing.write_text("".join(parts), encoding="utf-8")
    return ["-f", "concat", "-safe", "0", "-i", str(listing)]


def lavfi() -> list[str]:
    return ["-f", "lavfi", "-i", SOURCE]


def run_git(tree: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(tree), "-c", "user.name=Dev", "-c", "user.email=dev@invalid", *args],
        env=series.git_env(),
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return result.stdout.strip()


def write_base(root: Path, commit: str) -> None:
    (root / "base.env").write_text(
        f"FFMPEG_REMOTE=https://example.invalid/up.git\nFFMPEG_TAG=v1\nFFMPEG_COMMIT={commit}\n"
    )


def init_upstream(tree: Path, root: Path, write_file: Callable[[], None]) -> str:
    tree.mkdir()
    root.mkdir()
    run_git(tree, "init", "-q")
    write_file()
    run_git(tree, "add", "lib.c")
    run_git(tree, "commit", "-q", "-m", "base")
    commit = run_git(tree, "rev-parse", "HEAD")
    write_base(root, commit)
    return commit


def capture_main(main: Callable[[list[str]], int], *argv: str) -> tuple[int, str, str]:
    """Run main() and return the exit status, stdout and stderr."""
    with (
        contextlib.redirect_stdout(io.StringIO()) as out,
        contextlib.redirect_stderr(io.StringIO()) as err,
    ):
        status = main(list(argv))
    return status, out.getvalue(), err.getvalue()


def raw_output(done: subprocess.CompletedProcess[bytes], chain: str) -> bytes:
    if done.returncode != 0:
        raise AssertionError(f"{chain} exited {done.returncode}: {done.stderr.decode()}")
    return done.stdout


def filter_frames(
    ffmpeg: str,
    device: tuple[str, str],
    source: list[str],
    chain: str,
    *,
    noautoscale: bool = False,
) -> subprocess.CompletedProcess[bytes]:
    init, name = device
    cmd = [
        ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error",
        "-init_hw_device", init, "-filter_hw_device", name,
        *source, "-vf", chain, *(["-noautoscale"] if noautoscale else []),
        "-f", "rawvideo", "pipe:1",
    ]  # fmt: skip
    return subprocess.run(cmd, capture_output=True, timeout=300, check=False)


def filter_frame(
    ffmpeg: str, device: tuple[str, str], frame: bytes, chain: str, input_filter: tuple[int, str]
) -> subprocess.CompletedProcess[bytes]:
    init, name = device
    size, pq_input = input_filter
    cmd = [
        ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error",
        "-init_hw_device", init, "-filter_hw_device", name,
        "-f", "rawvideo", "-pix_fmt", "yuv420p10le", "-s", f"{size}x{size}", "-i", "pipe:0",
        "-vf", f"{pq_input},{chain}", "-f", "rawvideo", "pipe:1",
    ]  # fmt: skip
    return subprocess.run(cmd, input=frame, capture_output=True, timeout=120, check=False)


def hqdn3d_chains(
    fmt: str, planar: str, options: str, prefix: str, backend: str
) -> tuple[str, str]:
    cpu = f"format={fmt},format={planar},{prefix.format(f='hqdn3d')}hqdn3d{options}"
    upload = "hwupload_cuda" if backend == "cuda" else "hwupload"
    name = f"hqdn3d_{backend}"
    gpu = (
        f"format={fmt},{upload},{prefix.format(f=name)}{name}{options},"
        f"hwdownload,format={fmt},format={planar}"
    )
    return cpu, gpu


def compare_hqdn3d(
    source: list[str],
    chains: tuple[str, str],
    run: Callable[[list[str], str], bytes],
    format_info: tuple[str, int, bool],
) -> int:
    cpu_chain, gpu_chain = chains
    _, depth, high_bits = format_info
    return mismatches(run(source, cpu_chain), run(source, gpu_chain), depth, high_bits)


def assert_formats_match(
    case: unittest.TestCase, compare: Callable[..., int], formats: dict[str, tuple[str, int, bool]]
) -> None:
    for fmt in formats:
        for options in OPTIONS:
            with case.subTest(fmt=fmt, options=options):
                case.assertEqual(compare(lavfi(), fmt, options), 0)


def assert_temporal_match(
    case: unittest.TestCase, compare: Callable[..., int], formats: tuple[str, ...]
) -> None:
    for fmt in formats:
        with case.subTest(fmt=fmt):
            case.assertEqual(compare(lavfi(), fmt, "", TEMPORAL_ONLY), 0)


def assert_rebuild_matches(
    case: unittest.TestCase,
    ffmpeg: str,
    chains: Callable[..., tuple[str, str]],
    run: Callable[[list[str], str], bytes],
) -> None:
    cpu_chain, gpu_chain = chains("yuv420p", "")
    with tempfile.TemporaryDirectory() as tmp:
        source = rebuild_source(ffmpeg, tmp, "mjpeg")
        cpu = run(source, cpu_chain)
        case.assertEqual(len(cpu), REBUILD_BYTES)
        case.assertEqual(mismatches(cpu, run(source, gpu_chain), 8, False), 0)


def run_reset(tool: str, device: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run([tool, device], capture_output=True, timeout=300, check=False)


def assert_ffv1_rebuild_matches(
    case: unittest.TestCase, ffmpeg: str, compare: Callable[..., int]
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        source = rebuild_source(ffmpeg, tmp, "ffv1", ",format=yuv420p")
        case.assertEqual(compare(source, "yuv420p", ""), 0)


def assert_sizeless_rebuild_refused(
    case: unittest.TestCase,
    ffmpeg: str,
    chains: Callable[..., tuple[str, str]],
    run: Callable[[list[str], str], bytes],
) -> None:
    cpu_chain, _ = chains("yuv420p", "")
    with tempfile.TemporaryDirectory() as tmp:
        source = rebuild_source(ffmpeg, tmp, "ffv1")
        case.assertNotEqual(len(run(source, cpu_chain)), REBUILD_BYTES)


def flat_frame(y: int, u: int, v: int) -> bytes:
    """Return one SIZE x SIZE yuv420p10le frame with constant planes."""
    luma = y.to_bytes(2, "little") * (SIZE * SIZE)
    quarter = SIZE * SIZE // 4
    return luma + u.to_bytes(2, "little") * quarter + v.to_bytes(2, "little") * quarter


def centre_yuv(frame: bytes) -> tuple[int, int, int]:
    """Return the centre code values of a SIZE x SIZE yuv420p frame."""
    half = SIZE // 2
    luma = SIZE * SIZE
    chroma = half * (half // 2) + half // 2
    if len(frame) != luma * 3 // 2:
        raise ValueError(f"want {luma * 3 // 2} bytes, got {len(frame)}")
    return frame[half * SIZE + half], frame[luma + chroma], frame[luma + luma // 4 + chroma]


def worst_difference(want: tuple[int, ...], got: tuple[int, ...]) -> int:
    """Return the largest per-component difference of two code value tuples."""
    return max(abs(left - right) for left, right in zip(want, got, strict=True))
