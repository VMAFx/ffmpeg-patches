# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Keep the push guard active even when lefthook finds no push files."""

from __future__ import annotations

import os
import re
import tempfile
import unittest
from importlib import metadata
from pathlib import Path
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]


def run_with_fixture(method: str, files: dict[str, str]) -> unittest.TestResult:
    """Replay a policy check against planted files without touching the checkout."""
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        for name, content in files.items():
            (root / name).write_text(content, encoding="utf-8")
        result = unittest.TestResult()
        with patch(f"{__name__}.ROOT", root):
            LefthookPushGuardTest(method).run(result)
        return result


class LefthookPushGuardTest(unittest.TestCase):
    """Lefthook skips pre-push run: jobs when there are no push files.

    See v2.2.2 internal/run/controller/command/build_command.go; the guard
    must be a script job so empty file lists cannot bypass it.
    """

    def test_push_guard_is_first_unfiltered_script_job(self) -> None:
        config = yaml.safe_load((ROOT / "lefthook.yml").read_text(encoding="utf-8"))
        pre_push = config["pre-push"]
        self.assertIs(pre_push["parallel"], False)
        self.assertIn("jobs", pre_push, "push-guard must be a script job")
        self.assertTrue(pre_push["jobs"], "pre-push must have a push-guard job")
        guard = pre_push["jobs"][0]
        self.assertEqual(guard["name"], "push-guard")
        self.assertIn("script", guard)
        self.assertNotIn("run", guard)
        self.assertIs(guard["use_stdin"], True)
        # Literal custom args keep Git's remote name and URL out of shell text.
        self.assertEqual(guard.get("args"), "--from-config")
        for key in ("glob", "files", "root", "exclude", "file_types"):
            with self.subTest(key=key):
                self.assertNotIn(key, guard)

        script = ROOT / ".lefthook" / "pre-push" / guard["script"]
        self.assertTrue(script.is_file(), f"missing script: {script}")
        self.assertTrue(os.access(script, os.X_OK), f"script is not executable: {script}")
        self.assertIn(
            'exec sh .config/lefthook/python.sh scripts/push_guard.py "$@"',
            script.read_text(encoding="utf-8"),
        )

    def test_contributing_dependency_pins_match_ci(self) -> None:
        workflow = yaml.safe_load(
            (ROOT / ".github/workflows/series.yml").read_text(encoding="utf-8")
        )
        install_steps = [
            step
            for job in workflow["jobs"].values()
            for step in job["steps"]
            if step.get("name") == "Install ruff, reuse, numpy and PyYAML"
        ]
        self.assertEqual(len(install_steps), 1)
        prefix = "python3 -m pip install "
        ci_command = install_steps[0]["run"]
        self.assertTrue(ci_command.startswith(prefix))
        documented_commands = [
            line
            for line in (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8").splitlines()
            if line.startswith(prefix)
        ]
        self.assertEqual(len(documented_commands), 1)
        ci_pins = set(ci_command.removeprefix(prefix).split())
        self.assertTrue(ci_pins)
        self.assertTrue(all("==" in pin for pin in ci_pins))
        self.assertEqual(set(documented_commands[0].removeprefix(prefix).split()), ci_pins)

    def test_hook_jobs_never_interpolate_git_arguments(self) -> None:
        config = yaml.safe_load((ROOT / "lefthook.yml").read_text(encoding="utf-8"))
        for hook, settings in config.items():
            if not isinstance(settings, dict):
                continue
            jobs = list(settings.get("commands", {}).values()) + settings.get("jobs", [])
            for index, job in enumerate(jobs):
                for field in ("run", "args"):
                    with self.subTest(hook=hook, job=index, field=field):
                        self.assertNotRegex(str(job.get(field, "")), r"\{\d+\}")

    def test_contributing_python_minimum_supports_installed_pins(self) -> None:
        document = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
        minimum = re.search(r"Python (\d+)\.(\d+) or newer", document)
        self.assertIsNotNone(minimum)
        documented = tuple(map(int, minimum.groups()))
        pins = re.findall(r"([A-Za-z0-9_.-]+)==([^\s`]+)", document)
        self.assertTrue(pins)
        for name, version in pins:
            with self.subTest(pin=f"{name}=={version}"):
                try:
                    requires = metadata.metadata(name).get("Requires-Python", "")
                except metadata.PackageNotFoundError:
                    self.skipTest(f"{name} is not installed")
                lower = re.search(r"(?:^|,)\s*>=\s*(\d+)\.(\d+)(?=[.,\s]|$)", requires)
                if lower is None:
                    self.skipTest(f"{name}: no simple >=X.Y in Requires-Python {requires!r}")
                self.assertGreaterEqual(documented, tuple(map(int, lower.groups())))

    def test_planted_git_argument_templates_are_refused(self) -> None:
        for config in (
            "pre-push:\n  jobs:\n    - args: \"'{1}' '{2}'\"\n",
            'pre-commit:\n  commands:\n    example:\n      run: "echo {0}"\n',
            'post-commit:\n  jobs:\n    - run: "echo {123}"\n',
        ):
            with self.subTest(config=config):
                result = run_with_fixture(
                    "test_hook_jobs_never_interpolate_git_arguments", {"lefthook.yml": config}
                )
                self.assertEqual(len(result.failures), 1)
                self.assertFalse(result.errors)

    def test_planted_python_minimum_is_refused(self) -> None:
        with patch.object(metadata, "metadata", return_value={"Requires-Python": ">=3.12"}):
            result = run_with_fixture(
                "test_contributing_python_minimum_supports_installed_pins",
                {"CONTRIBUTING.md": "Python 3.10 or newer\npython3 -m pip install numpy==2.5.4\n"},
            )
        self.assertEqual(len(result.failures), 1)
        self.assertFalse(result.errors)

    def test_contributing_uses_ignored_virtual_environment(self) -> None:
        document = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
        self.assertRegex(
            document,
            r"```bash\npython3 -m venv \.venv\n\. \.venv/bin/activate\npython3 -m pip install ",
        )
        self.assertIn(".venv/", (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines())

    def test_planted_virtual_environment_omissions_are_refused(self) -> None:
        document = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
        ignored = (ROOT / ".gitignore").read_text(encoding="utf-8")
        for missing in ("python3 -m venv .venv\n", ". .venv/bin/activate\n", ".venv/\n"):
            with self.subTest(missing=missing):
                result = run_with_fixture(
                    "test_contributing_uses_ignored_virtual_environment",
                    {
                        "CONTRIBUTING.md": document.replace(missing, ""),
                        ".gitignore": ignored.replace(missing, ""),
                    },
                )
                self.assertEqual(len(result.failures), 1)
                self.assertFalse(result.errors)

    def test_selector_scan_accepts_binary_hook_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "lefthook.yml").write_text("{}\n", encoding="utf-8")
            hook = root / ".lefthook" / "pre-push"
            hook.mkdir(parents=True)
            (hook / ".DS_Store").write_bytes(b"\xff")
            with patch(f"{__name__}.ROOT", root):
                self.test_hooks_start_python_through_selector()

    def test_hooks_start_python_through_selector(self) -> None:
        config = yaml.safe_load((ROOT / "lefthook.yml").read_text(encoding="utf-8"))
        commands = {}
        for hook, settings in config.items():
            if not isinstance(settings, dict):
                continue
            jobs = list(settings.get("commands", {}).values()) + settings.get("jobs", [])
            for index, job in enumerate(jobs):
                if "run" in job:
                    commands[f"lefthook.yml:{hook}:{index}"] = job["run"]
        for script in (ROOT / ".lefthook").rglob("*"):
            if script.is_file():
                commands[str(script.relative_to(ROOT))] = script.read_text(
                    encoding="utf-8", errors="replace"
                )
        for source, command in commands.items():
            with self.subTest(source=source):
                self.assertNotRegex(
                    command,
                    r"(?m)(?:^|[;&|])\s*(?:exec\s+)?(?:python3?|py\s+-3)(?=\s|$)",
                    "start Python through .config/lefthook/python.sh",
                )
