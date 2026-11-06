# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Check that FFmpeg configure receives only approved environment names."""

from __future__ import annotations

import contextlib
import io
import os
import signal
import subprocess  # nosec B404 - temporary shell fixture, fixed argv and deadline.
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from scripts import ffmpeg_configure

PLANT = "plant-value-123"
CONTROL_ENV = {
    "PATH": "/usr/bin:/bin",
    "HOME": "/nonexistent",
    "CC": "cc",
    "CPPFLAGS": "-DPLANTED_CPPFLAG",
    "DUMMY_PLANT_TOKEN": PLANT,
    "DUMMY_OTHER": "other-value",
}


def process_exists(pid: int) -> bool:
    """Return whether a process still accepts a signal probe."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def kill_process_if_running(pid: int) -> None:
    """Kill a fixture process if it survived a failed assertion."""
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGKILL)


def process_exits_within(pid: int, attempts: int = 20) -> bool:
    """Poll for process exit with a bounded 0.1-second interval."""
    for _ in range(attempts):
        if not process_exists(pid):
            return True
        time.sleep(0.1)
    return not process_exists(pid)


def set_dump_value(log: str, name: str) -> str | None:
    """Return a named value from bash or dash set output."""
    prefix = f"{name}="
    for line in log.splitlines():
        if not line.startswith(prefix):
            continue
        value = line[len(prefix) :]
        if len(value) >= 2 and value.startswith("'") and value.endswith("'"):
            return value[1:-1].replace("'\\''", "'")
        return value
    return None


class FFmpegConfigureTest(unittest.TestCase):
    """Exercise environment selection and a real shell configure fixture."""

    def setUp(self) -> None:
        """Create an executable configure fixture in a temporary source tree."""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.build = self.root / "build"
        script = self.source / "configure"
        script.write_text("#!/bin/sh\nset > config.log\nexit 0\n", encoding="utf-8")
        script.chmod(0o755)

    def run_wrapper(self, *options: str) -> int:
        """Invoke the wrapper with the fixture and controlled planted names."""
        with (
            patch.dict(os.environ, CONTROL_ENV),
            patch.object(
                ffmpeg_configure,
                "os",
                SimpleNamespace(environ=CONTROL_ENV, killpg=os.killpg, name=os.name),
            ),
        ):
            return ffmpeg_configure.main(
                [*options, str(self.source), str(self.build), "--", "--disable-everything"]
            )

    def test_allowed_environment_filters_unlisted_names(self) -> None:
        """Keep approved build variables and drop every unlisted input."""
        self.assertEqual(
            ffmpeg_configure.allowed_environment(CONTROL_ENV, []),
            {"PATH": "/usr/bin:/bin", "HOME": "/nonexistent", "CC": "cc"},
        )

    def test_planted_secret_absent_from_config_log(self) -> None:
        """Prove the configure set dump excludes planted and unlisted names."""
        self.assertEqual(self.run_wrapper(), 0)
        log = (self.build / "config.log").read_text(encoding="utf-8")
        self.assertEqual(set_dump_value(log, "PATH"), "/usr/bin:/bin")
        self.assertNotIn("DUMMY_PLANT_TOKEN", log)
        self.assertNotIn(PLANT, log)
        self.assertNotIn("DUMMY_OTHER", log)

    def test_direct_configure_exposes_planted_value(self) -> None:
        """Prove the shell fixture detects a leak with its full input map."""
        self.build.mkdir()
        result = subprocess.run(  # nosec B603 - temporary fixture, fixed argv and env.
            [str(self.source / "configure")],
            cwd=self.build,
            env=CONTROL_ENV,
            timeout=5,
            check=False,
            stdin=subprocess.DEVNULL,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn(PLANT, (self.build / "config.log").read_text(encoding="utf-8"))

    def test_keep_passes_listed_name(self) -> None:
        """Pass an explicitly kept configure input to configure."""
        self.assertEqual(self.run_wrapper("--keep", "CPPFLAGS"), 0)
        log = (self.build / "config.log").read_text(encoding="utf-8")
        self.assertEqual(set_dump_value(log, "CPPFLAGS"), "-DPLANTED_CPPFLAG")

    def test_set_dump_value_reads_bash_and_dash(self) -> None:
        """Read equivalent bash and dash set dumps, including quoted values."""
        bash_log = "CPPFLAGS=-DX\nPATH=/usr/bin\n"
        dash_log = "CPPFLAGS='-DX'\nPATH='/usr/bin'\n"
        dash_quote_log = "V='a'\\''b'\n"
        for log in (bash_log, dash_log):
            with self.subTest(log=log):
                self.assertEqual(set_dump_value(log, "CPPFLAGS"), "-DX")
                self.assertEqual(set_dump_value(log, "PATH"), "/usr/bin")
        self.assertEqual(set_dump_value(dash_quote_log, "V"), "a'b")
        self.assertIsNone(set_dump_value(bash_log, "ABSENT"))

    def test_keep_refuses_unlisted_names(self) -> None:
        """Reject every unlisted keep name before starting configure."""
        names = (
            "LD_PRELOAD",
            "BASH_ENV",
            "PYTHONPATH",
            "DOCKER_AUTH_CONFIG",
            "SSH_AUTH_SOCK",
            "MY_API_KEY",
            "DUMMY_OTHER",
        )
        for name in names:
            with self.subTest(name=name):
                error = io.StringIO()
                with contextlib.redirect_stderr(error):
                    self.assertEqual(self.run_wrapper("--keep", name), 2)
                self.assertIn("--keep accepts only:", error.getvalue())
                self.assertFalse((self.build / "config.log").exists())

    def test_keep_rejects_too_many_options(self) -> None:
        """Reject more than the maximum number of keep options."""
        options = sum((("--keep", "CPPFLAGS") for _ in range(ffmpeg_configure.MAX_KEEP + 1)), ())
        error = io.StringIO()
        with contextlib.redirect_stderr(error):
            self.assertEqual(self.run_wrapper(*options), 2)
        self.assertIn(str(ffmpeg_configure.MAX_KEEP), error.getvalue())
        self.assertFalse((self.build / "config.log").exists())

    def test_configure_timeout_returns_deadline_code(self) -> None:
        """Return 124 and report the deadline when configure times out."""
        error = io.StringIO()
        process = SimpleNamespace(
            pid=123,
            returncode=None,
            wait=Mock(side_effect=(subprocess.TimeoutExpired("configure", 1800), 0)),
        )
        with (
            patch.object(
                ffmpeg_configure.subprocess,
                "Popen",
                return_value=process,
            ),
            patch.object(ffmpeg_configure.os, "killpg") as killpg,
            contextlib.redirect_stderr(error),
        ):
            result = self.run_wrapper()
        self.assertEqual(result, 124)
        self.assertIn("1800", error.getvalue())
        killpg.assert_called_once_with(123, signal.SIGKILL)

    @unittest.skipUnless(os.name == "posix", "requires POSIX process groups")
    def test_configure_timeout_kills_child_process(self) -> None:
        """Kill configure descendants before returning the deadline code."""
        (self.source / "configure").write_text(
            "#!/bin/sh\n"
            "(sleep 30 & echo $! > sleep.pid; wait; echo late > late.txt) &\n"
            "echo $! > child.pid\n"
            "wait\n",
            encoding="utf-8",
        )
        error = io.StringIO()
        with (
            patch.object(ffmpeg_configure, "TIMEOUT_S", 0.1),
            contextlib.redirect_stderr(error),
        ):
            result = self.run_wrapper()
        child_pid = int((self.build / "child.pid").read_text(encoding="utf-8"))
        sleep_pid = int((self.build / "sleep.pid").read_text(encoding="utf-8"))
        self.addCleanup(kill_process_if_running, child_pid)
        self.addCleanup(kill_process_if_running, sleep_pid)
        self.assertEqual(result, 124)
        self.assertIn("0.1", error.getvalue())
        self.assertTrue(process_exits_within(child_pid))
        self.assertFalse((self.build / "late.txt").exists())

    def test_configure_return_code_propagates(self) -> None:
        """Return the configure process exit code unchanged."""
        (self.source / "configure").write_text("#!/bin/sh\nexit 3\n", encoding="utf-8")
        self.assertEqual(self.run_wrapper(), 3)
