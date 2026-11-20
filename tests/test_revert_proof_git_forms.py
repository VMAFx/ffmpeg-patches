# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Guard stored-object Git reads and explicit descriptor ownership using Python ASTs."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PRODUCER = ROOT / "scripts" / "revert_proof.py"
RUN_EXCEPTIONS = {
    "git_result": "Appends --no-replace-objects to options before running Git; checked separately.",
    "run_test_command": "Runs the caller's test command, which need not be Git.",
    "cleanup_git": "Runs worktree and branch cleanup with caller-chosen arguments.",
    "measure_reverted": "Only git apply reads a patch from stdin without reading stored objects.",
}


def _is_attribute(node: ast.AST, owner: str, name: str) -> bool:
    """Match an attribute accessed directly through the specified name."""
    return (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == owner
        and node.attr == name
    )


def _is_string(node: ast.AST, value: str) -> bool:
    """Match an exact string literal without evaluating source code."""
    return isinstance(node, ast.Constant) and node.value == value


def _run_exception(function: str, call: ast.Call) -> bool:
    """Allow documented wrappers and only the literal git apply command in measure_reverted."""
    if function not in RUN_EXCEPTIONS:
        return False
    if function != "measure_reverted":
        return True
    return (
        bool(call.args)
        and isinstance(call.args[0], ast.List)
        and len(call.args[0].elts) >= 2
        and _is_string(call.args[0].elts[0], "git")
        and _is_string(call.args[0].elts[1], "apply")
    )


def _has_global_isolation(arguments: list[ast.expr]) -> bool:
    """Require a literal global prefix with isolation outside consumed -c and -C values."""
    isolated = False
    consume = False
    for argument in arguments[1:]:
        if not isinstance(argument, ast.Constant) or not isinstance(argument.value, str):
            return False
        value = argument.value
        if consume:
            consume = False
            continue
        if value == "--" or not value.startswith("-"):
            break
        if value in ("-c", "-C"):
            consume = True
        elif value == "--no-replace-objects":
            isolated = True
    return isolated and not consume


def _find_git_run_errors(source: str) -> list[int]:
    """Find dynamic calls or Git reads without isolation in the literal global prefix."""
    tree = ast.parse(source)
    functions = [
        n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    findings = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not _is_attribute(node.func, "upstream_lint", "_run"):
            continue
        owners = [f for f in functions if f.lineno <= node.lineno <= f.end_lineno]
        owner = max(owners, key=lambda f: f.lineno).name if owners else ""
        if _run_exception(owner, node):
            continue
        if not node.args or not isinstance(node.args[0], ast.List):
            findings.append(node.lineno)
            continue
        arguments = node.args[0].elts
        if arguments and _is_string(arguments[0], "git") and not _has_global_isolation(arguments):
            findings.append(node.lineno)
    return sorted(findings)


def _forwards_git_options(call: ast.Call) -> bool:
    """Match a literal Git command expanding isolated options before caller arguments."""
    if not call.args or not isinstance(call.args[0], ast.List):
        return False
    arguments = call.args[0].elts
    return (
        len(arguments) == 3
        and _is_string(arguments[0], "git")
        and all(
            isinstance(node, ast.Starred)
            and isinstance(node.value, ast.Name)
            and node.value.id == name
            for node, name in zip(arguments[1:], ("options", "args"), strict=True)
        )
    )


def _find_git_result_append_errors(source: str) -> list[int]:
    """Require isolation append before each git_result run and forwarding options before args."""
    findings = []
    for function in ast.walk(ast.parse(source)):
        if not isinstance(function, ast.FunctionDef) or function.name != "git_result":
            continue
        calls = [node for node in ast.walk(function) if isinstance(node, ast.Call)]
        appends = [
            node.lineno
            for node in calls
            if _is_attribute(node.func, "options", "append")
            and len(node.args) == 1
            and _is_string(node.args[0], "--no-replace-objects")
        ]
        findings.extend(
            node.lineno
            for node in calls
            if _is_attribute(node.func, "upstream_lint", "_run")
            and (not any(line < node.lineno for line in appends) or not _forwards_git_options(node))
        )
    return sorted(findings)


def _find_close_callbacks(source: str) -> list[int]:
    """Find name.callback(os.close, ...) calls whose ownership CodeQL cannot follow."""
    return sorted(
        node.lineno
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.attr == "callback"
        and node.args
        and _is_attribute(node.args[0], "os", "close")
    )


class RevertProofGitFormsTest(unittest.TestCase):
    """Replay AST rules against planted defects and current production sources."""

    def test_git_calls_read_stored_objects(self) -> None:
        """Literal Git reads require isolation in a literal global-option prefix."""
        for source in (
            'upstream_lint._run(["git", "merge-base", "--is-ancestor", commit, head], tree)',
            'upstream_lint._run(["git", "rev-parse", "--verify", ref], tree)',
            'def measure_reverted():\n    upstream_lint._run(["git", "log"], tree)',
            'upstream_lint._run(["git", "--", "--no-replace-objects", "log"], tree)',
            'upstream_lint._run(["git", "log", "--no-replace-objects"], tree)',
            'upstream_lint._run(["git", dynamic, "--no-replace-objects", "log"], tree)',
            'upstream_lint._run(["git", "--no-replace-objects", dynamic, "log"], tree)',
            'upstream_lint._run(["git", "-c", "--no-replace-objects", "log"], tree)',
            'upstream_lint._run(["git", "-C", "--no-replace-objects", "log"], tree)',
        ):
            with self.subTest(source=source):
                self.assertEqual(len(_find_git_run_errors(source)), 1)
        for source in (
            'upstream_lint._run(["git", "--no-replace-objects", "log"], tree)',
            'upstream_lint._run(["git", "-c", "x=y", "-C", "dir", '
            '"--no-replace-objects", "log"], tree)',
        ):
            with self.subTest(source=source):
                self.assertEqual(_find_git_run_errors(source), [])
        self.assertEqual(_find_git_run_errors(PRODUCER.read_text(encoding="utf-8")), [])

    def test_dynamic_run_requires_documented_exception(self) -> None:
        """Unreviewed nonliteral command lists fail while the caller-command wrapper passes."""
        self.assertEqual(_find_git_run_errors("upstream_lint._run(command, tree)"), [1])
        self.assertEqual(
            _find_git_run_errors("def run_test_command():\n    upstream_lint._run(command, tree)"),
            [],
        )
        self.assertEqual(_find_git_run_errors(PRODUCER.read_text(encoding="utf-8")), [])

    def test_git_result_appends_isolation_before_run(self) -> None:
        """The git_result exemption requires isolation append and ordered argv forwarding."""
        for body in (
            'upstream_lint._run(["git", *options, *args], tree)',
            'upstream_lint._run(["git", *options, *args], tree)\n'
            '    options.append("--no-replace-objects")',
            'options.append("--no-replace-objects")\n    upstream_lint._run(["git", *args], tree)',
            'options.append("--no-replace-objects")\n'
            '    upstream_lint._run(["git", *args, *options], tree)',
            'options.append("--no-replace-objects")\n    upstream_lint._run(command, tree)',
        ):
            with self.subTest(body=body):
                self.assertEqual(
                    len(_find_git_result_append_errors(f"def git_result():\n    {body}")), 1
                )
        self.assertEqual(_find_git_result_append_errors(PRODUCER.read_text(encoding="utf-8")), [])

    def test_no_close_callbacks(self) -> None:
        """All scripts close descriptors explicitly rather than registering os.close callbacks."""
        self.assertEqual(_find_close_callbacks("resources.callback(os.close, descriptor)"), [1])
        for path in sorted((ROOT / "scripts").glob("*.py")):
            with self.subTest(path=path.name):
                self.assertEqual(_find_close_callbacks(path.read_text(encoding="utf-8")), [])
