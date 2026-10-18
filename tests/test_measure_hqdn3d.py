# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Unit tests for scripts/measure_hqdn3d.py.

Verifies:
1. VMAF JSON parsing accepts backend_used=cpu and refuses backend_used=cuda (planted negative).
2. Rate deviation flags 1.15x target and does not flag 1.05x (planted negative).
3. Device budget parsing picks correct GPU section in multi-GPU text and flags
   budget under bound (planted negative).
4. Host gate refuses MemAvailable 11 GiB (< 12 GiB) (planted negative).
5. Median of 3 returns median rather than mean, min, or max (planted negative).
6. Parity calculation correctly counts planted 1-sample differences (planted negative).
7. Device mapping, backend parsing, and markdown table rendering.
"""

from __future__ import annotations

import json
import unittest
from array import array

from scripts.measure_hqdn3d import (
    build_filter_chains,
    check_device_gate,
    check_host_readings,
    check_rate_deviation,
    compute_median_fps,
    compute_parity,
    format_bd_rate,
    format_overlap,
    format_speed_fps,
    parse_backend,
    parse_device_mapping,
    parse_vmaf_json,
    parse_vulkan_device_budget,
    quality_overlap,
    render_all_tables,
    rises_with_rate,
)

FIXTURE_3_GPUS_VULKANINFO = """
GPU0:
VkPhysicalDeviceProperties:
    apiVersion        = 1.4.351 (4211039)
    deviceName        = NVIDIA GeForce RTX 4090
memoryHeaps: count = 2
    memoryHeaps[0]:
        size   = 25757220864 (0x5ff400000) (23.99 GiB)
        budget = 2147483648 (0x80000000) (2048.00 MiB)
        usage  = 0 (0x00000000) (0.00 B)
        flags: count = 1
            MEMORY_HEAP_DEVICE_LOCAL_BIT
    memoryHeaps[1]:
        size   = 48709850112 (0xb57555400) (45.36 GiB)
        budget = 48709850112 (0xb57555400) (45.36 GiB)
        flags:
            None

GPU1:
VkPhysicalDeviceProperties:
    apiVersion        = 1.4.354 (4211042)
    deviceName        = Intel(R) Arc(tm) A380 Graphics
memoryHeaps: count = 2
    memoryHeaps[0]:
        size   = 6383730688 (0x17c800000) (5.95 GiB)
        budget = 536870912 (0x20000000) (512.00 MiB)
        usage  = 0 (0x00000000) (0.00 B)
        flags: count = 1
            MEMORY_HEAP_DEVICE_LOCAL_BIT
    memoryHeaps[1]:
        size   = 48709850112 (0xb57555400) (45.36 GiB)
        budget = 18868076544 (0x464a00000) (17.57 GiB)
        flags:
            None

GPU2:
VkPhysicalDeviceProperties:
    apiVersion        = 1.4.354 (4211042)
    deviceName        = AMD Radeon Graphics
memoryHeaps: count = 2
    memoryHeaps[0]:
        size   = 17179869184 (0x400000000) (16.00 GiB)
        budget = 4294967296 (0x100000000) (4096.00 MiB)
        usage  = 0 (0x00000000) (0.00 B)
        flags: count = 1
            MEMORY_HEAP_DEVICE_LOCAL_BIT
    memoryHeaps[1]:
        size   = 48709850112 (0xb57555400) (45.36 GiB)
        flags:
            None
"""


class TestVmafJsonParsing(unittest.TestCase):
    """Test suite for VMAF JSON output validation."""

    def test_backend_cpu_accepted(self) -> None:
        raw_json = json.dumps(
            {
                "version": "v3.1.0",
                "pooled_metrics": {
                    "vmaf": {"mean": 95.84},
                    "psnr_y": {"mean": 42.15},
                },
                "backend_used": "cpu",
            }
        )
        vmaf_score, psnr_score, backend = parse_vmaf_json(raw_json)
        self.assertAlmostEqual(vmaf_score, 95.84)
        self.assertAlmostEqual(psnr_score, 42.15)
        self.assertEqual(backend, "cpu")

    def test_backend_cuda_refused_planted_negative(self) -> None:
        raw_json = json.dumps(
            {
                "version": "v3.1.0",
                "pooled_metrics": {
                    "vmaf": {"mean": 95.84},
                    "psnr_y": {"mean": 42.15},
                },
                "backend_used": "cuda",
            }
        )
        with self.assertRaises(ValueError) as ctx:
            parse_vmaf_json(raw_json)
        self.assertIn("vmaf backend_used must be cpu", str(ctx.exception))
        self.assertIn("cuda", str(ctx.exception))

    def test_backend_missing_or_other_refused(self) -> None:
        with self.assertRaises(ValueError):
            parse_vmaf_json('{"backend_used": "sycl"}')
        with self.assertRaises(ValueError):
            parse_vmaf_json('{"version": "v3.1.0"}')


class TestRateDeviation(unittest.TestCase):
    """Test suite for rate achievement threshold checking."""

    def test_rate_15_pct_flagged_planted_negative(self) -> None:
        target = 1000.0
        achieved = 1150.0  # 1.15x (+15%)
        dev, flagged = check_rate_deviation(achieved, target)
        self.assertAlmostEqual(dev, 0.15)
        self.assertTrue(flagged)

    def test_rate_5_pct_not_flagged(self) -> None:
        target = 1000.0
        achieved = 1050.0  # 1.05x (+5%)
        dev, flagged = check_rate_deviation(achieved, target)
        self.assertAlmostEqual(dev, 0.05)
        self.assertFalse(flagged)

    def test_rate_negative_deviation_bounds(self) -> None:
        target = 1000.0
        dev_neg15, flag_neg15 = check_rate_deviation(850.0, target)
        self.assertAlmostEqual(dev_neg15, -0.15)
        self.assertTrue(flag_neg15)

        dev_neg5, flag_neg5 = check_rate_deviation(950.0, target)
        self.assertAlmostEqual(dev_neg5, -0.05)
        self.assertFalse(flag_neg5)


class TestDeviceBudget(unittest.TestCase):
    """Test suite for vulkaninfo device-local budget parsing and gates."""

    def test_budget_parse_picks_correct_gpu_section(self) -> None:
        b0 = parse_vulkan_device_budget(FIXTURE_3_GPUS_VULKANINFO, 0)
        b1 = parse_vulkan_device_budget(FIXTURE_3_GPUS_VULKANINFO, 1)
        b2 = parse_vulkan_device_budget(FIXTURE_3_GPUS_VULKANINFO, 2)
        b_none = parse_vulkan_device_budget(FIXTURE_3_GPUS_VULKANINFO, 3)

        self.assertEqual(b0, 2048)
        self.assertEqual(b1, 512)
        self.assertEqual(b2, 4096)
        self.assertIsNone(b_none)

    def test_budget_under_bound_skips_planted_negative(self) -> None:
        ok0, msg0, bud0 = check_device_gate(
            "vk:0", {}, min_free_mib=1024, vulkaninfo_text=FIXTURE_3_GPUS_VULKANINFO
        )
        self.assertTrue(ok0)
        self.assertEqual(bud0, 2048)

        # GPU1 budget is 512 MiB < 1024 MiB -> skipped
        ok1, msg1, bud1 = check_device_gate(
            "vk:1", {}, min_free_mib=1024, vulkaninfo_text=FIXTURE_3_GPUS_VULKANINFO
        )
        self.assertFalse(ok1)
        self.assertEqual(bud1, 512)
        self.assertIn("< 1024 MiB", msg1)

    def test_memory_type_flag_is_not_a_device_local_heap_planted_negative(self) -> None:
        # The last heap has no heap flag; the memoryTypes list after it carries
        # MEMORY_PROPERTY_DEVICE_LOCAL_BIT, which must not make it device-local.
        text = (
            "GPU0:\nmemoryHeaps: count = 2\n"
            "\tmemoryHeaps[0]:\n\t\tbudget = 2147483648\n\t\tflags: count = 1\n"
            "\t\t\tMEMORY_HEAP_DEVICE_LOCAL_BIT\n"
            "\tmemoryHeaps[1]:\n\t\tbudget = 1048576\n\t\tflags:\n\t\t\tNone\n"
            "memoryTypes: count = 1\n\tmemoryTypes[0]:\n"
            "\t\t\tMEMORY_PROPERTY_DEVICE_LOCAL_BIT\n"
        )
        self.assertEqual(parse_vulkan_device_budget(text, 0), 2048)
        no_local = text.replace("\t\t\tMEMORY_HEAP_DEVICE_LOCAL_BIT\n", "")
        self.assertIsNone(parse_vulkan_device_budget(no_local, 0))

    def test_device_mapping_resolves_and_gates(self) -> None:
        mapping = {"cuda:0": "vk:1", "opencl:2.0": "vk:0"}
        ok_cu, msg_cu, bud_cu = check_device_gate(
            "cuda:0", mapping, min_free_mib=1024, vulkaninfo_text=FIXTURE_3_GPUS_VULKANINFO
        )
        self.assertFalse(ok_cu)
        self.assertEqual(bud_cu, 512)
        self.assertIn("GPU1", msg_cu)

        ok_ocl, _, bud_ocl = check_device_gate(
            "opencl:2.0", mapping, min_free_mib=1024, vulkaninfo_text=FIXTURE_3_GPUS_VULKANINFO
        )
        self.assertTrue(ok_ocl)
        self.assertEqual(bud_ocl, 2048)


class TestHostGate(unittest.TestCase):
    """Test suite for /proc/meminfo and /proc/loadavg host checks."""

    def test_host_gate_refuses_11gib_planted_negative(self) -> None:
        # 11 GiB in KiB = 11 * 1024 * 1024 = 11534336 kB
        mem_11gib = (
            "MemTotal:       65536000 kB\n"
            "MemFree:         4000000 kB\n"
            "MemAvailable:   11534336 kB\n"
            "SwapTotal:      33554432 kB\n"
            "SwapFree:       20971520 kB\n"
        )
        load_ok = "2.50 2.10 1.95 2/1200 4567\n"
        ok, reason, readings = check_host_readings(mem_11gib, load_ok)
        self.assertFalse(ok)
        self.assertIn("MemAvailable", reason)
        self.assertIn("< 12 GiB", reason)
        self.assertAlmostEqual(readings["mem_available_kib"], 11534336.0)

    def test_host_gate_accepts_healthy(self) -> None:
        mem_ok = (
            "MemTotal:       65536000 kB\n"
            "MemAvailable:   14680064 kB\n"  # 14 GiB
            "SwapTotal:      33554432 kB\n"
            "SwapFree:       18874368 kB\n"  # 18 GiB
        )
        load_ok = "3.20 2.50 1.80 2/1200 4567\n"
        ok, reason, _ = check_host_readings(mem_ok, load_ok)
        self.assertTrue(ok)
        self.assertIn("passed", reason)

    def test_host_gate_refuses_excessive_load(self) -> None:
        mem_ok = "MemAvailable: 15000000 kB\nSwapFree: 20000000 kB\n"
        load_high = "42.50 35.00 25.00 10/2000 8900\n"
        ok, reason, _ = check_host_readings(mem_ok, load_high)
        self.assertFalse(ok)
        self.assertIn("load1 42.50 >= 40.0", reason)

    def test_host_gate_refuses_insufficient_swap(self) -> None:
        # SwapFree 10 GiB < 15 GiB
        mem_low_swap = "MemAvailable: 15000000 kB\nSwapFree: 10485760 kB\n"
        load_ok = "1.00 1.00 1.00 1/1000 1000\n"
        ok, reason, _ = check_host_readings(mem_low_swap, load_ok)
        self.assertFalse(ok)
        self.assertIn("SwapFree", reason)
        self.assertIn("< 15 GiB", reason)


class TestMedianFps(unittest.TestCase):
    """Test suite for median calculation."""

    def test_median_of_three(self) -> None:
        self.assertEqual(compute_median_fps([100.0, 150.0, 120.0]), 120.0)
        self.assertEqual(compute_median_fps([10.0, 20.0, 30.0]), 20.0)
        self.assertEqual(compute_median_fps([30.0, 20.0, 10.0]), 20.0)

    def test_median_planted_negative_not_mean_or_extreme(self) -> None:
        runs = [100.0, 150.0, 120.0]
        med = compute_median_fps(runs)
        mean_val = sum(runs) / len(runs)
        self.assertNotEqual(med, mean_val)
        self.assertNotEqual(med, min(runs))
        self.assertNotEqual(med, max(runs))


class TestParityCalculation(unittest.TestCase):
    """Test suite for ADR-0004 parity comparison and max diff."""

    def test_parity_planted_one_sample_diff(self) -> None:
        cpu = bytes([128] * 48)
        # 1 sample differs by +6 at index 10
        gpu = bytes([128 if i != 10 else 134 for i in range(48)])
        mism, max_d = compute_parity(cpu, gpu, depth=8, high_bits=False)
        self.assertEqual(mism, 1)
        self.assertEqual(max_d, 6)

    def test_parity_identical(self) -> None:
        buf = bytes([50] * 32)
        mism, max_d = compute_parity(buf, buf, depth=8, high_bits=False)
        self.assertEqual(mism, 0)
        self.assertEqual(max_d, 0)

    def test_parity_p010le_high_bits_clamping(self) -> None:
        # ADR-0004: CPU writes 65535, GPU writes 1023 (format maximum) -> 0 mismatches
        cpu_10b = array("H", [65535, 500]).tobytes()
        gpu_10b = array("H", [1023, 500]).tobytes()
        mism, max_d = compute_parity(cpu_10b, gpu_10b, depth=10, high_bits=True)
        self.assertEqual(mism, 0)
        self.assertEqual(max_d, 0)

        # Planted negative in 10-bit: in-range mismatch of delta 4
        gpu_wrong = array("H", [1023, 504]).tobytes()
        mism_w, max_d_w = compute_parity(cpu_10b, gpu_wrong, depth=10, high_bits=True)
        self.assertEqual(mism_w, 1)
        self.assertEqual(max_d_w, 4)


class TestBdValidity(unittest.TestCase):
    """BD-rate is reported only for curves whose score rises with the bitrate."""

    def test_rising_curve_is_accepted(self) -> None:
        pts = [{"bitrate_kbps": r, "score": s} for r, s in ((300, 80), (150, 70), (600, 90))]
        self.assertTrue(rises_with_rate(pts))

    def test_falling_top_point_is_refused_planted_negative(self) -> None:
        pts = [{"bitrate_kbps": r, "score": s} for r, s in ((150, 71.3), (600, 91.3), (1200, 90.9))]
        self.assertFalse(rises_with_rate(pts))

    def test_flat_pair_is_refused_boundary(self) -> None:
        pts = [{"bitrate_kbps": r, "score": s} for r, s in ((150, 80.0), (300, 80.0))]
        self.assertFalse(rises_with_rate(pts))

    def test_undefined_bd_rate_prints_reason(self) -> None:
        self.assertEqual(format_bd_rate({"bd_rate_pct": None, "bd_rate_note": "n/a: x"}), "n/a: x")
        self.assertEqual(format_bd_rate({"bd_rate_pct": 11.07}), "+11.07")


class TestSpeedBoundAndOverlap(unittest.TestCase):
    """A timed-out speed run prints a bound; BD rows print the shared VMAF range."""

    def test_timed_out_run_prints_bound(self) -> None:
        self.assertEqual(format_speed_fps({"median_fps": None}), "< 2.0 (run > 120 s)")

    def test_measured_run_prints_fps_planted_negative(self) -> None:
        self.assertEqual(format_speed_fps({"median_fps": 37.25}), "37.2")

    def test_overlap_of_saturated_curve(self) -> None:
        base = [{"bitrate_kbps": r, "score": q} for r, q in ((150, 76.7), (1200, 92.3))]
        arm = [{"bitrate_kbps": r, "score": q} for r, q in ((300, 85.7), (3300, 91.4))]
        self.assertEqual(quality_overlap(base, arm), [85.7, 91.4])
        self.assertEqual(format_overlap([85.7, 91.4]), "85.70-91.40")

    def test_disjoint_curves_have_no_overlap_boundary(self) -> None:
        self.assertEqual(format_overlap([90.0, 80.0]), "none")


class TestCliAndFormatting(unittest.TestCase):
    """Test suite for CLI helpers, mappings, and markdown table rendering."""

    def test_device_mapping_parsing(self) -> None:
        args = ["cuda:0=vk:0", "opencl:2.0=vk:0,opencl:1.0=vk:1"]
        m = parse_device_mapping(args)
        self.assertEqual(m["cuda:0"], "vk:0")
        self.assertEqual(m["opencl:2.0"], "vk:0")
        self.assertEqual(m["opencl:1.0"], "vk:1")

    def test_backend_parsing_and_filter_chains(self) -> None:
        b_type, hw_init, f_dev, f_name = parse_backend("vulkan:0")
        self.assertEqual(b_type, "vulkan")
        self.assertEqual(hw_init, "vulkan=vk:0")
        self.assertEqual(f_dev, "vk")
        self.assertEqual(f_name, "hqdn3d_vulkan")

        cpu, gpu = build_filter_chains("yuv420p", f_name, "=luma_spatial=8")
        self.assertIn("hqdn3d=luma_spatial=8", cpu)
        self.assertIn("hwupload,hqdn3d_vulkan=luma_spatial=8,hwdownload", gpu)

    def test_markdown_tables_rendering(self) -> None:
        sample_data = {
            "results": [
                {
                    "stage": "parity",
                    "clip": "bbb",
                    "noise": 10,
                    "setting": "defaults",
                    "format": "yuv420p",
                    "backend": "vulkan:0",
                    "mismatches": 0,
                    "max_diff": 0,
                },
                {
                    "stage": "filter_quality",
                    "clip": "bbb",
                    "noise": 10,
                    "arm": "none",
                    "device": "none",
                    "vmaf": 92.5,
                    "psnr_y": 41.2,
                },
                {
                    "stage": "rate",
                    "clip": "bbb",
                    "noise": 10,
                    "arm": "none",
                    "encoder": "svt",
                    "target_kbps": 300,
                    "achieved_kbps": 298.5,
                    "vmaf": 89.2,
                    "flagged": False,
                },
                {
                    "stage": "speed",
                    "task": "hqdn3d_defaults",
                    "device": "cpu",
                    "median_fps": 145.2,
                    "runs": [144.0, 145.2, 146.1],
                },
            ]
        }
        rendered = render_all_tables(sample_data)
        self.assertIn("## Measure 1: Parity Bound", rendered)
        self.assertIn("## Measure 2: Denoiser Quality", rendered)
        self.assertIn("## Measure 3: Rate & BD-Rate", rendered)
        self.assertIn("## Measure 4: Speed", rendered)
        self.assertIn("vulkan:0", rendered)
        self.assertIn("145.2", rendered)


if __name__ == "__main__":
    unittest.main()
