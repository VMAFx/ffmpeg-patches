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

from scripts import upstream_lint

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

POPEN_PIPE_WAIT_FIXTURE = """# SPDX-License-Identifier: EUPL-1.2
\"\"\"Fixture functions for popen-pipe-wait semgrep rule testing.\"\"\"

import os
import signal
import subprocess
from subprocess import PIPE, Popen


def read_helper_output(proc, deadline):
    \"\"\"Simulate helper reading process pipe until deadline.\"\"\"
    pass


def log_process(target):
    \"\"\"Simulate a helper that records the process and reads no pipe.\"\"\"
    pass


def case_flagged_stdout_pipe():
    \"\"\"Flagged: stdout=PIPE then wait().\"\"\"
    proc = subprocess.Popen(["ls"], stdout=subprocess.PIPE)
    proc.wait(timeout=10)


def case_flagged_stderr_pipe():
    \"\"\"Flagged: stderr=PIPE then wait().\"\"\"
    proc = subprocess.Popen(["ls"], stderr=subprocess.PIPE)
    proc.wait(timeout=10)


def case_flagged_with_popen():
    \"\"\"Flagged: with Popen(... stdout=PIPE ...) as p then p.wait().\"\"\"
    with Popen(["ls"], stdout=PIPE) as proc:
        proc.wait(timeout=10)


def case_flagged_signal_then_wait():
    \"\"\"Flagged: signal sent before wait without pipe consumption.\"\"\"
    proc = subprocess.Popen(["ls"], stdout=subprocess.PIPE)
    proc.send_signal(signal.SIGINT)
    proc.wait(timeout=10)


def case_flagged_pid_passed():
    \"\"\"Flagged: pid passed to killpg before wait without pipe consumption.\"\"\"
    proc = subprocess.Popen(["ls"], stderr=subprocess.PIPE)
    os.killpg(proc.pid, signal.SIGTERM)
    proc.wait(timeout=10)


def case_flagged_wait_in_try_then_finally_kill(self):
    \"\"\"Flagged: wait in try block then kill and wait in finally.\"\"\"
    proc = None
    try:
        proc = subprocess.Popen(["ls"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        proc.wait(timeout=10)
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def case_flagged_process_asserted_after_wait(self):
    \"\"\"Flagged: process asserted after wait without pipe consumption.\"\"\"
    proc = subprocess.Popen(["ls"], stdout=subprocess.PIPE)
    proc.wait(timeout=10)
    self.assertIsNotNone(proc)


def case_flagged_unrelated_helper():
    \"\"\"Flagged: process passed to a helper that does not read its pipes.\"\"\"
    proc = subprocess.Popen(["ls"], stdout=subprocess.PIPE)
    log_process(proc)
    proc.wait(timeout=10)


def case_flagged_unrelated_helper_on_pipe():
    \"\"\"Flagged: pipe passed to a helper that does not read it.\"\"\"
    proc = subprocess.Popen(["ls"], stderr=subprocess.PIPE)
    log_process(proc.stderr)
    proc.wait(timeout=10)


def case_flagged_both_pipes_stdout_read():
    \"\"\"Flagged: both pipes, only stdout read before wait().\"\"\"
    proc = subprocess.Popen(["ls"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    proc.stdout.read()
    proc.wait(timeout=10)


def case_flagged_both_pipes_stderr_iterated():
    \"\"\"Flagged: both pipes, only stderr iterated before wait().\"\"\"
    proc = subprocess.Popen(["ls"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    for line in proc.stderr:
        pass
    proc.wait(timeout=10)


def case_flagged_both_pipes_helper_on_stdout():
    \"\"\"Flagged: both pipes, a read helper drains only stdout.\"\"\"
    proc = subprocess.Popen(["ls"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    read_helper_output(proc.stdout, 10.0)
    proc.wait(timeout=10)


def case_flagged_with_both_pipes_stderr_read():
    \"\"\"Flagged: with block, both pipes, only stderr read.\"\"\"
    with Popen(["ls"], stdout=PIPE, stderr=PIPE) as proc:
        proc.stderr.read()
        proc.wait(timeout=10)


def case_flagged_other_proc_communicate():
    \"\"\"Flagged: another process communicates, the piped one is only waited.\"\"\"
    proc = subprocess.Popen(["ls"], stdout=subprocess.PIPE)
    other = subprocess.Popen(["ls"])
    other.communicate(timeout=10)
    proc.wait(timeout=10)


def case_flagged_communicate_after_wait():
    \"\"\"Flagged: wait() runs before communicate() drains the pipe.\"\"\"
    proc = subprocess.Popen(["ls"], stderr=subprocess.PIPE)
    proc.wait(timeout=10)
    proc.communicate(timeout=10)


def case_ok_pipe_read_by_helper():
    \"\"\"OK: pipe read by helper before wait in finally.\"\"\"
    proc = subprocess.Popen(["ls"], stdout=subprocess.PIPE)
    try:
        read_helper_output(proc, 10.0)
    finally:
        proc.wait(timeout=5)
        proc.stdout.close()


def case_ok_pipe_read_directly():
    \"\"\"OK: pipe read directly before wait.\"\"\"
    proc = subprocess.Popen(["ls"], stdout=subprocess.PIPE)
    proc.stdout.read()
    proc.wait(timeout=10)


def case_ok_pipe_iterated():
    \"\"\"OK: pipe lines iterated before wait.\"\"\"
    proc = subprocess.Popen(["ls"], stdout=subprocess.PIPE)
    for line in proc.stdout:
        pass
    proc.wait(timeout=10)


def case_ok_wait_after_with_block():
    \"\"\"OK: wait called after with block ends.\"\"\"
    with Popen(["ls"], stdout=PIPE) as proc:
        pass
    proc.wait(timeout=10)


def case_ok_pipe_communicate():
    \"\"\"OK: stdout=PIPE then communicate().\"\"\"
    proc = subprocess.Popen(["ls"], stdout=subprocess.PIPE)
    proc.communicate(timeout=10)


def case_ok_devnull_wait():
    \"\"\"OK: stdout=DEVNULL then wait().\"\"\"
    proc = subprocess.Popen(["ls"], stdout=subprocess.DEVNULL)
    proc.wait(timeout=10)


def case_ok_no_pipe_wait():
    \"\"\"OK: no pipe keyword then wait().\"\"\"
    proc = subprocess.Popen(["ls"])
    proc.wait(timeout=10)


def case_ok_both_pipes_both_read():
    \"\"\"OK: both pipes read before wait().\"\"\"
    proc = subprocess.Popen(["ls"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    proc.stdout.read()
    proc.stderr.read()
    proc.wait(timeout=10)


def case_ok_both_pipes_helper_on_process():
    \"\"\"OK: both pipes, a read helper takes the process.\"\"\"
    proc = subprocess.Popen(["ls"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    read_helper_output(proc, 10.0)
    proc.wait(timeout=10)


def case_ok_both_pipes_both_iterated():
    \"\"\"OK: both pipes iterated before wait().\"\"\"
    proc = subprocess.Popen(["ls"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    for line in proc.stdout:
        pass
    for line in proc.stderr:
        pass
    proc.wait(timeout=10)


def case_ok_both_pipes_communicate_then_wait():
    \"\"\"OK: communicate() drains both pipes before wait().\"\"\"
    proc = subprocess.Popen(["ls"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, err = proc.communicate(timeout=10)
    proc.wait(timeout=10)


def case_ok_stderr_communicate_then_wait():
    \"\"\"OK: communicate() drains stderr before wait().\"\"\"
    proc = subprocess.Popen(["ls"], stderr=subprocess.PIPE)
    proc.communicate(timeout=10)
    proc.wait(timeout=10)


def case_ok_with_communicate_then_wait():
    \"\"\"OK: communicate() inside the with block before wait().\"\"\"
    with Popen(["ls"], stdout=PIPE) as proc:
        proc.communicate(timeout=10)
        proc.wait(timeout=10)
"""

SUBTEST_CLEANUP_FIXTURE = """# SPDX-License-Identifier: EUPL-1.2
\"\"\"Fixture functions for subtest-cleanup-outside-finally semgrep rule testing.\"\"\"

import shutil
from pathlib import Path

path = Path("tmp")
marker = Path("marker")


def case_flagged_unlink_after_assert(self):
    \"\"\"Flagged: assertion then path.unlink() in subTest body.\"\"\"
    with self.subTest("unlink"):
        self.assertEqual(1, 1)
        path.unlink()


def case_flagged_rmtree_after_assert(self):
    \"\"\"Flagged: assertion then shutil.rmtree(path) in subTest body.\"\"\"
    with self.subTest("rmtree"):
        self.assertTrue(True)
        shutil.rmtree(path)


def case_flagged_helper_assert_rmdir(self):
    \"\"\"Flagged: helper call self.assert_refused() then marker.rmdir().\"\"\"
    with self.subTest("rmdir"):
        self.assert_refused()
        marker.rmdir()


def case_flagged_conditional_cleanup_after_assert(self):
    \"\"\"Flagged: conditional cleanup after assertion in subTest.\"\"\"
    with self.subTest("conditional"):
        self.assert_refused()
        marker.rmdir() if marker.is_dir() else marker.unlink()


def case_ok_unlink_in_finally(self):
    \"\"\"OK: path.unlink() in finally block inside subTest.\"\"\"
    with self.subTest("unlink finally"):
        try:
            self.assertEqual(1, 1)
        finally:
            path.unlink()


def case_ok_rmtree_in_finally(self):
    \"\"\"OK: shutil.rmtree(path) in finally block inside subTest.\"\"\"
    with self.subTest("rmtree finally"):
        try:
            self.assertTrue(True)
        finally:
            shutil.rmtree(path)


def case_ok_rmdir_in_finally(self):
    \"\"\"OK: marker.rmdir() in finally block inside subTest.\"\"\"
    with self.subTest("rmdir finally"):
        try:
            self.assert_refused()
        finally:
            marker.rmdir()


def case_ok_conditional_cleanup_in_finally(self):
    \"\"\"OK: conditional cleanup in finally block inside subTest.\"\"\"
    with self.subTest("conditional finally"):
        try:
            self.assert_refused()
        finally:
            marker.rmdir() if marker.is_dir() else marker.unlink()


def case_ok_cleanup_before_assert(self):
    \"\"\"OK: cleanup before assertion (setup).\"\"\"
    with self.subTest("setup"):
        path.unlink()
        self.assertEqual(1, 1)


def case_ok_cleanup_after_with_block(self):
    \"\"\"OK: cleanup after with block ends.\"\"\"
    with self.subTest("after"):
        self.assertEqual(1, 1)
    path.unlink()
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


def _run_semgrep(
    fixture: Path,
    scratch: Path,
    config: Path | None = None,
    *,
    allow_errors: bool = False,
) -> dict:
    """Run semgrep scan on a fixture inside scratch with isolated TMPDIR; fail on a rule warning."""
    semgrep_bin = shutil.which("semgrep")
    if semgrep_bin is None:
        raise RuntimeError("semgrep executable not found on PATH")
    rules_path = ROOT / ".semgrep.yml" if config is None else config
    env = os.environ.copy()
    env["TMPDIR"] = str(scratch)
    env.setdefault("XDG_CONFIG_HOME", str(scratch))
    proc = subprocess.run(  # nosec B603 B607 - fixed semgrep argv, no shell, bounded timeout.
        [
            semgrep_bin,
            "scan",
            "--config",
            str(rules_path),
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
        raise RuntimeError(f"semgrep rule warnings for {rules_path.name}: {rule_warnings}")
    data: dict = json.loads(proc.stdout)
    if data.get("errors") and not allow_errors:
        raise RuntimeError(f"semgrep reported execution errors: {data['errors']}")
    scanned = data.get("paths", {}).get("scanned", [])
    if len(scanned) == 0:
        raise RuntimeError(f"Semgrep scanned 0 targets for fixture {fixture}")
    return data


def _expected_flagged(source: str) -> set[str]:
    """Derive expected function names starting with case_flagged_ from fixture source."""
    tree = ast.parse(source)
    expected: set[str] = set()
    limit = len(tree.body)
    for i in range(limit):
        node = tree.body[i]
        if isinstance(node, ast.FunctionDef) and node.name.startswith("case_flagged_"):
            expected.add(node.name)
    return expected


def _flagged_functions(
    results: list,
    source: str,
    rule_id: str = "oserror-swallowed-as-empty",
) -> set[str]:
    """Collect enclosing function names for matches of the specified semgrep rule."""
    tree = ast.parse(source)
    funcs = [n for n in tree.body if isinstance(n, ast.FunctionDef)]
    flagged: set[str] = set()
    limit = len(results)
    for i in range(limit):
        item = results[i]
        check_id = item.get("check_id", "")
        if check_id.endswith(rule_id):
            start_line = item["start"]["line"]
            enclosing = _find_enclosing_function(funcs, start_line)
            if enclosing is not None:
                flagged.add(enclosing)
    return flagged


def _flagged_in_fixture(source: str, rule_id: str) -> set[str]:
    """Scan fixture source in a temporary tests directory and return flagged function names."""
    with tempfile.TemporaryDirectory() as scratch_name:
        scratch_path = Path(scratch_name)
        tests_dir = scratch_path / "tests"
        tests_dir.mkdir(parents=True, exist_ok=True)
        fixture_file = tests_dir / "fixture.py"
        fixture_file.write_text(source, encoding="utf-8")
        data = _run_semgrep(fixture_file, scratch_path)
        return _flagged_functions(data.get("results", []), source, rule_id=rule_id)


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

    def test_popen_pipe_wait_rule(self) -> None:
        """Rule popen-pipe-wait flags pipe wait without communicate."""
        self.assertEqual(
            _flagged_in_fixture(POPEN_PIPE_WAIT_FIXTURE, "popen-pipe-wait"),
            _expected_flagged(POPEN_PIPE_WAIT_FIXTURE),
        )

    def test_subtest_cleanup_outside_finally_rule(self) -> None:
        """Rule subtest-cleanup-outside-finally flags cleanup outside finally block."""
        self.assertEqual(
            _flagged_in_fixture(SUBTEST_CLEANUP_FIXTURE, "subtest-cleanup-outside-finally"),
            _expected_flagged(SUBTEST_CLEANUP_FIXTURE),
        )


FFMPEG_C_DECLARATIONS = (
    "typedef struct SessionHandle { int ready; } SessionHandle;\n"
    "int ff_rtsp_connect(SessionHandle *handle);\n"
    "void ff_rtsp_close_streams(SessionHandle *handle);\n"
    "void ff_rtsp_close_connections(SessionHandle *handle);\n"
    "void ff_network_close(void);\n"
    "int start_recording_step(SessionHandle *handle);\n\n"
)

FFMPEG_PLANTED_WRITE_HEADER = (
    FFMPEG_C_DECLARATIONS
    + "static int mux_write_header(SessionHandle *handle)\n"
    + "{\n"
    + "    int status = ff_rtsp_connect(handle);\n"
    + "    if (status < 0)\n"
    + "        return status;\n"
    + "    status = start_recording_step(handle);\n"
    + "    if (status < 0) {\n"
    + "        ff_rtsp_close_streams(handle);\n"
    + "        ff_rtsp_close_connections(handle);\n"
    + "        return status;\n"
    + "    }\n"
    + "    return 0;\n"
    + "}\n"
)

FFMPEG_ALLOCATION_DECLARATIONS = """\
#define NULL ((void *)0)
typedef unsigned char uint8_t;
typedef unsigned short uint16_t;
void *av_malloc(unsigned long size);
void *av_mallocz(unsigned long size);
void *av_malloc_array(unsigned long count, unsigned long size);
enum { DST_RECT_BUF_SIZE = 128, DST_RECT_OFFSET = 16 };

"""

FFMPEG_PLANTED_ALLOCATION_TERNARY_DECLARATIONS = (
    FFMPEG_ALLOCATION_DECLARATIONS
    + """\
void fail(void);

static int reviewed(void)
{
    uint16_t *dst0_buf = av_malloc_array(DST_RECT_BUF_SIZE, sizeof(*dst0_buf));
    uint16_t *dst1_buf = av_malloc_array(DST_RECT_BUF_SIZE, sizeof(*dst1_buf));
    uint8_t *dst0 = dst0_buf ? (uint8_t *)(dst0_buf + DST_RECT_OFFSET) : NULL;
    uint8_t *dst1 = dst1_buf ? (uint8_t *)(dst1_buf + DST_RECT_OFFSET) : NULL;
    uint8_t *src0 = av_malloc(DST_RECT_BUF_SIZE);
    uint8_t *src1 = av_malloc(DST_RECT_BUF_SIZE);
    if (!dst0 || !dst1 || !src0 || !src1)
        fail();
    return 0;
}
"""
)

FFMPEG_PLANTED_ALLOCATION_TERNARY_ASSIGNMENT = (
    FFMPEG_ALLOCATION_DECLARATIONS
    + """\
static int assigned(void)
{
    uint8_t *buffer, *dst;
    buffer = av_mallocz(DST_RECT_BUF_SIZE);
    dst = buffer ? buffer + DST_RECT_OFFSET : NULL;
    if (!buffer)
        return -1;
    return 0;
}
"""
)

FFMPEG_CHECKED_ALLOCATION = (
    FFMPEG_ALLOCATION_DECLARATIONS
    + """\
static int accepted(void)
{
    uint8_t *dst0, *dst1;
    uint16_t *dst0_buf = av_malloc_array(DST_RECT_BUF_SIZE, sizeof(*dst0_buf));
    uint16_t *dst1_buf = av_malloc_array(DST_RECT_BUF_SIZE, sizeof(*dst1_buf));
    if (!dst0_buf || !dst1_buf)
        return -1;
    dst0 = (uint8_t *)(dst0_buf + DST_RECT_OFFSET);
    dst1 = (uint8_t *)(dst1_buf + DST_RECT_OFFSET);
    return 0;
}
"""
)

FFMPEG_PARAMETER_NULL_TERNARY = (
    FFMPEG_ALLOCATION_DECLARATIONS
    + """\
static void optional_buffer(uint8_t *buffer)
{
    uint8_t *dst = buffer ? buffer + DST_RECT_OFFSET : NULL;
    if (!buffer)
        return;
}
"""
)

FFMPEG_ALLOCATION_OTHER_ELSE = (
    FFMPEG_ALLOCATION_DECLARATIONS
    + """\
static void fallback_buffer(uint8_t *other)
{
    uint8_t *buffer = av_malloc(DST_RECT_BUF_SIZE);
    uint8_t *dst = buffer ? buffer + DST_RECT_OFFSET : other;
    if (!buffer)
        return;
}
"""
)

FFMPEG_ALLOCATION_SEPARATE_FUNCTION = (
    FFMPEG_ALLOCATION_DECLARATIONS
    + """\
static void allocate_buffer(void)
{
    uint8_t *buffer = av_mallocz(DST_RECT_BUF_SIZE);
}

static void optional_buffer(uint8_t *buffer)
{
    uint8_t *dst = buffer ? buffer + DST_RECT_OFFSET : NULL;
    if (!buffer)
        return;
}
"""
)

FFMPEG_UNCHECKED_ALLOCATION_TERNARY = (
    FFMPEG_ALLOCATION_DECLARATIONS
    + """\
void consume_buffer(uint8_t *dst);

static void unchecked(void)
{
    uint8_t *buffer = av_malloc(DST_RECT_BUF_SIZE);
    uint8_t *dst = buffer ? buffer + DST_RECT_OFFSET : NULL;
    consume_buffer(dst);
}
"""
)

FFMPEG_REASSIGNED_ALLOCATION_TERNARY = (
    FFMPEG_ALLOCATION_DECLARATIONS
    + """\
static int reassigned(uint8_t *other)
{
    uint8_t *buffer = av_malloc(DST_RECT_BUF_SIZE);
    buffer = other;
    uint8_t *dst = buffer ? buffer + DST_RECT_OFFSET : NULL;
    if (!buffer)
        return -1;
    return 0;
}
"""
)

FFMPEG_NULL_CHECKED_ALLOCATION_TERNARY = (
    FFMPEG_ALLOCATION_DECLARATIONS
    + """\
static int null_checked(void)
{
    uint8_t *buffer = av_malloc(DST_RECT_BUF_SIZE);
    if (buffer == NULL)
        return -1;
    uint8_t *dst = buffer ? buffer + DST_RECT_OFFSET : NULL;
    if (!buffer)
        return -1;
    return 0;
}
"""
)

FFMPEG_LATER_OR_CHECKED_ALLOCATION_TERNARY = (
    FFMPEG_ALLOCATION_DECLARATIONS
    + """\
static int later_checked(void)
{
    uint8_t *buffer = av_malloc(DST_RECT_BUF_SIZE);
    uint8_t *other = av_malloc(DST_RECT_BUF_SIZE);
    uint8_t *dst = buffer ? buffer + DST_RECT_OFFSET : NULL;
    if (!other || !buffer)
        return -1;
    return 0;
}
"""
)

FFMPEG_REALLOCATED_ALLOCATION_TERNARY = (
    FFMPEG_ALLOCATION_DECLARATIONS
    + """\
static int reallocated(void)
{
    uint8_t *buffer = av_malloc(DST_RECT_BUF_SIZE);
    if (!buffer)
        return -1;
    buffer = av_mallocz(DST_RECT_BUF_SIZE);
    uint8_t *dst = buffer ? buffer + DST_RECT_OFFSET : NULL;
    if (!buffer)
        return -1;
    return 0;
}
"""
)


FFMPEG_DESTINATION_WRITE_TERNARY = (
    FFMPEG_ALLOCATION_DECLARATIONS
    + """\
void consume_optional(uint8_t *dst);

static int destination_write(uint8_t *other)
{
    DECLARATIONS
    ALLOCATION;
    TERNARY;
    consume_optional(dst);
    dst = other;
    if (CHECK) {
        return -1;
    }
    return 0;
}
"""
)

FFMPEG_DESTINATION_WRITE_FORMS = (
    ("", "uint8_t *buffer", "uint8_t *dst"),
    ("uint8_t *dst;", "uint8_t *buffer", "dst"),
    ("uint8_t *buffer;", "buffer", "uint8_t *dst"),
    ("uint8_t *buffer, *dst;", "buffer", "dst"),
)


def _buffer_ternary_lines(source: str) -> list[int]:
    """Derive exact expected lines from buffer ternary statements in the fixture."""
    lines = source.splitlines()
    return [index + 1 for index in range(len(lines)) if " = buffer ? " in lines[index]]


def _earlier_nonnull_ternary_source(check: str) -> str:
    """Keep a later NULL check while replacing only the earlier check condition."""
    return FFMPEG_NULL_CHECKED_ALLOCATION_TERNARY.replace("if (buffer == NULL)", f"if ({check})", 1)


def _earlier_check_frame_source(check: str, frame: int, name: str) -> str:
    """Build a named allocation/ternary frame with earlier check statements."""
    declarations, allocation, destination = FFMPEG_DESTINATION_WRITE_FORMS[frame]
    return (
        FFMPEG_DESTINATION_WRITE_TERNARY.replace("destination_write", name)
        .replace("DECLARATIONS", declarations)
        .replace("ALLOCATION", f"{allocation} = av_malloc(128)")
        .replace("TERNARY", f"{destination} = buffer ? buffer + 16 : NULL")
        .replace(f"    {destination} = buffer ?", f"    {check}\n    {destination} = buffer ?", 1)
        .replace("CHECK", "!buffer")
    )


def _scan_ffmpeg_c(source: str) -> list[dict]:
    """Scan synthetic C source with .semgrep-ffmpeg.yml and return its result entries."""
    with tempfile.TemporaryDirectory() as scratch_name:
        scratch_path = Path(scratch_name)
        fixture_file = scratch_path / "fixture.c"
        fixture_file.write_text(source, encoding="utf-8")
        data = _run_semgrep(fixture_file, scratch_path, config=ROOT / ".semgrep-ffmpeg.yml")
        return list(data.get("results", []))


def _ternary_finding_lines(source: str) -> list[int]:
    """Return 1-based start lines for ffmpeg-null-ternary-after-allocation matches."""
    return [
        item["start"]["line"]
        for item in _scan_ffmpeg_c(source)
        if item["check_id"].split(".")[-1] == "ffmpeg-null-ternary-after-allocation"
    ]


class FfmpegRuleIdTest(unittest.TestCase):
    """upstream_lint keeps the check_id text after its last dot, so rule ids carry no dot."""

    def test_rule_ids_carry_no_dot(self) -> None:
        """Every id key in .semgrep-ffmpeg.yml exists and contains no dot."""
        text = upstream_lint.SEMGREP_FFMPEG_RULES.read_text(encoding="utf-8")
        ids = re.findall(r"^\s*(?:-\s*)?id:\s*(\S+)", text, re.MULTILINE)
        self.assertTrue(ids, "no id key read from .semgrep-ffmpeg.yml")
        self.assertEqual([rule_id for rule_id in ids if "." in rule_id], [])


@unittest.skipUnless(shutil.which("semgrep"), "semgrep not installed; make semgrep needs it")
class FfmpegSemgrepRulesTest(unittest.TestCase):
    """Verify .semgrep-ffmpeg.yml C rules against synthetic FFmpeg call fixtures."""

    def test_planted_allocation_ternary_declarations_flagged(self) -> None:
        """Flag both reviewed pointer declarations on their ternary lines."""
        results = _scan_ffmpeg_c(FFMPEG_PLANTED_ALLOCATION_TERNARY_DECLARATIONS)
        expected_lines = {
            index
            for index, line in enumerate(
                FFMPEG_PLANTED_ALLOCATION_TERNARY_DECLARATIONS.splitlines(), 1
            )
            if " ? " in line
        }
        self.assertEqual(len(results), 2)
        self.assertEqual({item["start"]["line"] for item in results}, expected_lines)
        self.assertEqual(
            {item["check_id"].split(".")[-1] for item in results},
            {"ffmpeg-null-ternary-after-allocation"},
        )

    def test_planted_allocation_ternary_assignment_flagged(self) -> None:
        """Flag a ternary assignment after an assigned av_mallocz result."""
        results = _scan_ffmpeg_c(FFMPEG_PLANTED_ALLOCATION_TERNARY_ASSIGNMENT)
        ternary_line = next(
            index
            for index, line in enumerate(
                FFMPEG_PLANTED_ALLOCATION_TERNARY_ASSIGNMENT.splitlines(), 1
            )
            if " ? " in line
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["start"]["line"], ternary_line)
        self.assertEqual(
            results[0]["check_id"].split(".")[-1], "ffmpeg-null-ternary-after-allocation"
        )

    def test_planted_allocation_ternary_early_check_reported(self) -> None:
        """Checked earlier: reported as the redundant reading."""
        source = FFMPEG_PLANTED_ALLOCATION_TERNARY_ASSIGNMENT.replace(
            "    dst = buffer ? buffer + DST_RECT_OFFSET : NULL;\n",
            "    if (!buffer) return -1;\n    dst = buffer ? buffer + DST_RECT_OFFSET : NULL;\n",
        )
        self.assertEqual(_ternary_finding_lines(source), _buffer_ternary_lines(source))

    def test_ternary_scope_use_before_check_reported_plain_shape(self) -> None:
        """Report use-before-check by plain shape because the rule does not follow flow."""
        source = (
            FFMPEG_ALLOCATION_DECLARATIONS
            + """\
static int use_before_check(uint8_t **out)
{
    uint8_t *buffer = av_malloc(DST_RECT_BUF_SIZE);
    uint8_t *dst = buffer ? buffer + DST_RECT_OFFSET : NULL;
    *out = dst;
    if (!buffer)
        return -1;
    return 0;
}
"""
        )
        self.assertEqual(_ternary_finding_lines(source), _buffer_ternary_lines(source))

    def test_ternary_scope_log_only_later_check_reported_plain_shape(self) -> None:
        """Report log-only-later-check by plain shape because the rule does not follow flow."""
        source = (
            FFMPEG_ALLOCATION_DECLARATIONS
            + """\
void log_failure(void);

static int log_only_later_check(void)
{
    uint8_t *buffer = av_malloc(DST_RECT_BUF_SIZE);
    uint8_t *dst = buffer ? buffer + DST_RECT_OFFSET : NULL;
    if (!buffer)
        log_failure();
    return 0;
}
"""
        )
        self.assertEqual(_ternary_finding_lines(source), _buffer_ternary_lines(source))

    def test_ternary_scope_stale_later_check_reported_plain_shape(self) -> None:
        """Report stale-later-check by plain shape because the rule does not follow flow."""
        source = (
            FFMPEG_ALLOCATION_DECLARATIONS
            + """\
static int stale_later_check(uint8_t *other)
{
    uint8_t *buffer = av_malloc(DST_RECT_BUF_SIZE);
    uint8_t *dst = buffer ? buffer + DST_RECT_OFFSET : NULL;
    if (!buffer)
        return -1;
    buffer = other;
    if (!buffer)
        return -1;
    return 0;
}
"""
        )
        self.assertEqual(_ternary_finding_lines(source), _buffer_ternary_lines(source))

    def test_ternary_scope_second_ternary_reported_plain_shape(self) -> None:
        """Report second-ternary by plain shape because the rule does not follow flow."""
        source = (
            FFMPEG_ALLOCATION_DECLARATIONS
            + """\
static int second_ternary(uint8_t *other)
{
    uint8_t *buffer = av_malloc(DST_RECT_BUF_SIZE);
    uint8_t *dst = buffer ? buffer + DST_RECT_OFFSET : NULL;
    dst = other ? other + DST_RECT_OFFSET : NULL;
    if (!dst)
        return -1;
    return 0;
}
"""
        )
        self.assertEqual(_ternary_finding_lines(source), _buffer_ternary_lines(source))

    def test_ternary_scope_same_buffer_two_ternaries_reported(self) -> None:
        """Report both same-buffer ternaries with distinct offsets before the later check."""
        source = (
            FFMPEG_ALLOCATION_DECLARATIONS
            + """\
static int same_buffer_two_ternaries(void)
{
    uint8_t *buffer = av_malloc(DST_RECT_BUF_SIZE);
    uint8_t *dst = buffer ? buffer + DST_RECT_OFFSET : NULL;
    dst = buffer ? buffer + 2 * DST_RECT_OFFSET : NULL;
    if (!dst)
        return -1;
    return 0;
}
"""
        )
        self.assertEqual(_ternary_finding_lines(source), _buffer_ternary_lines(source))

    def test_ternary_scope_conditional_early_check(self) -> None:
        """Checked earlier: reported as the redundant reading."""
        source = (
            FFMPEG_ALLOCATION_DECLARATIONS
            + """\
static int conditional_early_check(int required)
{
    uint8_t *buffer = av_malloc(DST_RECT_BUF_SIZE);
    if (required) {
        if (!buffer)
            return -1;
    }
    uint8_t *dst = buffer ? buffer + DST_RECT_OFFSET : NULL;
    if (!buffer)
        return -1;
    return 0;
}
"""
        )
        self.assertEqual(_ternary_finding_lines(source), _buffer_ternary_lines(source))

    def test_ternary_scope_earlier_allocation_check(self) -> None:
        """Report an earlier allocation check without tracking which allocation it guards."""
        source = (
            FFMPEG_ALLOCATION_DECLARATIONS
            + """\
void av_free(void *ptr);

static int earlier_allocation_check(void)
{
    uint8_t *buffer = av_malloc(DST_RECT_BUF_SIZE);
    if (!buffer)
        return -1;
    av_free(buffer);
    buffer = av_mallocz(DST_RECT_BUF_SIZE);
    uint8_t *dst = buffer ? buffer + DST_RECT_OFFSET : NULL;
    if (!buffer)
        return -1;
    return 0;
}
"""
        )
        self.assertEqual(_ternary_finding_lines(source), _buffer_ternary_lines(source))

    def test_ternary_scope_goto_into_ternary(self) -> None:
        """Report an earlier check with a goto without interpreting control flow."""
        source = (
            FFMPEG_ALLOCATION_DECLARATIONS
            + """\
static int goto_into_ternary(void)
{
    uint8_t *buffer = av_malloc(DST_RECT_BUF_SIZE);
    if (!buffer)
        goto reach_ternary;
reach_ternary:
    ;
    uint8_t *dst = buffer ? buffer + DST_RECT_OFFSET : NULL;
    if (!buffer)
        return -1;
    return 0;
}
"""
        )
        self.assertEqual(_ternary_finding_lines(source), _buffer_ternary_lines(source))

    def test_ternary_scope_unbraced_inner_loop_break(self) -> None:
        """Report an earlier check with an inner-loop break without interpreting flow."""
        source = (
            FFMPEG_ALLOCATION_DECLARATIONS
            + """\
static int unbraced_inner_loop_break(void)
{
    for (int outer = 0; outer < 2; outer++) {
        uint8_t *buffer = av_malloc(DST_RECT_BUF_SIZE);
        if (!buffer)
            for (int inner = 0; inner < 1; inner++)
                break;
        uint8_t *dst = buffer ? buffer + DST_RECT_OFFSET : NULL;
        if (!buffer)
            return -1;
    }
    return 0;
}
"""
        )
        self.assertEqual(_ternary_finding_lines(source), _buffer_ternary_lines(source))

    def test_checked_allocation_without_ternary_passes(self) -> None:
        """Accept pointer assignments after an allocation failure check."""
        self.assertEqual(_scan_ffmpeg_c(FFMPEG_CHECKED_ALLOCATION), [])

    def test_parameter_null_ternary_passes(self) -> None:
        """Accept a parameter ternary with a later check; adding an allocation reports it."""
        source = FFMPEG_PARAMETER_NULL_TERNARY
        self.assertEqual(_ternary_finding_lines(source), [])
        control = source.replace(
            "    uint8_t *dst =",
            "    buffer = av_malloc(DST_RECT_BUF_SIZE);\n    uint8_t *dst =",
        )
        self.assertEqual(_ternary_finding_lines(control), _buffer_ternary_lines(control))

    def test_allocation_ternary_with_other_else_passes(self) -> None:
        """Accept an allocation ternary whose fallback is another pointer."""
        self.assertEqual(_scan_ffmpeg_c(FFMPEG_ALLOCATION_OTHER_ELSE), [])

    def test_checked_allocation_ternary_with_null_else_flagged(self) -> None:
        """Flag the checked non-NULL fallback fixture when only its fallback becomes NULL."""
        source = FFMPEG_ALLOCATION_OTHER_ELSE.replace(" : other;", " : NULL;")
        self.assertEqual(len(_scan_ffmpeg_c(source)), 1)

    def test_replaced_buffer_before_later_check_reported_plain_shape(self) -> None:
        """Report replaced buffer before later check by plain shape without flow."""
        checks = ("!buffer", "buffer == NULL", "!other || !buffer", "!other || buffer == NULL")
        for declarations, allocation, destination in FFMPEG_DESTINATION_WRITE_FORMS:
            for check in checks:
                with self.subTest(allocation=allocation, destination=destination, check=check):
                    source = (
                        FFMPEG_DESTINATION_WRITE_TERNARY.replace(
                            "ALLOCATION", f"{allocation} = av_malloc(128)"
                        )
                        .replace("DECLARATIONS", declarations)
                        .replace("TERNARY", f"{destination} = buffer ? buffer + 16 : NULL")
                        .replace("dst = other;", "buffer = other;")
                        .replace("CHECK", check)
                    )
                    ternary_line = next(
                        index for index, line in enumerate(source.splitlines(), 1) if " ? " in line
                    )
                    self.assertEqual(_ternary_finding_lines(source), [ternary_line])

    def test_original_buffer_later_check_flagged(self) -> None:
        """Keep later buffer checks when no assignment replaces the allocation result."""
        for declarations, allocation, destination in FFMPEG_DESTINATION_WRITE_FORMS:
            with self.subTest(allocation=allocation, destination=destination):
                source = (
                    FFMPEG_DESTINATION_WRITE_TERNARY.replace(
                        "ALLOCATION", f"{allocation} = av_malloc(128)"
                    )
                    .replace("DECLARATIONS", declarations)
                    .replace("TERNARY", f"{destination} = buffer ? buffer + 16 : NULL")
                    .replace("    dst = other;\n", "")
                    .replace("CHECK", "!buffer")
                )
                self.assertEqual(len(_scan_ffmpeg_c(source)), 1)

    def test_fallthrough_buffer_check_before_ternary_reported(self) -> None:
        """Checked earlier: reported as the redundant reading."""
        bodies = ("log_failure();", "if (DST_RECT_BUF_SIZE == 0) return -1;")
        for body_index in range(len(bodies)):
            body = bodies[body_index]
            with self.subTest(body=body):
                source = FFMPEG_NULL_CHECKED_ALLOCATION_TERNARY.replace(
                    "if (buffer == NULL)\n        return -1;",
                    f"if (buffer == NULL) {{ {body} }}",
                )
                self.assertEqual(_ternary_finding_lines(source), _buffer_ternary_lines(source))

    def test_exiting_buffer_checks_before_ternary_reported(self) -> None:
        """Checked earlier: reported as the redundant reading."""
        exit_kinds = ("return", "goto", "break", "continue")
        for exit_index in range(len(exit_kinds)):
            exit_kind = exit_kinds[exit_index]
            with self.subTest(exit_kind=exit_kind):
                source = FFMPEG_NULL_CHECKED_ALLOCATION_TERNARY
                if exit_kind == "goto":
                    source = source.replace("return -1;", "goto failed;", 1).replace(
                        "    return 0;", "    return 0;\nfailed:\n    return -1;"
                    )
                elif exit_kind in ("break", "continue"):
                    source = (
                        source.replace("{\n", "{\n    for (int i = 0; i < 2; i++) {\n", 1)
                        .replace("return -1;", f"{exit_kind};", 1)
                        .replace("    return 0;", "    }\n    return 0;")
                    )
                self.assertEqual(_ternary_finding_lines(source), _buffer_ternary_lines(source))

    def test_multiline_ternary_span_covers_fallback(self) -> None:
        """Report only the three-line ternary expression, including its NULL fallback."""
        source = FFMPEG_LATER_OR_CHECKED_ALLOCATION_TERNARY.replace(
            "buffer ? buffer + DST_RECT_OFFSET : NULL",
            "buffer\n        ? buffer + DST_RECT_OFFSET\n        : NULL",
        )
        results = _scan_ffmpeg_c(source)
        lines = source.splitlines()
        start = next(index for index, line in enumerate(lines, 1) if "*dst =" in line)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["start"]["line"], start)
        self.assertEqual(results[0]["end"]["line"], start + 2)
        self.assertEqual(results[0]["start"]["col"], lines[start - 1].index("= buffer") + 3)
        self.assertEqual(results[0]["end"]["col"], lines[start + 1].index(";") + 1)
        wrapped = FFMPEG_LATER_OR_CHECKED_ALLOCATION_TERNARY.replace(
            "buffer ? buffer + DST_RECT_OFFSET : NULL",
            "choose(buffer ? buffer + DST_RECT_OFFSET : NULL)",
        )
        self.assertEqual(_scan_ffmpeg_c(wrapped), [])

    def test_allocation_in_separate_function_passes(self) -> None:
        """Keep bindings local despite a later check; removing the function boundary reports."""
        source = FFMPEG_ALLOCATION_SEPARATE_FUNCTION
        self.assertEqual(_ternary_finding_lines(source), [])
        control = source.replace("}\n\nstatic void optional_buffer(uint8_t *buffer)\n{\n", "")
        self.assertEqual(_ternary_finding_lines(control), _buffer_ternary_lines(control))

    def test_unchecked_allocation_ternary_passes(self) -> None:
        """Accept a ternary propagating allocation failure without a separate check."""
        self.assertEqual(_scan_ffmpeg_c(FFMPEG_UNCHECKED_ALLOCATION_TERNARY), [])

    def test_reassigned_allocation_ternary_reported_plain_shape(self) -> None:
        """Report reassignment before the ternary by plain shape, as without reassignment."""
        source = FFMPEG_REASSIGNED_ALLOCATION_TERNARY
        self.assertEqual(_ternary_finding_lines(source), _buffer_ternary_lines(source))
        control = source.replace("    buffer = other;\n", "")
        self.assertEqual(_ternary_finding_lines(control), _buffer_ternary_lines(control))

    def test_unrelated_earlier_conditions_keep_ternary_finding(self) -> None:
        """Report ternaries when an earlier condition does not directly test the buffer."""
        checks = (
            "flag",
            "ready()",
            "flag > 0",
            "record_attempt(buffer)",
            "buffer->field",
            "buffer[0]",
            "flag && record_attempt(buffer)",
            "buffer + 1",
            "buffer > other",
            "!flag",
            "flag != NULL",
        )
        for index in range(len(checks)):
            with self.subTest(check=checks[index]):
                source = _earlier_nonnull_ternary_source(checks[index])
                self.assertEqual(_ternary_finding_lines(source), _buffer_ternary_lines(source))

    def test_earlier_assert_macro_reported_plain_shape(self) -> None:
        """Report a macro assertion before the ternary because macros are not earlier ifs."""
        source = FFMPEG_NULL_CHECKED_ALLOCATION_TERNARY.replace(
            "static int null_checked(void)",
            (
                "#define av_assert0(condition) do { if (!(condition)) return -1; } while (0)\n"
                "static int null_checked(void)"
            ),
        ).replace("    if (buffer == NULL)\n        return -1;", "    av_assert0(buffer);", 1)
        self.assertEqual(_ternary_finding_lines(source), _buffer_ternary_lines(source))

    def test_null_checked_allocation_ternary_reported(self) -> None:
        """Checked earlier: reported as the redundant reading."""
        source = FFMPEG_NULL_CHECKED_ALLOCATION_TERNARY
        self.assertEqual(_ternary_finding_lines(source), _buffer_ternary_lines(source))

    def test_allocation_ternary_without_earlier_check_flagged(self) -> None:
        """Report the shared fixture ternary when only its earlier NULL check is removed."""
        source = FFMPEG_NULL_CHECKED_ALLOCATION_TERNARY.replace(
            "    if (buffer == NULL)\n        return -1;\n", "", 1
        )
        ternary_line = next(
            index for index, line in enumerate(source.splitlines(), 1) if " ? " in line
        )
        self.assertEqual(_ternary_finding_lines(source), [ternary_line])

    def test_later_or_checked_allocation_ternary_flagged(self) -> None:
        """Flag the ternary when a later OR condition checks the same buffer."""
        source = FFMPEG_LATER_OR_CHECKED_ALLOCATION_TERNARY
        results = _scan_ffmpeg_c(source)
        ternary_line = next(
            index for index, line in enumerate(source.splitlines(), 1) if " ? " in line
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["start"]["line"], ternary_line)
        self.assertEqual(
            results[0]["check_id"].split(".")[-1], "ffmpeg-null-ternary-after-allocation"
        )

    def test_later_derived_pointer_check_without_braces_flagged(self) -> None:
        """Flag a ternary whose derived pointer is checked later without braces."""
        source = (
            FFMPEG_ALLOCATION_DECLARATIONS
            + """\
static int derived_checked(void)
{
    uint8_t *buffer = av_malloc(DST_RECT_BUF_SIZE);
    uint8_t *dst = buffer ? buffer + DST_RECT_OFFSET : NULL;
    if (!dst)
        return -1;
    return 0;
}
"""
        )
        results = _scan_ffmpeg_c(source)
        ternary_line = next(
            index for index, line in enumerate(source.splitlines(), 1) if " ? " in line
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["start"]["line"], ternary_line)
        self.assertEqual(
            results[0]["check_id"].split(".")[-1], "ffmpeg-null-ternary-after-allocation"
        )

    def test_overwritten_destination_then_destination_check_reported_plain_shape(self) -> None:
        """Report overwritten destination then destination check by plain shape."""
        checks = ("!dst", "dst == NULL", "!other || !dst", "!other || dst == NULL")
        for declarations, allocation, destination in FFMPEG_DESTINATION_WRITE_FORMS:
            for check in checks:
                with self.subTest(allocation=allocation, destination=destination, check=check):
                    source = (
                        FFMPEG_DESTINATION_WRITE_TERNARY.replace(
                            "ALLOCATION", f"{allocation} = av_malloc(128)"
                        )
                        .replace("DECLARATIONS", declarations)
                        .replace("TERNARY", f"{destination} = buffer ? buffer + 16 : NULL")
                        .replace("CHECK", check)
                    )
                    ternary_line = next(
                        index for index, line in enumerate(source.splitlines(), 1) if " ? " in line
                    )
                    self.assertEqual(_ternary_finding_lines(source), [ternary_line])

    def test_overwritten_destination_with_later_or_earlier_buffer_check(self) -> None:
        """Report all four frames both with and without an earlier buffer check."""
        failure_check = "if (!buffer) {\n        return -1;\n    }"
        for frame in range(len(FFMPEG_DESTINATION_WRITE_FORMS)):
            declarations, allocation, destination = FFMPEG_DESTINATION_WRITE_FORMS[frame]
            for earlier in range(2):
                with self.subTest(allocation=allocation, destination=destination, earlier=earlier):
                    source = (
                        FFMPEG_DESTINATION_WRITE_TERNARY.replace(
                            "ALLOCATION", f"{allocation} = av_malloc(128)"
                        )
                        .replace("DECLARATIONS", declarations)
                        .replace("TERNARY", f"{destination} = buffer ? buffer + 16 : NULL")
                        .replace("CHECK", "!buffer")
                    )
                    if earlier:
                        source = source.replace(
                            f"    {destination} = buffer ?",
                            "    " + failure_check + f"\n    {destination} = buffer ?",
                            1,
                        )
                    ternary_line = next(
                        index for index, line in enumerate(source.splitlines(), 1) if " ? " in line
                    )
                    expected = [ternary_line]
                    self.assertEqual(_ternary_finding_lines(source), expected)

    def test_destination_assigned_after_check_flagged(self) -> None:
        """Keep the ternary finding when its destination is assigned only after the check."""
        for declarations, allocation, destination in FFMPEG_DESTINATION_WRITE_FORMS:
            with self.subTest(allocation=allocation, destination=destination):
                source = (
                    FFMPEG_DESTINATION_WRITE_TERNARY.replace(
                        "ALLOCATION", f"{allocation} = av_malloc(128)"
                    )
                    .replace("DECLARATIONS", declarations)
                    .replace("TERNARY", f"{destination} = buffer ? buffer + 16 : NULL")
                    .replace("CHECK", "!dst")
                    .replace("    dst = other;\n", "")
                    .replace("    return 0;", "    dst = other;\n    return 0;")
                )
                results = _scan_ffmpeg_c(source)
                ternary_line = next(
                    index for index, line in enumerate(source.splitlines(), 1) if " ? " in line
                )
                self.assertEqual(len(results), 1)
                self.assertEqual(results[0]["start"]["line"], ternary_line)

    def test_later_buffer_check_without_braces_flagged(self) -> None:
        """Flag a ternary whose buffer is checked later without braces."""
        source = FFMPEG_PLANTED_ALLOCATION_TERNARY_ASSIGNMENT.replace(
            "buffer = av_mallocz(DST_RECT_BUF_SIZE);",
            "buffer = av_malloc(DST_RECT_BUF_SIZE);",
        )
        results = _scan_ffmpeg_c(source)
        ternary_line = next(
            index for index, line in enumerate(source.splitlines(), 1) if " ? " in line
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["start"]["line"], ternary_line)
        self.assertEqual(
            results[0]["check_id"].split(".")[-1], "ffmpeg-null-ternary-after-allocation"
        )

    def test_derived_pointer_checked_before_ternary_flagged(self) -> None:
        """Report earlier destination and buffer checks without excluding either ternary."""
        source = (
            FFMPEG_ALLOCATION_DECLARATIONS
            + """\
static int earlier_derived_check(uint8_t *dst)
{
    uint8_t *buffer = av_malloc(DST_RECT_BUF_SIZE);
    if (!dst)
        return -1;
    dst = buffer ? buffer + DST_RECT_OFFSET : NULL;
    if (!buffer)
        return -1;
    return 0;
}
"""
        )
        self.assertEqual(_ternary_finding_lines(source), _buffer_ternary_lines(source))
        control = source.replace("if (!dst)", "if (!buffer)", 1)
        self.assertEqual(_ternary_finding_lines(control), _buffer_ternary_lines(control))

    def test_braced_derived_null_equality_flagged(self) -> None:
        """Flag a later braced NULL equality checking only the derived pointer."""
        source = FFMPEG_PLANTED_ALLOCATION_TERNARY_ASSIGNMENT.replace(
            "if (!buffer)\n        return -1;",
            "if (dst == NULL) {\n        return -1;\n    }",
        )
        results = _scan_ffmpeg_c(source)
        ternary_line = next(
            index for index, line in enumerate(source.splitlines(), 1) if " ? " in line
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["start"]["line"], ternary_line)
        self.assertEqual(
            results[0]["check_id"].split(".")[-1], "ffmpeg-null-ternary-after-allocation"
        )

    def test_earlier_or_buffer_null_equality_reported(self) -> None:
        """Checked earlier: reported as the redundant reading."""
        source = FFMPEG_NULL_CHECKED_ALLOCATION_TERNARY.replace(
            "if (buffer == NULL)\n        return -1;",
            "if (buffer == NULL || DST_RECT_BUF_SIZE == 0) {\n        return -1;\n    }",
        )
        self.assertEqual(_ternary_finding_lines(source), _buffer_ternary_lines(source))

    def test_reallocated_allocation_ternary_reported(self) -> None:
        """Report reallocation after an earlier check without tracking allocation identities."""
        source = FFMPEG_REALLOCATED_ALLOCATION_TERNARY
        self.assertEqual(_ternary_finding_lines(source), _buffer_ternary_lines(source))

    def test_earlier_check_spellings_reported(self) -> None:
        """Checked earlier: reported as the redundant reading in every frame and context."""
        checks = (
            "buffer",
            "!buffer",
            "buffer == NULL",
            "NULL == buffer",
            "buffer != NULL",
            "NULL != buffer",
            "(unsigned char)buffer",
            "!(unsigned char)buffer",
            "(void *)buffer",
            "!(void *)buffer",
            "(uint8_t *)buffer",
            "(void *)buffer == NULL",
            "buffer == (void *)0",
            "buffer != (void *)0",
            "buffer == (0)",
        )
        contexts = ("{}", "flag && {} && ready()", "flag || {} || ready()")
        sources = []
        for frame in range(4):
            for index in range(len(checks)):
                for context in range(len(contexts)):
                    condition = contexts[context].format(checks[index])
                    sources.append(
                        _earlier_check_frame_source(
                            f"if ({condition}) return -1;",
                            frame,
                            f"spelling_{frame}_{index}_{context}",
                        )
                    )
        source = "\n".join(sources)
        expected = _buffer_ternary_lines(source)
        self.assertEqual(len(expected), 4 * len(checks) * len(contexts))
        self.assertEqual(_ternary_finding_lines(source), expected)

    def test_review_d1_double_negation_and_zero_checks_reported(self) -> None:
        """D1: checked earlier via double negation or zero comparisons reports every frame."""
        checks = ("!!buffer", "buffer == 0", "0 == buffer", "buffer != 0", "0 != buffer")
        contexts = ("{}", "flag && {} && ready()", "flag || {} || ready()")
        sources = []
        for frame in range(4):
            for index in range(len(checks)):
                for context in range(len(contexts)):
                    condition = contexts[context].format(checks[index])
                    sources.append(
                        _earlier_check_frame_source(
                            f"if ({condition}) return -1;", frame, f"d1_{frame}_{index}_{context}"
                        )
                    )
        source = "\n".join(sources)
        expected = _buffer_ternary_lines(source)
        self.assertEqual(len(expected), 60)
        self.assertEqual(_ternary_finding_lines(source), expected)

    def test_review_d2_separate_cast_and_plain_checks_reported(self) -> None:
        """D2: checked earlier via separate cast and plain checks reports every frame."""
        cast = "if ((void *)buffer) return -1;"
        plain = "if (buffer) return -1;"
        checks = (
            f"{cast}\n    {plain}",
            f"{plain}\n    {cast}",
            "if (buffer && (unsigned char)buffer) return -1;",
            "if (buffer || (unsigned char)buffer) return -1;",
        )
        sources = []
        for frame in range(4):
            for index in range(len(checks)):
                sources.append(
                    _earlier_check_frame_source(checks[index], frame, f"d2_{frame}_{index}")
                )
        source = "\n".join(sources)
        expected = _buffer_ternary_lines(source)
        self.assertEqual(len(expected), 16)
        self.assertEqual(_ternary_finding_lines(source), expected)

    def test_redundant_ternary_reasoned_suppression(self) -> None:
        """Suppress a checked-earlier ternary with an adjacent rule ID and reason."""
        source = FFMPEG_NULL_CHECKED_ALLOCATION_TERNARY.replace(
            "    uint8_t *dst = buffer ?",
            (
                "    // semgrep-reason (expires 2026-12-19): "
                "preserve the optional-buffer interface\n"
                "    // nosemgrep: ffmpeg-null-ternary-after-allocation\n"
                "    uint8_t *dst = buffer ?"
            ),
        )
        self.assertEqual(_ternary_finding_lines(source), [])
        control = source.replace("    // nosemgrep: ffmpeg-null-ternary-after-allocation\n", "")
        self.assertEqual(_ternary_finding_lines(control), _buffer_ternary_lines(control))

    def test_other_buffer_and_nonallocator_boundaries(self) -> None:
        """Keep other-variable and non-allocator fixtures quiet beside a matching control."""
        control = FFMPEG_PLANTED_ALLOCATION_TERNARY_ASSIGNMENT
        boundaries = (
            control.replace("buffer = av_mallocz", "other = av_mallocz"),
            control.replace("av_mallocz(DST_RECT_BUF_SIZE)", "lookup_buffer(DST_RECT_BUF_SIZE)"),
        )
        for index in range(len(boundaries)):
            with self.subTest(boundary=index):
                self.assertEqual(_ternary_finding_lines(boundaries[index]), [])
        self.assertEqual(_ternary_finding_lines(control), _buffer_ternary_lines(control))

    def test_alias_and_struct_member_checks_out_of_scope(self) -> None:
        """Require a plain later buffer check rather than following aliases or members."""
        control = FFMPEG_PLANTED_ALLOCATION_TERNARY_ASSIGNMENT
        aliases = (
            ("uint8_t *alias = buffer;", "!alias"),
            ("struct { uint8_t *buffer; } state; state.buffer = buffer;", "!state.buffer"),
        )
        for index in range(len(aliases)):
            setup, check = aliases[index]
            with self.subTest(check=check):
                source = control.replace("if (!buffer)", f"{setup}\n    if ({check})")
                self.assertEqual(_ternary_finding_lines(source), [])
        self.assertEqual(_ternary_finding_lines(control), _buffer_ternary_lines(control))

    def test_planted_write_header_missing_network_close_flagged(self) -> None:
        """Planted connect-then-close failure without ff_network_close flags the close line."""
        results = _scan_ffmpeg_c(FFMPEG_PLANTED_WRITE_HEADER)
        close_line = next(
            index
            for index, line in enumerate(FFMPEG_PLANTED_WRITE_HEADER.splitlines(), 1)
            if "ff_rtsp_close_connections(handle);" in line
        )
        self.assertEqual(len(results), 1)
        self.assertTrue(
            results[0]["check_id"].endswith("ffmpeg-rtsp-connect-failure-without-network-close")
        )
        self.assertEqual(results[0]["start"]["line"], close_line)

    def test_write_header_with_network_close_passes(self) -> None:
        """Calling ff_network_close between ff_rtsp_close_connections and return passes."""
        fixed = FFMPEG_PLANTED_WRITE_HEADER.replace(
            "        ff_rtsp_close_connections(handle);\n        return status;\n",
            "        ff_rtsp_close_connections(handle);\n"
            "        ff_network_close();\n"
            "        return status;\n",
        )
        self.assertEqual(_scan_ffmpeg_c(fixed), [])

    def test_close_function_with_network_close_passes(self) -> None:
        """A close function calling ff_rtsp_close_connections then ff_network_close passes."""
        source = (
            FFMPEG_C_DECLARATIONS
            + "static int mux_close(SessionHandle *handle)\n"
            + "{\n"
            + "    ff_rtsp_close_streams(handle);\n"
            + "    ff_rtsp_close_connections(handle);\n"
            + "    ff_network_close();\n"
            + "    return 0;\n"
            + "}\n"
        )
        self.assertEqual(_scan_ffmpeg_c(source), [])

    def test_close_connections_without_rtsp_connect_passes(self) -> None:
        """A function without ff_rtsp_connect that closes connections and returns passes."""
        source = (
            FFMPEG_C_DECLARATIONS
            + "static int drop_connections_only(SessionHandle *handle)\n"
            + "{\n"
            + "    ff_rtsp_close_connections(handle);\n"
            + "    return -1;\n"
            + "}\n"
        )
        self.assertEqual(_scan_ffmpeg_c(source), [])

    def test_partial_parsing_c_source_still_matches_and_parses(self) -> None:
        """Real Semgrep PartialParsing JSON and findings pass upstream_lint's allow-list."""
        partial_fn = "void unparsed_fn(void)\n{\n    int broken = ;\n}\n\n"
        source = partial_fn + FFMPEG_PLANTED_WRITE_HEADER
        with tempfile.TemporaryDirectory() as scratch_name:
            scratch_path = Path(scratch_name)
            fixture_file = scratch_path / "partial.c"
            fixture_file.write_text(source, encoding="utf-8")
            data = _run_semgrep(
                fixture_file,
                scratch_path,
                config=ROOT / ".semgrep-ffmpeg.yml",
                allow_errors=True,
            )
            json_out = scratch_path / "out.json"
            json_out.write_text(json.dumps(data), encoding="utf-8")
            parsed = upstream_lint._read_semgrep_json(json_out)
        self.assertTrue(data.get("errors"), data)
        self.assertIsNotNone(parsed, data.get("errors"))
        matches, partial_paths = parsed or ([], [])
        self.assertEqual(len(matches), 1)
        self.assertEqual(len(partial_paths), len(data["errors"]))
