# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Exercise lane brief rendering, validation, and error handling."""

from __future__ import annotations

import ast
import contextlib
import io
import os
import re
import signal
import subprocess  # nosec B404 - Fixed argv is used for the zombie regression test.
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import yaml

from scripts import lane_brief, lane_isolation
from tests.helpers import run_git
from tests.test_lane_isolation import repo

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "scripts" / "lane_brief_template.md"
CONFIG = ROOT / ".config" / "lane-brief.yaml"
ARGS = [
    "render",
    "--branch",
    "feature",
    "--task",
    "work",
    "--finding",
    "problem",
    "--fix",
    "repair",
    "--tests",
    "unit",
    "--modules",
    "tests.test_example tests.test_other",
    "--paths",
    "scripts/a.py",
    "--issue",
    "42",
    "--round",
    "2",
]


def process_alive(pid: int) -> bool:
    """Return whether a process exists and has not exited."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    stat = Path(f"/proc/{pid}/stat")
    try:
        state = stat.read_text(encoding="utf-8").rpartition(")")[2].split()[0]
    except FileNotFoundError:
        return True
    return state not in {"Z", "X"}


def _stems_from_import(node: ast.Import) -> set[str]:
    """Collect test module stems from an ast.Import node."""
    stems: set[str] = set()
    for alias in node.names[:100]:
        parts = alias.name.split(".")
        if len(parts) >= 2 and parts[0] == "tests":
            stems.add(parts[1])
    return stems


def _stems_from_import_from(node: ast.ImportFrom) -> set[str]:
    """Collect test module stems from an ast.ImportFrom node."""
    stems: set[str] = set()
    if node.module == "tests":
        for alias in node.names[:100]:
            stems.add(alias.name)
        return stems
    if node.module and node.module.startswith("tests."):
        parts = node.module.split(".")
        if len(parts) >= 2 and parts[0] == "tests":
            stems.add(parts[1])
    return stems


def _extract_imported_stems(tree: ast.AST) -> set[str]:
    """Extract test module stems imported by recognized forms in an AST."""
    stems: set[str] = set()
    for idx, node in enumerate(ast.walk(tree)):
        if idx >= 50000:
            break
        if isinstance(node, ast.Import):
            stems.update(_stems_from_import(node))
        elif isinstance(node, ast.ImportFrom):
            stems.update(_stems_from_import_from(node))
    return stems


def _resolve_reach_set(candidate_files: list[Path]) -> set[str]:
    """Compute the set of test stems that reach the stub helper directly or via imports."""
    target = "secret" + "-tool"
    max_bytes = 1024 * 1024
    reaching: set[str] = set()
    imports_map: dict[str, set[str]] = {}

    for test_file in candidate_files[:500]:
        if not test_file.is_file():
            continue
        try:
            with test_file.open("rb") as handle:
                raw = handle.read(max_bytes)
            text = raw.decode("utf-8", errors="replace")
        except OSError:
            reaching.add(test_file.stem)
            continue

        if target in text:
            reaching.add(test_file.stem)

        try:
            tree = ast.parse(text, filename=test_file.name)
        except Exception:
            reaching.add(test_file.stem)
            continue

        imports_map[test_file.stem] = _extract_imported_stems(tree)

    max_passes = len(candidate_files) + 1
    for _ in range(max_passes):
        added = False
        for stem, imported_stems in list(imports_map.items())[: len(candidate_files)]:
            if stem not in reaching and not imported_stems.isdisjoint(reaching):
                reaching.add(stem)
                added = True
        if not added:
            break

    return reaching


def credential_stub_gaps(tests_dir: Path, declared: list[str]) -> tuple[list[str], list[str]]:
    """Return undeclared tests reaching the secret tool and stale declared entries."""
    max_files = 500
    candidate_files = sorted(tests_dir.glob("test_*.py"))[:max_files]
    reaching = _resolve_reach_set(candidate_files)
    undeclared: list[str] = []
    for test_file in candidate_files[:max_files]:
        if test_file.stem in reaching:
            dotted = f"tests.{test_file.stem}"
            if dotted not in declared and f"tests/{test_file.name}" not in declared:
                undeclared.append(dotted)

    stale: list[str] = []
    for entry in declared[:max_files]:
        stem = entry.removesuffix(".py").rpartition("/")[2].rpartition(".")[2]
        if not (tests_dir / f"{stem}.py").is_file():
            stale.append(entry)

    return sorted(undeclared), sorted(stale)


class LaneBriefTest(unittest.TestCase):
    """Check the CLI against real declarations and temporary briefs."""

    def setUp(self) -> None:
        """Create a temporary directory for input and output files."""
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop("LANE_BRIEF_RUNNER", None)
        patcher = patch.object(lane_brief, "check_isolation", return_value=0)
        self.isolation = patcher.start()
        self.addCleanup(patcher.stop)

    def run_cli(self, *args: str) -> tuple[int, str, str]:
        """Capture the command result and both output streams."""
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                rc = lane_brief.main(list(args))
            except SystemExit as error:
                rc = int(error.code)
        return rc, stdout.getvalue(), stderr.getvalue()

    def rendered(self) -> str:
        """Render a complete brief and return its contents."""
        rc, output, error = self.run_cli(*ARGS)
        self.assertEqual(rc, 0, error)
        return output

    def runner_fixture(self) -> tuple[Path, Path, Path]:
        """Create a brief and a configurable runner that records its arguments."""
        brief = self.root / "brief.md"
        brief.write_text(self.rendered(), encoding="utf-8")
        runner = self.root / "runner.sh"
        runner.write_text(
            "#!/bin/sh\n"
            'dir=$(dirname "$0")\n'
            'printf "%s\\n" "$@" >> "$dir/args"\n'
            'if [ -f "$dir/sleep" ]; then sleep 3; fi\n'
            'printf "runner stdout\\n"\n'
            'printf "runner stderr\\n" >&2\n'
            'exit "$(cat "$dir/rc")"\n',
            encoding="utf-8",
        )
        runner.chmod(0o755)
        (self.root / "rc").write_text("0", encoding="utf-8")
        return brief, runner, self.root / "args"

    def assert_child_gone(self, pid_file: Path) -> None:
        """Check the child disappears, cleaning it up if the check fails."""
        pid = int(pid_file.read_text(encoding="utf-8"))
        for _ in range(200):
            if not process_alive(pid):
                return
            time.sleep(0.01)
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        self.fail(f"child process {pid} still exists")

    @unittest.skipUnless(Path("/proc/self/stat").exists(), "requires procfs")
    def test_assert_child_gone_accepts_zombie(self) -> None:
        """Accept a killed child that has not yet been reaped."""
        child = subprocess.Popen(["sleep", "30"])  # nosec B603 B607 - Fixed test command.
        try:
            os.kill(child.pid, signal.SIGKILL)
            pid_file = self.root / "zombie_pid"
            pid_file.write_text(str(child.pid), encoding="utf-8")
            self.assert_child_gone(pid_file)
        finally:
            child.wait(timeout=5)

    def test_process_alive_states(self) -> None:
        """Distinguish live, exited, missing, and unavailable procfs states."""
        with patch("os.kill"), patch.object(Path, "read_text") as read_stat:
            for state, expected in (("R", True), ("Z", False), ("X", False)):
                with self.subTest(state=state):
                    read_stat.return_value = f"123 (command with ) parentheses) {state} 1"
                    self.assertEqual(process_alive(123), expected)
            read_stat.side_effect = FileNotFoundError
            self.assertTrue(process_alive(123))
        with patch("os.kill", side_effect=ProcessLookupError):
            self.assertFalse(process_alive(123))

    def constant_names(self, tree: ast.Module) -> set[str]:
        """Collect module-level constant names from assignments."""
        return {
            target.id
            for node in tree.body
            if isinstance(node, (ast.Assign, ast.AnnAssign))
            for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
            if isinstance(target, ast.Name) and re.fullmatch(r"[A-Z][A-Z0-9_]*", target.id)
        }

    def excluded_strings(self, tree: ast.Module) -> set[int]:
        """Exclude formatted string components and module and function docstrings."""
        excluded = {
            id(child)
            for node in ast.walk(tree)
            if isinstance(node, ast.JoinedStr)
            for child in ast.walk(node)
        }
        excluded.update(
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef))
            and ast.get_docstring(node) is not None
        )
        return excluded

    def test_constant_names_not_in_plain_strings(self) -> None:
        """Reject constant names accidentally left unformatted in string literals."""
        tree = ast.parse((ROOT / "scripts" / "lane_brief.py").read_text(encoding="utf-8"))
        names = self.constant_names(tree)
        excluded = self.excluded_strings(tree)
        pattern = re.compile(r"\b(?:" + "|".join(map(re.escape, sorted(names))) + r")\b")
        matches = [
            (node.lineno, match.group())
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in excluded
            if (match := pattern.search(node.value))
        ]
        self.assertEqual(matches, [])

    def test_runner_output_missing_stdout(self) -> None:
        """Reject a runner without a stdout pipe."""
        process = SimpleNamespace(stdout=None)
        with self.assertRaisesRegex(OSError, "runner stdout pipe unavailable"):
            lane_brief.read_runner_output(process, 0.0)

    def test_runner_success_and_arguments(self) -> None:
        """Forward the brief and worktree as exact runner arguments."""
        brief, runner, calls = self.runner_fixture()
        rc, _, error = self.run_cli(
            "check",
            str(brief),
            "--runner",
            str(runner),
            "--worktree",
            str(self.root),
            "--config",
            str(CONFIG),
        )
        self.assertEqual(rc, 0, error)
        self.assertEqual(error, "runner stdout\nrunner stderr\n")
        self.assertEqual(
            calls.read_text(encoding="utf-8").splitlines(),
            ["--check-brief", str(brief), str(self.root)],
        )

    def test_runner_output_boundary(self) -> None:
        """Forward output exactly at the byte limit."""
        brief, runner, _ = self.runner_fixture()
        runner.write_text("#!/bin/sh\nprintf 1234567890123456\n", encoding="utf-8")
        with patch.object(lane_brief, "RUNNER_OUTPUT_LIMIT", 16, create=True):
            rc, _, error = self.run_cli("check", str(brief), "--runner", str(runner))
        self.assertEqual(rc, 0)
        self.assertEqual(error, "1234567890123456")

    def test_runner_output_over_limit(self) -> None:
        """Reject output one byte above the limit even on runner success."""
        brief, runner, _ = self.runner_fixture()
        runner.write_text("#!/bin/sh\nprintf 12345678901234567\n", encoding="utf-8")
        with patch.object(lane_brief, "RUNNER_OUTPUT_LIMIT", 16, create=True):
            rc, _, error = self.run_cli("check", str(brief), "--runner", str(runner))
        self.assertEqual(rc, 2)
        self.assertEqual(error, "lane_brief: runner failed: output exceeds 16 bytes\n")

    def test_runner_output_stopped_and_reaped(self) -> None:
        """Stop and reap a runner as soon as its output exceeds the limit."""
        brief, runner, _ = self.runner_fixture()
        pid_file = self.root / "pid"
        child_file = self.root / "child_pid"
        runner.write_text(
            "#!/bin/sh\n"
            f"echo $$ > '{pid_file}'\n"
            "sleep 30 &\n"
            f"echo $! > '{child_file}'\n"
            "for i in $(seq 1 1000); do printf '%01024d' 0; done\n"
            "sleep 30\n",
            encoding="utf-8",
        )
        started = time.monotonic()
        with patch.object(lane_brief, "RUNNER_TIMEOUT", 12):
            rc, _, error = self.run_cli("check", str(brief), "--runner", str(runner))
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(rc, 2)
        self.assert_child_gone(child_file)
        self.assertEqual(
            error,
            f"lane_brief: runner failed: output exceeds {lane_brief.RUNNER_OUTPUT_LIMIT} bytes\n",
        )
        with self.assertRaises(ProcessLookupError):
            os.kill(int(pid_file.read_text(encoding="utf-8")), 0)

    def test_runner_combined_output_over_limit(self) -> None:
        """Count stdout and stderr together against the output limit."""
        brief, runner, _ = self.runner_fixture()
        runner.write_text("#!/bin/sh\nprintf 123456789\nprintf 12345678 >&2\n", encoding="utf-8")
        with patch.object(lane_brief, "RUNNER_OUTPUT_LIMIT", 16, create=True):
            rc, _, error = self.run_cli("check", str(brief), "--runner", str(runner))
        self.assertEqual(rc, 2)
        self.assertIn("output exceeds 16 bytes", error)

    def test_worktree_config_rejects_before_runner(self) -> None:
        """Check the selected worktree requirements before invoking the runner."""
        brief, runner, calls = self.runner_fixture()
        config = self.root / ".config" / "lane-brief.yaml"
        config.parent.mkdir()
        config.write_text("required:\n  - worktree only requirement\n", encoding="utf-8")
        rc, _, error = self.run_cli(
            "check", str(brief), "--runner", str(runner), "--worktree", str(self.root)
        )
        self.assertEqual(rc, 1)
        self.assertIn("missing required line: worktree only requirement", error)
        self.assertFalse(calls.exists())

    def test_explicit_config_overrides_worktree(self) -> None:
        """Prefer an explicit config over the selected worktree config."""
        brief, runner, calls = self.runner_fixture()
        config = self.root / ".config" / "lane-brief.yaml"
        config.parent.mkdir()
        config.write_text("required:\n  - worktree only requirement\n", encoding="utf-8")
        rc, _, error = self.run_cli(
            "check",
            str(brief),
            "--runner",
            str(runner),
            "--worktree",
            str(self.root),
            "--config",
            str(CONFIG),
        )
        self.assertEqual(rc, 0, error)
        self.assertTrue(calls.exists())

    def test_runner_refusal(self) -> None:
        """Map a runner refusal to invalid brief status."""
        brief, runner, _ = self.runner_fixture()
        (self.root / "rc").write_text("2", encoding="utf-8")
        rc, _, error = self.run_cli("check", str(brief), "--runner", str(runner))
        self.assertEqual(rc, 1)
        self.assertIn("lane_brief: runner refused brief (rc 2)", error)

    def test_runner_missing_and_not_executable(self) -> None:
        """Report missing and non-executable runners as input errors."""
        brief, runner, _ = self.runner_fixture()
        runner.chmod(0o644)
        for path in (self.root / "missing", runner):
            with self.subTest(path=path):
                rc, _, error = self.run_cli("check", str(brief), "--runner", str(path))
                self.assertEqual(rc, 2)
                self.assertIn("lane_brief: runner failed:", error)

    def test_runner_timeout(self) -> None:
        """Report a runner exceeding the timeout as an input error."""
        brief, runner, _ = self.runner_fixture()
        child_file = self.root / "child_pid"
        runner.write_text(
            f"#!/bin/sh\nsleep 30 &\necho $! > '{child_file}'\nwait\n",
            encoding="utf-8",
        )
        with patch.object(lane_brief, "RUNNER_TIMEOUT", 1):
            rc, _, error = self.run_cli("check", str(brief), "--runner", str(runner))
        self.assertEqual(rc, 2)
        self.assert_child_gone(child_file)
        self.assertEqual(error, "lane_brief: runner failed: timed out after 1 seconds\n")

    def test_runner_from_environment(self) -> None:
        """Use the environment runner and default worktree when unspecified."""
        brief, runner, calls = self.runner_fixture()
        with patch.dict(os.environ, {"LANE_BRIEF_RUNNER": str(runner)}):
            rc, _, error = self.run_cli("check", str(brief))
        self.assertEqual(rc, 0, error)
        self.assertEqual(
            calls.read_text(encoding="utf-8").splitlines(), ["--check-brief", str(brief), str(ROOT)]
        )

    def test_runner_unset(self) -> None:
        """Explain when a valid brief skips the runner check."""
        brief, _, _ = self.runner_fixture()
        rc, _, error = self.run_cli("check", str(brief))
        self.assertEqual(rc, 0)
        self.isolation.assert_not_called()
        self.assertEqual(
            error, "lane_brief: runner and lane isolation checks skipped: LANE_BRIEF_RUNNER unset\n"
        )

    def test_runner_empty_environment(self) -> None:
        """Treat an empty runner environment value as unset."""
        brief, _, _ = self.runner_fixture()
        with patch.dict(os.environ, {"LANE_BRIEF_RUNNER": ""}):
            rc, _, error = self.run_cli("check", str(brief))
        self.assertEqual(rc, 0)
        self.isolation.assert_not_called()
        self.assertEqual(
            error, "lane_brief: runner and lane isolation checks skipped: LANE_BRIEF_RUNNER unset\n"
        )

    def test_local_failure_does_not_call_runner(self) -> None:
        """Reject a locally invalid brief before invoking the runner."""
        brief, runner, calls = self.runner_fixture()
        content = brief.read_text(encoding="utf-8")
        brief.write_text(content.replace(lane_brief.read_required(CONFIG)[0], ""), encoding="utf-8")
        rc, _, error = self.run_cli("check", str(brief), "--runner", str(runner))
        self.assertEqual(rc, 1, error)
        self.assertIn("missing required line", error)
        self.assertFalse(calls.exists())

    @unittest.skipUnless(
        hasattr(os, "mkfifo") and hasattr(signal, "SIGALRM"), "FIFO alarm unavailable"
    )
    def test_brief_fifo_rejected(self) -> None:
        """Reject a FIFO brief without waiting for a writer."""
        brief = self.root / "brief.fifo"
        os.mkfifo(brief)
        previous = signal.signal(signal.SIGALRM, self._alarm_handler)
        try:
            signal.alarm(2)
            rc, _, error = self.run_cli("check", str(brief))
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, previous)
        self.assertEqual(rc, 2)
        self.assertIn("not a regular file", error)

    @staticmethod
    def _alarm_handler(signum: int, frame: object) -> None:
        """Interrupt a blocked FIFO read during the regression test."""
        raise TimeoutError("FIFO read blocked")

    def test_render_and_check(self) -> None:
        """Render all arguments and validate the result against repository standards."""
        brief = self.root / "brief.md"
        brief.write_text(self.rendered(), encoding="utf-8")
        self.assertEqual(self.run_cli("check", str(brief))[0], 0)
        self.assertIn("issue #42, round 2", brief.read_text(encoding="utf-8"))
        self.assertIn(
            "python -m unittest tests.test_example tests.test_other",
            brief.read_text(encoding="utf-8"),
        )
        self.assertNotIn("<modules>", brief.read_text(encoding="utf-8"))

    def test_brief_without_import_style_refused(self) -> None:
        """Accept complete briefs and reject briefs missing the import-style module."""
        content = self.rendered()
        brief = self.root / "brief.md"
        brief.write_text(content, encoding="utf-8")
        rc, _, error = self.run_cli("check", str(brief))
        self.assertEqual(rc, 0, error)
        brief.write_text(content.replace("tests.test_import_style", ""), encoding="utf-8")
        rc, _, error = self.run_cli("check", str(brief))
        self.assertEqual(rc, 1)
        self.assertIn("missing required line: -m unittest tests.test_import_style", error)
        self.assertIn("python -m unittest tests.test_import_style", content)

    def test_import_style_requirement_is_exact_module_name(self) -> None:
        """Reject the path form and accept the dotted module with a trailing flag."""
        content = self.rendered()
        brief = self.root / "brief.md"
        brief.write_text(
            content.replace("tests.test_import_style", "tests/test_import_style.py"),
            encoding="utf-8",
        )
        rc, _, error = self.run_cli("check", str(brief))
        self.assertEqual(rc, 1)
        self.assertIn("missing required line: -m unittest tests.test_import_style", error)
        brief.write_text(
            content.replace("tests.test_import_style", "tests.test_import_style -v"),
            encoding="utf-8",
        )
        rc, _, error = self.run_cli("check", str(brief))
        self.assertEqual(rc, 0, error)

    def test_import_style_prose_mention_refused(self) -> None:
        """Reject a brief naming the import-style module only in its task text."""
        content = self.rendered().replace("python -m unittest tests.test_import_style", "")
        content = content.replace("task: work", "task: inspect tests.test_import_style")
        self.assertIn("task: inspect tests.test_import_style", content)
        brief = self.root / "brief.md"
        brief.write_text(content, encoding="utf-8")
        rc, _, error = self.run_cli("check", str(brief))
        self.assertEqual(rc, 1)
        self.assertIn("missing required line: -m unittest tests.test_import_style", error)

    def test_import_style_accepts_real_invocations(self) -> None:
        """Accept supported invocations and reject the import-style module after another module."""
        content = self.rendered()
        brief = self.root / "brief.md"
        for invocation, expected in (
            ("python -m unittest tests.test_import_style", 0),
            ("/usr/bin/python3 -B -m unittest tests.test_import_style -v", 0),
            (
                "/usr/bin/python3 -B -m unittest tests.test_import_style "
                "tests.test_sweep_classes -v",
                0,
            ),
            (
                "/usr/bin/python3 -B -m unittest tests.test_sweep_classes "
                "tests.test_import_style -v",
                1,
            ),
        ):
            with self.subTest(invocation=invocation):
                brief.write_text(
                    content.replace("python -m unittest tests.test_import_style", invocation),
                    encoding="utf-8",
                )
                rc, _, error = self.run_cli("check", str(brief))
                self.assertEqual(rc, expected, error)
                if expected:
                    self.assertIn(
                        "missing required line: -m unittest tests.test_import_style", error
                    )

    def test_each_missing_required_line_fails(self) -> None:
        """Reject each independently removed required substring."""
        content = self.rendered()
        brief = self.root / "brief.md"
        for required in lane_brief.read_required(CONFIG):
            with self.subTest(required=required):
                brief.write_text(content.replace(required, ""), encoding="utf-8")
                rc, _, error = self.run_cli("check", str(brief))
                self.assertEqual(rc, 1)
                self.assertIn(f"missing required line: {required}", error)

    def test_template_has_unfilled_placeholders(self) -> None:
        """Reject the unrendered template and identify its placeholders."""
        rc, _, error = self.run_cli("check", str(TEMPLATE))
        self.assertEqual(rc, 1)
        for placeholder in lane_brief.NAMES:
            self.assertIn(f"unfilled placeholder <{placeholder}>", error)

    def test_missing_inputs_fail(self) -> None:
        """Return usage errors for absent files and absent or empty required lists."""
        brief = self.root / "brief.md"
        brief.write_text(self.rendered(), encoding="utf-8")
        standards = self.root / "config.yaml"
        cases = [
            ("check", str(self.root / "missing")),
            ("check", str(brief), "--config", str(self.root / "missing")),
        ]
        for args in cases:
            with self.subTest(args=args):
                self.assertEqual(self.run_cli(*args)[0], 2)
        for content in ("other: value\n", "required:\n"):
            with self.subTest(content=content):
                standards.write_text(content, encoding="utf-8")
                rc, _, error = self.run_cli("check", str(brief), "--config", str(standards))
                self.assertEqual(rc, 2)
                self.assertIn("required list", error)

    def test_render_requires_arguments(self) -> None:
        """Reject a render command missing required arguments."""
        self.assertEqual(self.run_cli("render", "--branch", "feature")[0], 2)
        args = ARGS.copy()
        index = args.index("--modules")
        del args[index : index + 2]
        self.assertEqual(self.run_cli(*args)[0], 2)

    def test_directory_brief_rejected(self) -> None:
        """Reject a directory as a brief."""
        rc, _, error = self.run_cli("check", str(self.root))
        self.assertEqual(rc, 2)
        self.assertIn("not a regular file", error)

    def test_brief_size_boundary(self) -> None:
        """Accept one MiB and reject a brief one byte larger."""
        brief = self.root / "brief.md"
        padding = b"x" * 59999 + b"\n"
        content = self.rendered().encode()
        remaining = 1024 * 1024 - len(content)
        content += padding * (remaining // len(padding)) + b"x" * (remaining % len(padding))
        brief.write_bytes(content)
        self.assertEqual(self.run_cli("check", str(brief))[0], 0)
        brief.write_bytes(content + b"x")
        rc, _, error = self.run_cli("check", str(brief))
        self.assertEqual(rc, 2)
        self.assertIn("size", error)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO unavailable")
    def test_config_fifo_rejected(self) -> None:
        """Reject a FIFO config without waiting for a writer."""
        config = self.root / "config.fifo"
        brief = self.root / "brief.md"
        os.mkfifo(config)
        brief.write_text(self.rendered(), encoding="utf-8")
        rc, _, error = self.run_cli("check", str(brief), "--config", str(config))
        self.assertEqual(rc, 2)
        self.assertIn("not a regular file", error)

    @unittest.skipUnless(
        hasattr(os, "mkfifo") and hasattr(signal, "SIGALRM"), "FIFO alarm unavailable"
    )
    def test_config_swapped_to_fifo_rejected(self) -> None:
        """Reject a FIFO substituted after a path-based regular-file check."""
        config = self.root / "config.yaml"
        config.write_text("required:\n  - present\n", encoding="utf-8")
        brief = self.root / "brief.md"
        brief.write_text("present\n", encoding="utf-8")
        original_open = os.open

        def swap(path: object, *args: object, **kwargs: object) -> int:
            """Replace the config with a FIFO immediately before opening it."""
            if path == config:
                config.unlink()
                os.mkfifo(config)
            return original_open(path, *args, **kwargs)

        previous = signal.signal(signal.SIGALRM, self._alarm_handler)
        try:
            with patch.object(lane_brief.os, "open", side_effect=swap):
                signal.alarm(2)
                rc, _, error = self.run_cli("check", str(brief), "--config", str(config))
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, previous)
        self.assertEqual(rc, 2)
        self.assertIn("not a regular file", error)

    def test_invalid_yaml_required_items(self) -> None:
        """Reject comments, unindented list items, and non-string values."""
        config = self.root / "config.yaml"
        brief = self.root / "brief.md"
        brief.write_text("first\n# TODO\n3\n", encoding="utf-8")
        for document in (
            "required:\n  - # TODO\n",
            "required:\n  - first\n- second\n",
            "required:\n  - 3\n",
        ):
            with self.subTest(document=document):
                config.write_text(document, encoding="utf-8")
                self.assertEqual(self.run_cli("check", str(brief), "--config", str(config))[0], 2)

    def test_yaml_mapping_validation(self) -> None:
        """Reject duplicate keys and invalid document or required-list shapes."""
        config = self.root / "config.yaml"
        brief = self.root / "brief.md"
        brief.write_text("first\n", encoding="utf-8")
        for document, reason in (
            ("required: [first]\nother: 1\nother: 2\n", "duplicate key other"),
            ("required: [first]\nrequired: [second]\n", "duplicate required key"),
            ("- first\n", "required list"),
            ("required: first\n", "required list"),
            ('required: ["line\\nbreak"]\n', "invalid required item"),
            ("required: [{}]\n", "invalid required item"),
        ):
            with self.subTest(document=document):
                config.write_text(document, encoding="utf-8")
                rc, _, error = self.run_cli("check", str(brief), "--config", str(config))
                self.assertEqual(rc, 2, error)
                self.assertIn(reason, error)
                self.assertIn(str(config), error)

    def test_repository_required_values(self) -> None:
        """Preserve every requirement in the repository declaration."""
        self.assertEqual(
            lane_brief.read_required(CONFIG),
            [
                "docstrings: every public function you add OR touch",
                "one-line docstring on every new test function",
                "ruff check scripts tests",
                "ruff format --check scripts tests",
                "docstring_diff.py --cached",
                "no commit, no push",
                "-m unittest tests.test_import_style",
            ],
        )

    def test_config_item_boundaries(self) -> None:
        """Accept maximum list and item lengths and reject values above them."""
        config = self.root / "config.yaml"
        brief = self.root / "brief.md"
        brief.write_text("a" * 201, encoding="utf-8")
        for count, length, expected, reason in (
            (32, 1, 0, ""),
            (33, 1, 2, "32 items"),
            (1, 200, 0, ""),
            (1, 201, 2, "200 characters"),
        ):
            with self.subTest(count=count, length=length):
                config.write_text(
                    "required:\n" + ("  - " + "a" * length + "\n") * count, encoding="utf-8"
                )
                rc, _, error = self.run_cli("check", str(brief), "--config", str(config))
                self.assertEqual(rc, expected, error)
                self.assertIn(reason, error)

    def test_declaration_matches_template(self) -> None:
        """Keep the real declaration nonempty and present in the tracked template."""
        required = lane_brief.read_required(CONFIG)
        self.assertTrue(required)
        content = TEMPLATE.read_text(encoding="utf-8")
        for item in required:
            with self.subTest(item=item):
                self.assertIn(item, content)

    def test_reader_accepts_bare_and_quoted_items_and_stops(self) -> None:
        """Read both item forms without consuming a neighboring section."""
        standards = self.root / "config.yaml"
        standards.write_text(
            '---\n# comment\nrequired:\n  - "quoted"\n  - bare value\n'
            "other:\n  - ignored\nnext:\n  - ignored\n",
            encoding="utf-8",
        )
        self.assertEqual(lane_brief.read_required(standards), ["quoted", "bare value"])

    def test_render_refuses_invalid_result_and_does_not_write(self) -> None:
        """Do not emit a rendered brief if an argument leaves a placeholder."""
        destination = self.root / "out.md"
        args = [*ARGS, "--finding", "<task>", "-o", str(destination)]
        rc, output, error = self.run_cli(*args)
        self.assertEqual(rc, 1)
        self.assertEqual(output, "")
        self.assertFalse(destination.exists())
        self.assertIn("unfilled placeholder <task>", error)

    def test_line_count_boundaries_for_brief_and_config(self) -> None:
        """Accept 10000 lines and reject 10001 lines in both inputs."""
        brief = self.root / "brief.md"
        config = self.root / "config.yaml"
        for target in (brief, config):
            with self.subTest(target=target.name):
                for count in (10000, 10001):
                    brief.write_text("present\n", encoding="utf-8")
                    config.write_text("required:\n  - present\n", encoding="utf-8")
                    prefix = "present\n" if target == brief else "required:\n  - present\n"
                    target.write_text(
                        prefix + "\n" * (count - prefix.count("\n")), encoding="utf-8"
                    )
                    rc, _, error = self.run_cli("check", str(brief), "--config", str(config))
                    self.assertEqual(rc, 0 if count == 10000 else 2, error)
                    if count == 10001:
                        self.assertIn("exceeds 10000 lines", error)

    def test_duplicate_required_block_rejected(self) -> None:
        """Reject a second required block even after another section."""
        brief = self.root / "brief.md"
        config = self.root / "config.yaml"
        brief.write_text("first\n", encoding="utf-8")
        config.write_text(
            "required:\n  - first\nother: value\nrequired:\n  - second\n", encoding="utf-8"
        )
        rc, output, error = self.run_cli("check", str(brief), "--config", str(config))
        self.assertEqual(rc, 2)
        self.assertEqual(output, "")
        self.assertIn("duplicate required key", error)

    def test_invalid_required_children_rejected(self) -> None:
        """Reject empty and malformed values in the required section."""
        brief = self.root / "brief.md"
        config = self.root / "config.yaml"
        brief.write_text("Task:\n", encoding="utf-8")
        for child in ("  -", "  -   ", "  -x", "  foo"):
            with self.subTest(child=child):
                config.write_text(f'required:\n  - "Task:"\n{child}\n', encoding="utf-8")
                rc, _, error = self.run_cli("check", str(brief), "--config", str(config))
                self.assertEqual(rc, 2, error)
                self.assertIn(str(config), error)

    def test_nested_scalar_continuation(self) -> None:
        """Require text that YAML folds into a preceding scalar."""
        brief = self.root / "brief.md"
        config = self.root / "config.yaml"
        brief.write_text("Task\n", encoding="utf-8")
        config.write_text("required:\n  - Task\n    - nested\n", encoding="utf-8")
        self.assertEqual(lane_brief.read_required(config), ["Task - nested"])
        self.assertEqual(self.run_cli("check", str(brief), "--config", str(config))[0], 1)

    def test_other_section_child_ignored(self) -> None:
        """Ignore valid content under another top-level section."""
        brief = self.root / "brief.md"
        config = self.root / "config.yaml"
        brief.write_text("Task:\n", encoding="utf-8")
        config.write_text('required:\n  - "Task:"\nother:\n  - nested\n', encoding="utf-8")
        rc, _, error = self.run_cli("check", str(brief), "--config", str(config))
        self.assertEqual(rc, 0, error)

    def test_render_line_limit_refuses_file_and_stdout(self) -> None:
        """Reject combined render values exceeding the output line limit."""
        destination = self.root / "out.md"
        args = [*ARGS, "--finding", "\n" * 4000, "--fix", "\n" * 4000, "--tests", "\n" * 4000]
        for extra in ([], ["-o", str(destination)]):
            with self.subTest(extra=extra):
                rc, output, error = self.run_cli(*args, *extra)
                self.assertEqual(rc, 2)
                self.assertEqual(output, "")
                self.assertFalse(destination.exists())
                self.assertIn("exceeds 10000 lines", error)

    def test_render_argument_character_boundaries(self) -> None:
        """Accept 8192 characters and reject 8193 for every render value."""
        for name in (
            "branch",
            "task",
            "finding",
            "fix",
            "tests",
            "paths",
            "modules",
            "issue",
            "round",
        ):
            for count in (8192, 8193):
                with self.subTest(name=name, count=count):
                    rc, output, error = self.run_cli(*ARGS, f"--{name}", "x" * count)
                    self.assertEqual(rc, 0 if count == 8192 else 2, error)
                    if count == 8193:
                        self.assertEqual(output, "")
                        self.assertIn(f"--{name}", error)

    def test_render_byte_limits_refuse_output(self) -> None:
        """Reject oversized rendered lines and content before writing output."""
        template = self.root / "template.md"
        destination = self.root / "out.md"
        for content, reason in (
            ("<finding>" * 3, "line exceeds"),
            ("<finding>\n" * 33, "size exceeds"),
        ):
            with self.subTest(reason=reason):
                template.write_text(content, encoding="utf-8")
                with patch.object(lane_brief, "TEMPLATE", template):
                    rc, output, error = self.run_cli(
                        *ARGS, "--finding", "😀" * 8192, "-o", str(destination)
                    )
                self.assertEqual(rc, 2)
                self.assertEqual(output, "")
                self.assertFalse(destination.exists())
                self.assertIn(reason, error)

    def test_run_runner_pid_guards(self) -> None:
        """Prove run_runner guards against invalid and current-group PIDs in cleanup."""

        def run_with(pid: int | bool) -> tuple[Mock, Mock]:
            killpg = Mock()
            process = SimpleNamespace(
                pid=pid,
                kill=Mock(),
                wait=Mock(return_value=0),
                poll=Mock(return_value=None),
                stdout=SimpleNamespace(close=Mock()),
            )
            with (
                patch.object(lane_brief.subprocess, "Popen", return_value=process),
                patch.object(lane_brief, "read_runner_output", side_effect=RuntimeError("planted")),
                patch.object(lane_brief.os, "killpg", killpg),
                self.assertRaisesRegex(RuntimeError, "planted"),
            ):
                lane_brief.run_runner(["dummy"])
            return killpg, process.kill

        for bad_pid in (0, 1, True):
            with self.subTest(bad_pid=bad_pid):
                killpg, kill = run_with(bad_pid)
                killpg.assert_not_called()
                kill.assert_not_called()

        with self.subTest(bad_pid=os.getpgrp()):
            killpg, kill = run_with(os.getpgrp())
            killpg.assert_not_called()
            kill.assert_called_once_with()

        good_pid = 4242 if os.getpgrp() != 4242 else 4243
        killpg, kill = run_with(good_pid)
        killpg.assert_called_once_with(good_pid, signal.SIGKILL)
        kill.assert_not_called()

        killpg, kill = run_with(2)
        killpg.assert_called_once_with(2, signal.SIGKILL)
        kill.assert_not_called()


def _write_import_chain(tests_dir: Path) -> list[str]:
    """Write import-chain fixture test files and return their sorted dotted module names."""
    target = "secret" + "-tool"
    (tests_dir / "test_planted.py").write_text(f"dummy = '{target}'\n", encoding="utf-8")
    (tests_dir / "test_alias.py").write_text(
        "from tests import test_planted as p\n", encoding="utf-8"
    )
    (tests_dir / "test_dotted.py").write_text("import tests.test_planted\n", encoding="utf-8")
    (tests_dir / "test_from.py").write_text(
        "from tests.test_planted import Base\n", encoding="utf-8"
    )
    (tests_dir / "test_chain.py").write_text("from tests import test_alias\n", encoding="utf-8")
    return [
        "tests.test_alias",
        "tests.test_chain",
        "tests.test_dotted",
        "tests.test_from",
        "tests.test_planted",
    ]


class CredentialStubGapsTest(unittest.TestCase):
    """Exercise gap detection for declared credential stub tests."""

    def test_repository_credential_stub_tests(self) -> None:
        """Verify repository credential_stub_tests has no undeclared or stale entries."""
        with CONFIG.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
        self.assertIsInstance(data, dict)
        self.assertIn("credential_stub_tests", data)
        stub_tests = data["credential_stub_tests"]
        self.assertIsInstance(stub_tests, list)
        self.assertEqual(credential_stub_gaps(ROOT / "tests", stub_tests), ([], []))

    def test_credential_stub_gaps_planted_secret_tool(self) -> None:
        """Report undeclared test files referencing the target stub tool."""
        target = "secret" + "-tool"
        with tempfile.TemporaryDirectory() as tmp_dir:
            tests_dir = Path(tmp_dir)
            (tests_dir / "test_planted.py").write_text(f"dummy = '{target}'\n", encoding="utf-8")
            self.assertEqual(
                credential_stub_gaps(tests_dir, []),
                (["tests.test_planted"], []),
            )

    def test_credential_stub_gaps_stale_declaration(self) -> None:
        """Report declared entries that do not exist as test files."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tests_dir = Path(tmp_dir)
            self.assertEqual(
                credential_stub_gaps(tests_dir, ["tests.test_gone"]),
                ([], ["tests.test_gone"]),
            )

    def test_credential_stub_gaps_boundary_cases(self) -> None:
        """Ignore non-matching variants and non-test helper files."""
        target = "secret" + "-tool"
        with tempfile.TemporaryDirectory() as tmp_dir:
            tests_dir = Path(tmp_dir)
            (tests_dir / "test_underscore.py").write_text("val = 'secret_tool'\n", encoding="utf-8")
            (tests_dir / "test_space.py").write_text('val = "secret tool"\n', encoding="utf-8")
            (tests_dir / "helper.py").write_text(f"val = '{target}'\n", encoding="utf-8")
            self.assertEqual(credential_stub_gaps(tests_dir, []), ([], []))

    def test_credential_stub_gaps_import_reach_chain(self) -> None:
        """Report all test modules reaching the secret tool via aliases and chained imports."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tests_dir = Path(tmp_dir)
            expected = _write_import_chain(tests_dir)
            self.assertEqual(credential_stub_gaps(tests_dir, []), (expected, []))

    def test_credential_stub_gaps_clean_and_declared_boundary(self) -> None:
        """Exclude clean tests and return empty gaps when all reaching tests are declared."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tests_dir = Path(tmp_dir)
            declared = _write_import_chain(tests_dir)
            (tests_dir / "test_clean.py").write_text(
                "import unittest\nfrom tests import helpers\n", encoding="utf-8"
            )
            self.assertEqual(credential_stub_gaps(tests_dir, declared), ([], []))

    def test_credential_stub_gaps_syntax_error_fail_closed(self) -> None:
        """Report invalid Python files as reaching the stub to fail closed."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tests_dir = Path(tmp_dir)
            (tests_dir / "test_broken.py").write_text("def (:\n", encoding="utf-8")
            self.assertEqual(
                credential_stub_gaps(tests_dir, []),
                (["tests.test_broken"], []),
            )

    def test_credential_stub_gaps_unreadable_fail_closed(self) -> None:
        """Report unreadable test files as reaching the stub to fail closed."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tests_dir = Path(tmp_dir)
            (tests_dir / "test_locked.py").write_text("x = 1\n", encoding="utf-8")
            real_open = Path.open

            def fake_open(path_obj: Path, *args: object, **kwargs: object) -> object:
                """Raise PermissionError for test_locked.py or delegate to real open."""
                if path_obj.name == "test_locked.py":
                    raise PermissionError("Access denied")
                return real_open(path_obj, *args, **kwargs)

            with patch("pathlib.Path.open", side_effect=fake_open, autospec=True):
                self.assertEqual(
                    credential_stub_gaps(tests_dir, []),
                    (["tests.test_locked"], []),
                )


class LaneBriefIsolationTest(unittest.TestCase):
    """Verify isolation blocks runner execution before its precheck starts."""

    def setUp(self) -> None:
        """Render a brief and create a runner that records every argument."""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.brief = self.root / "brief.md"
        self.assertEqual(lane_brief.main(ARGS[:] + ["-o", str(self.brief)]), 0)
        self.calls = self.root / "args"
        self.runner = self.root / "runner.sh"
        self.runner.write_text(
            '#!/bin/sh\ndir=$(dirname "$0")\nprintf "%s\\n" "$@" >> "$dir/args"\n',
            encoding="utf-8",
        )
        self.runner.chmod(0o755)

    def check_worktree(self, worktree: Path) -> tuple[int, str]:
        """Check a worktree with the real isolation check and a recording runner."""
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            rc = lane_brief.main(
                [
                    "check",
                    str(self.brief),
                    "--runner",
                    str(self.runner),
                    "--worktree",
                    str(worktree),
                    "--config",
                    str(CONFIG),
                ]
            )
        return rc, stderr.getvalue()

    def test_planted_main_never_starts_runner(self) -> None:
        """Reject the planted unmarked main checkout before starting its runner."""
        ref = f"refs/heads/local/{lane_isolation.PRIVATE_WORDS[0]}-topic"
        main = repo(self.root / "main", ref)
        rc, error = self.check_worktree(main)
        self.assertEqual(rc, 1)
        self.assertIn("lane isolation refused worktree", error)
        self.assertIn(ref, error)
        self.assertFalse(self.calls.exists())

    @unittest.skipUnless(os.name == "posix", "runner fixture is a POSIX shell script")
    def test_marked_clone_starts_runner(self) -> None:
        """Forward the exact brief and worktree arguments after isolation passes."""
        origin = repo(self.root / "origin")
        lane = self.root / "lane"
        run_git(self.root, "clone", "-q", str(origin), str(lane), preserve_config=True)
        self.assertEqual(lane_isolation.mark(lane), [])
        rc, error = self.check_worktree(lane)
        self.assertEqual((rc, error), (0, ""))
        self.assertEqual(
            self.calls.read_text(encoding="utf-8").splitlines(),
            ["--check-brief", str(self.brief), str(lane)],
        )

    def test_not_git_never_starts_runner(self) -> None:
        """Report a Git input error without starting the configured runner."""
        rc, error = self.check_worktree(self.root / "missing")
        self.assertEqual(rc, 2)
        self.assertIn("lane isolation failed", error)
        self.assertFalse(self.calls.exists())

    def test_git_unavailable_never_starts_runner(self) -> None:
        """Report unavailable Git as an isolation error without starting the runner."""
        with patch.object(lane_isolation.subprocess, "Popen", side_effect=FileNotFoundError("git")):
            rc, error = self.check_worktree(self.root / "missing")
        self.assertEqual(rc, 2)
        self.assertIn("lane isolation failed", error)
        self.assertFalse(self.calls.exists())


@unittest.skipUnless(os.name == "posix", "runner fixture uses /bin/sh")
class RunnerEnvironmentTest(unittest.TestCase):
    """Check that runners inherit no caller Git repository-location overrides."""

    def test_runner_drops_git_location(self) -> None:
        """Remove caller Git directory and worktree overrides before starting a runner."""
        with patch.dict(
            os.environ,
            {"GIT_DIR": "/nonexistent/private.git", "GIT_WORK_TREE": "/nonexistent/private"},
        ):
            result = lane_brief.run_runner(
                ["/bin/sh", "-c", 'printf "%s %s" "${GIT_DIR-unset}" "${GIT_WORK_TREE-unset}"']
            )
        self.assertEqual(result, (b"unset unset", 0))


if __name__ == "__main__":
    unittest.main()
