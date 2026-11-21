#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Compare Vulkan feature gates in libavfilter/vf_*_vulkan.c against linked SPIR-V needs."""

from __future__ import annotations

import re
import subprocess  # nosec B404 - fixed tool argv from allow-list, bounded timeout.
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.variant_sweep import (  # noqa: E402
    MAX_REPORT_BYTES,
    REPORT_TABLES,
    check_deadline,
    check_limit,
    git,
    mask_literals,
    read_sources,
)

TABLE = REPORT_TABLES["vulkan-features"]
GLSLANG = Path("/usr/bin/glslangValidator")
SPIRV_DIS = Path("/usr/bin/spirv-dis")
SPIRV_VAL = Path("/usr/bin/spirv-val")
VK_XML = Path("/usr/share/vulkan/registry/vk.xml")
GLSLANG_ARGS = ("-V", "--target-env", "spirv1.6", "--glsl-version", "460", "-S", "comp")
MAX_VULKAN_FILES = 64
MAX_SHADERS = 64
MAX_TOOL_BYTES = 8 * 1024 * 1024
SHADER_SYMBOL = re.compile(r"ff_(\w+)_comp_spv_data", re.ASCII)
FEATURE_READ = re.compile(r"->([A-Za-z]\w*)", re.ASCII)
VULKAN_C = re.compile(r"libavfilter/vf_.+_vulkan\.c\Z")
SHADER_PATH = re.compile(r"libavfilter/vulkan/.+\.comp\.glsl\Z")


class VulkanMapState:
    """Hold lazily loaded vk.xml maps for one process."""

    loaded = False
    allowlist: frozenset[str] = frozenset()
    capability_map: dict[str, set[str]] = {}
    feature_allowlist: frozenset[str] = frozenset()
    derivable: frozenset[str] = frozenset()


MAP_STATE = VulkanMapState()


class VulkanFeatureSweepError(RuntimeError):
    """Raised when Vulkan sweep prerequisites or shader compilation fail."""


class FeatureSite(NamedTuple):
    """Hold one feature gate read site in a Vulkan filter C file."""

    line: int
    feature: str


class ShaderNeeds(NamedTuple):
    """Hold derived shader feature requirements for one linked shader."""

    path: str
    features: set[str]
    evidence: dict[str, str]


def feature_allowlist_from_xml(root: ET.Element) -> frozenset[str]:
    """Collect VkBool32 members from VkPhysicalDeviceFeatures and Features2 extensions."""
    allow: set[str] = {"scalarBlockLayout"}
    for struct in root.iter("type"):
        if struct.get("category") != "struct":
            continue
        name = struct.get("name", "")
        extends = struct.get("structextends", "")
        if name != "VkPhysicalDeviceFeatures" and "VkPhysicalDeviceFeatures2" not in extends:
            continue
        for member in struct.findall("member"):
            kind = member.find("type")
            label = member.find("name")
            if kind is not None and kind.text == "VkBool32" and label is not None and label.text:
                allow.add(label.text)
    return frozenset(allow)


def derivable_features(cap_map: dict[str, set[str]]) -> frozenset[str]:
    """Return feature names derivable from SPIR-V capabilities, indexing, or scalar layout."""
    features: set[str] = {
        "scalarBlockLayout",
        "shaderStorageImageArrayDynamicIndexing",
        "shaderStorageImageArrayNonUniformIndexing",
    }
    for mapped in cap_map.values():
        features |= mapped
    return frozenset(features)


def load_maps(xml_path: Path) -> tuple[frozenset[str], dict[str, set[str]], frozenset[str]]:
    """Build SPIR-V capability map and Vk feature allow-lists from vk.xml."""
    root = ET.parse(xml_path).getroot()
    allow: set[str] = set()
    cap_map: dict[str, set[str]] = {}
    for cap in root.iter("spirvcapability"):
        name = cap.get("name")
        if not name:
            continue
        features: set[str] = set()
        for enable in cap.findall("enable"):
            feature = enable.get("feature")
            if feature:
                features.add(feature)
                allow.add(feature)
        if features:
            cap_map[name] = features
    feature_allow = feature_allowlist_from_xml(root)
    return frozenset(allow), cap_map, feature_allow


def ensure_maps() -> None:
    """Load vk.xml maps once on the first non-empty sweep scope."""
    if MAP_STATE.loaded:
        return
    if not VK_XML.is_file():
        raise VulkanFeatureSweepError(f"missing vk.xml: {VK_XML}")
    allowlist, capability_map, feature_allowlist = load_maps(VK_XML)
    MAP_STATE.allowlist = allowlist
    MAP_STATE.capability_map = capability_map
    MAP_STATE.feature_allowlist = feature_allowlist
    MAP_STATE.derivable = derivable_features(capability_map)
    MAP_STATE.loaded = True


def run_tool(argv: list[str], deadline: float, maximum: int, label: str) -> str:
    """Run one allow-listed external tool with a bounded deadline and output size."""
    timeout = min(120, check_deadline(deadline))
    if not Path(argv[0]).is_file():
        raise VulkanFeatureSweepError(f"missing tool: {argv[0]}")
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        result = subprocess.run(  # nosec B603 - argv built from fixed tool paths only.
            argv,
            stdout=stdout,
            stderr=stderr,
            timeout=timeout,
            check=False,
        )
        check_deadline(deadline)
        if result.returncode:
            stderr.seek(0)
            detail = stderr.read(4096).decode("utf-8", errors="replace")
            raise VulkanFeatureSweepError(f"{label} failed: {detail.strip()}")
        stdout.seek(0, 2)
        size = stdout.tell()
        check_limit(label, size, maximum)
        stdout.seek(0)
        return stdout.read(maximum + 1).decode("utf-8", errors="replace")


def linked_shaders(c_source: str) -> list[str]:
    """Return head-commit shader paths referenced by ff_*_comp_spv_data symbols."""
    paths = []
    for match in SHADER_SYMBOL.finditer(c_source):
        paths.append(f"libavfilter/vulkan/{match[1]}.comp.glsl")
    return paths


def diff_names(directory: str, base: str, head: str, deadline: float) -> set[str]:
    """Return libavfilter paths changed between base and head."""
    text = git(
        directory,
        "diff",
        "--no-ext-diff",
        "--no-textconv",
        "--no-color",
        "--name-only",
        base,
        head,
        "--",
        "libavfilter/",
        deadline=deadline,
    )
    return {line for line in text.splitlines() if line}


def shader_stem(path: str) -> str | None:
    """Return the ff_<stem>_comp_spv_data stem for one shader path."""
    name = Path(path).name
    if name.endswith(".comp.glsl"):
        return name[: -len(".comp.glsl")]
    return None


def owners_for_shaders(directory: str, head: str, shaders: set[str], deadline: float) -> set[str]:
    """Locate Vulkan C files that link changed shaders at head."""
    owners: set[str] = set()
    for shader in sorted(shaders):
        stem = shader_stem(shader)
        if stem is None:
            continue
        listing = git(
            directory,
            "grep",
            "-l",
            f"ff_{stem}_comp_spv_data",
            head,
            "--",
            "libavfilter/*_vulkan.c",
            deadline=deadline,
            missing_ok=True,
        )
        owners.update(line for line in listing.splitlines() if VULKAN_C.fullmatch(line))
    check_limit("vulkan file count", len(owners), MAX_VULKAN_FILES)
    return owners


def collect_targets(
    directory: str, head: str, names: set[str], deadline: float
) -> dict[str, list[str]]:
    """Map each in-scope Vulkan C file to linked shader paths at head."""
    vulkan = {path for path in names if VULKAN_C.fullmatch(path)}
    shaders = {path for path in names if SHADER_PATH.fullmatch(path)}
    vulkan |= owners_for_shaders(directory, head, shaders, deadline)
    if not vulkan:
        return {}
    sources: dict[str, str] = {}
    read_sources(directory, head, vulkan, sources, deadline)
    targets = {path: linked_shaders(source) for path, source in sources.items()}
    check_limit("shader count", sum(len(items) for items in targets.values()), MAX_SHADERS)
    return targets


def c_feature_reads(source: str) -> list[FeatureSite]:
    """Collect VkPhysicalDeviceFeatures gate reads from masked C source."""
    sites: list[FeatureSite] = []
    for line_number, line in enumerate(mask_literals(source).splitlines(), 1):
        for match in FEATURE_READ.finditer(line):
            feature = match[1]
            if feature in MAP_STATE.feature_allowlist:
                sites.append(FeatureSite(line_number, feature))
    return sites


def compile_shader(shader_text: str, work: Path, deadline: float) -> Path:
    """Compile one GLSL shader to SPIR-V in a temporary directory."""
    glsl = work / "shader.comp.glsl"
    spv = work / "shader.spv"
    glsl.write_text(shader_text, encoding="utf-8")
    argv = [str(GLSLANG), *GLSLANG_ARGS, "-o", str(spv), str(glsl)]
    run_tool(argv, deadline, MAX_TOOL_BYTES, "glslangValidator")
    if not spv.is_file():
        raise VulkanFeatureSweepError("glslangValidator produced no SPIR-V output")
    return spv


def spirv_capabilities(disasm: str) -> set[str]:
    """Parse OpCapability names from spirv-dis output."""
    return {match[1] for match in re.finditer(r"OpCapability (\w+)", disasm)}


def spirv_needs_scalar(spv: Path, deadline: float) -> bool:
    """Return whether spirv-val requires scalar-block-layout for this module."""
    base = [str(SPIRV_VAL), "--target-env", "vulkan1.3", str(spv)]
    scalar = [*base, "--scalar-block-layout"]
    timeout = min(120, check_deadline(deadline))
    for argv in (base, scalar):
        result = subprocess.run(  # nosec B603 - fixed spirv-val argv, bounded sweep deadline.
            argv,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        check_deadline(deadline)
        if argv is base:
            default_rc = result.returncode
        else:
            return default_rc != 0 and result.returncode == 0
    return False


def dynamic_storage_index(disasm: str) -> tuple[bool, bool]:
    """Detect dynamic and non-uniform storage-image array indexing in SPIR-V text."""
    assignments = {match[1]: match[2] for match in re.finditer(r"(%\w+) = (Op\w+)", disasm)}
    constants = {name for name, op in assignments.items() if op == "OpConstant"}
    for match in re.finditer(r"(%\w+) = OpCopyObject \S+ (%\w+)", disasm):
        if match[2].startswith("%int_") or match[2] in constants:
            constants.add(match[1])
    nonuniform_ids = {match[1] for match in re.finditer(r"OpDecorate (%\w+) NonUniform", disasm)}
    array_vars = {
        match[1] for match in re.finditer(r"(%\w+) = OpVariable %_ptr_UniformConstant_", disasm)
    }
    dynamic = False
    nonuniform = False
    for line in disasm.splitlines():
        chain = re.search(r"(%\w+) = OpAccessChain \S+ (%\w+)(?: (%\w+))?", line)
        if chain is None:
            continue
        base = chain[2]
        index = chain[3]
        if index is None or base not in array_vars:
            continue
        if index not in constants and not index.startswith("%int_"):
            dynamic = True
        if index in nonuniform_ids:
            nonuniform = True
    return dynamic, nonuniform


def shader_requirements(
    directory: str, head: str, shader_path: str, work: Path, deadline: float
) -> ShaderNeeds:
    """Derive required Vk features for one linked shader at head."""
    source = git(
        directory,
        "show",
        f"{head}:{shader_path}",
        deadline=deadline,
        maximum=MAX_TOOL_BYTES,
        limit_name="shader source size",
    )
    spv = compile_shader(source, work, deadline)
    disasm = run_tool([str(SPIRV_DIS), str(spv)], deadline, MAX_TOOL_BYTES, "spirv-dis")
    features: set[str] = set()
    evidence: dict[str, str] = {}
    for capability in spirv_capabilities(disasm):
        for feature in MAP_STATE.capability_map.get(capability, ()):
            features.add(feature)
            evidence.setdefault(feature, f"capability:{capability}")
    dynamic, nonuniform = dynamic_storage_index(disasm)
    if dynamic:
        features.add("shaderStorageImageArrayDynamicIndexing")
        evidence.setdefault(
            "shaderStorageImageArrayDynamicIndexing",
            f"{shader_path}:dynamic storage-image index",
        )
    if nonuniform:
        features.add("shaderStorageImageArrayNonUniformIndexing")
        evidence.setdefault(
            "shaderStorageImageArrayNonUniformIndexing",
            f"{shader_path}:NonUniform storage-image index",
        )
    if spirv_needs_scalar(spv, deadline):
        features.add("scalarBlockLayout")
        evidence.setdefault("scalarBlockLayout", f"{shader_path}:spirv-val scalar layout")
    return ShaderNeeds(shader_path, features, evidence)


def compare_gates(
    c_path: str,
    checked: list[FeatureSite],
    needs: list[ShaderNeeds],
) -> list[tuple[str, str, str, str]]:
    """Emit mismatch rows for one Vulkan filter file."""
    used: set[str] = set()
    evidence_by_feature: dict[str, str] = {}
    for entry in needs:
        used |= entry.features
        for feature, text in entry.evidence.items():
            evidence_by_feature.setdefault(feature, text)
    rows: list[tuple[str, str, str, str]] = []
    checked_names = {site.feature for site in checked}
    for site in checked:
        if site.feature not in MAP_STATE.derivable:
            rows.append((f"{c_path}:{site.line}", site.feature, "n/a", "unmapped"))
        elif site.feature not in used:
            rows.append((f"{c_path}:{site.line}", site.feature, "none", "required-unused"))
    for feature in sorted(used - checked_names):
        text = evidence_by_feature.get(feature, "shader")
        if "/" in text:
            anchor = text.split(":", 1)[0]
        elif needs:
            anchor = needs[0].path
        else:
            anchor = c_path
        rows.append((f"{anchor}:1", feature, text, "used-unchecked"))
    return rows


def sweep(directory: str, base: str, head: str, deadline: float) -> str:
    """Render vulkan-features rows for touched Vulkan filters and linked shaders."""
    names = diff_names(directory, base, head, deadline)
    vulkan = {path for path in names if VULKAN_C.fullmatch(path)}
    shaders = {path for path in names if SHADER_PATH.fullmatch(path)}
    if not vulkan and not shaders:
        return "\n".join(
            [
                "class: vulkan-features",
                f"head: {head}",
                "",
                TABLE,
                "| --- | --- | --- | --- | --- |",
                "no hits",
                "",
            ]
        )
    ensure_maps()
    targets = collect_targets(directory, head, names, deadline)
    lines = [
        "class: vulkan-features",
        f"head: {head}",
        "",
        TABLE,
        "| --- | --- | --- | --- | --- |",
    ]
    count = 0
    size = len(("\n".join(lines) + "\n").encode())
    with tempfile.TemporaryDirectory() as scratch:
        work = Path(scratch)
        for c_path in sorted(targets):
            check_deadline(deadline)
            c_source = git(
                directory,
                "show",
                f"{head}:{c_path}",
                deadline=deadline,
                maximum=MAX_TOOL_BYTES,
                limit_name="source size",
            )
            checked = c_feature_reads(c_source)
            needs = [
                shader_requirements(directory, head, shader, work, deadline)
                for shader in targets[c_path]
            ]
            for file_line, feature, evidence, direction in compare_gates(c_path, checked, needs):
                row = f"| {file_line} | {feature} | {evidence} | {direction} | TODO |"
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
