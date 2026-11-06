# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Check that FFmpeg configure receives only approved environment names."""

from __future__ import annotations

import contextlib
import io
import os
import subprocess  # nosec B404 - temporary shell fixture, fixed argv and deadline.
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts import ffmpeg_configure

PLANT = "plant-value-123"
CONTROL_ENV = {
    "PATH": "/usr/bin:/bin",
    "HOME": "/nonexistent",
    "CC": "cc",
    "DUMMY_PLANT_TOKEN": PLANT,
    "DUMMY_OTHER": "other-value",
}


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
            patch.object(ffmpeg_configure, "os", SimpleNamespace(environ=CONTROL_ENV)),
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
        self.assertIn("PATH=", log)
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

    def test_keep_passes_unlisted_name(self) -> None:
        """Pass an explicitly kept, nonsensitive variable to configure."""
        self.assertEqual(self.run_wrapper("--keep", "DUMMY_OTHER"), 0)
        self.assertIn("DUMMY_OTHER=", (self.build / "config.log").read_text(encoding="utf-8"))

    def test_keep_refuses_sensitive_name(self) -> None:
        """Reject a credential-like keep name before starting configure."""
        error = io.StringIO()
        with contextlib.redirect_stderr(error):
            self.assertEqual(self.run_wrapper("--keep", "MY_API_KEY"), 2)
        self.assertIn("MY_API_KEY", error.getvalue())
        self.assertFalse((self.build / "config.log").exists())

    def test_configure_return_code_propagates(self) -> None:
        """Return the configure process exit code unchanged."""
        (self.source / "configure").write_text("#!/bin/sh\nexit 3\n", encoding="utf-8")
        self.assertEqual(self.run_wrapper(), 3)
