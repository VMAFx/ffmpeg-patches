# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Fence fault tests for patch 0023 (hqdn3d_vulkan).

0023: tests/fixtures/vk_fault/vk_fault_layer.c is a Vulkan loader layer
(VK_LAYER_VMAFX_fault) that injects VK_ERROR_OUT_OF_DEVICE_MEMORY on demand
into vkBeginCommandBuffer on a command buffer that has dispatched compute,
vkQueueSubmit2 or vkQueueSubmit2KHR on a filter submission, vkCreateImageView
after the first dispatch, and vkCreateFence after an earlier injection.

Each injected failure must end FFmpeg within 30 seconds with a nonzero exit
status, not termination by a signal, and the layer's injection marker on
stderr; when `fence` is also active, stderr must carry both the vkCreateFence
injection marker and `Unable to restore the execution fence`. A baseline run
with the layer enabled and no fault configured must exit 0 with
`vk_fault: active none`.

The planted wrong case is patch 0023 before this fix, where the injected begin
and submit failures leave the execution context fence unsignalled and hang
FFmpeg in vkWaitForFences, and the injected image view failure combined with
`fence` lacks `Unable to restore the execution fence`.

The device cases need an FFmpeg built with the series, a Vulkan device, a C
compiler and the Vulkan headers:

    VK_FFMPEG=/path/to/ffmpeg VK_DEVICE=0 python3 -m unittest tests.test_hqdn3d_vulkan_fault

Without the variables the device cases are skipped.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess  # nosec B404 - fixed compiler and FFmpeg argv with bounded timeouts.
import tempfile
import unittest
import unittest.mock
from dataclasses import dataclass
from pathlib import Path

from tests import helpers

FFMPEG, DEVICE = helpers.device_environment("VK")
LAYER_SOURCE = Path(__file__).resolve().parent / "fixtures" / "vk_fault" / "vk_fault_layer.c"
LAYER_NAME = "VK_LAYER_VMAFX_fault"
DEADLINE = 30
COMPILE_DEADLINE = 120
RESTORE_FAILED = "Unable to restore the execution fence"
FAULTS = (
    "begin=1",
    "submit=2",
    "view=1",
    "begin=1,fence",
    "submit=2,fence",
    "view=1,fence",
)


@dataclass(frozen=True)
class FaultRun:
    """Outcome of one bounded FFmpeg invocation under the fault layer."""

    returncode: int | None
    stderr: str


def _compiler() -> str:
    """Return the absolute path of the C compiler cc found on PATH."""
    compiler = shutil.which("cc")
    if compiler is None:
        raise FileNotFoundError("no C compiler cc on PATH")
    return str(Path(compiler).absolute())


def _required_markers(fault: str) -> tuple[str, ...]:
    """Return the stderr substrings required for a configured fault."""
    if not fault:
        return ("vk_fault: active none",)
    markers = [f"vk_fault: active {fault}", "vk_fault: injected "]
    if "fence" in fault.split(","):
        markers.extend(("vk_fault: injected vkCreateFence", RESTORE_FAILED))
    return tuple(markers)


def fault_problems(fault: str, run: FaultRun) -> list[str]:
    """Return the broken bounded-failure rules for run, or [] when all hold."""
    if run.returncode is None:
        return [f"no exit within {DEADLINE} s"]
    problems: list[str] = []
    if run.returncode < 0:
        problems.append(f"killed by signal {-run.returncode}")
    elif not fault and run.returncode != 0:
        problems.append(f"exit status {run.returncode}")
    elif fault and run.returncode == 0:
        problems.append("exit status 0")
    for marker in _required_markers(fault):
        if marker not in run.stderr:
            problems.append(f"missing {marker}")
    return problems


def build_layer(directory: Path) -> Path:
    """Compile the Vulkan fault layer shared object and write its manifest."""
    lib = (directory / "libvk_fault.so").resolve()
    subprocess.run(  # nosec B603 - compiler argv uses fixture-controlled flags and paths.
        [
            _compiler(),
            "-std=c17",
            "-O1",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-shared",
            "-fPIC",
            "-pthread",
            "-o",
            str(lib),
            str(LAYER_SOURCE),
        ],
        timeout=COMPILE_DEADLINE,
        check=True,
        capture_output=True,
    )
    manifest = {
        "file_format_version": "1.2.0",
        "layer": {
            "name": LAYER_NAME,
            "type": "GLOBAL",
            "library_path": str(lib),
            "api_version": "1.4.0",
            "implementation_version": "1",
            "description": "VMAFx fault injection for the hqdn3d_vulkan fence tests",
            "functions": {
                "vkNegotiateLoaderLayerInterfaceVersion": "vkNegotiateLoaderLayerInterfaceVersion"
            },
        },
    }
    (directory / "VkLayer_vmafx_fault.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    return directory


def run_fault(binary: str, device: str, layer_dir: Path, fault: str) -> FaultRun:
    """Run FFmpeg hqdn3d_vulkan with the fault layer and return its bounded result."""
    cmd = [
        binary,
        "-hide_banner",
        "-nostdin",
        "-xerror",
        "-init_hw_device",
        f"vulkan=vk:{device}",
        "-filter_hw_device",
        "vk",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=s=160x96:d=0.4:r=10",
        "-vf",
        "format=yuv420p,hwupload,hqdn3d_vulkan,hwdownload,format=yuv420p",
        "-f",
        "null",
        "-",
    ]
    env = {
        **os.environ,
        "VK_ADD_LAYER_PATH": str(layer_dir),
        "VK_LOADER_LAYERS_ENABLE": LAYER_NAME,
        "VMAFX_VK_FAULT": fault,
    }
    try:
        done = subprocess.run(  # nosec B603 - explicit FFmpeg binary and fixed filter graph.
            cmd, env=env, capture_output=True, timeout=DEADLINE, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raw = exc.stderr
        stderr = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else (raw or "")
        return FaultRun(None, stderr)
    return FaultRun(done.returncode, done.stderr.decode("utf-8", errors="replace"))


class FaultRuleTest(unittest.TestCase):
    """The bounded-failure rule itself, without a device."""

    def test_clean_baseline_passes(self) -> None:
        """Accept a baseline run that exits zero with the active-none marker."""
        self.assertEqual(fault_problems("", FaultRun(0, "vk_fault: active none\n")), [])

    def test_nonzero_baseline_fails(self) -> None:
        """Reject a baseline run that exits nonzero."""
        self.assertEqual(
            fault_problems("", FaultRun(1, "vk_fault: active none\n")),
            ["exit status 1"],
        )

    def test_timed_out_run_fails(self) -> None:
        """Report a timed-out run as failing to exit within 30 seconds."""
        self.assertEqual(fault_problems("begin=1", FaultRun(None, "")), ["no exit within 30 s"])

    def test_fault_run_exiting_zero_fails(self) -> None:
        """Reject a fault run that exits zero despite injecting a failure."""
        stderr = (
            "vk_fault: active begin=1\nvk_fault: injected vkBeginCommandBuffer failure (call 1)\n"
        )
        self.assertEqual(fault_problems("begin=1", FaultRun(0, stderr)), ["exit status 0"])

    def test_fault_run_without_injection_marker_fails(self) -> None:
        """Reject a nonzero fault run that never printed an injection marker."""
        self.assertEqual(
            fault_problems("begin=1", FaultRun(1, "vk_fault: active begin=1\n")),
            ["missing vk_fault: injected "],
        )

    def test_fence_fault_with_markers_and_restore_message_passes(self) -> None:
        """Accept a begin=1,fence run carrying both injection markers and the restore error."""
        stderr = (
            "vk_fault: active begin=1,fence\n"
            "vk_fault: injected vkBeginCommandBuffer failure (call 1)\n"
            "vk_fault: injected vkCreateFence failure (call 1)\n"
            f"[hqdn3d_vulkan @ 0x1] {RESTORE_FAILED}\n"
        )
        self.assertEqual(fault_problems("begin=1,fence", FaultRun(1, stderr)), [])

    def test_fence_fault_without_restore_message_fails(self) -> None:
        """Reject a begin=1,fence run missing only the fence-restore diagnostic."""
        stderr = (
            "vk_fault: active begin=1,fence\n"
            "vk_fault: injected vkBeginCommandBuffer failure (call 1)\n"
            "vk_fault: injected vkCreateFence failure (call 1)\n"
        )
        self.assertEqual(
            fault_problems("begin=1,fence", FaultRun(1, stderr)),
            [f"missing {RESTORE_FAILED}"],
        )

    def test_fault_run_killed_by_signal_fails(self) -> None:
        """Reject a fault run terminated by a signal even with injection markers."""
        stderr = (
            "vk_fault: active begin=1\nvk_fault: injected vkBeginCommandBuffer failure (call 1)\n"
        )
        self.assertEqual(fault_problems("begin=1", FaultRun(-11, stderr)), ["killed by signal 11"])

    def test_baseline_killed_by_signal_fails(self) -> None:
        """Reject a baseline run terminated by a signal."""
        self.assertEqual(
            fault_problems("", FaultRun(-6, "vk_fault: active none\n")),
            ["killed by signal 6"],
        )

    def test_compiler_ignores_cc_environment(self) -> None:
        """Ignore CC in the environment when resolving the C compiler."""
        with unittest.mock.patch.dict(os.environ, {"CC": "/nonexistent/evil-cc"}):
            try:
                compiler = _compiler()
            except FileNotFoundError as exc:
                self.assertIn("cc", str(exc))
                return
        self.assertNotEqual(compiler, "/nonexistent/evil-cc")
        self.assertTrue(Path(compiler).is_absolute(), compiler)


@unittest.skipUnless(FFMPEG and DEVICE, "set VK_FFMPEG and VK_DEVICE")
class FenceFaultTest(unittest.TestCase):
    """0023: an injected start, submit, image view or fence failure must end
    FFmpeg, never hang it.
    """

    layer_dir: Path

    @classmethod
    def setUpClass(cls) -> None:
        """Build the Vulkan fault layer and manifest in a temporary directory."""
        scratch = tempfile.TemporaryDirectory()
        cls.addClassCleanup(scratch.cleanup)
        cls.layer_dir = build_layer(Path(scratch.name))

    def test_baseline_exits_zero(self) -> None:
        """Require the layer-enabled run without a configured fault to exit zero."""
        run = run_fault(FFMPEG, DEVICE, self.layer_dir, "")
        self.assertEqual(fault_problems("", run), [], run.stderr[-2000:])

    def test_faults_end_the_run(self) -> None:
        """Require each configured fault to terminate FFmpeg within the deadline."""
        for fault in FAULTS:
            with self.subTest(fault=fault):
                run = run_fault(FFMPEG, DEVICE, self.layer_dir, fault)
                self.assertEqual(fault_problems(fault, run), [], run.stderr[-2000:])


if __name__ == "__main__":
    unittest.main()
