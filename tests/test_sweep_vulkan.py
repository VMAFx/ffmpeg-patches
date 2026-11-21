# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Exercise Vulkan feature and port-parity sweeps against synthetic Git fixtures."""

from __future__ import annotations

import importlib.util
import subprocess  # nosec B404 - fixed interpreter argv in temporary repositories.
import tempfile
import time
import unittest
import unittest.mock
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

from scripts import sweep_port_parity, sweep_vulkan_features, variant_sweep
from tests import test_variant_sweep as fixtures
from tests.helpers import isolated_git_environment
from tests.helpers import run_git as fixture_git

run_git = fixture_git
SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "variant_sweep.py"
GLSLANG = Path("/usr/bin/glslangValidator")
SPIRV_DIS = Path("/usr/bin/spirv-dis")
SPIRV_VAL = Path("/usr/bin/spirv-val")
SKIP_VULKAN = not all(tool.is_file() for tool in (GLSLANG, SPIRV_DIS, SPIRV_VAL))
SKIP_REASON = "Vulkan toolchain binaries missing under /usr/bin"

SIMPLE_SHADER = """\
#version 460
#extension GL_EXT_shader_image_load_formatted : require
layout(local_size_x = 1) in;
layout(set = 0, binding = 0) uniform readonly uimage2D img;
void main() { imageLoad(img, ivec2(0)); }
"""

MATCHED_SHADER = """\
#version 460
#extension GL_EXT_shader_image_load_formatted : require
#extension GL_EXT_scalar_block_layout : require
#extension GL_EXT_nonuniform_qualifier : require
layout(local_size_x = 1) in;
layout(set = 0, binding = 0) uniform readonly uimage2D imgs[];
layout(set = 0, binding = 1, scalar) buffer B {
    layout(offset = 0) uint a;
    layout(offset = 8) uvec3 b;
} blk;
void main() {
    imageLoad(imgs[nonuniformEXT(0)], ivec2(0));
    blk.b = uvec3(1u);
}
"""

DYNAMIC_SHADER = """\
#version 460
#extension GL_EXT_shader_image_load_formatted : require
layout(local_size_x = 1) in;
layout(constant_id = 0) const uint planes = 3;
layout(set = 0, binding = 0) uniform readonly uimage2D imgs[planes];
layout(push_constant) uniform PC { uint idx; } pc;
void main() { imageLoad(imgs[pc.idx], ivec2(0)); }
"""

LITERAL_SHADER = """\
#version 460
#extension GL_EXT_shader_image_load_formatted : require
layout(local_size_x = 1) in;
layout(constant_id = 0) const uint planes = 3;
layout(set = 0, binding = 0) uniform readonly uimage2D imgs[planes];
void main() { imageLoad(imgs[0], ivec2(0)); }
"""

RUNTIME_BUFFER_SHADER = """\
#version 460
#extension GL_EXT_scalar_block_layout : require
layout(local_size_x = 1) in;
layout(set = 0, binding = 0, scalar) buffer B { int t[]; } blk;
void main() { blk.t[0] = 1; }
"""

MASK_SHADER = """\
#version 460
#extension GL_EXT_shader_image_load_formatted : require
layout(local_size_x = 1) in;
layout(set = 0, binding = 0) uniform readonly uimage2D img;
uint load(uint raw) { return raw >> 4u; }
void main() { imageLoad(img, ivec2(0)); }
"""

MASKED_SHADER = """\
#version 460
#extension GL_EXT_shader_image_load_formatted : require
layout(local_size_x = 1) in;
layout(set = 0, binding = 0) uniform readonly uimage2D img;
uint load(uint raw) { return (raw & 0xFFF0u) >> 4u; }
void main() { load(imageLoad(img, ivec2(0)).x); }
"""

SECOND_MASKED_SHADER = """\
#version 460
#extension GL_EXT_shader_image_load_formatted : require
layout(local_size_x = 1) in;
layout(set = 0, binding = 0) uniform readonly uimage2D img;
uint norm(uint raw) { return (raw & 0xFF00u) >> 8u; }
void main() { norm(imageLoad(img, ivec2(0)).x); }
"""


def reset_vulkan_maps() -> None:
    """Clear lazy vk.xml map caches so patched registry paths apply."""
    state = sweep_vulkan_features.MAP_STATE
    state.loaded = False
    state.allowlist = frozenset()
    state.capability_map = {}
    state.feature_allowlist = frozenset()
    state.derivable = frozenset()


def load_mutant_module(rel_path: str, old: str, new: str, tmp: Path) -> ModuleType:
    """Load one mutated sweep module copy from a temporary directory."""
    root = Path(__file__).resolve().parents[1]
    source = (root / rel_path).read_text(encoding="utf-8")
    mutant_path = tmp / "mutant.py"
    mutant_path.write_text(source.replace(old, new, 1), encoding="utf-8")
    name = f"mutant_{mutant_path.stem}_{id(mutant_path)}"
    spec = importlib.util.spec_from_file_location(name, mutant_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load mutant from {rel_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def section(report: str, name: str) -> str:
    """Return one named class section without neighboring class rows."""
    return report.split(f"class: {name}\n", 1)[1].split("class: ", 1)[0]


class VulkanSweepTest(unittest.TestCase):
    """Cover Vulkan feature gates and port-parity rows with synthetic fixtures."""

    def setUp(self) -> None:
        """Create an empty Git tree with libavfilter layout."""
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.repo = Path(self.directory.name)
        run_git(self.repo, "init", "-q")
        (self.repo / "libavfilter" / "vulkan").mkdir(parents=True)
        (self.repo / "libavfilter" / "fix.c").write_text(
            "int fix(void) { return 0; }\n", encoding="utf-8"
        )
        run_git(self.repo, "add", ".")
        run_git(self.repo, "commit", "-qm", "base")
        self.base = run_git(self.repo, "rev-parse", "HEAD")
        self.output = self.repo / "sweep.md"
        self.deadline = time.monotonic() + 120

    def commit_filter(
        self,
        *,
        c_vulkan: str,
        shader_name: str,
        shader: str,
        cpu: str | None = None,
        cpu_name: str = "vf_test.c",
    ) -> str:
        """Commit one Vulkan filter, shader, and optional CPU pair."""
        (self.repo / "libavfilter" / "vf_test_vulkan.c").write_text(c_vulkan, encoding="utf-8")
        (self.repo / "libavfilter" / "vulkan" / shader_name).write_text(shader, encoding="utf-8")
        if cpu is not None:
            (self.repo / "libavfilter" / cpu_name).write_text(cpu, encoding="utf-8")
        (self.repo / "libavfilter" / "fix.c").write_text(
            (self.repo / "libavfilter" / "fix.c").read_text(encoding="utf-8") + "\n",
            encoding="utf-8",
        )
        run_git(self.repo, "add", ".")
        run_git(self.repo, "commit", "-qm", "head")
        return run_git(self.repo, "rev-parse", "HEAD")

    def vk_report(self, head: str) -> str:
        """Run the vulkan-features sweep against one head revision."""
        return sweep_vulkan_features.sweep(str(self.repo), self.base, head, time.monotonic() + 120)

    def parity_report(self, head: str) -> str:
        """Run the port-parity sweep against one head revision."""
        return sweep_port_parity.sweep(str(self.repo), self.base, head, time.monotonic() + 120)

    @unittest.skipIf(SKIP_VULKAN, SKIP_REASON)
    def test_required_unused_features(self) -> None:
        """Case (a) lists required-unused rows when gates exceed shader needs."""
        c_source = (
            "extern const unsigned char ff_test_comp_spv_data[];\n"
            "int gate(VkPhysicalDeviceVulkan12Features *v12) {\n"
            "    if (!v12->runtimeDescriptorArray) return -1;\n"
            "    if (!v12->scalarBlockLayout) return -1;\n"
            "    return 0;\n"
            "}\n"
        )
        head = self.commit_filter(
            c_vulkan=c_source,
            shader_name="test.comp.glsl",
            shader=SIMPLE_SHADER,
        )
        report = self.vk_report(head)
        self.assertIn("| runtimeDescriptorArray |", report)
        self.assertIn("| scalarBlockLayout |", report)
        self.assertEqual(report.count("| required-unused |"), 2)

    @unittest.skipIf(SKIP_VULKAN, SKIP_REASON)
    def test_matched_gates_no_hits(self) -> None:
        """Case (b) emits no hits when C gates match linked shader needs."""
        c_source = (
            "extern const unsigned char ff_test_comp_spv_data[];\n"
            "int gate(VkPhysicalDeviceFeatures *feats, VkPhysicalDeviceVulkan12Features *v12) {\n"
            "    if (!feats->shaderStorageImageReadWithoutFormat) return -1;\n"
            "    if (!v12->runtimeDescriptorArray) return -1;\n"
            "    if (!v12->scalarBlockLayout) return -1;\n"
            "    if (!v12->shaderStorageImageArrayNonUniformIndexing) return -1;\n"
            "    return 0;\n"
            "}\n"
        )
        head = self.commit_filter(
            c_vulkan=c_source,
            shader_name="test.comp.glsl",
            shader=MATCHED_SHADER,
        )
        self.assertIn("no hits", self.vk_report(head))

    @unittest.skipIf(SKIP_VULKAN, SKIP_REASON)
    def test_dynamic_index_used_unchecked(self) -> None:
        """Case (c) reports used-unchecked when dynamic indexing is unchecked."""
        c_source = (
            "extern const unsigned char ff_test_comp_spv_data[];\n"
            "int gate(VkPhysicalDeviceFeatures *feats) {\n"
            "    if (!feats->shaderStorageImageReadWithoutFormat) return -1;\n"
            "    return 0;\n"
            "}\n"
        )
        head = self.commit_filter(
            c_vulkan=c_source,
            shader_name="test.comp.glsl",
            shader=DYNAMIC_SHADER,
        )
        report = self.vk_report(head)
        self.assertIn("| shaderStorageImageArrayDynamicIndexing |", report)
        self.assertIn("| used-unchecked |", report)

    @unittest.skipIf(SKIP_VULKAN, SKIP_REASON)
    def test_literal_index_boundary(self) -> None:
        """Case (d) emits no hits for constant storage-image array indices."""
        c_source = (
            "extern const unsigned char ff_test_comp_spv_data[];\n"
            "int gate(VkPhysicalDeviceFeatures *feats) {\n"
            "    if (!feats->shaderStorageImageReadWithoutFormat) return -1;\n"
            "    return 0;\n"
            "}\n"
        )
        head = self.commit_filter(
            c_vulkan=c_source,
            shader_name="test.comp.glsl",
            shader=LITERAL_SHADER,
        )
        self.assertIn("no hits", self.vk_report(head))

    @unittest.skipIf(SKIP_VULKAN, SKIP_REASON)
    def test_runtime_buffer_not_runtime_descriptor(self) -> None:
        """Case (e) treats runtime buffer members as not requiring runtimeDescriptorArray."""
        c_source = (
            "extern const unsigned char ff_test_comp_spv_data[];\n"
            "int gate(void *v12) {\n"
            "    if (!((typeof(v12))v12)->runtimeDescriptorArray) return -1;\n"
            "    return 0;\n"
            "}\n"
        )
        head = self.commit_filter(
            c_vulkan=c_source,
            shader_name="test.comp.glsl",
            shader=RUNTIME_BUFFER_SHADER,
        )
        report = self.vk_report(head)
        self.assertIn("| runtimeDescriptorArray |", report)
        self.assertIn("| required-unused |", report)

    def test_empty_scope_skips_tools(self) -> None:
        """Case (f) returns no hits without calling external tools when scope is empty."""
        (self.repo / "libavfilter" / "fix.c").write_text(
            "int fix(void) { return 1; }\n", encoding="utf-8"
        )
        run_git(self.repo, "add", "libavfilter/fix.c")
        run_git(self.repo, "commit", "-qm", "head")
        head = run_git(self.repo, "rev-parse", "HEAD")
        with unittest.mock.patch("scripts.sweep_vulkan_features.run_tool") as run_tool:
            report = self.vk_report(head)
            run_tool.assert_not_called()
        self.assertIn("no hits", report)

    @unittest.skipIf(SKIP_VULKAN, SKIP_REASON)
    def test_missing_glslang_raises(self) -> None:
        """Case (g) raises when scope is non-empty but glslangValidator is unavailable."""
        head = self.commit_filter(
            c_vulkan=(
                "extern const unsigned char ff_test_comp_spv_data[];\nint f(void){return 0;}\n"
            ),
            shader_name="test.comp.glsl",
            shader=SIMPLE_SHADER,
        )
        with (
            unittest.mock.patch.object(
                sweep_vulkan_features,
                "GLSLANG",
                Path("/nonexistent/glslangValidator"),
            ),
            self.assertRaises(sweep_vulkan_features.VulkanFeatureSweepError),
        ):
            self.vk_report(head)

    def test_port_parity_missing_shader_mask(self) -> None:
        """Port-parity reports none when the CPU masks but the shader does not."""
        cpu = "void f(const uint16_t *src) { uint16_t v = src[0] & 0xFFF0u; (void)v; }\n"
        c_vulkan = (
            "extern const unsigned char ff_test_comp_spv_data[];\nint f(void) { return 0; }\n"
        )
        head = self.commit_filter(
            c_vulkan=c_vulkan,
            shader_name="test.comp.glsl",
            shader=MASK_SHADER,
            cpu=cpu,
        )
        report = self.parity_report(head)
        self.assertIn("| mask | none |", report)

    def test_port_parity_shader_mask_site(self) -> None:
        """Port-parity names the shader line when the same mask is present."""
        cpu = "void f(const uint16_t *src) { uint16_t v = src[0] & 0xFFF0u; (void)v; }\n"
        c_vulkan = "extern const unsigned char ff_test_comp_spv_data[];\nint f(void){return 0;}\n"
        head = self.commit_filter(
            c_vulkan=c_vulkan,
            shader_name="test.comp.glsl",
            shader=MASKED_SHADER,
            cpu=cpu,
        )
        report = self.parity_report(head)
        self.assertRegex(report, r"libavfilter/vulkan/test\.comp\.glsl:\d+")

    def test_port_parity_no_pair_in_diff(self) -> None:
        """Port-parity emits no hits when the diff does not touch a CPU/Vulkan pair."""
        (self.repo / "libavfilter" / "other.c").write_text(
            "int other(void){return 1;}\n", encoding="utf-8"
        )
        run_git(self.repo, "add", "libavfilter/other.c")
        run_git(self.repo, "commit", "-qm", "head")
        head = run_git(self.repo, "rev-parse", "HEAD")
        self.assertIn("no hits", self.parity_report(head))

    def test_t1_import_without_registry_empty_scope(self) -> None:
        """t1: modules import with missing vk.xml; empty scope prints no hits."""
        missing = Path("/nonexistent/vk.xml")
        reset_vulkan_maps()
        self.addCleanup(reset_vulkan_maps)
        with unittest.mock.patch.object(sweep_vulkan_features, "VK_XML", missing):
            (self.repo / "libavfilter" / "fix.c").write_text(
                "int fix(void){return 2;}\n", encoding="utf-8"
            )
            run_git(self.repo, "add", "libavfilter/fix.c")
            run_git(self.repo, "commit", "-qm", "head")
            head = run_git(self.repo, "rev-parse", "HEAD")
            deadline = time.monotonic() + 120
            self.assertIn(
                "no hits",
                sweep_vulkan_features.sweep(str(self.repo), self.base, head, deadline),
            )
            self.assertIn(
                "no hits",
                sweep_port_parity.sweep(str(self.repo), self.base, head, deadline),
            )

    @unittest.skipIf(SKIP_VULKAN, SKIP_REASON)
    def test_t1_missing_registry_nonempty_scope(self) -> None:
        """t1: a non-empty scope with missing vk.xml raises the named registry error."""
        missing = Path("/nonexistent/vk-registry-missing/vk.xml")
        reset_vulkan_maps()
        self.addCleanup(reset_vulkan_maps)
        with unittest.mock.patch.object(sweep_vulkan_features, "VK_XML", missing):
            head = self.commit_filter(
                c_vulkan=(
                    "extern const unsigned char ff_test_comp_spv_data[];\nint f(void){return 0;}\n"
                ),
                shader_name="test.comp.glsl",
                shader=SIMPLE_SHADER,
            )
            with self.assertRaisesRegex(
                sweep_vulkan_features.VulkanFeatureSweepError,
                f"missing vk.xml: {missing}",
            ):
                sweep_vulkan_features.sweep(str(self.repo), self.base, head, time.monotonic() + 120)

    def test_t2_linked_worktree_leaves_git_clean(self) -> None:
        """t2: sweeps use TMPDIR scratch and leave a separate-git-dir worktree clean."""
        root = Path(self.directory.name)
        git_parent = root / "actual.git"
        work = root / "wt"
        work.mkdir()
        subprocess.run(  # nosec B603 B607 - fixed git init argv in an isolated fixture.
            [
                "git",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "commit.gpgSign=false",
                "-c",
                "user.name=Dev",
                "-c",
                "user.email=dev@invalid",
                "init",
                "--separate-git-dir",
                str(git_parent),
                str(work),
            ],
            check=True,
            capture_output=True,
            timeout=60,
            env=isolated_git_environment(),
        )
        (work / "libavfilter" / "vulkan").mkdir(parents=True)
        (work / "libavfilter" / "fix.c").write_text("int fix(void){return 0;}\n", encoding="utf-8")
        run_git(work, "add", ".")
        run_git(work, "commit", "-qm", "base")
        base = run_git(work, "rev-parse", "HEAD")
        (work / "libavfilter" / "fix.c").write_text("int fix(void){return 1;}\n", encoding="utf-8")
        run_git(work, "add", "libavfilter/fix.c")
        run_git(work, "commit", "-qm", "head")
        head = run_git(work, "rev-parse", "HEAD")
        status_before = run_git(work, "status", "--short")
        self.assertEqual(status_before, "")
        sweep_vulkan_features.sweep(str(work), base, head, time.monotonic() + 120)
        sweep_port_parity.sweep(str(work), base, head, time.monotonic() + 120)
        self.assertEqual(run_git(work, "status", "--short"), "")
        self.assertTrue((work / ".git").is_file())

    @unittest.skipIf(SKIP_VULKAN, SKIP_REASON)
    def test_t3_non_feature_members_no_row(self) -> None:
        """t3: sampler, shader, and storage_buf reads produce no vulkan-features row."""
        c_source = (
            "extern const unsigned char ff_test_comp_spv_data[];\n"
            "typedef struct { void *sampler; void *shader; void *storage_buf; } S;\n"
            "int gate(VkPhysicalDeviceFeatures *feats, S *s) {\n"
            "    if (!feats->shaderStorageImageReadWithoutFormat) return -1;\n"
            "    if (!s->sampler || !s->shader || !s->storage_buf) return -1;\n"
            "    return 0;\n"
            "}\n"
        )
        head = self.commit_filter(
            c_vulkan=c_source, shader_name="test.comp.glsl", shader=SIMPLE_SHADER
        )
        self.assertIn("no hits", self.vk_report(head))

    @unittest.skipIf(SKIP_VULKAN, SKIP_REASON)
    def test_t3_unmapped_feature_struct_member(self) -> None:
        """t3: an un-derivable VkPhysicalDeviceFeatures member yields one unmapped row."""
        c_source = (
            "extern const unsigned char ff_test_comp_spv_data[];\n"
            "int gate(VkPhysicalDeviceFeatures *feats) {\n"
            "    if (!feats->robustBufferAccess) return -1;\n"
            "    return 0;\n"
            "}\n"
        )
        head = self.commit_filter(
            c_vulkan=c_source, shader_name="test.comp.glsl", shader=SIMPLE_SHADER
        )
        report = self.vk_report(head)
        self.assertIn("| robustBufferAccess |", report)
        self.assertIn("| unmapped |", report)
        self.assertEqual(report.count("| unmapped |"), 1)

    @unittest.skipIf(SKIP_VULKAN, SKIP_REASON)
    def test_t4_shader_without_owner_no_hits(self) -> None:
        """t4: a changed shader with no owning Vulkan C file yields no hits in both sweeps."""
        (self.repo / "libavfilter" / "vulkan" / "orphan.comp.glsl").write_text(
            SIMPLE_SHADER, encoding="utf-8"
        )
        (self.repo / "libavfilter" / "fix.c").write_text(
            (self.repo / "libavfilter" / "fix.c").read_text(encoding="utf-8") + "\n",
            encoding="utf-8",
        )
        run_git(self.repo, "add", ".")
        run_git(self.repo, "commit", "-qm", "head")
        head = run_git(self.repo, "rev-parse", "HEAD")
        self.assertIn("no hits", self.vk_report(head))
        self.assertIn("no hits", self.parity_report(head))

    def test_t5_pair_limit_before_slice(self) -> None:
        """t5: two touched filter pairs exceed MAX_PAIRS=1 and raise the named limit error."""
        for stem in ("one", "two"):
            (self.repo / "libavfilter" / f"vf_{stem}.c").write_text(
                f"void f_{stem}(void){{}}\n", encoding="utf-8"
            )
            (self.repo / "libavfilter" / f"vf_{stem}_vulkan.c").write_text(
                "int vk(void){return 0;}\n", encoding="utf-8"
            )
        (self.repo / "libavfilter" / "fix.c").write_text(
            (self.repo / "libavfilter" / "fix.c").read_text(encoding="utf-8") + "\n",
            encoding="utf-8",
        )
        run_git(self.repo, "add", ".")
        run_git(self.repo, "commit", "-qm", "head")
        head = run_git(self.repo, "rev-parse", "HEAD")
        with (
            unittest.mock.patch.object(sweep_port_parity, "MAX_PAIRS", 1),
            self.assertRaisesRegex(RuntimeError, "filter pair count"),
        ):
            sweep_port_parity.sweep(str(self.repo), self.base, head, time.monotonic() + 120)

    def test_t6_second_linked_shader_mask_site(self) -> None:
        """t6: port-parity names the second linked shader when only it applies the mask."""
        cpu = "void f(const uint16_t *src) { uint16_t v = src[0] & 0xFF00u; (void)v; }\n"
        c_vulkan = (
            "extern const unsigned char ff_a_comp_spv_data[];\n"
            "extern const unsigned char ff_b_comp_spv_data[];\n"
            "int f(void) { return 0; }\n"
        )
        (self.repo / "libavfilter" / "vf_test_vulkan.c").write_text(c_vulkan, encoding="utf-8")
        (self.repo / "libavfilter" / "vf_test.c").write_text(cpu, encoding="utf-8")
        (self.repo / "libavfilter" / "vulkan" / "a.comp.glsl").write_text(
            MASK_SHADER, encoding="utf-8"
        )
        (self.repo / "libavfilter" / "vulkan" / "b.comp.glsl").write_text(
            SECOND_MASKED_SHADER, encoding="utf-8"
        )
        (self.repo / "libavfilter" / "fix.c").write_text(
            (self.repo / "libavfilter" / "fix.c").read_text(encoding="utf-8") + "\n",
            encoding="utf-8",
        )
        run_git(self.repo, "add", ".")
        run_git(self.repo, "commit", "-qm", "head")
        head = run_git(self.repo, "rev-parse", "HEAD")
        report = self.parity_report(head)
        self.assertRegex(report, r"libavfilter/vulkan/b\.comp\.glsl:\d+")


class VulkanFullReportTest(unittest.TestCase):
    """Validate full reports that include the new sweep sections."""

    setUp = fixtures.VariantSweepTest.setUp
    commit_head = fixtures.VariantSweepTest.commit_head
    sweep = fixtures.VariantSweepTest.sweep

    @unittest.skipIf(SKIP_VULKAN, SKIP_REASON)
    def test_full_report_sections_and_validation(self) -> None:
        """A full sweep renders both new sections and refuses unresolved TODO rows."""
        head = self.commit_head("int f(void) { return 0; }\n")
        result = self.sweep(head, "all")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = self.output.read_text(encoding="utf-8")
        self.assertIn("class: vulkan-features", report)
        self.assertIn("class: port-parity", report)
        with self.assertRaisesRegex(RuntimeError, "review-classes:|TODO"):
            variant_sweep.validate_full_report(self.output, head)
        cleaned = report.replace("| TODO |", "| no-variant: fixture |")
        self.output.write_text(cleaned, encoding="utf-8")
        self.assertTrue(variant_sweep.validate_full_report(self.output, head))


class VulkanMutantHarness(unittest.TestCase):
    """Shared synthetic repository setup for planted mutant checks."""

    setUp = VulkanSweepTest.setUp
    commit_filter = VulkanSweepTest.commit_filter
    vk_report = VulkanSweepTest.vk_report
    parity_report = VulkanSweepTest.parity_report

    def assert_mutant_kills(
        self,
        rel_path: str,
        old: str,
        new: str,
        real_check: Callable[[], None],
        mutant_check: Callable[[ModuleType], None],
    ) -> None:
        """Assert the real module passes a check and the mutant copy fails it."""
        real_check()
        with tempfile.TemporaryDirectory() as scratch:
            mutant = load_mutant_module(rel_path, old, new, Path(scratch))
            mutant_check(mutant)


class PortParityMutantTest(VulkanMutantHarness):
    """Run port-parity mutants without requiring the Vulkan toolchain."""

    def test_mutant_m4_shader_mask(self) -> None:
        """m4: disabling shader mask matching passes on real and fails on mutant."""
        cpu = "void f(const uint16_t *src) { uint16_t v = src[0] & 0xFFF0u; (void)v; }\n"
        head = self.commit_filter(
            c_vulkan=(
                "extern const unsigned char ff_test_comp_spv_data[];\nint f(void){return 0;}\n"
            ),
            shader_name="test.comp.glsl",
            shader=MASKED_SHADER,
            cpu=cpu,
        )

        def real() -> None:
            self.assertRegex(self.parity_report(head), r"libavfilter/vulkan/test\.comp\.glsl:\d+")

        def mutant(module: ModuleType) -> None:
            report = module.sweep(str(self.repo), self.base, head, time.monotonic() + 120)
            self.assertRegex(report, r"\| mask \| none \|")

        self.assert_mutant_kills(
            "scripts/sweep_port_parity.py",
            "if pattern.search(window):",
            "if False and pattern.search(window):",
            real,
            mutant,
        )

    def test_mutant_m5_pair_limit_slice(self) -> None:
        """m5: slicing before the pair limit passes on real and fails on mutant."""
        for stem in ("one", "two"):
            (self.repo / "libavfilter" / f"vf_{stem}.c").write_text(
                f"void f_{stem}(void){{}}\n", encoding="utf-8"
            )
            (self.repo / "libavfilter" / f"vf_{stem}_vulkan.c").write_text(
                "int vk(void){return 0;}\n", encoding="utf-8"
            )
        (self.repo / "libavfilter" / "fix.c").write_text(
            (self.repo / "libavfilter" / "fix.c").read_text(encoding="utf-8") + "\n",
            encoding="utf-8",
        )
        run_git(self.repo, "add", ".")
        run_git(self.repo, "commit", "-qm", "head")
        head = run_git(self.repo, "rev-parse", "HEAD")

        def real() -> None:
            with (
                unittest.mock.patch.object(sweep_port_parity, "MAX_PAIRS", 1),
                self.assertRaisesRegex(RuntimeError, "filter pair count"),
            ):
                sweep_port_parity.sweep(str(self.repo), self.base, head, time.monotonic() + 120)

        def mutant(module: ModuleType) -> None:
            module.MAX_PAIRS = 1
            module.sweep(str(self.repo), self.base, head, time.monotonic() + 120)

        slice_mutant_old = (
            '    check_limit("filter pair count", len(stems), MAX_PAIRS)\n'
            "    pairs: dict[str, tuple[str, str, bool]] = {}\n"
            "    for stem in sorted(stems):"
        )
        slice_mutant_new = (
            "    pairs: dict[str, tuple[str, str, bool]] = {}\n"
            "    for stem in sorted(stems)[:MAX_PAIRS]:"
        )
        self.assert_mutant_kills(
            "scripts/sweep_port_parity.py",
            slice_mutant_old,
            slice_mutant_new,
            real,
            mutant,
        )

    def test_mutant_m6_first_shader_only(self) -> None:
        """m6: reading only the first linked shader passes on real and fails on mutant."""
        cpu = "void f(const uint16_t *src) { uint16_t v = src[0] & 0xFF00u; (void)v; }\n"
        c_vulkan = (
            "extern const unsigned char ff_a_comp_spv_data[];\n"
            "extern const unsigned char ff_b_comp_spv_data[];\n"
            "int f(void) { return 0; }\n"
        )
        (self.repo / "libavfilter" / "vf_test_vulkan.c").write_text(c_vulkan, encoding="utf-8")
        (self.repo / "libavfilter" / "vf_test.c").write_text(cpu, encoding="utf-8")
        (self.repo / "libavfilter" / "vulkan" / "a.comp.glsl").write_text(
            MASK_SHADER, encoding="utf-8"
        )
        (self.repo / "libavfilter" / "vulkan" / "b.comp.glsl").write_text(
            SECOND_MASKED_SHADER, encoding="utf-8"
        )
        (self.repo / "libavfilter" / "fix.c").write_text(
            (self.repo / "libavfilter" / "fix.c").read_text(encoding="utf-8") + "\n",
            encoding="utf-8",
        )
        run_git(self.repo, "add", ".")
        run_git(self.repo, "commit", "-qm", "head")
        head = run_git(self.repo, "rev-parse", "HEAD")

        def real() -> None:
            self.assertRegex(self.parity_report(head), r"libavfilter/vulkan/b\.comp\.glsl:\d+")

        def mutant(module: ModuleType) -> None:
            report = module.sweep(str(self.repo), self.base, head, time.monotonic() + 120)
            self.assertRegex(report, r"\| mask \| none \|")

        self.assert_mutant_kills(
            "scripts/sweep_port_parity.py",
            "    for shader_path in sorted(shaders):",
            "    for shader_path in sorted(shaders)[:1]:",
            real,
            mutant,
        )


class VulkanMutantTest(VulkanMutantHarness):
    """Run planted mutants on temporary module copies without editing scripts/."""

    @classmethod
    def setUpClass(cls) -> None:
        """Skip the suite when the Vulkan toolchain is unavailable."""
        if SKIP_VULKAN:
            raise unittest.SkipTest(SKIP_REASON)

    def test_mutant_m1_required_unused(self) -> None:
        """m1: disabling required-unused detection passes on real and fails on mutant."""
        head = self.commit_filter(
            c_vulkan=(
                "extern const unsigned char ff_test_comp_spv_data[];\n"
                "int gate(VkPhysicalDeviceVulkan12Features *v12) {\n"
                "    if (!v12->runtimeDescriptorArray) return -1;\n"
                "    if (!v12->scalarBlockLayout) return -1;\n"
                "    return 0;\n"
                "}\n"
            ),
            shader_name="test.comp.glsl",
            shader=SIMPLE_SHADER,
        )

        def real() -> None:
            self.assertIn("| required-unused |", self.vk_report(head))

        def mutant(module: ModuleType) -> None:
            report = module.sweep(str(self.repo), self.base, head, time.monotonic() + 120)
            self.assertNotIn("| required-unused |", report)

        self.assert_mutant_kills(
            "scripts/sweep_vulkan_features.py",
            "elif site.feature not in used:",
            "elif False and site.feature not in used:",
            real,
            mutant,
        )

    def test_mutant_m2_dynamic_index(self) -> None:
        """m2: disabling dynamic index detection passes on real and fails on mutant."""
        head = self.commit_filter(
            c_vulkan=(
                "extern const unsigned char ff_test_comp_spv_data[];\n"
                "int gate(VkPhysicalDeviceFeatures *feats) {\n"
                "    if (!feats->shaderStorageImageReadWithoutFormat) return -1;\n"
                "    return 0;\n"
                "}\n"
            ),
            shader_name="test.comp.glsl",
            shader=DYNAMIC_SHADER,
        )

        def real() -> None:
            self.assertIn("| used-unchecked |", self.vk_report(head))

        def mutant(module: ModuleType) -> None:
            report = module.sweep(str(self.repo), self.base, head, time.monotonic() + 120)
            self.assertNotIn("| used-unchecked |", report)

        self.assert_mutant_kills(
            "scripts/sweep_vulkan_features.py",
            'if index not in constants and not index.startswith("%int_"):',
            'if False and index not in constants and not index.startswith("%int_"):',
            real,
            mutant,
        )

    def test_mutant_m3_spirv_scalar(self) -> None:
        """m3: disabling scalar layout detection passes on real and fails on mutant."""
        head = self.commit_filter(
            c_vulkan=(
                "extern const unsigned char ff_test_comp_spv_data[];\n"
                "int gate(VkPhysicalDeviceFeatures *feats,\n"
                "        VkPhysicalDeviceVulkan12Features *v12) {\n"
                "    if (!feats->shaderStorageImageReadWithoutFormat) return -1;\n"
                "    if (!v12->runtimeDescriptorArray) return -1;\n"
                "    if (!v12->scalarBlockLayout) return -1;\n"
                "    if (!v12->shaderStorageImageArrayNonUniformIndexing) return -1;\n"
                "    return 0;\n"
                "}\n"
            ),
            shader_name="test.comp.glsl",
            shader=MATCHED_SHADER,
        )

        def real() -> None:
            self.assertIn("no hits", self.vk_report(head))

        def mutant(module: ModuleType) -> None:
            report = module.sweep(str(self.repo), self.base, head, time.monotonic() + 120)
            self.assertNotIn("no hits", report)

        self.assert_mutant_kills(
            "scripts/sweep_vulkan_features.py",
            "return default_rc != 0 and result.returncode == 0",
            "return False",
            real,
            mutant,
        )

    def test_mutant_m7_import_maps(self) -> None:
        """m7: import-time map load passes on real and fails on mutant."""
        missing = Path("/nonexistent/vk-registry-mutant/vk.xml")

        def real() -> None:
            reset_vulkan_maps()
            with unittest.mock.patch.object(sweep_vulkan_features, "VK_XML", missing):
                head = self.commit_filter(
                    c_vulkan=(
                        "extern const unsigned char ff_test_comp_spv_data[];\n"
                        "int f(void){return 0;}\n"
                    ),
                    shader_name="test.comp.glsl",
                    shader=SIMPLE_SHADER,
                )
                with self.assertRaisesRegex(
                    sweep_vulkan_features.VulkanFeatureSweepError,
                    f"missing vk.xml: {missing}",
                ):
                    sweep_vulkan_features.sweep(
                        str(self.repo), self.base, head, time.monotonic() + 120
                    )
            reset_vulkan_maps()

        def mutant(_module: ModuleType) -> None:
            root = Path(__file__).resolve().parents[1]
            source = (root / "scripts/sweep_vulkan_features.py").read_text(encoding="utf-8")
            source = source.replace(
                'VK_XML = Path("/usr/share/vulkan/registry/vk.xml")',
                f'VK_XML = Path("{missing}")',
            )
            source = (
                source.rstrip()
                + "\nALLOWLIST, CAPABILITY_MAP, FEATURE_ALLOWLIST = load_maps(VK_XML)\n"
                "DERIVABLE = derivable_features(CAPABILITY_MAP)\n_MAPS_LOADED = True\n"
            )
            with tempfile.TemporaryDirectory() as scratch:
                mutant_path = Path(scratch) / "sweep_vulkan_features_mutant.py"
                mutant_path.write_text(source, encoding="utf-8")
                spec = importlib.util.spec_from_file_location(
                    "sweep_vulkan_features_mutant", mutant_path
                )
                self.assertIsNotNone(spec)
                self.assertIsNotNone(spec.loader)
                module = importlib.util.module_from_spec(spec)
                with self.assertRaises(
                    (FileNotFoundError, sweep_vulkan_features.VulkanFeatureSweepError)
                ):
                    spec.loader.exec_module(module)

        self.assert_mutant_kills(
            "scripts/sweep_vulkan_features.py",
            "MAP_STATE = VulkanMapState()",
            "MAP_STATE = VulkanMapState()",
            real,
            mutant,
        )


class PortParityWithoutToolsTest(VulkanMutantHarness):
    """Prove port-parity survives missing Vulkan binaries while feature sweeps do not."""

    def test_port_parity_second_shader_with_hidden_tools(self) -> None:
        """Port-parity names the second linked shader when tool paths are patched away."""
        missing = Path(self.directory.name) / "no-vulkan-tool"
        cpu = "void f(const uint16_t *src) { uint16_t v = src[0] & 0xFF00u; (void)v; }\n"
        c_vulkan = (
            "extern const unsigned char ff_a_comp_spv_data[];\n"
            "extern const unsigned char ff_b_comp_spv_data[];\n"
            "int f(void) { return 0; }\n"
        )
        (self.repo / "libavfilter" / "vf_test_vulkan.c").write_text(c_vulkan, encoding="utf-8")
        (self.repo / "libavfilter" / "vf_test.c").write_text(cpu, encoding="utf-8")
        (self.repo / "libavfilter" / "vulkan" / "a.comp.glsl").write_text(
            MASK_SHADER, encoding="utf-8"
        )
        (self.repo / "libavfilter" / "vulkan" / "b.comp.glsl").write_text(
            SECOND_MASKED_SHADER, encoding="utf-8"
        )
        (self.repo / "libavfilter" / "fix.c").write_text(
            (self.repo / "libavfilter" / "fix.c").read_text(encoding="utf-8") + "\n",
            encoding="utf-8",
        )
        run_git(self.repo, "add", ".")
        run_git(self.repo, "commit", "-qm", "head")
        head = run_git(self.repo, "rev-parse", "HEAD")
        with (
            unittest.mock.patch.object(sweep_vulkan_features, "GLSLANG", missing),
            unittest.mock.patch.object(sweep_vulkan_features, "SPIRV_DIS", missing),
            unittest.mock.patch.object(sweep_vulkan_features, "SPIRV_VAL", missing),
        ):
            report = self.parity_report(head)
        self.assertRegex(report, r"libavfilter/vulkan/b\.comp\.glsl:\d+")

    def test_vk_report_raises_with_hidden_tools(self) -> None:
        """Vulkan-feature sweep raises when tool paths are patched away."""
        missing = Path(self.directory.name) / "no-vulkan-tool"
        head = self.commit_filter(
            c_vulkan=(
                "extern const unsigned char ff_test_comp_spv_data[];\nint f(void){return 0;}\n"
            ),
            shader_name="test.comp.glsl",
            shader=SIMPLE_SHADER,
        )
        with (
            unittest.mock.patch.object(sweep_vulkan_features, "GLSLANG", missing),
            unittest.mock.patch.object(sweep_vulkan_features, "SPIRV_DIS", missing),
            unittest.mock.patch.object(sweep_vulkan_features, "SPIRV_VAL", missing),
            self.assertRaises(sweep_vulkan_features.VulkanFeatureSweepError),
        ):
            self.vk_report(head)


if __name__ == "__main__":
    unittest.main()
