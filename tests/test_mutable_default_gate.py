# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Tests for the mutable default argument lint gate."""

from __future__ import annotations

import io
import os
import subprocess  # nosec B404 - fixture runner drives the Python gate on temporary fixtures.
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import lint_mutable_defaults as gate

ROOT = Path(__file__).resolve().parent.parent


def check_pylint_toolchain() -> str | None:
    """Return a skip reason unless the gate can resolve its pinned toolchain."""
    if gate.resolve_pylint() is None:
        return (
            f"neither pylint=={gate.PYLINT_VERSION} under {sys.executable} or on PATH nor uvx found"
        )
    return None


def run_gate(fixture_path: Path) -> subprocess.CompletedProcess[str]:
    """Execute the Python gate directly on a fixture file."""
    return subprocess.run(  # nosec B603 - local script and explicit fixture path, no shell.
        [sys.executable, str(ROOT / "scripts/lint_mutable_defaults.py"), str(fixture_path)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )


class MutableDefaultGateTest(unittest.TestCase):
    """Verify that the Python gate catches dangerous default values."""

    def setUp(self) -> None:
        """Skip when the gate cannot resolve its pinned Pylint or uvx."""
        reason = check_pylint_toolchain()
        if reason:
            print(reason)
            self.skipTest(reason)

    def test_planted_name_bound_default_fails(self) -> None:
        """Planted name-bound mutable default must fail with W0102 in output."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            fixture = Path(tmp_dir) / "fixture_name_bound.py"
            fixture.write_text(
                "EMPTY_LIST = []\n\n\n"
                "def dummy_func(path, items=EMPTY_LIST):\n"
                "    return (path, items)\n",
                encoding="utf-8",
            )
            result = run_gate(fixture)
            combined = result.stdout + result.stderr
            self.assertNotEqual(result.returncode, 0, f"Expected non-zero exit code: {combined}")
            self.assertIn("W0102", combined, f"Expected W0102 in output: {combined}")

    def test_planted_literal_list_default_fails(self) -> None:
        """Planted literal [] default must fail with non-zero exit code."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            fixture = Path(tmp_dir) / "fixture_literal.py"
            fixture.write_text(
                "def dummy_func(path, items=[]):\n    return (path, items)\n",
                encoding="utf-8",
            )
            result = run_gate(fixture)
            combined = result.stdout + result.stderr
            self.assertNotEqual(result.returncode, 0, f"Expected non-zero exit code: {combined}")
            self.assertIn("W0102", combined, f"Expected W0102 in output: {combined}")

    def test_clean_none_default_passes(self) -> None:
        """Clean None default parameter must pass with exit code 0."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            fixture = Path(tmp_dir) / "fixture_clean.py"
            fixture.write_text(
                "def dummy_func(path, items=None):\n"
                "    if items is None:\n"
                "        items = []\n"
                "    return (path, items)\n",
                encoding="utf-8",
            )
            result = run_gate(fixture)
            combined = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, f"Expected zero exit code: {combined}")
            self.assertNotIn("W0102", combined)


class ResolvePylintTest(unittest.TestCase):
    """Exercise exact version matching and the pinned uvx fallback."""

    def test_exact_version_is_accepted(self) -> None:
        """Accept only the exact pinned first version line."""
        with (
            patch.object(gate.shutil, "which", return_value="pylint") as which,
            patch.object(gate.subprocess, "run") as run,
        ):
            run.side_effect = [
                OSError("cannot start interpreter pylint"),
                subprocess.CompletedProcess(
                    ["pylint", "--version"],
                    0,
                    stdout=f"pylint {gate.PYLINT_VERSION}\nastroid 4.3.4\n",
                ),
            ]
            self.assertEqual(gate.resolve_pylint(), ["pylint"])
            which.assert_called_once_with("pylint")
            self.assertEqual(
                run.call_args_list,
                [
                    unittest.mock.call(
                        [sys.executable, "-m", "pylint", "--version"],
                        capture_output=True,
                        text=True,
                        check=True,
                        timeout=10,
                    ),
                    unittest.mock.call(
                        ["pylint", "--version"],
                        capture_output=True,
                        text=True,
                        check=True,
                        timeout=10,
                    ),
                ],
            )

    def test_nonexact_versions_are_refused(self) -> None:
        """Reject suffixes, longer releases, empty output and a later matching line."""
        versions = (
            "pylint 4.1.3.dev0",
            "pylint 4.1.3.post1",
            "pylint 4.1.3.1",
            "pylint 4.1.30",
            "pylint 4.1.2",
            "",
            f"banner\npylint {gate.PYLINT_VERSION}",
        )
        for version in versions:
            with (
                self.subTest(version=version),
                patch.object(gate.shutil, "which", side_effect=["pylint", None]),
                patch.object(gate.subprocess, "run") as run,
            ):
                run.return_value.stdout = version
                self.assertIsNone(gate.resolve_pylint())

    def test_uvx_fallback_is_pinned(self) -> None:
        """Use pinned uvx when Pylint is absent or has an unsupported version."""
        for pylint in (None, "pylint"):
            with (
                self.subTest(pylint=pylint),
                patch.object(gate.shutil, "which", side_effect=[pylint, "uvx"]),
                patch.object(gate.subprocess, "run") as run,
            ):
                run.return_value.stdout = "pylint 4.1.2\n"
                self.assertEqual(
                    gate.resolve_pylint(),
                    ["uvx", "--from", f"pylint=={gate.PYLINT_VERSION}", "pylint"],
                )
                self.assertEqual(run.call_count, 1 + int(pylint is not None))

    def test_failed_version_check_falls_back(self) -> None:
        """Fall back when the local version command fails, times out or cannot start."""
        failures = (
            subprocess.CalledProcessError(1, ["pylint", "--version"]),
            subprocess.TimeoutExpired(["pylint", "--version"], 10),
            OSError("cannot start pylint"),
        )
        for failure in failures:
            with (
                self.subTest(failure=failure),
                patch.object(gate.shutil, "which", side_effect=["pylint", "uvx"]),
                patch.object(gate.subprocess, "run", side_effect=failure),
            ):
                self.assertEqual(
                    gate.resolve_pylint(),
                    ["uvx", "--from", f"pylint=={gate.PYLINT_VERSION}", "pylint"],
                )

    def test_missing_tools_supply_pinned_skip_reason(self) -> None:
        """Skip fixtures with the pinned version named when neither tool is available."""
        with (
            patch.object(gate.shutil, "which", return_value=None),
            patch.object(
                gate.subprocess, "run", side_effect=OSError("no interpreter pylint")
            ) as run,
        ):
            self.assertIn(f"pylint=={gate.PYLINT_VERSION}", check_pylint_toolchain())
            run.assert_called_once_with(
                [sys.executable, "-m", "pylint", "--version"],
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            )

    def test_unsupported_pylint_supplies_skip_reason(self) -> None:
        """Skip fixtures when only an unsupported local Pylint exists."""
        with (
            patch.object(gate.shutil, "which", side_effect=["pylint", None]),
            patch.object(gate.subprocess, "run") as run,
        ):
            run.return_value.stdout = "pylint 4.1.2\n"
            self.assertIn(f"pylint=={gate.PYLINT_VERSION}", check_pylint_toolchain())

    def test_resolver_ignores_environment_override(self) -> None:
        """Resolve the pinned tool despite a planted PYLINT=true environment override."""
        with (
            patch.dict(os.environ, {"PYLINT": "true", "PYLINT_VERSION": "0.0.0"}),
            patch.object(gate.shutil, "which", return_value="pylint"),
            patch.object(gate.subprocess, "run") as run,
        ):
            run.side_effect = [
                OSError("no interpreter pylint"),
                subprocess.CompletedProcess(
                    ["pylint", "--version"], 0, stdout=f"pylint {gate.PYLINT_VERSION}\n"
                ),
            ]
            self.assertEqual(gate.resolve_pylint(), ["pylint"])
            self.assertEqual(run.call_count, 2)

    def test_interpreter_pylint_at_pin_is_accepted(self) -> None:
        """Accept pinned interpreter Pylint without querying PATH."""
        with (
            patch.object(gate.shutil, "which") as which,
            patch.object(gate.subprocess, "run") as run,
        ):
            run.return_value = subprocess.CompletedProcess(
                [sys.executable, "-m", "pylint", "--version"],
                0,
                stdout=f"pylint {gate.PYLINT_VERSION}\nastroid 4.3.4\n",
            )
            self.assertEqual(gate.resolve_pylint(), [sys.executable, "-m", "pylint"])
            which.assert_not_called()
            run.assert_called_once_with(
                [sys.executable, "-m", "pylint", "--version"],
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            )

    def test_interpreter_version_mismatch_falls_back_to_path(self) -> None:
        """Fall back to pinned PATH Pylint when interpreter Pylint version differs."""
        path_pylint = "/custom/bin/pylint"
        with (
            patch.object(gate.shutil, "which", return_value=path_pylint) as which,
            patch.object(gate.subprocess, "run") as run,
        ):
            run.side_effect = [
                subprocess.CompletedProcess(
                    [sys.executable, "-m", "pylint", "--version"],
                    0,
                    stdout="pylint 4.1.2\n",
                ),
                subprocess.CompletedProcess(
                    [path_pylint, "--version"],
                    0,
                    stdout=f"pylint {gate.PYLINT_VERSION}\n",
                ),
            ]
            self.assertEqual(gate.resolve_pylint(), [path_pylint])
            which.assert_called_once_with("pylint")
            self.assertEqual(
                run.call_args_list,
                [
                    unittest.mock.call(
                        [sys.executable, "-m", "pylint", "--version"],
                        capture_output=True,
                        text=True,
                        check=True,
                        timeout=10,
                    ),
                    unittest.mock.call(
                        [path_pylint, "--version"],
                        capture_output=True,
                        text=True,
                        check=True,
                        timeout=10,
                    ),
                ],
            )

    def test_interpreter_oserror_and_missing_path_falls_back_to_uvx(self) -> None:
        """Fall back to pinned uvx when interpreter probe raises OSError and PATH has no Pylint."""
        with (
            patch.object(gate.shutil, "which", side_effect=[None, "/usr/bin/uvx"]) as which,
            patch.object(
                gate.subprocess, "run", side_effect=OSError("no interpreter pylint")
            ) as run,
        ):
            self.assertEqual(
                gate.resolve_pylint(),
                ["/usr/bin/uvx", "--from", f"pylint=={gate.PYLINT_VERSION}", "pylint"],
            )
            run.assert_called_once_with(
                [sys.executable, "-m", "pylint", "--version"],
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            )
            self.assertEqual(
                which.call_args_list,
                [
                    unittest.mock.call("pylint"),
                    unittest.mock.call("uvx"),
                ],
            )


class GateInvocationTest(unittest.TestCase):
    """Check discovery failures, portable empty handling and protected operands."""

    def setUp(self) -> None:
        """Isolate subprocesses, tool resolution and filesystem lookups."""
        self.run = self.enterContext(patch.object(gate.subprocess, "run"))
        self.run.return_value = subprocess.CompletedProcess([], 0, stdout=b"")
        self.resolve = self.enterContext(
            patch.object(gate, "resolve_pylint", return_value=["pylint"])
        )
        self.is_file = self.enterContext(patch.object(gate.Path, "is_file", return_value=True))
        self.stdout = self.enterContext(patch("sys.stdout", new_callable=io.StringIO))
        self.stderr = self.enterContext(patch("sys.stderr", new_callable=io.StringIO))

    def test_git_failure_fails_closed(self) -> None:
        """Reject failed Git discovery even if it supplies partial file output."""
        self.run.side_effect = subprocess.CalledProcessError(
            128, ["git", "ls-files"], output=b"partial.py\0", stderr=b"fatal: not a repository"
        )
        self.assertNotEqual(gate.main([]), 0)
        self.assertIn("git", self.stderr.getvalue())
        self.resolve.assert_not_called()

    def test_missing_files_and_directories_are_filtered(self) -> None:
        """Ignore deleted paths and directories emitted by Git before linting."""
        self.run.side_effect = [
            subprocess.CompletedProcess([], 0, stdout=b"deleted.py\0folder.py\0kept.py\0"),
            subprocess.CompletedProcess([], 0),
        ]
        self.is_file.side_effect = [False, False, True]
        self.assertEqual(gate.main([]), 0)
        self.run.assert_any_call(
            ["git", "ls-files", "-c", "-o", "--exclude-standard", "-z", "--", "*.py"],
            capture_output=True,
            check=True,
            timeout=30,
        )
        self.assertEqual(
            self.run.call_args.args[0], ["pylint", *gate.PYLINT_FLAGS, "--", "./kept.py"]
        )

    def test_empty_discovery_does_not_run_pylint(self) -> None:
        """Succeed without resolving or running Pylint when Git lists no Python files."""
        self.assertEqual(gate.main([]), 0)
        self.assertIn("no Python files", self.stdout.getvalue())
        self.assertEqual(self.run.call_count, 1)
        self.resolve.assert_not_called()

    def test_all_missing_paths_do_not_run_pylint(self) -> None:
        """Succeed without a toolchain when every explicit path is missing."""
        self.is_file.return_value = False
        self.assertEqual(gate.main(["deleted.py"]), 0)
        self.assertIn("no Python files", self.stdout.getvalue())
        self.run.assert_not_called()
        self.resolve.assert_not_called()

    def test_option_shaped_filename_is_prefixed(self) -> None:
        """Protect init-hook filenames before Pylint's preprocessor sees them."""
        self.assertEqual(gate.main(["--init-hook=x.py"]), 0)
        self.run.assert_called_once_with(
            ["pylint", *gate.PYLINT_FLAGS, "--", "./--init-hook=x.py"],
            check=False,
            timeout=120,
        )

    def test_shell_metacharacters_remain_one_operand(self) -> None:
        """Pass spaces, newlines and shell syntax unchanged in one path operand."""
        filename = "space ;$(touch sentinel)\n'quoted'.py"
        self.assertEqual(gate.main([filename]), 0)
        self.assertEqual(self.run.call_args.args[0][-1], f"./{filename}")
        self.assertNotIn("shell", self.run.call_args.kwargs)

    def test_git_nul_delimiters_preserve_path_boundaries(self) -> None:
        """Split Git's output only on NUL, retaining embedded whitespace."""
        self.run.side_effect = [
            subprocess.CompletedProcess([], 0, stdout=b"space name.py\0line\nbreak.py\0"),
            subprocess.CompletedProcess([], 0),
        ]
        self.assertEqual(gate.main([]), 0)
        self.assertEqual(self.run.call_args.args[0][-2:], ["./space name.py", "./line\nbreak.py"])

    def test_absolute_path_is_preserved(self) -> None:
        """Accept native absolute paths without adding a relative prefix."""
        absolute = str(ROOT / "tests/fixture.py")
        self.assertEqual(gate.main([absolute]), 0)
        self.assertEqual(self.run.call_args.args[0][-1], absolute)

    def test_missing_tools_fail_with_pinned_message(self) -> None:
        """Fail with both tool names when files exist but the resolver returns None."""
        self.resolve.return_value = None
        self.assertNotEqual(gate.main(["fixture.py"]), 0)
        self.assertIn(f"pylint=={gate.PYLINT_VERSION}", self.stderr.getvalue())
        self.assertIn("uvx", self.stderr.getvalue())
        self.run.assert_not_called()

    def test_no_command_override_environment_is_honoured(self) -> None:
        """Ignore planted Makefile-style overrides for command, flags, version and files."""
        overrides = {
            "PYLINT": "true",
            "PYLINT_VERSION": "0.0.0",
            "PYLINT_FLAGS": "--exit-zero",
            "FILES": "ignored.py",
        }
        with patch.dict(os.environ, overrides):
            self.assertEqual(gate.main(["fixture.py"]), 0)
        self.run.assert_called_once_with(
            ["pylint", *gate.PYLINT_FLAGS, "--", "./fixture.py"], check=False, timeout=120
        )

    def test_lint_status_is_propagated(self) -> None:
        """Return Pylint's nonzero status without turning it into success."""
        self.run.return_value.returncode = 4
        self.assertEqual(gate.main(["fixture.py"]), 4)

    def test_subprocess_start_and_timeout_failures_fail_closed(self) -> None:
        """Report failed starts and timeouts during discovery and lint as failures."""
        for argv in ([], ["fixture.py"]):
            for failure in (OSError("cannot start"), subprocess.TimeoutExpired("tool", 30)):
                with self.subTest(argv=argv, failure=failure):
                    self.run.side_effect = failure
                    self.assertNotEqual(gate.main(argv), 0)
                    self.assertIn(str(failure), self.stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
