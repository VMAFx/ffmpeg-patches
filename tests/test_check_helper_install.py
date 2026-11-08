# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Exercise the ffmpeg-forgejo helper installation check script."""

from __future__ import annotations

import os
import subprocess  # nosec B404 - fixed Git and Python test commands in temporary directories.
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from tests.helpers import isolated_git_environment

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_helper_install.py"


class CheckHelperInstallTest(unittest.TestCase):
    """Test the installation and staleness check for the ffmpeg-forgejo helper script."""

    def setUp(self) -> None:
        """Create a temporary repository with a committed helper and origin/main ref."""
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.repo = Path(self.directory.name) / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        self.scripts_dir = self.repo / "scripts"
        self.scripts_dir.mkdir()
        self.helper_script = self.scripts_dir / "ffmpeg-forgejo"
        self.helper_content = b"#!/usr/bin/env bash\necho forgejo\n"
        self.helper_script.write_bytes(self.helper_content)
        self.helper_script.chmod(0o755)
        self.git("add", "scripts/ffmpeg-forgejo")
        self.git("commit", "-qm", "initial commit")
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")
        self.bin_dir = Path(self.directory.name) / "bin"
        self.bin_dir.mkdir()
        self.install_path = self.bin_dir / "ffmpeg-forgejo"

    def git(self, *args: str) -> str:
        """Run bounded Git commands in the temporary fixture repository."""
        result = subprocess.run(  # nosec B603 B607 - fixed Git executable and fixture arguments.
            [
                "git",
                "-C",
                str(self.repo),
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@invalid",
                "-c",
                "commit.gpgsign=false",
                *args,
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
            env=isolated_git_environment(),
        )
        return result.stdout.strip()

    def run_check(self, *args: str) -> subprocess.CompletedProcess[str]:
        """Run check_helper_install.py with explicit arguments and timeout."""
        return subprocess.run(  # nosec B603 - fixed Python executable and test script.
            [sys.executable, "-B", str(SCRIPT), *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
            env=isolated_git_environment(),
        )

    def test_absent_path_reports_skipped(self) -> None:
        """An absent helper path exits 0 and reports skipped on stdout."""
        proc = self.run_check("--path", str(self.install_path), "--repo", str(self.repo))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(f"helper not installed at {self.install_path}; skipped", proc.stdout)

    def test_regular_copy_reports_replacement_command(self) -> None:
        """A regular file copy exits 1 and reports the replacement command."""
        self.install_path.write_bytes(self.helper_content)
        proc = self.run_check("--path", str(self.install_path), "--repo", str(self.repo))
        self.assertEqual(proc.returncode, 1, proc.stdout)
        expected = (
            f"installed copy, not a symlink: {self.install_path}; "
            f"replace with: ln -sf {self.repo}/scripts/ffmpeg-forgejo {self.install_path}"
        )
        self.assertIn(expected, proc.stderr)

    def test_dangling_symlink_names_target(self) -> None:
        """A dangling symlink exits 1 and names the missing target."""
        missing = self.scripts_dir / "nonexistent"
        self.install_path.symlink_to(missing)
        proc = self.run_check("--path", str(self.install_path), "--repo", str(self.repo))
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("dangling symlink", proc.stderr)
        self.assertIn(str(missing), proc.stderr)

    def test_matching_symlink_reports_ok(self) -> None:
        """A symlink matching origin/main exits 0 and reports helper ok."""
        self.install_path.symlink_to(self.helper_script)
        proc = self.run_check("--path", str(self.install_path), "--repo", str(self.repo))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(f"helper ok: {self.install_path} -> {self.helper_script}", proc.stdout)

    def test_matching_non_executable_symlink_reports_not_executable(self) -> None:
        """A matching target without execute permission exits 1 and names the fix."""
        self.helper_script.chmod(0o644)
        self.install_path.symlink_to(self.helper_script)
        proc = self.run_check("--path", str(self.install_path), "--repo", str(self.repo))
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn(f"not executable: {self.helper_script}; run: chmod +x", proc.stderr)

    def test_git_repository_environment_does_not_redirect_fixture(self) -> None:
        """Inherited Git repository paths do not redirect the fixture check."""
        bogus = Path(self.directory.name) / "bogus"
        overrides = {
            "GIT_DIR": str(bogus / "git"),
            "GIT_INDEX_FILE": str(bogus / "index"),
        }
        self.install_path.symlink_to(self.helper_script)
        with unittest.mock.patch.dict(os.environ, overrides):
            proc = self.run_check("--path", str(self.install_path), "--repo", str(self.repo))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(bogus.exists())

    def test_stale_symlink_reports_differs_from_origin_main(self) -> None:
        """A symlink pointing to differing bytes exits 1 and reports stale."""
        other = self.scripts_dir / "other"
        other.write_bytes(b"#!/usr/bin/env bash\necho differing\n")
        self.install_path.symlink_to(other)
        proc = self.run_check("--path", str(self.install_path), "--repo", str(self.repo))
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn(f"stale: {other} differs from origin/main", proc.stderr)

    def test_missing_origin_main_reports_unreadable(self) -> None:
        """An unreadable origin/main revision exits 1 and names the reference."""
        self.git("update-ref", "-d", "refs/remotes/origin/main")
        self.install_path.symlink_to(self.helper_script)
        proc = self.run_check("--path", str(self.install_path), "--repo", str(self.repo))
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("origin/main", proc.stderr)


if __name__ == "__main__":
    unittest.main()
