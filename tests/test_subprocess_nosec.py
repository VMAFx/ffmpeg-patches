# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Require rationales on Python subprocess security suppressions."""

from __future__ import annotations

import ast
import io
import re
import tokenize
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SUBPROCESS_CALLS = frozenset({"run", "Popen", "call", "check_call", "check_output"})
NOSEC_RATIONALE = re.compile(r"^#\s*nosec\b.*\s-\s+\S")


def find_subprocess_calls_without_nosec(source: str, *, filename: str = "<unknown>") -> list[int]:
    """Return line numbers of subprocess calls without a suppression rationale."""

    tree = ast.parse(source, filename=filename)
    module_names = {"subprocess"}
    function_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            module_names.update(
                name.asname or name.name for name in node.names if name.name == "subprocess"
            )
        elif isinstance(node, ast.ImportFrom) and node.module == "subprocess":
            function_names.update(
                name.asname or name.name for name in node.names if name.name in SUBPROCESS_CALLS
            )
    comments: dict[int, list[str]] = {}
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT:
            comments.setdefault(token.start[0], []).append(token.string)
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        is_module_call = (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in module_names
            and node.func.attr in SUBPROCESS_CALLS
        )
        is_imported_call = isinstance(node.func, ast.Name) and node.func.id in function_names
        if not is_module_call and not is_imported_call:
            continue
        call_comments = (
            comment
            for line in range(node.lineno, node.end_lineno + 1)
            for comment in comments.get(line, ())
        )
        if not any(NOSEC_RATIONALE.match(comment) for comment in call_comments):
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
    def test_syntax_error_names_scanned_file(self) -> None:
        """A syntax error identifies the repository file being scanned."""

        with self.assertRaises(SyntaxError) as caught:
            find_subprocess_calls_without_nosec("if:\n", filename="planted.py")
        self.assertEqual(caught.exception.filename, "planted.py")

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

    def test_module_alias_call_without_rationale_is_reported(self) -> None:
        """A subprocess module alias remains in scanner scope."""

        source = "import subprocess as process\nprocess.run(['tool'])\n"
        self.assertEqual(find_subprocess_calls_without_nosec(source), [2])

    def test_imported_alias_call_without_rationale_is_reported(self) -> None:
        """An imported subprocess function alias remains in scanner scope."""

        source = "from subprocess import run as execute\nexecute(['tool'])\n"
        self.assertEqual(find_subprocess_calls_without_nosec(source), [2])

    def test_nosec_text_in_string_is_not_a_rationale(self) -> None:
        """A nosec-shaped string literal does not suppress a call."""

        source = "import subprocess\nsubprocess.run(['# nosec B603 - fake'])\n"
        self.assertEqual(find_subprocess_calls_without_nosec(source), [2])

    def test_blank_nosec_rationale_is_reported(self) -> None:
        """A nosec comment with a blank rationale does not suppress a call."""

        source = "import subprocess\nsubprocess.run(['tool'])  # nosec B603 - \n"
        self.assertEqual(find_subprocess_calls_without_nosec(source), [2])

    def test_repository_subprocess_calls_have_nosec_rationales(self) -> None:
        """Every in-scope subprocess call carries an inline nosec rationale."""

        offenders = []
        for path in python_sources():
            source = path.read_text()
            offenders.extend(
                f"{path.relative_to(ROOT)}:{line}"
                for line in find_subprocess_calls_without_nosec(
                    source, filename=str(path.relative_to(ROOT))
                )
            )
        self.assertEqual(offenders, [], "missing nosec rationale:\n" + "\n".join(offenders))


if __name__ == "__main__":
    unittest.main()
