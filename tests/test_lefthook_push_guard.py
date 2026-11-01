# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Keep the push guard active even when lefthook finds no push files."""

from __future__ import annotations

import os
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


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
        for key in ("args", "glob", "files", "root", "exclude", "file_types"):
            with self.subTest(key=key):
                self.assertNotIn(key, guard)

        script = ROOT / ".lefthook" / "pre-push" / guard["script"]
        self.assertTrue(script.is_file(), f"missing script: {script}")
        self.assertTrue(os.access(script, os.X_OK), f"script is not executable: {script}")
        self.assertIn(
            'exec sh .config/lefthook/python.sh scripts/push_guard.py "$@"',
            script.read_text(encoding="utf-8"),
        )

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
                commands[str(script.relative_to(ROOT))] = script.read_text(encoding="utf-8")
        for source, command in commands.items():
            with self.subTest(source=source):
                self.assertNotRegex(
                    command,
                    r"(?m)(?:^|[;&|])\s*(?:exec\s+)?(?:python3?|py\s+-3)(?=\s|$)",
                    "start Python through .config/lefthook/python.sh",
                )
