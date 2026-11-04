# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Tests for centralized forge settings and credential discovery."""

from __future__ import annotations

import ast
import contextlib
import importlib
import io
import json
import os
import subprocess  # nosec B404 - fixed ffmpeg-forgejo invocation in test fixtures.
import sys
import tempfile
import unittest
import unittest.mock
import urllib.error
from pathlib import Path

from scripts import fairy_unanswered, forge_settings, series, upstream_drift

ROOT = Path(__file__).resolve().parent.parent
FORBIDDEN_LITERALS = ("code.ffmpeg.org", "FFmpeg/FFmpeg", "Forgejo_Fairy", "lusoris")
PERMITTED_SCRIPT_LINES = {
    (
        "scripts/fairy_unanswered.py",
        (
            "        if (actual := owed(comments, reviews, "
            'login="lusoris", bot="Forgejo_Fairy")) != want'
        ),
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


def setUpModule() -> None:
    """Reject unmocked network access throughout the forge settings fixtures."""
    for target, name in (
        (forge_settings.urllib.request, "urlopen"),
        (forge_settings.urllib.request.AbstractHTTPHandler, "do_open"),
    ):
        guard = unittest.mock.patch.object(
            target, name, side_effect=AssertionError("network forbidden")
        )
        guard.start()
        unittest.addModuleCleanup(guard.stop)


def _run_ffmpeg_forgejo_host(host: str) -> subprocess.CompletedProcess[str]:
    """Run host validation through the offline review-summary entry point."""
    with tempfile.TemporaryDirectory() as scratch:
        reviews = Path(scratch) / "reviews.json"
        reviews.write_text("[]", encoding="utf-8")
        return subprocess.run(  # nosec B603 B607 - fixed offline helper invocation in test.
            [
                "bash",
                str(ROOT / "scripts" / "ffmpeg-forgejo"),
                "fairy-reviews",
                "--from-json",
                str(reviews),
            ],
            env=dict(os.environ, FFMPEG_FORGEJO_HOST=host),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )


class ForgeSettingsTestCase(unittest.TestCase):
    """Isolate only the four forge settings and restore contributor overrides."""

    def setUp(self) -> None:
        """Clear forge overrides while preserving unrelated configuration."""
        overrides = unittest.mock.patch.dict(os.environ)
        overrides.start()
        self.addCleanup(overrides.stop)
        for name in ("HOST", "REPO", "BOT_REVIEWER", "LOGIN"):
            os.environ.pop(f"FFMPEG_FORGEJO_{name}", None)


class ForgeSettingsPositiveTest(ForgeSettingsTestCase):
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
            pattern = forge_settings.pull_url_pattern()
            match = pattern.fullmatch("https://custom.forge/custom/repo/pulls/99")
            self.assertIsNotNone(match)
            self.assertEqual(match.groups(), ("https://custom.forge", "custom", "repo", "99"))
            self.assertIsNone(pattern.fullmatch("https://custom.forge/FFmpeg/FFmpeg/pulls/99"))
            self.assertEqual(
                upstream_drift.api_url("https://custom.forge/custom/repo/pulls/99", 99),
                "https://custom.forge/api/v1/repos/custom/repo/pulls/99",
            )

    def test_series_pull_url_pattern_matches_host(self) -> None:
        """Series validates covered_by against the configured host and repository."""
        with unittest.mock.patch.dict(os.environ, {"FFMPEG_FORGEJO_HOST": "https://custom.forge"}):
            pat = series.pull_url_pattern()
            self.assertIsNotNone(pat.fullmatch("https://custom.forge/FFmpeg/FFmpeg/pulls/99"))
            self.assertIsNone(pat.fullmatch("https://code.ffmpeg.org/FFmpeg/FFmpeg/pulls/99"))
            for host, expected_errors in (("custom.forge", 0), ("code.ffmpeg.org", 1)):
                findings = series.Findings()
                series.check_covered_by(
                    {
                        "status": "unsubmitted",
                        "covered_by": f"https://{host}/FFmpeg/FFmpeg/pulls/99",
                    },
                    "fixture",
                    findings,
                )
                self.assertEqual(len(findings.errors), expected_errors)

    def test_fairy_unanswered_discovery_uses_login(self) -> None:
        """Fairy unanswered thread queries use the configured login."""
        calls = []

        def fake_paginated(path: str, **kwargs: object) -> list[dict]:
            """Record discovery paths without network access."""
            calls.append(path)
            return []

        with (
            unittest.mock.patch.dict(os.environ, {"FFMPEG_FORGEJO_LOGIN": "other_user"}),
            unittest.mock.patch.object(fairy_unanswered, "paginated", side_effect=fake_paginated),
            unittest.mock.patch.object(fairy_unanswered, "discover_page_size", return_value=50),
            unittest.mock.patch.object(
                fairy_unanswered, "discover_threads", wraps=fairy_unanswered.discover_threads
            ) as discovery,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(fairy_unanswered.main([]), 0)
            discovery.assert_called_once_with("other_user")
            self.assertIn("/pulls?state=open&poster=other_user", calls)
            self.assertIn("/issues?type=issues&state=open&created_by=other_user", calls)

    def test_dynamic_repo_attribute(self) -> None:
        """The dynamic REPO attribute and Fairy API use the configured repository."""
        for repo in ("owner/project", "other/repo"):
            with (
                self.subTest(repo=repo),
                unittest.mock.patch.dict(os.environ, {"FFMPEG_FORGEJO_REPO": repo}),
            ):
                self.assertEqual(forge_settings.REPO, forge_settings.repo())
                self.assertEqual(
                    fairy_unanswered.api_url(), f"{forge_settings.api_base()}/repos/{repo}"
                )

    def test_fixture_settings_isolation_and_restore(self) -> None:
        """Default fixtures clear four overrides and restore them during cleanup."""
        overrides = {
            "FFMPEG_FORGEJO_HOST": "https://fixture.forge",
            "FFMPEG_FORGEJO_REPO": "fixture/project",
            "FFMPEG_FORGEJO_BOT_REVIEWER": "FixtureBot",
            "FFMPEG_FORGEJO_LOGIN": "fixture_user",
        }
        with unittest.mock.patch.dict(os.environ, overrides):
            case = ForgeSettingsPositiveTest("test_defaults")
            try:
                case.setUp()
                for key in overrides:
                    self.assertFalse(key in os.environ, key)
                case.test_defaults()
            finally:
                case.doCleanups()
            for key, value in overrides.items():
                self.assertEqual(os.environ[key], value)

    def test_ffmpeg_forgejo_host_env_override(self) -> None:
        """FFmpeg-forgejo accepts a valid host override without online discovery."""
        proc = _run_ffmpeg_forgejo_host("https://custom.forge")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "0\t-\t-\n")


class ForgeSettingsNegativeTest(ForgeSettingsTestCase):
    """Verify validation rejection of invalid configuration parameters."""

    def test_other_repository_pull_url_rejected(self) -> None:
        """Pull URL matching rejects other owners and repositories on the same host."""
        pattern = forge_settings.pull_url_pattern()
        for repo in ("other/FFmpeg", "FFmpeg/other", "other/project"):
            with self.subTest(repo=repo):
                self.assertIsNone(pattern.fullmatch(f"https://code.ffmpeg.org/{repo}/pulls/1"))

    def test_keyring_nonzero_status_rejected(self) -> None:
        """A failed lookup rejects stdout and reports only the service and return code."""
        with (
            unittest.mock.patch.object(
                forge_settings.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 1, "x", "private-stderr"),
            ),
            self.assertRaises(RuntimeError) as caught,
        ):
            forge_settings._lookup_keyring_token("unit.forge")
        message = str(caught.exception)
        self.assertIn("unit.forge", message)
        self.assertIn("1", message)
        self.assertNotIn("x", message)
        self.assertNotIn("private-stderr", message)

    def test_login_redirect_handler_refuses_all_redirects(self) -> None:
        """Login discovery refuses same-origin, foreign-origin, and HTTP redirects."""
        request = forge_settings.urllib.request.Request("https://fixture.forge/api/v1/user")
        handler = forge_settings.NoRedirectHandler()
        for code in (301, 302, 303, 307, 308):
            for target in (
                "https://fixture.forge/x",
                "https://other.forge/x",
                "http://fixture.forge/x",
            ):
                with self.subTest(code=code, target=target):
                    self.assertIsNone(
                        handler.redirect_request(request, None, code, "Found", {}, target)
                    )

    def test_login_opener_refuses_redirect_before_second_request(self) -> None:
        """A fake 302 becomes a tool error before any redirected request is sent."""
        response = unittest.mock.Mock(code=302, status=302, msg="Found")
        response.info.return_value = {"location": "https://other.forge/user"}
        with (
            unittest.mock.patch.object(
                forge_settings.urllib.request.HTTPSHandler,
                "https_open",
                side_effect=[response, AssertionError("redirect followed")],
            ) as transport,
            self.assertRaisesRegex(RuntimeError, "failed to fetch /user"),
        ):
            forge_settings._fetch_user_login("https://fixture.forge/api/v1", "fixture-token")
        transport.assert_called_once()
        response.close.assert_called_once()

    def test_invalid_host_imports_and_cli_errors(self) -> None:
        """Invalid hosts permit imports and produce CLI errors without tracebacks."""
        with unittest.mock.patch.dict(os.environ, {"FFMPEG_FORGEJO_HOST": "http://invalid.forge"}):
            importlib.reload(series)
            importlib.reload(upstream_drift)
            for module, args in ((series, ["check"]), (upstream_drift, [])):
                errors = io.StringIO()
                with (
                    self.subTest(module=module.__name__),
                    contextlib.redirect_stderr(errors),
                ):
                    self.assertEqual(module.main(args), 2)
                self.assertIn("FFMPEG_FORGEJO_HOST", errors.getvalue())
                self.assertNotIn("Traceback", errors.getvalue())

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
            unittest.mock.patch.object(
                forge_settings.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0, "", ""),
            ),
            unittest.mock.patch.object(
                forge_settings, "_fetch_user_login", side_effect=AssertionError("fetch forbidden")
            ) as fetch,
        ):
            with self.assertRaises(RuntimeError) as ctx:
                forge_settings.login()
            self.assertIn("FFMPEG_FORGEJO_LOGIN", str(ctx.exception))
        fetch.assert_not_called()

    def test_user_failure_http_error(self) -> None:
        """HTTP failures during /user discovery name FFMPEG_FORGEJO_LOGIN."""
        err = urllib.error.URLError("connection refused")
        with (
            unittest.mock.patch.object(forge_settings, "_lookup_keyring_token", return_value="tok"),
            unittest.mock.patch.object(
                forge_settings.urllib.request.OpenerDirector, "open", side_effect=err
            ),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                forge_settings.login()
            self.assertIn("FFMPEG_FORGEJO_LOGIN", str(ctx.exception))

    def test_user_failure_malformed_payload(self) -> None:
        """Responses without login field name FFMPEG_FORGEJO_LOGIN."""
        bio = io.BytesIO(json.dumps({"id": 1}).encode("utf-8"))
        with (
            unittest.mock.patch.object(forge_settings, "_lookup_keyring_token", return_value="tok"),
            unittest.mock.patch.object(
                forge_settings.urllib.request.OpenerDirector, "open", return_value=bio
            ),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                forge_settings.login()
            self.assertIn("FFMPEG_FORGEJO_LOGIN", str(ctx.exception))

    def test_ffmpeg_forgejo_invalid_host_rejected(self) -> None:
        """FFmpeg-forgejo exits non-zero naming FFMPEG_FORGEJO_HOST on bad URL."""
        for bad in (
            "http://insecure.forge",
            "https://slash.forge/",
            "https://query.forge?x",
            "https://fragment.forge#x",
        ):
            with self.subTest(bad=bad):
                proc = _run_ffmpeg_forgejo_host(bad)
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn("FFMPEG_FORGEJO_HOST", proc.stderr)

    def test_keyring_unexpected_errors_propagate(self) -> None:
        """Programming errors from the mocked keyring subprocess remain visible."""
        with (
            unittest.mock.patch.object(
                forge_settings.subprocess, "run", side_effect=TypeError("planted keyring bug")
            ) as lookup,
            self.assertRaisesRegex(TypeError, "planted keyring bug"),
        ):
            forge_settings._lookup_keyring_token("forge.invalid")
        lookup.assert_called_once()

    def test_fetch_unexpected_errors_propagate(self) -> None:
        """Programming errors from the mocked HTTP transport remain visible."""
        with (
            unittest.mock.patch.object(
                forge_settings.urllib.request.OpenerDirector,
                "open",
                side_effect=TypeError("planted transport bug"),
            ) as request,
            self.assertRaisesRegex(TypeError, "planted transport bug"),
        ):
            forge_settings._fetch_user_login("https://forge.invalid/api/v1", "fixture-token")
        request.assert_called_once()


class ForgeSettingsBoundaryTest(ForgeSettingsTestCase):
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
    """Enforce shared forge configuration and deferred validation across scripts."""

    def test_settings_consumers_import_with_invalid_environment(self) -> None:
        """Every forge settings consumer defers validation until runtime."""
        scripts_dir = ROOT / "scripts"
        modules = []
        for path in sorted(scripts_dir.glob("*.py")):
            imports = set()
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import):
                    imports.update(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    imports.add(node.module)
                    imports.update(f"{node.module}.{alias.name}" for alias in node.names)
            if imports.intersection({"forge_settings", "scripts.forge_settings"}):
                modules.append(path.stem)
        self.assertIn("fairy_unanswered", modules)
        for module in modules:
            with self.subTest(module=module):
                proc = subprocess.run(  # nosec B603 - local module found by AST scan.
                    [sys.executable, "-c", f"import {module}"],
                    cwd=scripts_dir,
                    env=dict(
                        os.environ,
                        FFMPEG_FORGEJO_HOST="http://bad",
                        FFMPEG_FORGEJO_REPO="bad",
                    ),
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)

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
