# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Tests for repository semgrep rules."""

from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess  # nosec B404 - subprocess run for semgrep CLI tool.
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Semgrep prints a rule configuration warning on stderr and still exits 0.
RULE_WARNING_RE = re.compile(r"^Rule \S+ contains .*$", re.MULTILINE)

FIXTURE_SOURCE = """# SPDX-License-Identifier: EUPL-1.2
\"\"\"Fixture functions for semgrep rule testing.\"\"\"

import logging
import sys

log = logging.getLogger(__name__)


def case_flagged_oserror():
    \"\"\"Flagged: OSError returning empty list.\"\"\"
    try:
        pass
    except OSError:
        return []


def case_flagged_permission_error():
    \"\"\"Flagged: PermissionError returning empty string.\"\"\"
    try:
        pass
    except PermissionError as exc:
        return ""


def case_flagged_tuple_value_os():
    \"\"\"Flagged: Tuple with OSError returning empty dict.\"\"\"
    try:
        pass
    except (ValueError, OSError):
        return {}


def case_flagged_tuple_fnf_permission():
    \"\"\"Flagged: Tuple with PermissionError returning 0.\"\"\"
    try:
        pass
    except (FileNotFoundError, PermissionError):
        return 0


def case_flagged_fnf():
    \"\"\"Flagged: FileNotFoundError returning empty string.\"\"\"
    try:
        pass
    except FileNotFoundError:
        return ""


def case_flagged_fnf_as_exc():
    \"\"\"Flagged: FileNotFoundError as exc returning empty list.\"\"\"
    try:
        pass
    except FileNotFoundError as exc:
        return []


def case_flagged_state_then_empty():
    \"\"\"Flagged: OSError mutating state before returning empty list.\"\"\"
    try:
        pass
    except OSError:
        state = 1
        return []


def case_not_flagged_print_stderr_then_empty():
    \"\"\"Not flagged: OSError printed to stderr returning empty list.\"\"\"
    try:
        pass
    except OSError as exc:
        print(exc, file=sys.stderr)
        return []


def case_not_flagged_stderr_write_then_empty():
    \"\"\"Not flagged: OSError written to stderr returning empty string.\"\"\"
    try:
        pass
    except OSError as exc:
        sys.stderr.write(str(exc))
        return ""


def case_not_flagged_logged_then_empty():
    \"\"\"Not flagged: OSError logged returning empty dict.\"\"\"
    try:
        pass
    except OSError as exc:
        log.warning("warning: %s", exc)
        return {}


def case_not_flagged_oserror_raise():
    \"\"\"Not flagged: OSError re-raised.\"\"\"
    try:
        pass
    except OSError:
        raise


def case_not_flagged_oserror_print_err():
    \"\"\"Not flagged: OSError printed to stderr returning 1.\"\"\"
    try:
        pass
    except OSError as exc:
        sys.stderr.write(str(exc))
        return 1
"""

NEGATED_STATUS_FIXTURE = (
    "#!/usr/bin/env bash\n"
    "# SPDX-"
    "License-Identifier: EUPL-1.2\n"
    "set -euo pipefail\n\n"
    "f() { return 77; }\n\n"
    "g() {\n"
    '  if ! out="$(f)"; then\n'
    "    rc=$?\n"
    '    echo "$rc"\n'
    "  fi\n"
    "  if ! f; then\n"
    "    local rc=$?\n"
    "  fi\n"
    "  if ! f; then rc=$?; fi\n"
    "  if ! f\n"
    "  then\n"
    "    rc=$?\n"
    "  fi\n"
    "  if ! f; then echo first\n"
    "  elif ! f; then rc=$?; fi\n"
    "  if f; then rc=0; else rc=$?; fi\n"
    "  f && rc=0 || rc=$?\n"
    "  # if ! f; then rc=$?; fi\n"
    '  echo "if ! f; then rc=$?"\n'
    "  if ! f; then :; fi; if f; then rc=$?; fi\n"
    "  if ! f 1; then # say why\n"
    "    rc=$?\n"
    "  fi\n"
    "  if ! f 2; then\n"
    "    # say why\n"
    "    rc=$?\n"
    "  fi\n"
    "  if ! f 3;\n"
    "  then\n"
    "    rc=$?\n"
    "  fi\n"
    "  if ! f 4; # say why\n"
    "  then rc=$?; fi\n"
    "  if ! grep -q '#' f; then\n"
    "    rc=$?\n"
    "  fi\n"
    "  if ! f 5; then # rc=$? is the negation\n"
    "    echo none\n"
    "  fi\n"
    "}\n"
)


def _negated_status_expected_lines() -> set[int]:
    """Return expected 1-based line numbers flagged in NEGATED_STATUS_FIXTURE."""
    lines = NEGATED_STATUS_FIXTURE.splitlines()
    return {
        next(i for i, line in enumerate(lines, 1) if 'if ! out="$(f)"; then' in line),
        next(i for i, line in enumerate(lines, 1) if line.strip() == "if ! f; then"),
        next(i for i, line in enumerate(lines, 1) if line.strip() == "if ! f; then rc=$?; fi"),
        next(i for i, line in enumerate(lines, 1) if line.strip() == "if ! f"),
        next(i for i, line in enumerate(lines, 1) if line.strip() == "elif ! f; then rc=$?; fi"),
        next(i for i, line in enumerate(lines, 1) if line.strip() == "if ! f 1; then # say why"),
        next(i for i, line in enumerate(lines, 1) if line.strip() == "if ! f 2; then"),
        next(i for i, line in enumerate(lines, 1) if line.strip() == "if ! f 3;"),
        next(i for i, line in enumerate(lines, 1) if line.strip() == "if ! f 4; # say why"),
        next(i for i, line in enumerate(lines, 1) if line.strip() == "if ! grep -q '#' f; then"),
    }


def _find_enclosing_function(
    functions: list[ast.FunctionDef],
    lineno: int,
) -> str | None:
    """Find the name of the function enclosing the given line number."""
    limit = len(functions)
    for i in range(limit):
        fn = functions[i]
        end_line = fn.end_lineno if fn.end_lineno is not None else fn.lineno
        if fn.lineno <= lineno <= end_line:
            return fn.name
    return None


def _run_semgrep(fixture: Path, scratch: Path) -> dict:
    """Run semgrep scan on a fixture inside scratch with isolated TMPDIR; fail on a rule warning."""
    semgrep_bin = shutil.which("semgrep")
    if semgrep_bin is None:
        raise RuntimeError("semgrep executable not found on PATH")
    env = os.environ.copy()
    env["TMPDIR"] = str(scratch)
    env.setdefault("XDG_CONFIG_HOME", str(scratch))
    proc = subprocess.run(  # nosec B603 B607 - fixed semgrep argv, no shell, bounded timeout.
        [
            semgrep_bin,
            "scan",
            "--config",
            str(ROOT / ".semgrep.yml"),
            "--json",
            "--metrics",
            "off",
            "--disable-version-check",
            str(fixture),
        ],
        cwd=scratch,
        env=env,
        timeout=300,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode not in (0, 1):
        raise RuntimeError(f"semgrep exited with code {proc.returncode}: {proc.stderr}")
    rule_warnings = RULE_WARNING_RE.findall(proc.stderr)
    if rule_warnings:
        raise RuntimeError(f"semgrep rule warnings for .semgrep.yml: {rule_warnings}")
    data: dict = json.loads(proc.stdout)
    if data.get("errors"):
        raise RuntimeError(f"semgrep reported execution errors: {data['errors']}")
    scanned = data.get("paths", {}).get("scanned", [])
    if len(scanned) == 0:
        raise RuntimeError(f"Semgrep scanned 0 targets for fixture {fixture}")
    return data


def _flagged_functions(results: list, source: str) -> set[str]:
    """Collect enclosing function names for matches of oserror-swallowed-as-empty."""
    tree = ast.parse(source)
    funcs = [n for n in tree.body if isinstance(n, ast.FunctionDef)]
    flagged: set[str] = set()
    limit = len(results)
    for i in range(limit):
        item = results[i]
        check_id = item.get("check_id", "")
        if check_id.endswith("oserror-swallowed-as-empty"):
            start_line = item["start"]["line"]
            enclosing = _find_enclosing_function(funcs, start_line)
            if enclosing is not None:
                flagged.add(enclosing)
    return flagged


@unittest.skipUnless(shutil.which("semgrep"), "semgrep not installed; make semgrep needs it")
class SemgrepRulesTest(unittest.TestCase):
    """Verify repository semgrep rule behavior against test fixtures."""

    def test_oserror_swallowed_as_empty_rule(self) -> None:
        """Rule oserror-swallowed-as-empty flags swallowed errors unless reported."""
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        scratch_path = Path(scratch.name)
        scripts_dir = scratch_path / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        fixture_file = scripts_dir / "fixture.py"
        fixture_file.write_text(FIXTURE_SOURCE, encoding="utf-8")

        data = _run_semgrep(fixture_file, scratch_path)
        flagged = _flagged_functions(data.get("results", []), FIXTURE_SOURCE)
        expected_flagged = {
            "case_flagged_oserror",
            "case_flagged_permission_error",
            "case_flagged_tuple_value_os",
            "case_flagged_tuple_fnf_permission",
            "case_flagged_fnf",
            "case_flagged_fnf_as_exc",
            "case_flagged_state_then_empty",
        }
        self.assertEqual(flagged, expected_flagged)

    def test_oserror_rule_skips_excluded_checkpoint(self) -> None:
        """Rule oserror-swallowed-as-empty skips excluded checkpoint helper."""
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        scratch_path = Path(scratch.name)
        target_dir = scratch_path / ".config" / "lefthook" / "scripts"
        target_dir.mkdir(parents=True, exist_ok=True)
        checkpoint_file = target_dir / "checkpoint.py"
        fixture_content = (
            "# SPDX-"
            "License-Identifier: EUPL-1.2\n"
            '"""Checkpoint fixture for exclusion testing."""\n\n'
            "def case_checkpoint_open():\n"
            '    """Deliberately swallowed FileNotFoundError returning empty string."""\n'
            "    try:\n"
            "        pass\n"
            "    except FileNotFoundError:\n"
            '        return ""\n'
        )
        checkpoint_file.write_text(fixture_content, encoding="utf-8")

        data = _run_semgrep(checkpoint_file, scratch_path)
        flagged = _flagged_functions(data.get("results", []), fixture_content)
        self.assertEqual(flagged, set())
        scanned = [Path(p).resolve() for p in data.get("paths", {}).get("scanned", [])]
        self.assertIn(checkpoint_file.resolve(), scanned)

    def test_negated_if_status_read_rule(self) -> None:
        """Rule flags negated if/elif status reads, not comments, strings or later statements."""
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        scratch_path = Path(scratch.name)
        scripts_dir = scratch_path / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        fixture_file = scripts_dir / "fixture.sh"
        fixture_file.write_text(NEGATED_STATUS_FIXTURE, encoding="utf-8")

        data = _run_semgrep(fixture_file, scratch_path)
        flagged_lines = {
            item["start"]["line"]
            for item in data.get("results", [])
            if item.get("check_id", "").endswith("negated-if-status-read")
        }
        expected_lines = _negated_status_expected_lines()
        self.assertEqual(flagged_lines, expected_lines)

    def test_negated_if_status_read_rule_covers_forgejo_script(self) -> None:
        """Rule negated-if-status-read scans ffmpeg-forgejo without extension."""
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        scratch_path = Path(scratch.name)
        scripts_dir = scratch_path / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        fixture_file = scripts_dir / "ffmpeg-forgejo"
        fixture_file.write_text(NEGATED_STATUS_FIXTURE, encoding="utf-8")

        data = _run_semgrep(fixture_file, scratch_path)
        flagged_lines = {
            item["start"]["line"]
            for item in data.get("results", [])
            if item.get("check_id", "").endswith("negated-if-status-read")
        }
        expected_lines = _negated_status_expected_lines()
        self.assertEqual(flagged_lines, expected_lines)
        scanned = [Path(p).resolve() for p in data.get("paths", {}).get("scanned", [])]
        self.assertIn(fixture_file.resolve(), scanned)
