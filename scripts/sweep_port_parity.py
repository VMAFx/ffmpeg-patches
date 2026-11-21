#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Compare CPU sample normalization in vf_*.c with linked Vulkan shader loaders."""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.sweep_vulkan_features import (  # noqa: E402
    SHADER_PATH,
    diff_names,
    linked_shaders,
    owners_for_shaders,
)
from scripts.variant_sweep import (  # noqa: E402
    MAX_REPORT_BYTES,
    REPORT_TABLES,
    check_deadline,
    check_limit,
    git,
    mask_literals,
    read_sources,
)

TABLE = REPORT_TABLES["port-parity"]
MAX_PAIRS = 64
FILTER_PAIR = re.compile(r"libavfilter/vf_(\w+)\.c\Z")
VULKAN_PAIR = re.compile(r"libavfilter/vf_(\w+)_vulkan\.c\Z")
SAMPLE_READ = re.compile(
    r"(?:src\s*\[[^\]]+\]|AV_RN16[A-Z]?\s*\(|AV_RL16\s*\(|AV_RB16\s*\()",
    re.ASCII,
)
MASK = re.compile(r"&\s*0x[0-9A-Fa-f]+[uUlL]?", re.ASCII)
SHIFT = re.compile(r">>\s*(?:\([^)]+\)|\w+)", re.ASCII)
CLIP = re.compile(r"\b(?:av_clip|CLIP)\w*\s*\(", re.ASCII)
CLAMP = re.compile(r"\bclamp\s*\(", re.ASCII)
SHADER_LOAD = re.compile(r"\b(?:imageLoad|texelFetch)\s*\(", re.ASCII)


def pair_name(path: str) -> str | None:
    """Return the filter stem for a CPU or Vulkan C path."""
    if match := VULKAN_PAIR.fullmatch(path):
        return match[1]
    if match := FILTER_PAIR.fullmatch(path):
        return match[1]
    return None


def collect_pairs(
    directory: str, head: str, names: set[str], deadline: float
) -> dict[str, tuple[str, str, bool]]:
    """Map filter stems to CPU/Vulkan pairs and whether the diff touched them."""
    shaders = {path for path in names if SHADER_PATH.fullmatch(path)}
    stems: dict[str, bool] = {}
    for path in names:
        if stem := pair_name(path):
            stems[stem] = True
    for path in owners_for_shaders(directory, head, shaders, deadline):
        if stem := pair_name(path):
            stems[stem] = True
    check_limit("filter pair count", len(stems), MAX_PAIRS)
    pairs: dict[str, tuple[str, str, bool]] = {}
    for stem in sorted(stems):
        cpu = f"libavfilter/vf_{stem}.c"
        vk = f"libavfilter/vf_{stem}_vulkan.c"
        pairs[stem] = (cpu, vk, stems[stem])
    return pairs


def normalization_kind(statement: str) -> str | None:
    """Classify one statement as mask, shift, clip, or clamp normalization."""
    if CLIP.search(statement):
        return "clip"
    if CLAMP.search(statement):
        return "clamp"
    if MASK.search(statement):
        return "mask"
    if SHIFT.search(statement):
        return "shift"
    return None


def cpu_sites(source: str) -> list[tuple[int, str]]:
    """List sample-load normalization sites in one CPU filter source."""
    sites: list[tuple[int, str]] = []
    masked = mask_literals(source)
    for line_number, line in enumerate(masked.splitlines(), 1):
        if not SAMPLE_READ.search(line):
            continue
        kind = normalization_kind(line)
        if kind is not None:
            sites.append((line_number, kind))
    return sites


def shader_site(shader_path: str, source: str, kind: str) -> str | None:
    """Return the first shader file:line that applies the same normalization kind."""
    checks = {
        "mask": MASK,
        "shift": SHIFT,
        "clip": CLIP,
        "clamp": CLAMP,
    }
    pattern = checks.get(kind)
    if pattern is None:
        return None
    body = mask_literals(source).splitlines()
    for line_number in range(1, len(body) + 1):
        if not SHADER_LOAD.search(body[line_number - 1]):
            continue
        window = "\n".join(body[max(0, line_number - 4) : min(len(body), line_number + 6)])
        if pattern.search(window):
            return f"{shader_path}:{line_number}"
    return None


def first_shader_match(
    directory: str,
    head: str,
    shaders: list[str],
    kind: str,
    deadline: float,
) -> str | None:
    """Search sorted linked shaders and return the first matching normalization site."""
    for shader_path in sorted(shaders):
        source = git(
            directory,
            "show",
            f"{head}:{shader_path}",
            deadline=deadline,
            maximum=2 * 1024 * 1024,
            limit_name="shader source size",
        )
        site = shader_site(shader_path, source, kind)
        if site is not None:
            return site
    return None


def sweep(directory: str, base: str, head: str, deadline: float) -> str:
    """Render port-parity rows for CPU/Vulkan filter pairs touched by the diff."""
    names = diff_names(directory, base, head, deadline)
    pairs = collect_pairs(directory, head, names, deadline)
    touched_pairs = {stem: paths for stem, paths in pairs.items() if paths[2]}
    if not touched_pairs:
        return "\n".join(
            [
                "class: port-parity",
                f"head: {head}",
                "",
                TABLE,
                "| --- | --- | --- | --- |",
                "no hits",
                "",
            ]
        )
    lines = ["class: port-parity", f"head: {head}", "", TABLE, "| --- | --- | --- | --- |"]
    count = 0
    size = len(("\n".join(lines) + "\n").encode())
    for _stem, (cpu_path, vk_path, _touched) in touched_pairs.items():
        check_deadline(deadline)
        sources: dict[str, str] = {}
        read_sources(directory, head, {cpu_path, vk_path}, sources, deadline)
        if cpu_path not in sources or vk_path not in sources:
            continue
        shaders = linked_shaders(sources[vk_path])
        for line_number, kind in cpu_sites(sources[cpu_path]):
            site = first_shader_match(directory, head, shaders, kind, deadline)
            shader_col = site if site is not None else "none"
            row = f"| {cpu_path}:{line_number} | {kind} | {shader_col} | TODO |"
            size += len((row + "\n").encode())
            check_limit("report size", size, MAX_REPORT_BYTES)
            lines.append(row)
            count += 1
    if not count:
        lines.append("no hits")
    check_deadline(deadline)
    report = "\n".join(lines) + "\n"
    check_limit("report size", len(report.encode()), MAX_REPORT_BYTES)
    return report
