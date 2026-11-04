# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Exercise the Linux libvmaf setup without fetching or building dependencies."""

from __future__ import annotations

import os
import re
import shlex
import subprocess  # nosec B404 - bash runs the action with fixture-only build tools.
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def libvmaf_step() -> dict:
    """Locate the dependency build in the composite action."""
    action = yaml.safe_load(
        (ROOT / ".github/actions/prepare-series/action.yml").read_text(encoding="utf-8")
    )
    return next(step for step in action["runs"]["steps"] if "LIBVMAF_COMMIT" in step.get("env", {}))


def run_build(step: dict, actual: str) -> tuple[subprocess.CompletedProcess[str], str]:
    """Run the actual shell with a planted checkout identity and inert tools."""
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        for name, body in {
            "git": 'if [ "$1" = -C ]; then echo "$ACTUAL_COMMIT"; fi\n',
            "meson": 'echo meson >> "$COMMAND_LOG"\n',
            "ninja": 'echo ninja >> "$COMMAND_LOG"\n',
            "pkg-config": "echo 3.2.0\n",
        }.items():
            script = root / name
            script.write_text("#!/bin/bash\n" + body, encoding="utf-8")
            script.chmod(0o755)
        environment = dict(os.environ)
        environment.update(step["env"])
        environment.update(
            PATH=f"{root}:/usr/bin:/bin",
            RUNNER_TEMP=str(root),
            GITHUB_ENV=str(root / "github-env"),
            COMMAND_LOG=str(root / "commands"),
            ACTUAL_COMMIT=actual,
        )
        result = subprocess.run(  # nosec B603 B607 - fixed bash; tools are inert fixtures.
            ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", step["run"]],
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        commands = root / "commands"
        return result, commands.read_text(encoding="utf-8") if commands.exists() else ""


class PrepareSeriesTest(unittest.TestCase):
    def test_linux_configuration_compiles_libvmaf(self) -> None:
        """The coverage job must explicitly enable the changed filter."""
        workflow = yaml.safe_load(
            (ROOT / ".github/workflows/series.yml").read_text(encoding="utf-8")
        )
        steps = workflow["jobs"]["apply-build"]["steps"]
        configure = next(step["run"] for step in steps if step.get("name") == "Configure FFmpeg")
        arguments = shlex.split(configure.split("../ffmpeg/configure", 1)[1])
        self.assertIn("--enable-libvmaf", arguments)
        self.assertIn("--extra-libs=-lstdc++", arguments)

    def test_build_stays_in_the_linux_toolchain(self) -> None:
        """Draft, scope and platform conditions also guard dependency setup."""
        self.assertEqual(
            libvmaf_step()["if"],
            "inputs.draft != 'true' && inputs.in-scope == 'true' && inputs.toolchain == 'linux'",
        )

    def test_matching_commit_builds(self) -> None:
        """The pinned checkout reaches both build tools and reports its version."""
        step = libvmaf_step()
        result, commands = run_build(step, step["env"]["LIBVMAF_COMMIT"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(commands.splitlines(), ["meson", "ninja"])
        self.assertIn("3.2.0", result.stdout)

    def test_moved_tag_stops_before_build(self) -> None:
        """A planted tag mismatch must stop before untrusted source is built."""
        result, commands = run_build(libvmaf_step(), "0" * 40)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("::error::", result.stdout)
        self.assertEqual(commands, "")

    def test_planted_missing_pin_check_is_detected(self) -> None:
        """Removing the guard demonstrates the negative case's failure mode."""
        step = dict(libvmaf_step())
        step["run"], count = re.subn(r"(?ms)^if .*?^fi\n", "", step["run"], count=1)
        self.assertEqual(count, 1)
        result, commands = run_build(step, "0" * 40)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("meson", commands)


if __name__ == "__main__":
    unittest.main()
