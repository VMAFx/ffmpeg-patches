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


def _header_present(header: str) -> bool:
    """Return whether cc preprocesses a translation unit that includes header."""
    try:
        compiler = _compiler()
    except FileNotFoundError:
        return False
    proc = subprocess.run(  # nosec B603 - compiler argv is fixed; header name comes from tests.
        [compiler, "-E", "-x", "c", "-o", os.devnull, "-"],
        input=f"#include <{header}>\n",
        text=True,
        timeout=COMPILE_DEADLINE,
        check=False,
        capture_output=True,
    )
    return proc.returncode == 0


def _secure_getenv_present() -> bool:
    """Return whether cc compiles and links a call to secure_getenv."""
    try:
        compiler = _compiler()
    except FileNotFoundError:
        return False
    proc = subprocess.run(  # nosec B603 - compiler argv and source are fixed.
        [compiler, "-std=c17", "-D_GNU_SOURCE", "-Werror", "-x", "c", "-o", os.devnull, "-"],
        input='#include <stdlib.h>\nint main(void) { return secure_getenv("PATH") != NULL; }\n',
        text=True,
        timeout=COMPILE_DEADLINE,
        check=False,
        capture_output=True,
    )
    return proc.returncode == 0


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
    proc = subprocess.run(  # nosec B603 - compiler argv uses fixture-controlled flags and paths.
        [
            _compiler(),
            "-std=c17",
            "-D_GNU_SOURCE",
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
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"cc exited {proc.returncode} on {LAYER_SOURCE.name}: {proc.stderr[-2000:]}"
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


def _fault_environment(layer_dir: Path, fault: str) -> dict[str, str]:
    """Build the child environment with the fault layer in the loader search path."""
    env = {
        **os.environ,
        "VK_ADD_LAYER_PATH": str(layer_dir),
        "VK_LOADER_LAYERS_ENABLE": LAYER_NAME,
        "VMAFX_VK_FAULT": fault,
    }
    caller_path = os.environ.get("VK_LAYER_PATH")
    if caller_path:
        env["VK_LAYER_PATH"] = str(layer_dir) + os.pathsep + caller_path
    else:
        env.pop("VK_LAYER_PATH", None)
    return env


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
    env = _fault_environment(layer_dir, fault)
    try:
        done = subprocess.run(  # nosec B603 - explicit FFmpeg binary and fixed filter graph.
            cmd, env=env, capture_output=True, timeout=DEADLINE, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raw = exc.stderr
        stderr = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else (raw or "")
        return FaultRun(None, stderr)
    return FaultRun(done.returncode, done.stderr.decode("utf-8", errors="replace"))


class FaultEnvironmentTest(unittest.TestCase):
    """Fault layer discovery with inherited loader paths, without a device."""

    def test_caller_layer_path_is_prefixed(self) -> None:
        """Prepend the fault layer directory to the caller's nonempty layer path."""
        layer_dir = Path("fault-layer")
        caller_path = os.pathsep.join(("caller-layer", "other-layer"))
        with unittest.mock.patch.dict(os.environ, {"VK_LAYER_PATH": caller_path}):
            env = _fault_environment(layer_dir, "begin=1")
        self.assertEqual(env["VK_LAYER_PATH"], str(layer_dir) + os.pathsep + caller_path)
        self.assertEqual(env["VK_ADD_LAYER_PATH"], str(layer_dir))
        self.assertEqual(env["VK_LOADER_LAYERS_ENABLE"], LAYER_NAME)
        self.assertEqual(env["VMAFX_VK_FAULT"], "begin=1")

    def test_absent_caller_layer_path_stays_absent(self) -> None:
        """Keep the additive layer path when the caller has no layer path."""
        layer_dir = Path("fault-layer")
        with unittest.mock.patch.dict(os.environ):
            os.environ.pop("VK_LAYER_PATH", None)
            env = _fault_environment(layer_dir, "submit=2")
        self.assertNotIn("VK_LAYER_PATH", env)
        self.assertEqual(env["VK_ADD_LAYER_PATH"], str(layer_dir))
        self.assertEqual(env["VK_LOADER_LAYERS_ENABLE"], LAYER_NAME)
        self.assertEqual(env["VMAFX_VK_FAULT"], "submit=2")

    def test_empty_caller_layer_path_is_omitted(self) -> None:
        """Omit an empty caller layer path while retaining the additive layer path."""
        layer_dir = Path("fault-layer")
        with unittest.mock.patch.dict(os.environ, {"VK_LAYER_PATH": ""}):
            env = _fault_environment(layer_dir, "")
        self.assertNotIn("VK_LAYER_PATH", env)
        self.assertEqual(env["VK_ADD_LAYER_PATH"], str(layer_dir))
        self.assertEqual(env["VK_LOADER_LAYERS_ENABLE"], LAYER_NAME)
        self.assertEqual(env["VMAFX_VK_FAULT"], "")


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

    def test_build_layer_compiles_without_device(self) -> None:
        """Build without a device; skip missing cc, Vulkan headers or secure_getenv outside VK."""
        if not (FFMPEG and DEVICE) and (
            not _header_present("vulkan/vulkan.h") or not _secure_getenv_present()
        ):
            self.skipTest(
                "no cc, Vulkan headers or secure_getenv; "
                "VK_FFMPEG and VK_DEVICE make the build mandatory"
            )
        with tempfile.TemporaryDirectory() as tmp:
            lib = build_layer(Path(tmp)) / "libvk_fault.so"
            self.assertTrue(lib.is_file())

    def test_build_layer_skips_without_secure_getenv_or_device(self) -> None:
        """Skip the no-device layer build when secure_getenv is unavailable."""
        with (
            unittest.mock.patch(__name__ + ".FFMPEG", ""),
            unittest.mock.patch(__name__ + ".DEVICE", ""),
            unittest.mock.patch(__name__ + "._header_present", return_value=True),
            unittest.mock.patch(__name__ + "._secure_getenv_present", return_value=False),
            unittest.mock.patch(
                __name__ + ".build_layer", side_effect=AssertionError("unexpected layer build")
            ) as build,
        ):
            with self.assertRaises(unittest.SkipTest):
                self.test_build_layer_compiles_without_device()
            build.assert_not_called()

    def test_build_layer_is_mandatory_with_device_without_secure_getenv(self) -> None:
        """Build the layer in the VK job even when secure_getenv is unavailable."""
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "libvk_fault.so").write_bytes(b"")
            with (
                unittest.mock.patch(__name__ + ".FFMPEG", "/fixture/ffmpeg"),
                unittest.mock.patch(__name__ + ".DEVICE", "0"),
                unittest.mock.patch(__name__ + "._header_present", return_value=True),
                unittest.mock.patch(__name__ + "._secure_getenv_present", return_value=False),
                unittest.mock.patch(__name__ + ".build_layer", return_value=Path(tmp)) as build,
            ):
                self.test_build_layer_compiles_without_device()
                build.assert_called_once()

    def test_secure_getenv_without_compiler_is_false(self) -> None:
        """Report secure_getenv as unavailable when no compiler exists."""
        with unittest.mock.patch(__name__ + "._compiler", side_effect=FileNotFoundError):
            self.assertFalse(_secure_getenv_present())

    @unittest.skipUnless(shutil.which("cc"), "no C compiler cc on PATH")
    def test_header_probe_finds_a_standard_header(self) -> None:
        """Report a standard C header as present when cc exists."""
        self.assertTrue(_header_present("stddef.h"))

    @unittest.skipUnless(shutil.which("cc"), "no C compiler cc on PATH")
    def test_header_probe_misses_an_absent_header(self) -> None:
        """Report a header no system ships as absent."""
        self.assertFalse(_header_present("vmafx_absent_header_0023.h"))

    def test_header_probe_without_compiler_is_false(self) -> None:
        """Report every header as absent when cc is not on PATH."""
        with unittest.mock.patch.object(shutil, "which", return_value=None):
            self.assertFalse(_header_present("stddef.h"))

    @unittest.skipUnless(shutil.which("cc"), "no C compiler cc on PATH")
    def test_build_failure_names_the_compiler_error(self) -> None:
        """Raise with the compiler stderr when the fault layer does not compile."""
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.c"
            bad.write_text("#error planted_0023_build_failure\n", encoding="utf-8")
            with (
                unittest.mock.patch(__name__ + ".LAYER_SOURCE", bad),
                self.assertRaises(RuntimeError) as raised,
            ):
                build_layer(Path(tmp))
            self.assertIn("planted_0023_build_failure", str(raised.exception))


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
