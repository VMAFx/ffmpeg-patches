# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Keep the push guard active even when lefthook finds no push files."""

from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
from collections.abc import Iterator
from importlib import metadata
from pathlib import Path
from unittest.mock import patch

import yaml

from scripts.lint_mutable_defaults import PYLINT_VERSION

ROOT = Path(__file__).resolve().parents[1]
SERIES_INSTALL_COMMAND = "python3 -m pip install --require-hashes -r requirements-dev.txt"


def _series_steps(root: Path) -> list[dict]:
    """Load all steps from the series workflow."""
    workflow = yaml.safe_load((root / ".github/workflows/series.yml").read_text(encoding="utf-8"))
    return [step for job in workflow["jobs"].values() for step in job["steps"]]


def _series_install_steps(workflow: dict) -> list[dict]:
    """Find all pinned dependency installation steps in the series workflow."""
    return [
        step
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
        if step.get("name") == "Install the pinned development tools"
    ]


def _series_install_findings(workflow: dict) -> list[str]:
    """Require pinned installs before each job's dependency-consuming steps."""
    install_steps = _series_install_steps(workflow)
    findings = [] if install_steps else ["workflow has no pinned development tools install"]
    for step in install_steps:
        if step.get("run") != SERIES_INSTALL_COMMAND:
            findings.append("install step must run the pinned command")
    for job_name, job in workflow["jobs"].items():
        installed = False
        for step in job.get("steps", []):
            command = step.get("run", "")
            for line in command.splitlines():
                if (
                    line.strip().startswith("python3 -m pip install ")
                    and line.strip() != SERIES_INSTALL_COMMAND
                ):
                    findings.append(f"{job_name}: pip install must use the pinned command")
            if not installed and re.search(
                r"\bscripts/series\.py\s+lengths\b|\bmake\s+test\b", command
            ):
                findings.append(f"{job_name}: install pinned development tools before {command}")
            if step in install_steps and command == SERIES_INSTALL_COMMAND:
                installed = True
    return findings


def _normalize_package_name(name: str) -> str:
    """Normalize package names according to PEP 503."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _development_pins(content: str) -> set[tuple[str, str]]:
    """Parse exact development pins, refusing every other requirement shape."""
    pins = set()
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9._-]*)==([0-9][A-Za-z0-9.!+_-]*)", line)
        if match is None:
            raise ValueError(f"expected name==version: {line!r}")
        name, version = match.groups()
        pins.add((_normalize_package_name(name), version))
    return pins


def _hashed_lock_pins(content: str) -> set[tuple[str, str]]:
    """Read lockfile pins immediately followed by a SHA-256 hash line."""
    return {
        (_normalize_package_name(name), version)
        for name, version in re.findall(
            r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s]+) \\\n"
            r"[ \t]+--hash=sha256:[0-9a-f]{64}(?=\s|$)",
            content,
            re.MULTILINE,
        )
    }


def _reuse_ci_findings(makefile: str, requirements: str) -> list[str]:
    """Find duplicate REUSE recipes and development dependency pins."""
    findings = []
    if re.search(r"(?m)^\t[@+-]*[ \t]*reuse[ \t]+lint\b", makefile):
        findings.append("Makefile runs reuse lint outside the REUSE workflow")
    if any(name == "reuse" for name, _ in _development_pins(requirements)):
        findings.append("requirements-dev.in pins reuse outside the REUSE workflow")
    return findings


def _pylint_custom_manager(config: dict) -> dict:
    """Require exactly one regex manager for the Pylint script pin."""
    managers = [
        manager
        for manager in config.get("customManagers", [])
        if manager.get("customType") == "regex" and manager.get("depNameTemplate") == "pylint"
    ]
    if len(managers) != 1:
        raise AssertionError("expected exactly one Pylint regex manager")
    return managers[0]


def _praetor_custom_manager(config: dict) -> dict:
    """Require exactly one regex manager for the Praetor engine pin."""
    managers = [
        manager
        for manager in config.get("customManagers", [])
        if manager.get("customType") == "regex" and manager.get("depNameTemplate") == "praetor"
    ]
    if len(managers) != 1:
        raise AssertionError("expected exactly one Praetor regex manager")
    return managers[0]


def run_with_fixture(method: str, files: dict[str, str]) -> unittest.TestResult:
    """Replay a policy check against planted files without touching the checkout."""
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        for name, content in files.items():
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_text(content, encoding="utf-8")
        result = unittest.TestResult()
        with patch(f"{__name__}.ROOT", root):
            LefthookPushGuardTest(method).run(result)
        return result


def iter_hook_jobs(root: Path) -> Iterator[tuple[str, int, dict]]:
    """Yield each lefthook job with its hook name and index."""

    config = yaml.safe_load((root / "lefthook.yml").read_text(encoding="utf-8"))
    for hook, settings in config.items():
        if not isinstance(settings, dict):
            continue
        jobs = list(settings.get("commands", {}).values()) + settings.get("jobs", [])
        for index, job in enumerate(jobs):
            yield hook, index, job


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
        """CI jobs install the documented hashed lockfile before consuming dependencies."""
        workflow = yaml.safe_load((ROOT / ".github/workflows/series.yml").read_text("utf-8"))
        self.assertEqual(_series_install_findings(workflow), [])
        prefix = "python3 -m pip install "
        documented_commands = [
            line
            for line in (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8").splitlines()
            if line.startswith(prefix)
        ]
        self.assertEqual(documented_commands, [SERIES_INSTALL_COMMAND])

    def test_planted_missing_or_late_job_install_is_refused(self) -> None:
        """Reject missing or late installs even when another job installs dependencies."""
        install = {
            "name": "Install the pinned development tools",
            "run": SERIES_INSTALL_COMMAND,
        }
        for command in ("python3 scripts/series.py lengths ffmpeg", "make test"):
            for steps in ([{"run": command}], [{"run": command}, install]):
                with self.subTest(command=command, steps=steps):
                    workflow = {
                        "jobs": {
                            "check": {"steps": [install, {"run": "make test"}]},
                            "apply-build": {"steps": steps},
                        }
                    }
                    self.assertEqual(
                        _series_install_findings(workflow),
                        [f"apply-build: install pinned development tools before {command}"],
                    )

    def test_planted_second_install_command_drift_is_refused(self) -> None:
        """Reject any named install step whose command differs from the pinned install."""
        for command in (SERIES_INSTALL_COMMAND + " --no-deps", "echo skipped install"):
            with self.subTest(command=command):
                workflow = {
                    "jobs": {
                        "check": {
                            "steps": [
                                {
                                    "name": "Install the pinned development tools",
                                    "run": SERIES_INSTALL_COMMAND,
                                },
                                {"name": "Install the pinned development tools", "run": command},
                            ]
                        }
                    }
                }
                self.assertIn(
                    "install step must run the pinned command", _series_install_findings(workflow)
                )

    def test_job_without_dependency_consumers_needs_no_install(self) -> None:
        """Allow jobs without lengths or make test to omit dependency installation."""
        workflow = {
            "jobs": {
                "check": {
                    "steps": [
                        {
                            "name": "Install the pinned development tools",
                            "run": SERIES_INSTALL_COMMAND,
                        },
                        {"run": "make test"},
                    ]
                },
                "other": {"steps": [{"run": "echo no dependencies"}]},
                "reusable": {"uses": "./.github/workflows/reuse.yml"},
            }
        }
        self.assertEqual(_series_install_findings(workflow), [])

    def test_planted_install_command_drift_is_refused(self) -> None:
        """Reject altered installation commands in CI and altered or duplicate documentation."""
        expected = "python3 -m pip install --require-hashes -r requirements-dev.txt"
        for commands, document in (
            ([expected + " --no-deps"], expected),
            ([expected], expected + " --no-deps"),
            ([expected], expected + "\n" + expected),
            ([expected, expected + " --no-deps"], expected),
        ):
            with self.subTest(commands=commands, document=document):
                steps = [
                    {
                        "name": "Install the pinned development tools" if index == 0 else "Other",
                        "run": command,
                    }
                    for index, command in enumerate(commands)
                ]
                result = run_with_fixture(
                    "test_contributing_dependency_pins_match_ci",
                    {
                        ".github/workflows/series.yml": yaml.safe_dump(
                            {"jobs": {"check": {"steps": steps}}}
                        ),
                        "CONTRIBUTING.md": document,
                    },
                )
                self.assertEqual(len(result.failures), 1)
                self.assertFalse(result.errors)

    def test_series_pylint_pin_matches_script(self) -> None:
        """The development Pylint pin matches the gate's PYLINT_VERSION."""
        pins = _development_pins((ROOT / "requirements-dev.in").read_text(encoding="utf-8"))
        self.assertIn(("pylint", PYLINT_VERSION), pins)

    def test_reuse_ci_has_no_makefile_or_development_pin(self) -> None:
        """Keep REUSE CI lint exclusively in the REUSE workflow."""
        self.assertEqual(
            _reuse_ci_findings(
                (ROOT / "Makefile").read_text(encoding="utf-8"),
                (ROOT / "requirements-dev.in").read_text(encoding="utf-8"),
            ),
            [],
        )

    def test_planted_duplicate_reuse_paths_are_refused(self) -> None:
        """Reject a planted REUSE recipe or development pin independently."""
        self.assertEqual(_reuse_ci_findings("lint:\n\t@ruff check scripts tests\n", ""), [])
        for makefile, requirements in (
            ("lint:\n\t@reuse lint\n", ""),
            ("", "reuse==6.2.0\n"),
        ):
            with self.subTest(makefile=makefile, requirements=requirements):
                self.assertEqual(len(_reuse_ci_findings(makefile, requirements)), 1)

    def test_reuse_workflow_action_is_sha_pinned(self) -> None:
        """Require exactly one REUSE action pinned to a full commit SHA."""
        workflow = yaml.safe_load(
            (ROOT / ".github/workflows/reuse.yml").read_text(encoding="utf-8")
        )
        actions = [
            step["uses"]
            for job in workflow["jobs"].values()
            for step in job["steps"]
            if step.get("uses", "").startswith("fsfe/reuse-action@")
        ]
        self.assertEqual(len(actions), 1)
        self.assertRegex(actions[0], r"\Afsfe/reuse-action@[0-9a-fA-F]{40}\Z")

    def test_planted_reuse_workflow_action_refs_are_refused(self) -> None:
        """Reject missing, duplicate, floating and shortened REUSE action refs."""
        pinned = "fsfe/reuse-action@" + "a" * 40
        for actions in ([], [pinned, pinned], ["fsfe/reuse-action@v6.0.0"], [pinned[:-1]]):
            with self.subTest(actions=actions):
                workflow = {"jobs": {"reuse": {"steps": [{"uses": ref} for ref in actions]}}}
                result = run_with_fixture(
                    "test_reuse_workflow_action_is_sha_pinned",
                    {".github/workflows/reuse.yml": yaml.safe_dump(workflow)},
                )
                self.assertEqual(len(result.failures), 1)
                self.assertFalse(result.errors)

    def test_renovate_pylint_manager_matches_script(self) -> None:
        """Renovate discovers the script pin exactly once with its current version."""
        config = json.loads((ROOT / "renovate.json").read_text(encoding="utf-8"))
        manager = _pylint_custom_manager(config)
        self.assertEqual(manager["datasourceTemplate"], "pypi")
        self.assertEqual(len(manager["managerFilePatterns"]), 1)
        script = "scripts/lint_mutable_defaults.py"
        self.assertRegex(script, manager["managerFilePatterns"][0].strip("/"))
        self.assertEqual(len(manager["matchStrings"]), 1)
        pattern = re.sub(r"\(\?<([A-Za-z_][A-Za-z_0-9]*)>", r"(?P<\1>", manager["matchStrings"][0])
        matches = list(re.finditer(pattern, (ROOT / script).read_text(encoding="utf-8")))
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].group("currentValue"), PYLINT_VERSION)

    def test_renovate_groups_pylint_updates(self) -> None:
        """Renovate groups the Pylint script pin and development lock updates."""
        config = json.loads((ROOT / "renovate.json").read_text(encoding="utf-8"))
        self.assertEqual(_pylint_custom_manager(config)["depNameTemplate"], "pylint")
        self.assertTrue(
            any(
                rule.get("matchPackageNames") == ["pylint"] and rule.get("groupName") == "pylint"
                for rule in config["packageRules"]
            )
        )

    def test_planted_renovate_pylint_omissions_are_refused(self) -> None:
        """Reject a missing Pylint manager or package grouping rule."""
        config = json.loads((ROOT / "renovate.json").read_text(encoding="utf-8"))
        without_manager = {key: value for key, value in config.items() if key != "customManagers"}
        with self.assertRaisesRegex(AssertionError, "exactly one Pylint regex manager"):
            _pylint_custom_manager(without_manager)
        without_group = dict(config, packageRules=[])
        result = run_with_fixture(
            "test_renovate_groups_pylint_updates", {"renovate.json": json.dumps(without_group)}
        )
        self.assertEqual(len(result.failures), 1)
        self.assertFalse(result.errors)

    def test_renovate_praetor_manager_matches_workflow(self) -> None:
        """Renovate discovers the workflow's full Praetor digest exactly once."""
        config = json.loads((ROOT / "renovate.json").read_text(encoding="utf-8"))
        manager = _praetor_custom_manager(config)
        self.assertEqual(manager["datasourceTemplate"], "git-refs")
        self.assertEqual(manager["currentValueTemplate"], "main")
        self.assertEqual(manager["packageNameTemplate"], "https://github.com/cordanaLLM/praetor")
        self.assertEqual(len(manager["managerFilePatterns"]), 1)
        workflow = ".github/workflows/series.yml"
        self.assertRegex(workflow, manager["managerFilePatterns"][0].strip("/"))
        self.assertEqual(len(manager["matchStrings"]), 1)
        pattern = re.sub(r"\(\?<([A-Za-z_][A-Za-z_0-9]*)>", r"(?P<\1>", manager["matchStrings"][0])
        content = (ROOT / workflow).read_text(encoding="utf-8")
        matches = list(re.finditer(pattern, content))
        self.assertEqual(len(matches), 1)
        digest = matches[0].group("currentDigest")
        self.assertEqual(digest, yaml.safe_load(content)["env"]["PRAETOR_REF"])
        self.assertRegex(digest, r"\A[0-9a-f]{40}\Z")

    def test_renovate_praetor_has_no_release_age(self) -> None:
        """Renovate exempts the Praetor git digest from timestamp-based release age."""
        config = json.loads((ROOT / "renovate.json").read_text(encoding="utf-8"))
        manager = _praetor_custom_manager(config)
        self.assertTrue(
            any(
                "minimumReleaseAge" in rule
                and rule["minimumReleaseAge"] is None
                and manager["packageNameTemplate"] in rule.get("matchPackageNames", [])
                and "git-refs" in rule.get("matchDatasources", [])
                for rule in config["packageRules"]
            )
        )

    def test_planted_renovate_praetor_omissions_are_refused(self) -> None:
        """Reject a missing or duplicate Praetor manager and missing release-age rule."""
        config = json.loads((ROOT / "renovate.json").read_text(encoding="utf-8"))
        manager = _praetor_custom_manager(config)
        others = [item for item in config["customManagers"] if item != manager]
        for managers in (others, [*others, manager, manager]):
            with (
                self.subTest(managers=managers),
                self.assertRaisesRegex(AssertionError, "exactly one Praetor regex manager"),
            ):
                _praetor_custom_manager(dict(config, customManagers=managers))
        without_rule = dict(
            config,
            packageRules=[
                rule
                for rule in config["packageRules"]
                if manager["packageNameTemplate"] not in rule.get("matchPackageNames", [])
            ],
        )
        result = run_with_fixture(
            "test_renovate_praetor_has_no_release_age", {"renovate.json": json.dumps(without_rule)}
        )
        self.assertEqual(len(result.failures), 1)
        self.assertFalse(result.errors)

    def test_planted_renovate_praetor_invalid_refs_are_refused(self) -> None:
        """Reject shortened, tagged, uppercase and overlong Praetor pins."""
        config = json.loads((ROOT / "renovate.json").read_text(encoding="utf-8"))
        manager = _praetor_custom_manager(config)
        pattern = re.sub(r"\(\?<([A-Za-z_][A-Za-z_0-9]*)>", r"(?P<\1>", manager["matchStrings"][0])
        content = (ROOT / ".github/workflows/series.yml").read_text(encoding="utf-8")
        digest = yaml.safe_load(content)["env"]["PRAETOR_REF"]
        for invalid in (digest[:7], "v1.0.0", digest.upper(), digest + "a"):
            with self.subTest(ref=invalid):
                self.assertEqual(list(re.finditer(pattern, content.replace(digest, invalid))), [])

    def test_development_lock_matches_input_pins(self) -> None:
        """Every development input pin has the same version and hashes in the lockfile."""
        pins = _development_pins((ROOT / "requirements-dev.in").read_text(encoding="utf-8"))
        self.assertTrue(pins)
        locked = _hashed_lock_pins((ROOT / "requirements-dev.txt").read_text(encoding="utf-8"))
        for pin in sorted(pins):
            with self.subTest(pin=pin):
                self.assertIn(pin, locked)

    def test_development_pin_parser_boundaries(self) -> None:
        """Accept comments and normalized exact pins while refusing ranges and bare names."""
        self.assertEqual(_development_pins("\n # comment\n"), set())
        self.assertEqual(
            _development_pins("PyYAML==6.0.3\nSome._Name==1.0\n"),
            {("pyyaml", "6.0.3"), ("some-name", "1.0")},
        )
        for requirement in ("ruff>=0.16", "ruff", "ruff==", "ruff==0.16.*"):
            with self.subTest(requirement=requirement), self.assertRaises(ValueError):
                _development_pins(requirement)

    def test_planted_lock_drift_and_missing_hashes_are_refused(self) -> None:
        """Reject changed versions, absent pins, missing hashes and malformed hashes."""
        hash_line = f"    --hash=sha256:{'a' * 64}\n"
        for locked in (
            "",
            "ruff==0.16.8\n",
            f"ruff==0.16.7 \\\n{hash_line}",
            "ruff==0.16.8 \\\n    --hash=sha256:abc\n",
        ):
            with self.subTest(locked=locked):
                result = run_with_fixture(
                    "test_development_lock_matches_input_pins",
                    {"requirements-dev.in": "ruff==0.16.8\n", "requirements-dev.txt": locked},
                )
                self.assertEqual(len(result.failures), 1)
                self.assertFalse(result.errors)

    def test_lock_pin_names_are_normalized(self) -> None:
        """Match mixed-case lockfile names and collapsed punctuation in package names."""
        locked = f"PyYAML==6.0.3 \\\n    --hash=sha256:{'a' * 64}\n"
        locked += f"some._Name==1.0 \\\n    --hash=sha256:{'b' * 64}\n"
        self.assertEqual(_hashed_lock_pins(locked), {("pyyaml", "6.0.3"), ("some-name", "1.0")})

    def test_series_pip_installs_have_no_inline_pins(self) -> None:
        """Every series workflow pip install keeps version pins in requirements files."""
        for step in _series_steps(ROOT):
            command = step.get("run", "")
            if re.search(r"\bpip(?:3)?\s+install\b", command):
                with self.subTest(step=step.get("name")):
                    self.assertNotIn("==", command)

    def test_planted_inline_pip_pins_are_refused(self) -> None:
        """Reject inline pip pins in any job, including multiline run commands."""
        workflow = (
            "jobs:\n  other:\n    steps:\n      - run: |\n"
            "          python3 -m pip install ruff==0.16.8\n"
        )
        result = run_with_fixture(
            "test_series_pip_installs_have_no_inline_pins",
            {".github/workflows/series.yml": workflow},
        )
        self.assertEqual(len(result.failures), 1)
        self.assertFalse(result.errors)

    def test_hook_jobs_never_interpolate_git_arguments(self) -> None:
        """No lefthook run/args field contains a {N} positional git-argument placeholder."""

        for hook, index, job in iter_hook_jobs(ROOT):
            for field in ("run", "args"):
                with self.subTest(hook=hook, job=index, field=field):
                    self.assertNotRegex(str(job.get(field, "")), r"\{\d+\}")

    def test_contributing_python_minimum_supports_installed_pins(self) -> None:
        """The documented Python minimum supports all pinned development tools."""
        document = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
        minimum = re.search(r"Python (\d+)\.(\d+) or newer", document)
        self.assertIsNotNone(minimum)
        documented = tuple(map(int, minimum.groups()))
        pins = _development_pins((ROOT / "requirements-dev.in").read_text(encoding="utf-8"))
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
        """A documented Python minimum below a pin's Requires-Python fails the check."""
        with patch.object(metadata, "metadata", return_value={"Requires-Python": ">=3.12"}):
            result = run_with_fixture(
                "test_contributing_python_minimum_supports_installed_pins",
                {
                    "CONTRIBUTING.md": "Python 3.10 or newer\n",
                    "requirements-dev.in": "numpy==2.5.4\n",
                },
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
        """Every hook command and .lefthook script starts Python through the selector."""

        commands = {}
        for hook, index, job in iter_hook_jobs(ROOT):
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
