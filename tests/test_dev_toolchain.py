# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Tests for python.sh venv selection, Makefile Python recipes, and check_dev_deps."""

from __future__ import annotations

import importlib.metadata
import os
import re
import subprocess  # nosec B404 - fixed sh, git and interpreter argv in temporary test trees.
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.check_dev_deps import pin_findings
from tests.helpers import isolated_git_environment

ROOT = Path(__file__).resolve().parents[1]

_ALLOWED_WRAPPER = "sh .config/lefthook/python.sh"
_SINGLE_QUOTE_RE = re.compile(r"'[^']*'")
_DOUBLE_QUOTE_RE = re.compile(r'"[^"]*"')
_INTERPRETER_RE = re.compile(r"(?<![\w.-])(?:python[0-9.]*|py)(?![\w.-])")


def make_fake_venv(target_dir: Path) -> Path:
    """Create an executable fake python3 interpreter in target_dir/.venv/bin/."""
    venv_bin = target_dir / ".venv" / "bin"
    venv_bin.mkdir(parents=True, exist_ok=True)
    venv_py = venv_bin / "python3"
    script = (
        "#!/bin/sh\n"
        'for arg in "$@"; do\n'
        '  case "$arg" in\n'
        '    *version_info*) echo "3.99"; exit 0 ;;\n'
        "  esac\n"
        "done\n"
        'echo "venv-selected"\n'
    )
    venv_py.write_text(script, encoding="utf-8")
    venv_py.chmod(0o755)
    return venv_py


def run_python_sh(cwd: Path, ceiling: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Execute python.sh in a subprocess under a Git ceiling directory."""
    env = dict(os.environ)
    env["GIT_CEILING_DIRECTORIES"] = str(ceiling)
    cmd = ["sh", str(ROOT / ".config/lefthook/python.sh"), *args]
    return subprocess.run(  # nosec B603 - fixed sh argv runs the tracked python.sh.
        cmd,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def run_check_dev_deps(*args: str) -> subprocess.CompletedProcess[str]:
    """Execute scripts/check_dev_deps.py in a subprocess with arguments."""
    cmd = [sys.executable, "-B", str(ROOT / "scripts/check_dev_deps.py"), *args]
    return subprocess.run(  # nosec B603 - the current interpreter runs the tracked check_dev_deps.py.
        cmd,
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def run_with_requirements(content: str) -> subprocess.CompletedProcess[str]:
    """Execute check_dev_deps.py against temporary requirements file content."""
    with tempfile.TemporaryDirectory() as tmp:
        req_file = Path(tmp) / "requirements-dev.in"
        req_file.write_text(content, encoding="utf-8")
        return run_check_dev_deps("--requirements", str(req_file))


def run_git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Execute a Git command in a subprocess under the given directory."""
    return subprocess.run(  # nosec B603 B607 - fixed git argv in a temporary test tree.
        ["git", *args],
        cwd=cwd,
        env=isolated_git_environment(),
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def _direct_python_findings(makefile: str) -> list[str]:
    """Allow-list Makefile reader; allowed forms removed, any interpreter word left is a finding."""
    findings: list[str] = []
    for line in makefile.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        rest = line.replace(_ALLOWED_WRAPPER, "")
        rest = _SINGLE_QUOTE_RE.sub("", rest)
        rest = _DOUBLE_QUOTE_RE.sub("", rest)
        if _INTERPRETER_RE.search(rest):
            findings.append(line)
    return findings


class TestPythonShSelection(unittest.TestCase):
    """Test interpreter selection logic in .config/lefthook/python.sh."""

    def test_git_untracked_venv_selected(self) -> None:
        """Select untracked .venv/bin/python3 in a Git repository."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            make_fake_venv(tmp_path)
            proc_git = run_git(tmp_path, "init", "-q")
            self.assertEqual(proc_git.returncode, 0, proc_git.stderr)
            proc = run_python_sh(tmp_path, tmp_path.parent, "-c", "print('path-python')")
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout.strip(), "venv-selected")

    def test_git_tracked_venv_refused(self) -> None:
        """Refuse tracked .venv/bin/python3 and fall back to PATH Python."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            make_fake_venv(tmp_path)
            proc_init = run_git(tmp_path, "init", "-q")
            self.assertEqual(proc_init.returncode, 0, proc_init.stderr)
            proc_add = run_git(tmp_path, "add", "-f", ".venv/bin/python3")
            self.assertEqual(proc_add.returncode, 0, proc_add.stderr)
            proc = run_python_sh(tmp_path, tmp_path.parent, "-c", "print('path-python')")
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout.strip(), "path-python")

    def test_non_git_directory_uses_path(self) -> None:
        """Fall back to PATH Python when not inside a Git repository."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            make_fake_venv(tmp_path)
            proc = run_python_sh(tmp_path, tmp_path.parent, "-c", "print('path-python')")
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout.strip(), "path-python")

    def test_git_without_venv_uses_path(self) -> None:
        """Fall back to PATH Python when no .venv exists in a Git repository."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            proc_init = run_git(tmp_path, "init", "-q")
            self.assertEqual(proc_init.returncode, 0, proc_init.stderr)
            proc = run_python_sh(tmp_path, tmp_path.parent, "-c", "print('path-python')")
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout.strip(), "path-python")


class TestMakefilePythonGuard(unittest.TestCase):
    """Guard against direct Python invocations in Makefile recipes."""

    def test_real_makefile_has_no_direct_python(self) -> None:
        """Ensure Makefile and engine.mk contain zero direct Python invocations."""
        makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
        self.assertEqual(_direct_python_findings(makefile), [])
        engine_mk = ROOT / ".config/praetor/engine.mk"
        if engine_mk.exists():
            self.assertEqual(_direct_python_findings(engine_mk.read_text(encoding="utf-8")), [])

    def test_planted_direct_python_detected(self) -> None:
        """Verify each planted direct Python snippet produces one finding."""
        planted_snippets = [
            "\t@python3 scripts/x.py\n",
            "\t\tpython3 scripts/series.py lengths t; \\\n",
            "\t@x.py -- python3 -m unittest\n",
            "\t@if true; then python3 x.py; fi\n",
            "\t@FOO=1 python3 x.py\n",
            "\t@env python3 x.py\n",
            "X := $(shell python3 -c 1)\n",
            "\t@/usr/bin/python3 x.py\n",
            "\t@py -3 x.py\n",
            "\t@python3.14 x.py\n",
            "\t@sh .config/lefthook/python.sh a.py && python b.py\n",
        ]
        for snippet in planted_snippets:
            with self.subTest(snippet=snippet):
                findings = _direct_python_findings(snippet)
                self.assertEqual(len(findings), 1)

    def test_makefile_controls_give_no_findings(self) -> None:
        """Verify valid Python wrappers and comments trigger zero findings."""
        controls = [
            "\t@sh .config/lefthook/python.sh -B scripts/x.py\n",
            "\t\techo 'pipx install --python python3.14 semgrep' >&2; \\\n",
            "# python3 scripts/x.py\n",
            "DIST_FILES = scripts/series.py README.md\n",
            '\t@echo "run python3 -m venv .venv" >&2\n',
        ]
        for control in controls:
            with self.subTest(snippet=control):
                self.assertEqual(_direct_python_findings(control), [])


class TestCheckDevDeps(unittest.TestCase):
    """Test verification of pinned development dependencies."""

    def test_real_requirements_pass(self) -> None:
        """Pass when checking the repository's real requirements-dev.in."""
        proc = run_check_dev_deps()
        self.assertEqual(proc.returncode, 0)
        self.assertIn("pins and the tree-sitter C parser load", proc.stdout)

    def test_tree_sitter_blocked_fails(self) -> None:
        """Fail when tree-sitter or tree-sitter-c import is blocked."""
        source = (
            "import sys, runpy\n"
            "sys.modules['tree_sitter'] = None\n"
            "sys.modules['tree_sitter_c'] = None\n"
            "sys.argv = ['check_dev_deps.py']\n"
            f"runpy.run_path({str(ROOT / 'scripts/check_dev_deps.py')!r}, run_name='__main__')\n"
        )
        proc = subprocess.run(  # nosec B603 - the current interpreter runs a fixed inline script.
            [sys.executable, "-B", "-c", source],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("tree-sitter", proc.stderr)
        self.assertIn("requirements-dev.txt", proc.stderr)
        self.assertIn(".venv", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)

    def test_outdated_pin_fails(self) -> None:
        """Fail with installed version reported when a pin is outdated."""
        proc = run_with_requirements("tree-sitter==0.0.1\n")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("tree-sitter==0.0.1: installed", proc.stderr)

    def test_missing_package_fails(self) -> None:
        """Fail when a pinned package is missing from the environment."""
        proc = run_with_requirements("no-such-package-vmafx==1.0\n")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("no-such-package-vmafx==1.0: not installed", proc.stderr)

    def test_invalid_pin_syntax_fails(self) -> None:
        """Fail when a requirement line does not follow exact name==version."""
        proc = run_with_requirements("tree-sitter>=0.26\n")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("expected name==version", proc.stderr)

    def test_comment_only_requirements_passes(self) -> None:
        """Succeed with 0 pins reported for a comment-only requirements file."""
        proc = run_with_requirements("# only a comment\n")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("0 pins", proc.stdout)

    def test_unreadable_requirements_fails(self) -> None:
        """Fail when requirements path is missing or names a directory."""
        with tempfile.TemporaryDirectory() as tmp:
            missing_path = Path(tmp) / "missing.in"
            proc_missing = run_check_dev_deps("--requirements", str(missing_path))
            self.assertEqual(proc_missing.returncode, 1)
            self.assertIn("cannot read", proc_missing.stderr)
            self.assertNotIn("Traceback", proc_missing.stderr)

            proc_dir = run_check_dev_deps("--requirements", tmp)
            self.assertEqual(proc_dir.returncode, 1)
            self.assertIn("cannot read", proc_dir.stderr)
            self.assertNotIn("Traceback", proc_dir.stderr)


class TestPinFindings(unittest.TestCase):
    """Test pin_findings with mock version lookups."""

    def test_pin_findings_match_mismatch_missing(self) -> None:
        """Verify pin_findings returns empty on match and findings on mismatch or missing."""

        def fake_version_of(name: str) -> str:
            """Provide deterministic mock package versions."""
            db = {"pkg-a": "1.0.0", "pkg-b": "1.2.3"}
            if name not in db:
                raise importlib.metadata.PackageNotFoundError(name)
            return db[name]

        self.assertEqual(pin_findings([("pkg-a", "1.0.0")], version_of=fake_version_of), [])
        self.assertEqual(
            pin_findings([("pkg-b", "2.0.0")], version_of=fake_version_of),
            ["pkg-b==2.0.0: installed 1.2.3"],
        )
        self.assertEqual(
            pin_findings([("pkg-c", "1.0.0")], version_of=fake_version_of),
            ["pkg-c==1.0.0: not installed"],
        )


if __name__ == "__main__":
    unittest.main()
