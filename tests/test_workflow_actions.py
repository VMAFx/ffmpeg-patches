# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Keep shared workflow setup in its composite actions."""

from __future__ import annotations

import os
import subprocess  # nosec B404 - only the local grep action runs against fixtures.
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
PYTHON_ACTION = ROOT / ".github/actions/setup-python/action.yml"
BASE_ACTION = ROOT / ".github/actions/load-base/action.yml"
BASE_EXPORT = "grep -E '^FFMPEG_(REMOTE|TAG|COMMIT)=' base.env >> \"$GITHUB_ENV\""


def github_yaml_files() -> list[Path]:
    """Enumerate GitHub YAML files, including workflows and composite actions."""
    return sorted(
        path for path in (ROOT / ".github").rglob("*") if path.suffix in {".yml", ".yaml"}
    )


def load_base_action() -> dict:
    """Read the actual shared base-loading action."""
    return yaml.safe_load(BASE_ACTION.read_text(encoding="utf-8"))


def run_load_base(contents: str) -> tuple[subprocess.CompletedProcess[str], str]:
    """Execute the action's actual script with isolated input and output files."""
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "base.env").write_text(contents, encoding="utf-8")
        github_env = root / "github-env"
        github_env.touch()
        result = subprocess.run(  # nosec B603 B607 - fixed bash, fixture-only local input.
            [
                "bash",
                "--noprofile",
                "--norc",
                "-eo",
                "pipefail",
                "-c",
                load_base_action()["runs"]["steps"][0]["run"],
            ],
            cwd=root,
            env=dict(os.environ, GITHUB_ENV=str(github_env)),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return result, github_env.read_text(encoding="utf-8")


class WorkflowActionsTest(unittest.TestCase):
    def assert_python_setup_is_shared(self, text: str) -> None:
        """Reject direct setup-python uses in workflow or action steps."""
        document = yaml.safe_load(text)
        steps = list(document.get("runs", {}).get("steps", []))
        for job in document.get("jobs", {}).values():
            steps.extend(job.get("steps", []))
        for step in steps:
            self.assertNotEqual(step.get("uses", "").split("@", 1)[0], "actions/setup-python")

    def assert_base_loading_is_shared(self, text: str) -> None:
        """Reject inline base exports outside the shared action."""
        self.assertNotIn('base.env >> "$GITHUB_ENV"', text)

    def test_python_setup_has_single_owner(self) -> None:
        """Only the Python composite action may use actions/setup-python directly."""
        for path in github_yaml_files():
            if path != PYTHON_ACTION:
                with self.subTest(path=path.relative_to(ROOT)):
                    self.assert_python_setup_is_shared(path.read_text(encoding="utf-8"))

    def test_base_loading_has_single_owner(self) -> None:
        """Only load-base may export the pinned base inline."""
        for path in github_yaml_files():
            if path != BASE_ACTION:
                with self.subTest(path=path.relative_to(ROOT)):
                    self.assert_base_loading_is_shared(path.read_text(encoding="utf-8"))

    def test_planted_raw_python_setup_is_rejected(self) -> None:
        """Reject planted raw setup-python steps in workflows and composites."""
        for fixture in (
            "jobs:\n  build:\n    steps:\n      - uses: actions/setup-python@planted\n",
            "runs:\n  using: composite\n  steps:\n    - uses: actions/setup-python@planted\n",
        ):
            with self.subTest(fixture=fixture), self.assertRaises(AssertionError):
                self.assert_python_setup_is_shared(fixture)

    def test_planted_inline_base_export_is_rejected(self) -> None:
        """Reject a planted inline copy of the base export."""
        fixture = f"jobs:\n  build:\n    steps:\n      - run: {BASE_EXPORT}\n"
        with self.assertRaises(AssertionError):
            self.assert_base_loading_is_shared(fixture)

    def test_load_base_action_contract(self) -> None:
        """The composite action runs exactly the pinned-base grep under bash."""
        action = load_base_action()
        self.assertEqual(action["name"], "Load the pinned base")
        self.assertEqual(
            action["runs"],
            {"using": "composite", "steps": [{"shell": "bash", "run": BASE_EXPORT}]},
        )

    def test_load_base_exports_only_pinned_keys(self) -> None:
        """Export all three pinned values while excluding unrelated variables."""
        expected = (
            "FFMPEG_REMOTE=https://example.invalid/ffmpeg.git\n"
            "FFMPEG_TAG=n8.0\nFFMPEG_COMMIT=abc123\n"
        )
        result, exported = run_load_base(expected + "FOO=1\nFFMPEG_TAG_EXTRA=excluded\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(exported, expected)

    def test_load_base_without_pinned_keys_fails(self) -> None:
        """No pinned keys yield grep rc 1, which must fail the step."""
        result, exported = run_load_base("FOO=1\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(exported, "")


if __name__ == "__main__":
    unittest.main()
