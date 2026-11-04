# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Require rationales on subprocess imports and suppressions, and deadlines on blocking calls."""

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
# Popen returns at once; wait() and communicate() on the started process block.
BLOCKING_CALLS = SUBPROCESS_CALLS - {"Popen"}
PROCESS_WAIT_METHODS = frozenset({"wait", "communicate"})
POSITIONAL_TIMEOUT = {"wait": 0, "communicate": 1}
NOSEC_RATIONALE = re.compile(r"^#\s*nosec\b(?=[^#]*\s-\s+\S)(?P<ids>[^#]*)")
# Bandit 1.7.10 scans selectors through the rationale, up to a second '#':
# https://github.com/PyCQA/bandit/blob/1.7.10/bandit/core/manager.py#L439-L459
NOSEC_SELECTORS = re.compile(r"\b(?:B\d+|[a-z\d]+(?:_[a-z\d]+)+)\b")


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


def find_subprocess_imports_without_nosec(source: str, *, filename: str = "<unknown>") -> list[int]:
    """Return line numbers of subprocess imports without a Bandit B404 rationale."""

    tree = ast.parse(source, filename=filename)
    comments = comments_by_line(source)
    offenders = []
    for node in ast.walk(tree):
        imports_subprocess = (
            isinstance(node, ast.Import) and any(n.name == "subprocess" for n in node.names)
        ) or (isinstance(node, ast.ImportFrom) and node.module == "subprocess")
        if imports_subprocess and not has_rationale(comments, node, required_test="B404"):
            offenders.append(node.lineno)
    return sorted(offenders)


def comments_by_line(source: str) -> dict[int, list[str]]:
    """Map each line number to the comment tokens on it."""

    comments: dict[int, list[str]] = {}
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT:
            comments.setdefault(token.start[0], []).append(token.string)
    return comments


def has_rationale(
    comments: dict[int, list[str]], node: ast.stmt | ast.expr, *, required_test: str | None = None
) -> bool:
    """Require a rationale and read test selectors across the full nosec comment."""

    for line in range(node.lineno, (node.end_lineno or node.lineno) + 1):
        for comment in comments.get(line, ()):
            match = NOSEC_RATIONALE.match(comment)
            if match is None:
                continue
            selectors = set(NOSEC_SELECTORS.findall(match["ids"]))
            if required_test is None or not selectors or required_test in selectors:
                return True
            if required_test == "B404" and "import_subprocess" in selectors:
                return True
    return False


def find_subprocess_calls_without_nosec(source: str, *, filename: str = "<unknown>") -> list[int]:
    """Return line numbers of subprocess calls without a suppression rationale."""

    tree = ast.parse(source, filename=filename)
    comments = comments_by_line(source)
    return sorted(
        node.lineno for node, _ in subprocess_calls(tree) if not has_rationale(comments, node)
    )


def find_blocking_calls_without_timeout(source: str, *, filename: str = "<unknown>") -> list[int]:
    """Return line numbers of blocking subprocess calls that pass no explicit timeout."""

    tree = ast.parse(source, filename=filename)
    calls = subprocess_calls(tree)
    blocking = [node for node, name in calls if name in BLOCKING_CALLS]
    blocking.extend(popen_waits(tree, [node for node, name in calls if name == "Popen"]))
    return sorted(node.lineno for node in blocking if not has_deadline(node))


def popen_waits(tree: ast.AST, popens: list[ast.Call]) -> list[ast.Call]:
    """Return wait() and communicate() calls on a Popen result, chained or bound to a name."""

    process_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and node.value in popens:
            process_names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif (
            isinstance(node, ast.AnnAssign)
            and node.value in popens
            and isinstance(node.target, ast.Name)
        ):
            process_names.add(node.target.id)
        elif (
            isinstance(node, ast.withitem)
            and node.context_expr in popens
            and isinstance(node.optional_vars, ast.Name)
        ):
            process_names.add(node.optional_vars.id)
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in PROCESS_WAIT_METHODS
        and (
            node.func.value in popens
            or (isinstance(node.func.value, ast.Name) and node.func.value.id in process_names)
        )
    ]


def has_deadline(node: ast.Call) -> bool:
    """Return whether a call passes a timeout other than a literal None."""

    timeout = next((kw.value for kw in node.keywords if kw.arg == "timeout"), None)
    if timeout is not None:
        return not (isinstance(timeout, ast.Constant) and timeout.value is None)
    if not isinstance(node.func, ast.Attribute):
        return False
    index = POSITIONAL_TIMEOUT.get(node.func.attr)
    if index is None or len(node.args) <= index:
        return False
    if any(isinstance(arg, ast.Starred) for arg in node.args[: index + 1]):
        return False
    value = node.args[index]
    return not (isinstance(value, ast.Constant) and value.value is None)


class SubprocessNosecTest(unittest.TestCase):
    def test_rationale_ids_do_not_blanket_suppress_b404(self) -> None:
        """An ID after the separator still selects tests instead of suppressing all."""

        for selectors in ("B603", "B603 B607", "B4040", "subprocess_popen_with_shell_equals_true"):
            with self.subTest(selectors=selectors):
                source = f"import subprocess  # nosec - {selectors} is checked at call sites\n"
                self.assertEqual(find_subprocess_imports_without_nosec(source), [1])
        for selectors in ("B404", "B603, B404", "import_subprocess", "fixed argv only"):
            with self.subTest(selectors=selectors):
                source = f"import subprocess  # nosec - {selectors} is checked at call sites\n"
                self.assertEqual(find_subprocess_imports_without_nosec(source), [])

    def test_unrelated_suppression_does_not_cover_b404(self) -> None:
        """Selective call suppressions cannot justify a subprocess import."""

        for ids in ("B603", "B603 B607", "B4040", "subprocess_popen_with_shell_equals_true"):
            with self.subTest(ids=ids):
                source = f"import subprocess  # nosec {ids} - fixed argv only.\n"
                self.assertEqual(find_subprocess_imports_without_nosec(source), [1])
        for ids in ("B404", "B603, B404", "import_subprocess", ""):
            with self.subTest(ids=ids):
                source = f"import subprocess  # nosec {ids} - fixed argv only.\n"
                self.assertEqual(find_subprocess_imports_without_nosec(source), [])

    def test_variant_import_rationales_cover_b404(self) -> None:
        """Both reviewed imports already justify B404; planted removals are caught."""

        for relative in ("scripts/variant_sweep.py", "tests/test_variant_sweep.py"):
            with self.subTest(path=relative):
                source = (ROOT / relative).read_text()
                self.assertEqual(find_subprocess_imports_without_nosec(source), [])
                planted = re.sub(r"(import subprocess)\s+# nosec[^\n]*", r"\1", source)
                self.assertEqual(len(find_subprocess_imports_without_nosec(planted)), 1)

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
        for path in python_sources(ROOT, preserve_config=True):
            source = path.read_text()
            offenders.extend(
                f"{path.relative_to(ROOT)}:{line}"
                for line in find_subprocess_calls_without_nosec(
                    source, filename=str(path.relative_to(ROOT))
                )
            )
        self.assertEqual(offenders, [], "missing nosec rationale:\n" + "\n".join(offenders))

    def test_import_without_nosec_rationale_is_reported(self) -> None:
        """Planted subprocess imports without a B404 rationale are reported in every form."""

        source = "\n".join(
            (
                "import subprocess",
                "import os, subprocess as process",
                "from subprocess import run",
                "import subprocess  # nosec B404 - ",
                "import subprocess  # nosec B404 - fixed argv only.",
                "from subprocess import (  # nosec B404 - CalledProcessError type only.",
                "    CalledProcessError,",
                ")",
                "import subprocessing",
                "",
            )
        )
        self.assertEqual(find_subprocess_imports_without_nosec(source), [1, 2, 3, 4])

    def test_repository_subprocess_imports_have_nosec_rationales(self) -> None:
        """Every in-scope subprocess import carries an inline B404 rationale."""

        offenders = []
        for path in python_sources(ROOT, preserve_config=True):
            relative = str(path.relative_to(ROOT))
            offenders.extend(
                f"{relative}:{line}"
                for line in find_subprocess_imports_without_nosec(
                    path.read_text(), filename=relative
                )
            )
        self.assertEqual(offenders, [], "missing B404 rationale:\n" + "\n".join(offenders))

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
                "subprocess.run(['tool'], 5)",
                "subprocess.check_output(['tool'], 5)",
                "subprocess.check_call(['tool'], 5)",
                "",
            )
        )
        self.assertEqual(find_blocking_calls_without_timeout(source), [3, 4, 5, 6, 7, 8, 9])

    def test_popen_wait_without_timeout_is_reported(self) -> None:
        """wait() and communicate() on a Popen result need a deadline; other waits do not."""

        source = "\n".join(
            (
                "import subprocess",
                "proc = subprocess.Popen(['tool'])",
                "proc.communicate()",
                "ann: subprocess.Popen = subprocess.Popen(['tool'])",
                "ann.wait()",
                "subprocess.Popen(['tool']).wait()",
                "proc.wait(timeout=None)",
                "proc.wait(None)",
                "proc.wait(5, timeout=None)",
                "proc.communicate(b'', None)",
                "proc.wait(*())",
                "proc.wait(*deadline)",
                "proc.communicate(*args)",
                "proc.communicate(b'', *rest)",
                "proc.communicate(*args, 5)",
                "proc.wait(timeout=5)",
                "with subprocess.Popen(['tool']) as child:",
                "    child.wait()",
                "event.wait()",
                "await asyncio.wait_for(event.wait(), timeout=5)",
                "",
            )
        )
        self.assertEqual(
            find_blocking_calls_without_timeout(source),
            [3, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 18],
        )

    def test_blocking_call_with_timeout_and_popen_are_accepted(self) -> None:
        """A call with an explicit timeout and a Popen constructor produce no offenders."""

        source = "\n".join(
            (
                "import subprocess",
                "subprocess.run(['tool'], timeout=0)",
                "proc = subprocess.Popen(['tool'])",
                "proc.wait(5)",
                "proc.wait(*(), timeout=5)",
                "proc.communicate(None, 5)",
                "proc.communicate(None, None, timeout=5)",
                "",
            )
        )
        self.assertEqual(find_blocking_calls_without_timeout(source), [])

    def test_repository_blocking_subprocess_calls_have_timeouts(self) -> None:
        """Every in-scope blocking subprocess call passes an explicit timeout."""

        offenders = []
        for path in python_sources(ROOT, preserve_config=True):
            relative = str(path.relative_to(ROOT))
            offenders.extend(
                f"{relative}:{line}"
                for line in find_blocking_calls_without_timeout(path.read_text(), filename=relative)
            )
        self.assertEqual(offenders, [], "missing subprocess timeout:\n" + "\n".join(offenders))


if __name__ == "__main__":
    unittest.main()
