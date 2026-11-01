# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Require rationales on Python subprocess security suppressions."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SUBPROCESS_CALLS = frozenset({"run", "Popen", "call", "check_call", "check_output"})


def find_subprocess_calls_without_nosec(source: str) -> list[int]:
    """Return line numbers of subprocess calls without a suppression rationale."""

    lines = source.splitlines()
    offenders = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        owner = node.func.value
        if not isinstance(owner, ast.Name) or owner.id != "subprocess":
            continue
        if node.func.attr not in SUBPROCESS_CALLS:
            continue
        comments = lines[node.lineno - 1 : node.end_lineno]
        if not any("# nosec" in line and " - " in line for line in comments):
            offenders.append(node.lineno)
    return sorted(offenders)


def python_sources() -> list[Path]:
    """Return Python modules and shebang-identified extension-less scripts."""

    sources = [*ROOT.joinpath("tests").rglob("*.py"), *ROOT.joinpath("scripts").rglob("*.py")]
    for path in ROOT.joinpath("scripts").rglob("*"):
        if path.is_file() and not path.suffix:
            first_line = path.read_text().splitlines()[:1]
            if first_line and first_line[0].startswith("#!") and "python" in first_line[0]:
                sources.append(path)
    return sorted(set(sources))


class SubprocessNosecTest(unittest.TestCase):
    def test_call_without_nosec_rationale_is_reported(self) -> None:
        """A planted unannotated subprocess call produces one offender."""

        source = "import subprocess\nsubprocess.run(['tool'])\n"
        self.assertEqual(find_subprocess_calls_without_nosec(source), [2])

    def test_call_with_nosec_rationale_is_accepted(self) -> None:
        """A planted subprocess call with a rationale produces no offenders."""

        source = "\n".join(
            (
                "import subprocess",
                "subprocess.run(  # nosec B603 - fixed test argv.",
                "    ['tool']",
                ")",
                "",
            )
        )
        self.assertEqual(find_subprocess_calls_without_nosec(source), [])

    def test_repository_subprocess_calls_have_nosec_rationales(self) -> None:
        """Every in-scope subprocess call carries an inline nosec rationale."""

        offenders = []
        for path in python_sources():
            source = path.read_text()
            offenders.extend(
                f"{path.relative_to(ROOT)}:{line}"
                for line in find_subprocess_calls_without_nosec(source)
            )
        self.assertEqual(offenders, [], "missing nosec rationale:\n" + "\n".join(offenders))


if __name__ == "__main__":
    unittest.main()
