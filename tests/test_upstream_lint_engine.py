# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Replay the planted docstrings finding against the pinned Praetor engine."""

from __future__ import annotations

import os
import subprocess  # nosec B404 - fixed Git commands in disposable fixture repositories.
import tempfile
import unittest
from pathlib import Path

from scripts import upstream_lint
from tests.helpers import isolated_git_environment

BASE_SOURCE = "int ff_value(void)\n{\n    return 1;\n}\n"
STATIC_FUNCTION = (
    "static int clamp_positive(int value)\n"
    "{\n"
    "    if (value < 0) {\n"
    "        return 0;\n"
    "    }\n"
    "    return value;\n"
    "}\n"
)


@unittest.skipIf(
    os.environ.get("GITHUB_ACTIONS") == "true",
    "GitHub Actions runs no Praetor engine",
)
class DocstringsEngineTest(unittest.TestCase):
    """Exercise the docstrings step with the repository's pinned Praetor engine."""

    def setUp(self) -> None:
        """Create an isolated template-less Git repository with one base C file."""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name) / "tree"
        self.root.mkdir()
        template = Path(directory.name) / "template"
        template.mkdir()
        self.git("init", "-q", f"--template={template}")
        self.write("libavfilter/vf_x.c", BASE_SOURCE)
        self.commit("avfilter/vf_x: initial fixture")
        self.base = self.git("rev-parse", "HEAD").strip()

    def git(self, *args: str) -> str:
        """Run bounded Git commands with isolated environment and disabled signing."""
        result = subprocess.run(  # nosec B603 B607 - fixed Git, fixture-only arguments.
            [
                "git",
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@invalid",
                "-c",
                "commit.gpgsign=false",
                "-c",
                "core.hooksPath=/dev/null",
                *args,
            ],
            cwd=self.root,
            env=isolated_git_environment(),
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        return result.stdout

    def write(self, name: str, source: str) -> None:
        """Write one UTF-8 source file inside the disposable repository."""
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")

    def commit(self, subject: str) -> str:
        """Stage and commit all fixture edits and return the head revision."""
        self.git("add", ".")
        self.git("commit", "-qm", subject)
        return self.git("rev-parse", "HEAD").strip()

    def test_planted_undocumented_static_function_fails(self) -> None:
        """Adding an undocumented static C function with an if branch fails with rc 1."""
        self.write("libavfilter/vf_x.c", f"{BASE_SOURCE}\n{STATIC_FUNCTION}")
        head = self.commit("avfilter/vf_x: add clamp_positive")
        step = upstream_lint._collect(
            "docstrings",
            lambda: upstream_lint._docstrings(self.root, self.base, head, ""),
        )
        self.assertEqual(step.rc, 1, step.output)
        self.assertIn("clamp_positive", step.output)
        self.assertIn(
            "add a doc comment to each listed function, or pass --docstring-reason",
            step.output,
        )

    def test_documented_static_function_passes(self) -> None:
        """Adding the same static C function with a /** ... */ doc comment returns rc 0."""
        documented = f"{BASE_SOURCE}\n/** Clamp negative inputs to zero. */\n{STATIC_FUNCTION}"
        self.write("libavfilter/vf_x.c", documented)
        head = self.commit("avfilter/vf_x: add clamp_positive")
        step = upstream_lint._collect(
            "docstrings",
            lambda: upstream_lint._docstrings(self.root, self.base, head, ""),
        )
        self.assertEqual(step.rc, 0, step.output)
        self.assertIn("Docstring coverage: 100.00%", step.output)


if __name__ == "__main__":
    unittest.main()
