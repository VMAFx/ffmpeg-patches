#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""HQDN3D GPU vs CPU quality, parity, and performance measurement harness.

Runs the measurement plan from docs/measurements/hqdn3d-gpu.md:
1. parity: GPU vs CPU sample exactness and max difference across formats and settings.
2. filter-quality: Denoised YUV VMAF and PSNR-Y against clean reference.
3. rate: Rate-distortion curves, achieved bitrate checks, and BD-rate calculation.
4. speed: Multi-run median FPS benchmarks on 1080p content.
5. table: Formats JSON measurement results into Markdown tables.
6. all: Runs parity, filter-quality, rate, and speed in sequence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from array import array
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Ensure repository root is on sys.path for test/tool imports
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.test_hqdn3d_vulkan import mismatches  # noqa: E402

DEFAULT_GRAIN_LEVELS = [10, 20]
DEFAULT_RATES_KBPS = [150, 300, 600, 1200]
DEFAULT_SETTINGS: list[tuple[str, str]] = [
    ("defaults", ""),
    ("luma_spatial=8", "=luma_spatial=8"),
]
DEFAULT_MIN_FREE_MIB = 1024
DEFAULT_GATE_WAIT_S = 600
DEFAULT_GATE_POLL_S = 30
MAX_HOST_LOAD1 = 40.0
MIN_HOST_MEM_AVAILABLE_KIB = 12 * 1024 * 1024
MIN_HOST_SWAP_FREE_KIB = 15 * 1024 * 1024

FORMATS: dict[str, tuple[str, int, bool]] = {
    "yuv420p": ("yuv420p", 8, False),
    "p010le": ("yuv420p10le", 10, True),
}


@dataclass(frozen=True)
class ClipSpec:
    """Specification of a video test clip."""

    name: str
    fps: int
    path: Path
    width: int = 640
    height: int = 360
    frames: int = 48


@dataclass(frozen=True)
class GrainedClip:
    """Specification of a pre-rendered grained clip."""

    clip: ClipSpec
    noise: int
    path: Path


@dataclass(frozen=True)
class ToolPaths:
    """Paths to host binaries and working directory."""

    ffmpeg: str
    decoder_ffmpeg: str
    vmaf: str
    model: str
    bd_rate: str
    workdir: Path


@dataclass(frozen=True)
class ParityCase:
    """Test case configuration for a parity check run."""

    format: str
    backend: str
    setting_label: str
    option: str


@dataclass(frozen=True)
class EncodeParams:
    """Parameters for video encoding and decoding."""

    enc_args: list[str]
    rate_k: int
    grain_off: bool = False


def parse_meminfo(text: str) -> dict[str, int]:
    """Parse /proc/meminfo text into a dictionary of integer KiB values."""
    res: dict[str, int] = {}
    for line in text.splitlines():
        parts = line.split(":")
        if len(parts) == 2:
            key = parts[0].strip()
            val_parts = parts[1].strip().split()
            if val_parts and val_parts[0].isdigit():
                res[key] = int(val_parts[0])
    return res


def parse_loadavg(text: str) -> float:
    """Parse 1-minute load average from /proc/loadavg text."""
    fields = text.strip().split()
    if not fields:
        raise ValueError("Empty loadavg content")
    return float(fields[0])


def check_host_readings(meminfo_text: str, loadavg_text: str) -> tuple[bool, str, dict[str, float]]:
    """Check if host meets load, memory and swap criteria."""
    load1 = parse_loadavg(loadavg_text)
    meminfo = parse_meminfo(meminfo_text)
    mem_avail = meminfo.get("MemAvailable", 0)
    swap_free = meminfo.get("SwapFree", 0)
    readings = {
        "load1": load1,
        "mem_available_kib": float(mem_avail),
        "swap_free_kib": float(swap_free),
    }
    if load1 >= MAX_HOST_LOAD1:
        return False, f"load1 {load1:.2f} >= {MAX_HOST_LOAD1}", readings
    if mem_avail < MIN_HOST_MEM_AVAILABLE_KIB:
        avail_gib = mem_avail / (1024 * 1024)
        return False, f"MemAvailable {avail_gib:.2f} GiB < 12 GiB", readings
    if swap_free < MIN_HOST_SWAP_FREE_KIB:
        free_gib = swap_free / (1024 * 1024)
        return False, f"SwapFree {free_gib:.2f} GiB < 15 GiB", readings
    return True, "Host gate passed", readings


def wait_for_host_gate(
    max_wait_s: float = DEFAULT_GATE_WAIT_S,
    poll_s: float = DEFAULT_GATE_POLL_S,
) -> dict[str, float]:
    """Poll host gate until criteria are met or timeout expires."""
    max_iterations = max(1, int(max_wait_s / poll_s) + 1)
    last_reason = "No check performed"
    for _ in range(max_iterations):
        with open("/proc/meminfo", encoding="utf-8") as f:
            mem_text = f.read()
        with open("/proc/loadavg", encoding="utf-8") as f:
            load_text = f.read()
        ok, reason, readings = check_host_readings(mem_text, load_text)
        if ok:
            return readings
        last_reason = reason
        time.sleep(poll_s)
    raise RuntimeError(f"Host gate wait timeout ({max_wait_s}s): {last_reason}")


def parse_device_mapping(map_args: list[str]) -> dict[str, str]:
    """Parse repeatable or comma-separated device mapping arguments."""
    mapping: dict[str, str] = {}
    for arg in map_args:
        for item in arg.split(","):
            cleaned = item.strip()
            if "=" in cleaned:
                src, dst = cleaned.split("=", 1)
                mapping[src.strip()] = dst.strip()
    return mapping


def parse_vulkan_device_budget(vulkaninfo_text: str, target_gpu: int) -> int | None:
    """Parse device-local heap budget in MiB for target GPU from vulkaninfo text."""
    gpu_pattern = re.compile(rf"^GPU{target_gpu}:", re.MULTILINE)
    match = gpu_pattern.search(vulkaninfo_text)
    if not match:
        return None
    start = match.start()
    next_gpu = re.search(r"^GPU\d+:", vulkaninfo_text[match.end() :], re.MULTILINE)
    end = (match.end() + next_gpu.start()) if next_gpu else len(vulkaninfo_text)
    section = vulkaninfo_text[start:end].split("memoryTypes:", 1)[0]

    heap_chunks = re.split(r"memoryHeaps\[\d+\]:", section)
    for chunk in heap_chunks[1:]:
        if "MEMORY_HEAP_DEVICE_LOCAL_BIT" in chunk:
            b_match = re.search(r"budget\s*=\s*(\d+)", chunk)
            if b_match:
                return int(b_match.group(1)) // (1024 * 1024)
    return None


def resolve_device_to_vulkan(device_str: str, mapping: dict[str, str]) -> int | None:
    """Resolve a backend device string to a Vulkan GPU index via map."""
    resolved = mapping.get(device_str, device_str)
    vk_match = re.search(r"(?:vulkan|vk):?(\d+)", resolved)
    if vk_match:
        return int(vk_match.group(1))
    if resolved.isdigit():
        return int(resolved)
    return None


def query_vulkaninfo_text(timeout: float = 30.0) -> str:
    """Run vulkaninfo command safely and return text output."""
    res = subprocess.run(  # nosec B603 B607 - fixed diagnostic command and bounded timeout.
        ["vulkaninfo"],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if res.returncode != 0:
        return ""
    return res.stdout


def check_device_gate(
    device_str: str,
    mapping: dict[str, str],
    min_free_mib: int = DEFAULT_MIN_FREE_MIB,
    vulkaninfo_text: str | None = None,
) -> tuple[bool, str, int | None]:
    """Check if device has sufficient local memory budget."""
    gpu_idx = resolve_device_to_vulkan(device_str, mapping)
    if gpu_idx is None:
        return False, f"Device {device_str} cannot resolve to Vulkan GPU", None
    v_text = vulkaninfo_text if vulkaninfo_text is not None else query_vulkaninfo_text()
    if not v_text:
        return False, "Failed to query vulkaninfo", None
    budget = parse_vulkan_device_budget(v_text, gpu_idx)
    if budget is None:
        return False, f"GPU{gpu_idx} budget not found in vulkaninfo", None
    if budget < min_free_mib:
        msg = f"GPU{gpu_idx} budget {budget} MiB < {min_free_mib} MiB"
        return False, msg, budget
    return True, f"GPU{gpu_idx} budget {budget} MiB ok", budget


def run_command_line(
    cmd: list[str],
    timeout: float = 300.0,
    use_scope: bool = False,
) -> subprocess.CompletedProcess[bytes]:
    """Execute a command list with timeout and optional systemd scope."""
    full_cmd = (
        ["systemd-run", "--user", "--scope", "--expand-environment=no", "-p", "MemoryHigh=8G", *cmd]
        if use_scope
        else cmd
    )
    try:
        return subprocess.run(  # nosec B603 - caller builds the benchmark argv from validated inputs.
            full_cmd,
            capture_output=True,
            timeout=timeout,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        err = exc.stderr.decode("utf-8", errors="replace")
        raise RuntimeError(f"Command failed ({exc.returncode}): {' '.join(cmd)}\n{err}") from exc


def compute_file_sha256(path: Path) -> str:
    """Compute SHA256 digest of a local file in bounded chunks."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def get_git_commit(repo_root: Path) -> str:
    """Retrieve current git HEAD commit hash of repository."""
    cmd = ["git", "-C", str(repo_root), "rev-parse", "HEAD"]
    return run_command_line(cmd, timeout=10).stdout.decode().strip()


def get_tool_version(cmd: list[str]) -> str:
    """Retrieve first line of version output for a tool."""
    done = run_command_line(cmd, timeout=10)
    # vmaf prints its version on stderr, ffmpeg on stdout.
    lines = (done.stdout or done.stderr).decode().strip().splitlines()
    if not lines:
        raise RuntimeError(f"{cmd[0]} printed no version")
    return lines[0].strip()


def parse_backend(backend: str) -> tuple[str, str, str, str]:
    """Parse backend string into (backend_type, hw_init_arg, filter_dev, filter_name)."""
    parts = backend.split(":", 1)
    b_type = parts[0].strip().lower()
    val = parts[1].strip() if len(parts) > 1 else "0"
    if b_type in ("vulkan", "vk"):
        return "vulkan", f"vulkan=vk:{val}", "vk", "hqdn3d_vulkan"
    if b_type in ("cuda", "cu"):
        return "cuda", f"cuda=cu:{val}", "cu", "hqdn3d_cuda"
    if b_type in ("opencl", "ocl"):
        return "opencl", f"opencl=ocl:{val}", "ocl", "hqdn3d_opencl"
    raise ValueError(f"Unknown backend identifier: {backend}")


def build_filter_chains(fmt: str, filter_name: str, options: str) -> tuple[str, str]:
    """Return CPU and GPU filter chains for format, filter and options."""
    planar = FORMATS[fmt][0]
    cpu = f"format={fmt},format={planar},hqdn3d{options}"
    gpu = f"format={fmt},hwupload,{filter_name}{options},hwdownload,format={fmt},format={planar}"
    return cpu, gpu


def compute_max_diff(cpu: bytes, gpu: bytes, depth: int, high_bits: bool) -> int:
    """Compute maximum absolute sample difference with ADR-0004 clamping."""
    if len(cpu) != len(gpu) or not cpu:
        return -1
    if depth == 8:
        return max(abs(a - b) for a, b in zip(cpu, gpu, strict=True))
    top = (1 << depth) - 1
    max_d = 0
    for a, b in zip(array("H", cpu), array("H", gpu), strict=True):
        want = top if high_bits and a > top else a
        diff = abs(b - want)
        max_d = max(max_d, diff)
    return max_d


def compute_parity(cpu: bytes, gpu: bytes, depth: int, high_bits: bool) -> tuple[int, int]:
    """Count mismatches and compute maximum absolute difference."""
    count = mismatches(cpu, gpu, depth, high_bits)
    max_d = compute_max_diff(cpu, gpu, depth, high_bits)
    return count, max_d


def parse_vmaf_json(json_content: str | dict[str, Any]) -> tuple[float, float, str]:
    """Extract VMAF mean, PSNR-Y mean, and verify backend is CPU."""
    data = json.loads(json_content) if isinstance(json_content, str) else json_content
    backend = data.get("backend_used", "")
    if backend != "cpu":
        raise ValueError(f"vmaf backend_used must be cpu, got: {backend!r}")
    pooled = data.get("pooled_metrics", {})
    vmaf_val = float(pooled.get("vmaf", {}).get("mean", 0.0))
    psnr_val = float(pooled.get("psnr_y", {}).get("mean", 0.0))
    return vmaf_val, psnr_val, backend


def check_rate_deviation(achieved_kbps: float, target_kbps: float) -> tuple[float, bool]:
    """Calculate rate deviation fraction and whether it exceeds 10% threshold."""
    if target_kbps <= 0:
        return 0.0, False
    dev = (achieved_kbps / target_kbps) - 1.0
    return dev, abs(dev) > 0.10


def run_bd_rate(
    bd_rate_script: str,
    baseline_pts: list[dict[str, float]],
    arm_pts: list[dict[str, float]],
    workdir: Path,
) -> dict[str, float]:
    """Execute external bd_rate.py script on baseline vs arm curves."""
    base_file = workdir / "base_curve.json"
    arm_file = workdir / "arm_curve.json"
    with open(base_file, "w", encoding="utf-8") as f:
        json.dump(baseline_pts, f)
    with open(arm_file, "w", encoding="utf-8") as f:
        json.dump(arm_pts, f)
    cmd = [sys.executable, bd_rate_script, str(base_file), str(arm_file)]
    res = subprocess.run(  # nosec B603 - command comes from the validated benchmark configuration.
        cmd, capture_output=True, text=True, timeout=60, check=True
    )
    match = re.search(r"\{.*?\}", res.stdout, re.DOTALL)
    if not match:
        raise ValueError(f"No JSON in bd_rate output: {res.stdout}")
    data = json.loads(match.group(0))
    return {
        "bd_rate_pct": float(data.get("bd_rate_pct", 0.0)),
        "bd_quality": float(data.get("bd_quality", 0.0)),
    }


def rises_with_rate(pts: list[dict[str, float]]) -> bool:
    """True when the score rises strictly with the bitrate (BD-rate is defined)."""
    ordered = sorted(pts, key=lambda p: p["bitrate_kbps"])
    scores = [p["score"] for p in ordered]
    return all(b > a for a, b in zip(scores, scores[1:], strict=False))


def quality_overlap(base: list[dict[str, float]], arm: list[dict[str, float]]) -> list[float]:
    """Score range both curves span, over which BD-rate integrates."""
    lo = max(min(p["score"] for p in base), min(p["score"] for p in arm))
    hi = min(max(p["score"] for p in base), max(p["score"] for p in arm))
    return [lo, hi]


def compute_median_fps(runs: list[float]) -> float:
    """Return median FPS value from a list of run timings."""
    if not runs:
        return 0.0
    s = sorted(runs)
    return s[len(s) // 2]


def ensure_grained_clip(
    ffmpeg: str,
    clip: ClipSpec,
    noise: int,
    workdir: Path,
) -> Path:
    """Generate or retrieve cached grained YUV clip."""
    stem = clip.path.stem
    grained = workdir / f"{stem}_n{noise}.yuv"
    if grained.exists() and grained.stat().st_size > 0:
        return grained
    cmd = [
        ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
        "-f", "rawvideo", "-pix_fmt", "yuv420p", "-s", f"{clip.width}x{clip.height}",
        "-r", str(clip.fps), "-i", str(clip.path),
        "-vf", f"noise=alls={noise}:allf=t+u:all_seed=1",
        "-f", "rawvideo", str(grained),
    ]  # fmt: skip
    run_command_line(cmd, timeout=120)
    return grained


def run_single_parity(
    ffmpeg: str,
    grained_path: Path,
    clip: ClipSpec,
    case: ParityCase,
) -> tuple[int, int, list[str], list[str]]:
    """Run CPU and GPU chains and return (mismatches, max_diff, cpu_cmd, gpu_cmd)."""
    _, hw_init, f_dev, f_name = parse_backend(case.backend)
    cpu_chain, gpu_chain = build_filter_chains(case.format, f_name, case.option)
    src_args = [
        "-f", "rawvideo", "-pix_fmt", "yuv420p",
        "-s", f"{clip.width}x{clip.height}", "-r", str(clip.fps), "-i", str(grained_path),
    ]  # fmt: skip
    cpu_cmd = [
        ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error",
        *src_args, "-vf", cpu_chain, "-f", "rawvideo", "pipe:1",
    ]  # fmt: skip
    gpu_cmd = [
        ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error",
        "-init_hw_device", hw_init, "-filter_hw_device", f_dev,
        *src_args, "-vf", gpu_chain, "-f", "rawvideo", "pipe:1",
    ]  # fmt: skip
    cpu_out = run_command_line(cpu_cmd, timeout=120).stdout
    gpu_out = run_command_line(gpu_cmd, timeout=120).stdout
    _, depth, high_bits = FORMATS[case.format]
    mism, max_d = compute_parity(cpu_out, gpu_out, depth, high_bits)
    return mism, max_d, cpu_cmd, gpu_cmd


def run_parity_stage(
    args: argparse.Namespace,
    clips: list[ClipSpec],
    mapping: dict[str, str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Execute parity measurement matrix across clips, grains, settings, and backends."""
    results: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    workdir = Path(args.workdir)
    backends: list[str] = args.backend or ["vulkan:0", "cuda:0", "opencl:2.0"]
    for clip in clips:
        for n in DEFAULT_GRAIN_LEVELS:
            grained = ensure_grained_clip(args.ffmpeg, clip, n, workdir)
            vulkan_info = query_vulkaninfo_text()
            for set_label, opt in DEFAULT_SETTINGS:
                for fmt in ("yuv420p", "p010le"):
                    for backend in backends:
                        ok, reason, _ = check_device_gate(
                            backend, mapping, args.min_free_mib, vulkan_info
                        )
                        if not ok:
                            skipped.append(
                                {"stage": "parity", "backend": backend, "reason": reason}
                            )
                            continue
                        case = ParityCase(fmt, backend, set_label, opt)
                        mism, max_d, cpu_cmd, gpu_cmd = run_single_parity(
                            args.ffmpeg, grained, clip, case
                        )
                        results.append({
                            "stage": "parity", "clip": clip.name, "fps": clip.fps,
                            "noise": n, "setting": set_label, "format": fmt,
                            "backend": backend, "mismatches": mism, "max_diff": max_d,
                            "argv": gpu_cmd, "cpu_argv": cpu_cmd,
                        })  # fmt: skip
    return results, skipped


def evaluate_vmaf_score(
    tools: ToolPaths,
    ref_yuv: Path,
    dist_yuv: Path,
    clip: ClipSpec,
) -> tuple[float, float, list[str]]:
    """Run VMAF scoring command and return (vmaf, psnr_y, argv)."""
    out_json = tools.workdir / f"vmaf_{os.getpid()}_{time.time_ns()}.json"
    cmd = [
        tools.vmaf, "-r", str(ref_yuv), "-d", str(dist_yuv),
        "-w", str(clip.width), "-h", str(clip.height), "-p", "420", "-b", "8",
        "--backend", "cpu", "-m", f"path={tools.model}",
        "--feature", "psnr", "--threads", "4", "--json",
        "-o", str(out_json), "-q",
    ]  # fmt: skip
    run_command_line(cmd, timeout=180)
    with open(out_json, encoding="utf-8") as f:
        vmaf_s, psnr_s, _ = parse_vmaf_json(f.read())
    out_json.unlink(missing_ok=True)
    return vmaf_s, psnr_s, cmd


def first_free_vulkan(
    backends: list[str],
    mapping: dict[str, str],
    min_free_mib: int,
) -> tuple[str, bool, str]:
    """Return the first Vulkan backend that passes the device gate, or the reasons."""
    reasons: list[str] = []
    candidates = [b for b in backends if parse_backend(b)[0] == "vulkan"] or ["vulkan:0"]
    for backend in candidates:
        ok, reason, _ = check_device_gate(backend, mapping, min_free_mib)
        if ok:
            return backend, True, reason
        reasons.append(f"{backend}: {reason}")
    return ",".join(candidates), False, "; ".join(reasons)


def run_filter_quality_stage(
    tools: ToolPaths,
    clips: list[ClipSpec],
    mapping: dict[str, str],
    backends: list[str],
    min_free_mib: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Execute filter-quality VMAF and PSNR-Y measurement stage."""
    results: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    vk_backend, vk_ok, vk_reason = first_free_vulkan(backends, mapping, min_free_mib)

    for clip in clips:
        for n in DEFAULT_GRAIN_LEVELS:
            grained = ensure_grained_clip(tools.ffmpeg, clip, n, tools.workdir)
            gclip = GrainedClip(clip=clip, noise=n, path=grained)
            v_none, p_none, c_none = evaluate_vmaf_score(tools, clip.path, grained, clip)
            results.append({
                "stage": "filter_quality", "clip": clip.name, "noise": n,
                "arm": "none", "device": "none", "vmaf": v_none,
                "psnr_y": p_none, "argv": c_none,
            })  # fmt: skip
            run_cpu_filter_quality(tools, gclip, results)
            if not vk_ok:
                skipped.append(
                    {"stage": "filter_quality", "device": vk_backend, "reason": vk_reason}
                )
                continue
            run_vulkan_nlmeans_quality(tools, gclip, vk_backend, results)
    return results, skipped


def run_cpu_filter_quality(
    tools: ToolPaths,
    gclip: GrainedClip,
    results: list[dict[str, Any]],
) -> None:
    """Helper to evaluate CPU hqdn3d filter arms."""
    clip = gclip.clip
    for set_label, opt in DEFAULT_SETTINGS:
        out_yuv = tools.workdir / f"{clip.name}_n{gclip.noise}_hqdn3d_{set_label}.yuv"
        cmd = [
            tools.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
            "-f", "rawvideo", "-pix_fmt", "yuv420p", "-s", f"{clip.width}x{clip.height}",
            "-r", str(clip.fps), "-i", str(gclip.path), "-vf", f"hqdn3d{opt}",
            "-f", "rawvideo", str(out_yuv),
        ]  # fmt: skip
        run_command_line(cmd, timeout=120)
        v_hq, p_hq, _ = evaluate_vmaf_score(tools, clip.path, out_yuv, clip)
        results.append({
            "stage": "filter_quality", "clip": clip.name, "noise": gclip.noise,
            "arm": f"hqdn3d_{set_label}", "device": "cpu", "vmaf": v_hq,
            "psnr_y": p_hq, "argv": cmd,
        })  # fmt: skip


def run_vulkan_nlmeans_quality(
    tools: ToolPaths,
    gclip: GrainedClip,
    vk_backend: str,
    results: list[dict[str, Any]],
) -> None:
    """Helper to evaluate Vulkan nlmeans filter arms."""
    clip = gclip.clip
    _, hw_init, f_dev, _ = parse_backend(vk_backend)
    for s_val in (1, 3):
        out_nl = tools.workdir / f"{clip.name}_n{gclip.noise}_nlmeans_s{s_val}.yuv"
        chain = f"format=yuv420p,hwupload,nlmeans_vulkan=s={s_val},hwdownload,format=yuv420p"
        cmd = [
            tools.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
            "-init_hw_device", hw_init, "-filter_hw_device", f_dev,
            "-f", "rawvideo", "-pix_fmt", "yuv420p", "-s", f"{clip.width}x{clip.height}",
            "-r", str(clip.fps), "-i", str(gclip.path), "-vf", chain,
            "-f", "rawvideo", str(out_nl),
        ]  # fmt: skip
        run_command_line(cmd, timeout=180)
        v_nl, p_nl, _ = evaluate_vmaf_score(tools, clip.path, out_nl, clip)
        results.append({
            "stage": "filter_quality", "clip": clip.name, "noise": gclip.noise,
            "arm": f"nlmeans_vulkan_s{s_val}", "device": vk_backend,
            "vmaf": v_nl, "psnr_y": p_nl, "argv": cmd,
        })  # fmt: skip


def encode_and_decode_point(
    tools: ToolPaths,
    input_yuv: Path,
    clip: ClipSpec,
    params: EncodeParams,
) -> tuple[float, bool, Path, list[str]]:
    """Encode stream to IVF, check achieved bitrate, and decode to YUV."""
    ivf_out = tools.workdir / f"enc_{os.getpid()}_{time.time_ns()}.ivf"
    enc_cmd = [
        tools.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
        "-f", "rawvideo", "-pix_fmt", "yuv420p", "-s", f"{clip.width}x{clip.height}",
        "-r", str(clip.fps), "-i", str(input_yuv),
        *params.enc_args, "-b:v", f"{params.rate_k}k", str(ivf_out),
    ]  # fmt: skip
    run_command_line(enc_cmd, timeout=180)
    dur = clip.frames / clip.fps
    achieved = (ivf_out.stat().st_size * 8.0) / (dur * 1000.0)
    _, flagged = check_rate_deviation(achieved, float(params.rate_k))

    dec_yuv = tools.workdir / f"dec_{os.getpid()}_{time.time_ns()}.yuv"
    dec_flags = ["-export_side_data", "film_grain"] if params.grain_off else []
    dec_cmd = [
        tools.decoder_ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
        "-c:v", "libdav1d", *dec_flags, "-i", str(ivf_out),
        "-f", "rawvideo", str(dec_yuv),
    ]  # fmt: skip
    run_command_line(dec_cmd, timeout=120)
    ivf_out.unlink(missing_ok=True)
    return achieved, flagged, dec_yuv, enc_cmd


def score_rate_point(
    tools: ToolPaths,
    input_yuv: Path,
    clip: ClipSpec,
    params: EncodeParams,
    curve: list[dict[str, float]],
) -> tuple[float, bool, float, list[str]]:
    """Encode, score and record one rate-distortion point."""
    achieved, flagged, dec_yuv, cmd = encode_and_decode_point(tools, input_yuv, clip, params)
    v_score, _, _ = evaluate_vmaf_score(tools, clip.path, dec_yuv, clip)
    dec_yuv.unlink(missing_ok=True)
    curve.append({"bitrate_kbps": achieved, "score": v_score})
    return achieved, flagged, v_score, cmd


def run_rate_stage(
    tools: ToolPaths,
    clips: list[ClipSpec],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Run rate-distortion sweep, VMAF scoring and BD-rate calculation."""
    results: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for clip in clips:
        for n in DEFAULT_GRAIN_LEVELS:
            grained = ensure_grained_clip(tools.ffmpeg, clip, n, tools.workdir)
            gclip = GrainedClip(clip=clip, noise=n, path=grained)
            filter_yuvs = {
                "none": grained,
                "hqdn3d_defaults": tools.workdir / f"{clip.name}_n{n}_hqdn3d_defaults.yuv",
                "hqdn3d_luma8": tools.workdir / f"{clip.name}_n{n}_hqdn3d_luma_spatial=8.yuv",
            }
            curves: dict[str, list[dict[str, float]]] = {}
            for enc_name, enc_opts in (
                ("svt", ["-c:v", "libsvtav1", "-preset", "8"]),
                ("aom", ["-c:v", "libaom-av1", "-cpu-used", "6"]),
            ):
                for arm, in_path in filter_yuvs.items():
                    if not in_path.exists():
                        raise RuntimeError(f"{in_path} missing: run filter-quality first")
                    key = f"{enc_name}_{arm}"
                    curves[key] = []
                    for rate in DEFAULT_RATES_KBPS:
                        params = EncodeParams(enc_args=enc_opts, rate_k=rate)
                        achieved, flagged, v_score, cmd = score_rate_point(
                            tools, in_path, clip, params, curves[key]
                        )
                        results.append({
                            "stage": "rate", "clip": clip.name, "noise": n, "arm": arm,
                            "encoder": enc_name, "grain_synth": "off", "target_kbps": rate,
                            "achieved_kbps": achieved, "flagged": flagged, "vmaf": v_score,
                            "argv": cmd,
                        })  # fmt: skip
            run_encoder_arms_and_bd(tools, gclip, curves, results)
    return results, skipped


def run_encoder_arms_and_bd(
    tools: ToolPaths,
    gclip: GrainedClip,
    curves: dict[str, list[dict[str, float]]],
    results: list[dict[str, Any]],
) -> None:
    """Helper to run encoder-specific denoiser arms and compute BD-rate deltas."""
    clip = gclip.clip
    enc_configs = [
        (
            "svt_filmgrain",
            "svt",
            [
                "-c:v",
                "libsvtav1",
                "-preset",
                "8",
                "-svtav1-params",
                "film-grain=10:film-grain-denoise=1",
            ],
        ),
        (
            "aom_denoise",
            "aom",
            ["-c:v", "libaom-av1", "-cpu-used", "6", "-denoise-noise-level", "10"],
        ),
    ]
    for arm_name, base_enc, enc_opts in enc_configs:
        for g_off in (False, True):
            g_label = "off" if g_off else "on"
            key = f"{arm_name}_{g_label}"
            curves[key] = []
            for rate in DEFAULT_RATES_KBPS:
                params = EncodeParams(enc_args=enc_opts, rate_k=rate, grain_off=g_off)
                achieved, flagged, v_score, cmd = score_rate_point(
                    tools, gclip.path, clip, params, curves[key]
                )
                results.append({
                    "stage": "rate", "clip": clip.name, "noise": gclip.noise, "arm": arm_name,
                    "encoder": base_enc, "grain_synth": g_label, "target_kbps": rate,
                    "achieved_kbps": achieved, "flagged": flagged, "vmaf": v_score,
                    "argv": cmd,
                })  # fmt: skip
    if tools.bd_rate and Path(tools.bd_rate).exists():
        compute_bd_deltas(tools, gclip, curves, results)


def compute_bd_deltas(
    tools: ToolPaths,
    gclip: GrainedClip,
    curves: dict[str, list[dict[str, float]]],
    results: list[dict[str, Any]],
) -> None:
    """Compute and record BD-rate vs baseline none arm."""
    for enc in ("svt", "aom"):
        base_curve = curves.get(f"{enc}_none")
        if not base_curve or len(base_curve) < 4:
            continue
        for key, pts in curves.items():
            if key == f"{enc}_none" or not key.startswith(enc) or len(pts) < 4:
                continue
            bd: dict[str, Any] = run_bd_rate(tools.bd_rate, base_curve, pts, tools.workdir)
            bd["q_overlap"] = quality_overlap(base_curve, pts)
            if not (rises_with_rate(base_curve) and rises_with_rate(pts)):
                bd["bd_rate_pct"] = None
                bd["bd_rate_note"] = "n/a: score falls as bitrate rises"
            results.append(
                {
                    "stage": "bd_rate",
                    "clip": gclip.clip.name,
                    "noise": gclip.noise,
                    "arm": key,
                    "encoder": enc,
                    "bd_rate_pct": bd["bd_rate_pct"],
                    "bd_quality": bd["bd_quality"],
                    "bd_rate_note": bd.get("bd_rate_note", ""),
                    "q_overlap": bd["q_overlap"],
                }
            )


def ensure_speed_clip(ffmpeg: str, workdir: Path, w: int = 1920, h: int = 1080) -> Path:
    """Generate or retrieve 1080p speed benchmark clip."""
    clip = workdir / "speed_1080p_n20.yuv"
    if clip.exists() and clip.stat().st_size >= 240 * w * h * 3 // 2:
        return clip
    cmd = [
        ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"testsrc2=s={w}x{h}:d=8:r=30",
        "-vf", "noise=alls=20:allf=t+u:all_seed=1",
        "-pix_fmt", "yuv420p", "-f", "rawvideo", str(clip),
    ]  # fmt: skip
    run_command_line(cmd, timeout=300)
    return clip


SPEED_FRAMES = 240
SPEED_TIMEOUT_S = 120


def format_speed_fps(row: dict[str, Any]) -> str:
    """Median FPS cell, or the upper bound of a run that timed out."""
    if row.get("median_fps") is None:
        return f"< {SPEED_FRAMES / SPEED_TIMEOUT_S:.1f} (run > {SPEED_TIMEOUT_S} s)"
    return f"{row['median_fps']:.1f}"


def measure_filter_speed_fps(
    ffmpeg: str,
    input_yuv: Path,
    hw_args: list[str],
    chain: str,
) -> tuple[float | None, list[float], list[str]]:
    """Execute 3 runs of a filter chain and return (median_fps, runs, argv).

    A run past SPEED_TIMEOUT_S ends the series: median_fps is None, which the
    table prints as the bound SPEED_FRAMES / SPEED_TIMEOUT_S.
    """
    cmd = [
        ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error",
        *hw_args, "-f", "rawvideo", "-pix_fmt", "yuv420p",
        "-s", "1920x1080", "-r", "30", "-i", str(input_yuv),
        "-vf", chain, "-f", "null", "-",
    ]  # fmt: skip
    med, runs = time_runs(cmd)
    return med, runs, cmd


def time_runs(cmd: list[str]) -> tuple[float | None, list[float]]:
    """Three timed runs of cmd; (None, runs so far) once one passes SPEED_TIMEOUT_S."""
    runs: list[float] = []
    for _ in range(3):
        t0 = time.perf_counter()
        try:
            run_command_line(cmd, timeout=SPEED_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            return None, runs
        wall = time.perf_counter() - t0
        runs.append(SPEED_FRAMES / max(wall, 1e-6))
    return compute_median_fps(runs), runs


def run_speed_stage(
    args: argparse.Namespace,
    mapping: dict[str, str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Execute speed benchmarks for baseline transfer, filters, and encoders."""
    results: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    workdir = Path(args.workdir)
    speed_clip = ensure_speed_clip(args.ffmpeg, workdir)

    for set_label, opt in DEFAULT_SETTINGS:
        med, runs, cmd = measure_filter_speed_fps(
            args.ffmpeg, speed_clip, [], f"format=yuv420p,hqdn3d{opt}"
        )
        results.append({
            "stage": "speed", "task": f"hqdn3d_{set_label}", "device": "cpu",
            "median_fps": med, "runs": runs, "argv": cmd,
        })  # fmt: skip

    for b in args.backend or ["vulkan:0", "cuda:0", "opencl:2.0"]:
        ok, reason, _ = check_device_gate(b, mapping, args.min_free_mib)
        if not ok:
            skipped.append({"stage": "speed", "device": b, "reason": reason})
            continue
        run_device_speed_arms(args.ffmpeg, speed_clip, b, results)
    run_encoder_speed(args.ffmpeg, speed_clip, results)
    return results, skipped


def run_device_speed_arms(
    ffmpeg: str,
    speed_clip: Path,
    backend: str,
    results: list[dict[str, Any]],
) -> None:
    """Helper to benchmark transfer baseline, hqdn3d and nlmeans on device."""
    b_type, hw_init, f_dev, f_name = parse_backend(backend)
    hw_args = ["-init_hw_device", hw_init, "-filter_hw_device", f_dev]
    base_chain = "format=yuv420p,hwupload,hwdownload,format=yuv420p"
    m_base, r_base, c_base = measure_filter_speed_fps(ffmpeg, speed_clip, hw_args, base_chain)
    results.append({
        "stage": "speed", "task": "transfer_baseline", "device": backend,
        "median_fps": m_base, "runs": r_base, "argv": c_base,
    })  # fmt: skip
    for set_label, opt in DEFAULT_SETTINGS:
        chain = f"format=yuv420p,hwupload,{f_name}{opt},hwdownload,format=yuv420p"
        med, runs, cmd = measure_filter_speed_fps(ffmpeg, speed_clip, hw_args, chain)
        results.append({
            "stage": "speed", "task": f"{f_name}_{set_label}", "device": backend,
            "median_fps": med, "runs": runs, "argv": cmd,
        })  # fmt: skip
    if b_type == "vulkan":
        for s_val in (1, 3):
            chain = f"format=yuv420p,hwupload,nlmeans_vulkan=s={s_val},hwdownload,format=yuv420p"
            med, runs, cmd = measure_filter_speed_fps(ffmpeg, speed_clip, hw_args, chain)
            results.append({
                "stage": "speed", "task": f"nlmeans_vulkan_s{s_val}", "device": backend,
                "median_fps": med, "runs": runs, "argv": cmd,
            })  # fmt: skip


def run_encoder_speed(
    ffmpeg: str,
    speed_clip: Path,
    results: list[dict[str, Any]],
) -> None:
    """Measure encoder speed at 1200k with and without denoisers."""
    configs = [
        ("svt_plain", ["-c:v", "libsvtav1", "-preset", "8", "-b:v", "1200k"]),
        (
            "svt_denoise",
            [
                "-c:v",
                "libsvtav1",
                "-preset",
                "8",
                "-svtav1-params",
                "film-grain=10:film-grain-denoise=1",
                "-b:v",
                "1200k",
            ],
        ),
        ("aom_plain", ["-c:v", "libaom-av1", "-cpu-used", "6", "-b:v", "1200k"]),
        (
            "aom_denoise",
            ["-c:v", "libaom-av1", "-cpu-used", "6", "-denoise-noise-level", "10", "-b:v", "1200k"],
        ),
    ]
    for name, enc_args in configs:
        cmd = [
            ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error",
            "-f", "rawvideo", "-pix_fmt", "yuv420p", "-s", "1920x1080",
            "-r", "30", "-i", str(speed_clip), *enc_args, "-f", "null", "-",
        ]  # fmt: skip
        med, runs = time_runs(cmd)
        results.append(
            {
                "stage": "speed",
                "task": f"encoder_{name}_1200k",
                "device": "cpu",
                "median_fps": med,
                "runs": runs,
                "argv": cmd,
            }
        )


def render_markdown_table(headers: list[str], rows: list[list[str]]) -> str:
    """Render a clean GitHub-flavored markdown table."""
    widths = [len(h) for h in headers]
    for r in rows:
        for i, val in enumerate(r):
            widths[i] = max(widths[i], len(val))
    header_line = "| " + " | ".join(h.ljust(widths[i]) for i, h in enumerate(headers)) + " |"
    sep_line = "| " + " | ".join("-" * widths[i] for i in range(len(headers))) + " |"
    data_lines = [
        "| " + " | ".join(r[i].ljust(widths[i]) for i in range(len(headers))) + " |" for r in rows
    ]
    return "\n".join([header_line, sep_line, *data_lines])


def render_parity_table(results: list[dict[str, Any]]) -> str:
    """Format parity measurement results into markdown table."""
    headers = ["Clip", "Noise", "Setting", "Format", "Backend", "Mismatches", "Max Diff"]
    rows: list[list[str]] = []
    for r in results:
        if r.get("stage") == "parity":
            rows.append([
                str(r.get("clip", "")), str(r.get("noise", "")),
                str(r.get("setting", "")), str(r.get("format", "")),
                str(r.get("backend", "")), str(r.get("mismatches", "")),
                str(r.get("max_diff", "")),
            ])  # fmt: skip
    return render_markdown_table(headers, rows) if rows else "No parity results"


def render_filter_quality_table(results: list[dict[str, Any]]) -> str:
    """Format filter-quality results into markdown table."""
    headers = ["Clip", "Noise", "Arm", "Device", "VMAF", "PSNR-Y"]
    rows: list[list[str]] = []
    for r in results:
        if r.get("stage") == "filter_quality":
            vmaf_val = f"{r.get('vmaf', 0.0):.2f}"
            psnr_val = f"{r.get('psnr_y', 0.0):.2f}"
            rows.append([
                str(r.get("clip", "")), str(r.get("noise", "")),
                str(r.get("arm", "")), str(r.get("device", "")),
                vmaf_val, psnr_val,
            ])  # fmt: skip
    return render_markdown_table(headers, rows) if rows else "No filter quality results"


def format_overlap(overlap: list[float] | None) -> str:
    """VMAF range shared by both curves, or n/a when they do not overlap."""
    if not overlap or overlap[1] <= overlap[0]:
        return "none"
    return f"{overlap[0]:.2f}-{overlap[1]:.2f}"


def format_bd_rate(row: dict[str, Any]) -> str:
    """BD-rate cell: the percentage, or the reason it is undefined."""
    if row.get("bd_rate_pct") is None:
        return str(row.get("bd_rate_note") or "n/a")
    return f"{row['bd_rate_pct']:+.2f}"


def render_rate_table(results: list[dict[str, Any]]) -> str:
    """Format rate and BD-rate results into markdown table."""
    headers = [
        "Clip", "Noise", "Arm", "Encoder", "Grain synthesis", "Target (kbps)",
        "Achieved (kbps)", "VMAF", "Flagged",
    ]  # fmt: skip
    rows: list[list[str]] = []
    for r in results:
        if r.get("stage") == "rate":
            flag = "FLAG" if r.get("flagged") else "ok"
            rows.append([
                str(r.get("clip", "")), str(r.get("noise", "")),
                str(r.get("arm", "")), str(r.get("encoder", "")),
                str(r.get("grain_synth", "")), str(r.get("target_kbps", "")),
                f"{r.get('achieved_kbps', 0.0):.1f}",
                f"{r.get('vmaf', 0.0):.2f}", flag,
            ])  # fmt: skip
    table = render_markdown_table(headers, rows) if rows else "No rate results"

    bd_headers = [
        "Clip", "Noise", "Arm", "Encoder", "VMAF overlap", "BD-Rate (%)", "BD-Quality",
    ]  # fmt: skip
    bd_rows: list[list[str]] = []
    for r in results:
        if r.get("stage") == "bd_rate":
            bd_rows.append([
                str(r.get("clip", "")), str(r.get("noise", "")),
                str(r.get("arm", "")), str(r.get("encoder", "")),
                format_overlap(r.get("q_overlap")), format_bd_rate(r),
                f"{r.get('bd_quality', 0.0):+.3f}",
            ])  # fmt: skip
    if bd_rows:
        table += "\n\n### Bjøntegaard Delta (BD-Rate / BD-Quality)\n\n"
        table += render_markdown_table(bd_headers, bd_rows)
    return table


def render_speed_table(results: list[dict[str, Any]]) -> str:
    """Format speed benchmark results into markdown table."""
    headers = ["Task", "Device", "Median FPS", "Run 1", "Run 2", "Run 3"]
    rows: list[list[str]] = []
    for r in results:
        if r.get("stage") == "speed":
            runs = r.get("runs", [])
            r1 = f"{runs[0]:.1f}" if len(runs) > 0 else "-"
            r2 = f"{runs[1]:.1f}" if len(runs) > 1 else "-"
            r3 = f"{runs[2]:.1f}" if len(runs) > 2 else "-"
            rows.append([
                str(r.get("task", "")), str(r.get("device", "")),
                format_speed_fps(r), r1, r2, r3,
            ])  # fmt: skip
    return render_markdown_table(headers, rows) if rows else "No speed results"


def render_all_tables(data: dict[str, Any]) -> str:
    """Render all markdown tables from data dictionary."""
    results = data.get("results", [])
    sections = [
        "## Measure 1: Parity Bound\n\n" + render_parity_table(results),
        "## Measure 2: Denoiser Quality (Pre-encode)\n\n" + render_filter_quality_table(results),
        "## Measure 3: Rate & BD-Rate\n\n" + render_rate_table(results),
        "## Measure 4: Speed\n\n" + render_speed_table(results),
    ]
    return "\n\n".join(sections)


def parse_clip_arg(clip_str: str) -> tuple[str, int]:
    """Parse clip argument in name:fps format."""
    parts = clip_str.split(":", 1)
    name = parts[0].strip()
    fps = int(parts[1].strip()) if len(parts) > 1 and parts[1].strip().isdigit() else 30
    return name, fps


def resolve_clips(clip_args: list[str] | None, corpus_dir: str | None) -> list[ClipSpec]:
    """Resolve clip specifications to names, fps, and file paths."""
    clips: list[ClipSpec] = []
    raw_clips = clip_args or ["bbb:30", "netflix-bar:60"]
    corpus = Path(corpus_dir) if corpus_dir else Path(".")
    for c in raw_clips:
        name, fps = parse_clip_arg(c)
        clips.append(ClipSpec(name=name, fps=fps, path=corpus / f"{name}.yuv"))
    return clips


def collect_meta(args: argparse.Namespace, clips: list[ClipSpec]) -> dict[str, Any]:
    """Collect metadata block for test run."""
    clip_hashes = {}
    for clip in clips:
        if clip.path.exists():
            clip_hashes[clip.name] = compute_file_sha256(clip.path)
    ff_path = Path(args.ffmpeg) if args.ffmpeg else Path("ffmpeg")
    return {
        "series_commit": get_git_commit(REPO_ROOT),
        "ffmpeg_version": (
            get_tool_version([args.ffmpeg, "-version"]) if args.ffmpeg else "unknown"
        ),
        "ffmpeg_sha256": compute_file_sha256(ff_path) if ff_path.exists() else "unknown",
        "vmaf_version": get_tool_version([args.vmaf, "--version"]) if args.vmaf else "unknown",
        "clip_sha256s": clip_hashes,
        "start_time": datetime.now(UTC).isoformat(),
    }


def build_arg_parser() -> argparse.ArgumentParser:
    """Build command line argument parser for measure_hqdn3d.py."""
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--ffmpeg", default="", help="Path to FFmpeg binary with patch series")
    p.add_argument(
        "--decoder-ffmpeg", default="/usr/bin/ffmpeg", help="Path to FFmpeg with libdav1d"
    )
    p.add_argument("--vmaf", default="", help="Path to VMAFx vmaf binary")
    p.add_argument("--model", default="", help="Path to VMAF JSON model")
    p.add_argument("--bd-rate", default="", help="Path to bd_rate.py")
    p.add_argument("--corpus", default="", help="Corpus directory containing clean YUV clips")
    p.add_argument("--clip", action="append", help="Clips in name:fps format (e.g. bbb:30)")
    p.add_argument("--workdir", default="./measure_workdir", help="Work dir for temporary YUVs")
    p.add_argument("--json", default="measure_results.json", help="JSON results path")
    p.add_argument(
        "--gate-wait", type=float, default=DEFAULT_GATE_WAIT_S, help="Host gate max wait s"
    )
    p.add_argument("--gate-poll", type=float, default=DEFAULT_GATE_POLL_S, help="Host gate poll s")
    p.add_argument(
        "--min-free-mib", type=int, default=DEFAULT_MIN_FREE_MIB, help="Min device free MiB"
    )
    p.add_argument("--map", action="append", default=[], help="Device map (e.g. cuda:0=vk:0)")
    p.add_argument("--backend", action="append", default=[], help="Backend devices (e.g. vulkan:0)")

    sub = p.add_subparsers(dest="subcommand", required=True)
    sub.add_parser("parity", help="Run parity comparison matrix")
    sub.add_parser("filter-quality", help="Run filter quality pre-encode evaluation")
    sub.add_parser("rate", help="Run rate-distortion sweep and BD-rate calculation")
    sub.add_parser("speed", help="Run speed benchmark runs on 1080p clip")
    sub.add_parser("all", help="Run all measurement stages in sequence")
    sub.add_parser("table", help="Format JSON results into markdown tables on stdout")
    return p


def run_stages(
    args: argparse.Namespace,
    tools: ToolPaths,
    clips: list[ClipSpec],
    data: dict[str, Any],
) -> None:
    """Run the requested stages, each after a host gate, appending to data."""
    mapping = parse_device_mapping(args.map)
    stages = {
        "parity": lambda: run_parity_stage(args, clips, mapping),
        "filter-quality": lambda: run_filter_quality_stage(
            tools, clips, mapping, args.backend, args.min_free_mib
        ),
        "rate": lambda: run_rate_stage(tools, clips),
        "speed": lambda: run_speed_stage(args, mapping),
    }
    for stage, run in stages.items():
        if args.subcommand not in (stage, "all"):
            continue
        readings = wait_for_host_gate(args.gate_wait, args.gate_poll)
        stamp = {"stage": stage, "time": datetime.now(UTC).isoformat()}
        data["meta"].setdefault("host_gates", []).append({**stamp, **readings})
        results, skipped = run()
        data["results"].extend(results)
        data["skipped"].extend(skipped)


def load_or_start(args: argparse.Namespace, clips: list[ClipSpec]) -> dict[str, Any]:
    """Load the results file to append to, refusing one from another FFmpeg build."""
    meta = collect_meta(args, clips)
    out_path = Path(args.json)
    if not out_path.exists():
        return {"meta": meta, "results": [], "skipped": []}
    with open(out_path, encoding="utf-8") as f:
        data = json.load(f)
    for key in ("ffmpeg_sha256", "series_commit", "clip_sha256s"):
        if data["meta"].get(key) != meta[key]:
            raise RuntimeError(f"{out_path}: {key} differs from this run; use a new --json")
    return data


def main(argv: list[str] | None = None) -> int:
    """Main CLI entrypoint."""
    args = build_arg_parser().parse_args(argv)
    if args.subcommand == "table":
        with open(args.json, encoding="utf-8") as f:
            print(render_all_tables(json.load(f)))
        return 0

    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    clips = resolve_clips(args.clip, args.corpus)
    tools = ToolPaths(
        ffmpeg=args.ffmpeg,
        decoder_ffmpeg=args.decoder_ffmpeg,
        vmaf=args.vmaf,
        model=args.model,
        bd_rate=args.bd_rate,
        workdir=workdir,
    )
    data = load_or_start(args, clips)
    run_stages(args, tools, clips, data)
    data["meta"]["end_time"] = datetime.now(UTC).isoformat()
    with open(args.json, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
