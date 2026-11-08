# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Reject Git subprocesses that inherit repository-selection environment variables."""

from __future__ import annotations

import ast
import unittest
from collections.abc import Iterator
from pathlib import Path

from tests.helpers import python_sources

ROOT = Path(__file__).resolve().parent.parent
GIT_SUBPROCESS_METHODS = frozenset({"run", "check_output", "check_call", "call", "Popen"})
HOOK_OVERRIDE = "core.hooksPath=/dev/null"


def _literal_git_calls(tree: ast.AST) -> Iterator[ast.Call]:
    """Yield literal Git subprocess calls from an AST."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if not isinstance(node.func.value, ast.Name) or node.func.value.id != "subprocess":
            continue
        if node.func.attr not in GIT_SUBPROCESS_METHODS or not node.args:
            continue
        command = node.args[0]
        if not isinstance(command, (ast.List, ast.Tuple)) or not command.elts:
            continue
        first = command.elts[0]
        if not isinstance(first, ast.Constant) or first.value != "git":
            continue
        yield node


def find_git_calls_without_env(source: str, *, filename: str) -> list[int]:
    """Return lines of literal Git subprocess calls without an explicit environment."""
    tree = ast.parse(source, filename=filename)
    findings = []
    for node in _literal_git_calls(tree):
        if any(keyword.arg == "env" for keyword in node.keywords):
            continue
        findings.append(node.lineno)
    return sorted(findings)


def find_fixture_commits_without_hook_override(source: str, *, filename: str) -> list[int]:
    """Return lines of fixture Git calls that disable signing but do not isolate hooks."""
    tree = ast.parse(source, filename=filename)
    findings = []
    for node in _literal_git_calls(tree):
        command = node.args[0]
        constants = [
            elt.value
            for elt in command.elts
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
        ]
        has_signing_disabled = any(val.lower() == "commit.gpgsign=false" for val in constants)
        has_hook_override = any(val == HOOK_OVERRIDE for val in constants)
        if has_signing_disabled and not has_hook_override:
            findings.append(node.lineno)
    return sorted(findings)


class GitSubprocessEnvironmentTest(unittest.TestCase):
    """Check the Git subprocess environment rule against examples and tracked tests."""

    def test_detector_reports_only_unisolated_literal_git_calls(self) -> None:
        """Detect literal Git calls without env while ignoring safe and unrelated calls."""
        source = """\
import subprocess
args = ["status"]
subprocess.run(["git", "init"])
subprocess.run(["git", *args])
subprocess.run(["git", "status"], env={})
subprocess.run(["python3", "-V"])
subprocess.run(("git", "status"))
"""
        self.assertEqual(find_git_calls_without_env(source, filename="planted.py"), [3, 4, 7])

    def test_hook_detector_reports_signing_override_without_hook_override(self) -> None:
        """Detect fixture Git calls disabling signing without overriding core.hooksPath."""
        source = (
            "import subprocess\n"
            'args = ["commit", "-qm", "msg"]\n'
            'subprocess.run(["git", "-c", "commit.gpgsign=false", *args], env={})\n'
            'subprocess.run(["git", "-c", "commit.gpgsign=false", "-c", '
            '"core.hooksPath=/dev/null", *args], env={})\n'
            'subprocess.run(["git", "-c", "commit.gpgSign=false", "commit"], env={})\n'
            'subprocess.run(["git", "status"], env={})\n'
        )
        self.assertEqual(
            find_fixture_commits_without_hook_override(source, filename="planted.py"),
            [3, 5],
        )

    def test_tracked_test_git_calls_have_isolated_environment(self) -> None:
        """Require every tracked test Git subprocess to pass an explicit environment."""
        findings = []
        tests_root = ROOT / "tests"
        for path in python_sources(ROOT):
            if tests_root in path.parents:
                findings.extend(
                    f"{path.relative_to(ROOT)}:{line}"
                    for line in find_git_calls_without_env(
                        path.read_text(encoding="utf-8"), filename=str(path)
                    )
                )
        self.assertEqual(findings, [], "unisolated Git subprocesses:\n" + "\n".join(findings))

    def test_tracked_test_fixture_commits_override_hooks(self) -> None:
        """Require fixture Git calls disabling signing to also isolate hook paths."""
        findings = []
        tests_root = ROOT / "tests"
        for path in python_sources(ROOT):
            if tests_root in path.parents:
                findings.extend(
                    f"{path.relative_to(ROOT)}:{line}"
                    for line in find_fixture_commits_without_hook_override(
                        path.read_text(encoding="utf-8"), filename=str(path)
                    )
                )
        self.assertEqual(
            findings,
            [],
            "fixture commits without hook override:\n" + "\n".join(findings),
        )


if __name__ == "__main__":
    unittest.main()
