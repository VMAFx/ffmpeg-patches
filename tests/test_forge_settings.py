# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Tests for centralized forge settings and credential discovery."""

from __future__ import annotations

import io
import json
import os
import subprocess  # nosec B404 - fixed ffmpeg-forgejo invocation in test fixtures.
import unittest
import unittest.mock
import urllib.error
from pathlib import Path

from scripts import fairy_unanswered, forge_settings, series

ROOT = Path(__file__).resolve().parent.parent
FORBIDDEN_LITERALS = ("code.ffmpeg.org", "FFmpeg/FFmpeg", "Forgejo_Fairy", "lusoris")
PERMITTED_SCRIPT_LINES = {
    (
        "scripts/fairy_unanswered.py",
        '        user_login = "lusoris"',
    ),
    (
        "scripts/fairy_unanswered.py",
        '    mine = _fixture_word("lusoris", "reply", "2026-11-01T00:00:00Z")',
    ),
    (
        "scripts/ffmpeg-forgejo",
        "# Environment: FFMPEG_FORGEJO_HOST (default https://code.ffmpeg.org),",
    ),
    (
        "scripts/ffmpeg-forgejo",
        "#   FFMPEG_FORGEJO_REPO (default FFmpeg/FFmpeg), FFMPEG_FORGEJO_BASE (default master),",
    ),
    (
        "scripts/ffmpeg-forgejo",
        (
            "#   FFMPEG_FORGEJO_BOT_REVIEWER (default Forgejo_Fairy), "
            "FFMPEG_FORGEJO_OFFLINE=1 (skip optional check)."
        ),
    ),
    (
        "scripts/ffmpeg-forgejo",
        'HOST="${FFMPEG_FORGEJO_HOST:-https://code.ffmpeg.org}"',
    ),
    (
        "scripts/ffmpeg-forgejo",
        'REPO="${FFMPEG_FORGEJO_REPO:-FFmpeg/FFmpeg}"',
    ),
    (
        "scripts/ffmpeg-forgejo",
        'BOT_REVIEWER="${FFMPEG_FORGEJO_BOT_REVIEWER:-Forgejo_Fairy}"',
    ),
}


class ForgeSettingsPositiveTest(unittest.TestCase):
    """Verify default configurations and environment overrides."""

    def test_defaults(self) -> None:
        """Verify standard fallback settings and derived helpers."""
        self.assertEqual(forge_settings.DEFAULT_HOST, "https://code.ffmpeg.org")
        self.assertEqual(forge_settings.DEFAULT_REPO, "FFmpeg/FFmpeg")
        self.assertEqual(forge_settings.DEFAULT_BOT_REVIEWER, "Forgejo_Fairy")
        self.assertEqual(forge_settings.keyring_service(), "code.ffmpeg.org")
        self.assertEqual(forge_settings.api_base(), "https://code.ffmpeg.org/api/v1")
        self.assertEqual(forge_settings.git_url(), "https://code.ffmpeg.org/FFmpeg/FFmpeg.git")
        pattern = forge_settings.pull_url_pattern()
        match = pattern.fullmatch("https://code.ffmpeg.org/FFmpeg/FFmpeg/pulls/1234")
        self.assertIsNotNone(match)
        self.assertEqual(match.groups(), ("https://code.ffmpeg.org", "FFmpeg", "FFmpeg", "1234"))

    def test_env_override_flows_to_helpers(self) -> None:
        """Environment variables change all derived endpoints and credentials."""
        overrides = {
            "FFMPEG_FORGEJO_HOST": "https://custom.forge",
            "FFMPEG_FORGEJO_REPO": "custom/repo",
            "FFMPEG_FORGEJO_BOT_REVIEWER": "CustomBot",
            "FFMPEG_FORGEJO_LOGIN": "custom_user",
        }
        with unittest.mock.patch.dict(os.environ, overrides):
            self.assertEqual(forge_settings.host(), "https://custom.forge")
            self.assertEqual(forge_settings.repo(), "custom/repo")
            self.assertEqual(forge_settings.bot_reviewer(), "CustomBot")
            self.assertEqual(forge_settings.keyring_service(), "custom.forge")
            self.assertEqual(forge_settings.api_base(), "https://custom.forge/api/v1")
            self.assertEqual(forge_settings.git_url(), "https://custom.forge/custom/repo.git")
            self.assertEqual(forge_settings.login(), "custom_user")

    def test_series_pull_url_pattern_matches_host(self) -> None:
        """Series PULL_URL matches pull URLs on the configured forge host."""
        self.assertIsNotNone(series.PULL_URL)
        with unittest.mock.patch.dict(os.environ, {"FFMPEG_FORGEJO_HOST": "https://custom.forge"}):
            pat = forge_settings.pull_url_pattern()
            self.assertIsNotNone(pat.fullmatch("https://custom.forge/owner/project/pulls/99"))
            self.assertIsNone(pat.fullmatch("https://code.ffmpeg.org/owner/project/pulls/99"))

    def test_fairy_unanswered_discovery_uses_login(self) -> None:
        """Fairy unanswered thread queries use the configured login."""
        calls = []

        def fake_paginated(path: str, **kwargs: object) -> list[dict]:
            calls.append(path)
            return []

        with (
            unittest.mock.patch.dict(os.environ, {"FFMPEG_FORGEJO_LOGIN": "other_user"}),
            unittest.mock.patch.object(fairy_unanswered, "paginated", side_effect=fake_paginated),
        ):
            threads = fairy_unanswered.discover_threads("other_user")
            self.assertEqual(threads, {})
            self.assertIn("/pulls?state=open&poster=other_user", calls)
            self.assertIn("/issues?type=issues&state=open&created_by=other_user", calls)

    def test_ffmpeg_forgejo_host_env_override(self) -> None:
        """FFmpeg-forgejo bash helper adopts FFMPEG_FORGEJO_HOST."""
        env = dict(os.environ, FFMPEG_FORGEJO_HOST="https://custom.forge")
        proc = subprocess.run(  # nosec B603 B607 - fixed helper invocation in test.
            ["bash", str(ROOT / "scripts" / "ffmpeg-forgejo"), "fairy-reviews"],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertIn("custom.forge", proc.stderr)


class ForgeSettingsNegativeTest(unittest.TestCase):
    """Verify validation rejection of invalid configuration parameters."""

    def test_invalid_host_scheme_rejected(self) -> None:
        """Non-HTTPS host URLs are rejected with variable name in error."""
        with self.assertRaises(ValueError) as ctx:
            forge_settings.validate_host("http://code.ffmpeg.org")
        self.assertIn("FFMPEG_FORGEJO_HOST", str(ctx.exception))

    def test_invalid_host_trailing_slash_rejected(self) -> None:
        """Host URLs with trailing slash are rejected naming the variable."""
        with self.assertRaises(ValueError) as ctx:
            forge_settings.validate_host("https://code.ffmpeg.org/")
        self.assertIn("FFMPEG_FORGEJO_HOST", str(ctx.exception))

    def test_invalid_host_path_rejected(self) -> None:
        """Host URLs containing a path segment are rejected."""
        with self.assertRaises(ValueError) as ctx:
            forge_settings.validate_host("https://code.ffmpeg.org/api")
        self.assertIn("FFMPEG_FORGEJO_HOST", str(ctx.exception))

    def test_invalid_repo_charset_rejected(self) -> None:
        """Malformed repository paths are rejected naming FFMPEG_FORGEJO_REPO."""
        for bad in ("", "FFmpeg", "FFmpeg/FFmpeg/extra", "invalid repo"):
            with self.subTest(repo=bad):
                with self.assertRaises(ValueError) as ctx:
                    forge_settings.validate_repo(bad)
                self.assertIn("FFMPEG_FORGEJO_REPO", str(ctx.exception))

    def test_invalid_bot_reviewer_rejected(self) -> None:
        """Malformed bot reviewer logins are rejected naming the variable."""
        with self.assertRaises(ValueError) as ctx:
            forge_settings.validate_bot_reviewer("bad bot!")
        self.assertIn("FFMPEG_FORGEJO_BOT_REVIEWER", str(ctx.exception))

    def test_invalid_login_rejected(self) -> None:
        """Malformed user logins are rejected naming the variable."""
        with self.assertRaises(ValueError) as ctx:
            forge_settings.validate_login("bad user!")
        self.assertIn("FFMPEG_FORGEJO_LOGIN", str(ctx.exception))

    def test_user_failure_missing_keyring_token(self) -> None:
        """Missing keyring tokens fail naming FFMPEG_FORGEJO_LOGIN."""
        with (
            unittest.mock.patch.dict(os.environ, {}, clear=True),
            unittest.mock.patch.object(forge_settings, "_lookup_keyring_token", return_value=""),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                forge_settings.login()
            self.assertIn("FFMPEG_FORGEJO_LOGIN", str(ctx.exception))

    def test_user_failure_http_error(self) -> None:
        """HTTP failures during /user discovery name FFMPEG_FORGEJO_LOGIN."""
        err = urllib.error.URLError("connection refused")
        with (
            unittest.mock.patch.dict(os.environ, {}, clear=True),
            unittest.mock.patch.object(forge_settings, "_lookup_keyring_token", return_value="tok"),
            unittest.mock.patch.object(forge_settings.urllib.request, "urlopen", side_effect=err),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                forge_settings.login()
            self.assertIn("FFMPEG_FORGEJO_LOGIN", str(ctx.exception))

    def test_user_failure_malformed_payload(self) -> None:
        """Responses without login field name FFMPEG_FORGEJO_LOGIN."""
        bio = io.BytesIO(json.dumps({"id": 1}).encode("utf-8"))
        with (
            unittest.mock.patch.dict(os.environ, {}, clear=True),
            unittest.mock.patch.object(forge_settings, "_lookup_keyring_token", return_value="tok"),
            unittest.mock.patch.object(forge_settings.urllib.request, "urlopen", return_value=bio),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                forge_settings.login()
            self.assertIn("FFMPEG_FORGEJO_LOGIN", str(ctx.exception))

    def test_ffmpeg_forgejo_invalid_host_rejected(self) -> None:
        """FFmpeg-forgejo exits non-zero naming FFMPEG_FORGEJO_HOST on bad URL."""
        for bad in ("http://insecure.forge", "https://slash.forge/"):
            with self.subTest(bad=bad):
                proc = subprocess.run(  # nosec B603 B607 - fixed helper invocation in test.
                    ["bash", str(ROOT / "scripts" / "ffmpeg-forgejo"), "whoami"],
                    env=dict(os.environ, FFMPEG_FORGEJO_HOST=bad),
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn("FFMPEG_FORGEJO_HOST", proc.stderr)


class ForgeSettingsBoundaryTest(unittest.TestCase):
    """Verify edge cases including port specifications and offline operation."""

    def test_boundary_host_with_port(self) -> None:
        """Host URLs with explicit port numbers are accepted across helpers."""
        custom = "https://code.ffmpeg.org:8443"
        self.assertEqual(forge_settings.validate_host(custom), custom)
        self.assertEqual(forge_settings.keyring_service(custom), "code.ffmpeg.org:8443")
        self.assertEqual(forge_settings.api_base(custom), "https://code.ffmpeg.org:8443/api/v1")
        pat = forge_settings.pull_url_pattern(custom)
        self.assertIsNotNone(pat.fullmatch("https://code.ffmpeg.org:8443/FFmpeg/FFmpeg/pulls/1"))

    def test_boundary_login_env_set_skips_network(self) -> None:
        """When FFMPEG_FORGEJO_LOGIN is set, network and keyring are skipped."""
        with (
            unittest.mock.patch.dict(os.environ, {"FFMPEG_FORGEJO_LOGIN": "offline_dev"}),
            unittest.mock.patch.object(
                forge_settings,
                "_lookup_keyring_token",
                side_effect=AssertionError("keyring called"),
            ),
            unittest.mock.patch.object(
                forge_settings.urllib.request,
                "urlopen",
                side_effect=AssertionError("network called"),
            ),
        ):
            self.assertEqual(forge_settings.login(), "offline_dev")


class ForgeSettingsSourceScanTest(unittest.TestCase):
    """Enforce absence of hard-coded forge literals across repository scripts."""

    def test_source_scan_forbids_literals_in_scripts(self) -> None:
        """Every script outside forge_settings.py forbids hard-coded forge literals."""
        violations = []
        scripts_dir = ROOT / "scripts"
        for path in sorted(scripts_dir.iterdir()):
            if path.name in {"forge_settings.py", "__pycache__"} or path.is_dir():
                continue
            rel_path = f"scripts/{path.name}"
            for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                stripped = line.strip()
                if (rel_path, line) in PERMITTED_SCRIPT_LINES or (
                    rel_path,
                    stripped,
                ) in PERMITTED_SCRIPT_LINES:
                    continue
                for literal in FORBIDDEN_LITERALS:
                    if literal in line:
                        violations.append(f"{rel_path}:{line_no} [{literal}]: {line}")
        self.assertEqual(violations, [])
