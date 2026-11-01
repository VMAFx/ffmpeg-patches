# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Require rationales on Python subprocess security suppressions and deadlines on blocking calls."""

from __future__ import annotations

import ast
import io
import re
import tokenize
import unittest
from pathlib import Path

from tests.helpers import python_sources

ROOT = Path(__file__).resolve().parent.parent
SUBPROCESS_CALLS = frozenset({"run", "Popen", "call", "check_call", "check_output"})
# Popen returns at once; in a module that starts one, its wait() and communicate() block.
BLOCKING_CALLS = SUBPROCESS_CALLS - {"Popen"}
PROCESS_WAIT_METHODS = frozenset({"wait", "communicate"})
NOSEC_RATIONALE = re.compile(r"^#\s*nosec\b.*\s-\s+\S")


def subprocess_calls(tree: ast.AST) -> list[tuple[ast.Call, str]]:
    """Return each subprocess call in a parsed module with the subprocess function it names."""

    module_names = {"subprocess"}
    function_names: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            module_names.update(
                name.asname or name.name for name in node.names if name.name == "subprocess"
            )
        elif isinstance(node, ast.ImportFrom) and node.module == "subprocess":
            function_names.update(
                (name.asname or name.name, name.name)
                for name in node.names
                if name.name in SUBPROCESS_CALLS
            )
    calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id in module_names
            and func.attr in SUBPROCESS_CALLS
        ):
            calls.append((node, func.attr))
        elif isinstance(func, ast.Name) and func.id in function_names:
            calls.append((node, function_names[func.id]))
    return calls


def find_subprocess_calls_without_nosec(source: str, *, filename: str = "<unknown>") -> list[int]:
    """Return line numbers of subprocess calls without a suppression rationale."""

    tree = ast.parse(source, filename=filename)
    comments: dict[int, list[str]] = {}
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT:
            comments.setdefault(token.start[0], []).append(token.string)
    offenders = []
    for node, _ in subprocess_calls(tree):
        call_comments = (
            comment
            for line in range(node.lineno, node.end_lineno + 1)
            for comment in comments.get(line, ())
        )
        if not any(NOSEC_RATIONALE.match(comment) for comment in call_comments):
            offenders.append(node.lineno)
    return sorted(offenders)


def find_blocking_calls_without_timeout(source: str, *, filename: str = "<unknown>") -> list[int]:
    """Return line numbers of blocking subprocess calls that pass no explicit timeout."""

    tree = ast.parse(source, filename=filename)
    calls = subprocess_calls(tree)
    blocking = [node for node, name in calls if name in BLOCKING_CALLS]
    if any(name == "Popen" for _, name in calls):
        blocking.extend(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in PROCESS_WAIT_METHODS
        )
    return sorted(node.lineno for node in blocking if not has_deadline(node))


def has_deadline(node: ast.Call) -> bool:
    """Return whether a call passes a timeout keyword other than a literal None."""

    return any(
        kw.arg == "timeout" and not (isinstance(kw.value, ast.Constant) and kw.value.value is None)
        for kw in node.keywords
    )


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
        for path in python_sources(ROOT):
            source = path.read_text()
            offenders.extend(
                f"{path.relative_to(ROOT)}:{line}"
                for line in find_subprocess_calls_without_nosec(
                    source, filename=str(path.relative_to(ROOT))
                )
            )
        self.assertEqual(offenders, [], "missing nosec rationale:\n" + "\n".join(offenders))

    def test_blocking_call_without_timeout_is_reported(self) -> None:
        """Planted blocking calls without a timeout are reported, module and imported form alike."""

        source = "\n".join(
            (
                "import subprocess",
                "from subprocess import check_output as out",
                "subprocess.run(['tool'], check=True)",
                "out(['tool'])",
                "subprocess.run(['tool'], **options)",
                "subprocess.run(['tool'], timeout=None)",
                "",
            )
        )
        self.assertEqual(find_blocking_calls_without_timeout(source), [3, 4, 5, 6])

    def test_popen_wait_without_timeout_is_reported(self) -> None:
        """In a module that starts a Popen, wait() and communicate() need a deadline."""

        source = "\n".join(
            (
                "import subprocess",
                "proc = subprocess.Popen(['tool'])",
                "proc.communicate()",
                "subprocess.Popen(['tool']).wait()",
                "proc.wait(timeout=None)",
                "proc.wait(timeout=5)",
                "",
            )
        )
        self.assertEqual(find_blocking_calls_without_timeout(source), [3, 4, 5])

    def test_blocking_call_with_timeout_and_popen_are_accepted(self) -> None:
        """A call with an explicit timeout and a Popen constructor produce no offenders."""

        source = (
            "import subprocess\nsubprocess.run(['tool'], timeout=0)\nsubprocess.Popen(['tool'])\n"
        )
        self.assertEqual(find_blocking_calls_without_timeout(source), [])

    def test_repository_blocking_subprocess_calls_have_timeouts(self) -> None:
        """Every in-scope blocking subprocess call passes an explicit timeout."""

        offenders = []
        for path in python_sources(ROOT):
            relative = str(path.relative_to(ROOT))
            offenders.extend(
                f"{relative}:{line}"
                for line in find_blocking_calls_without_timeout(path.read_text(), filename=relative)
            )
        self.assertEqual(offenders, [], "missing subprocess timeout:\n" + "\n".join(offenders))


if __name__ == "__main__":
    unittest.main()
