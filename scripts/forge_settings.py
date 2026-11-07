#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Centralized forge configuration and credential discovery."""

from __future__ import annotations

import json
import os
import re
import subprocess  # nosec B404 - fixed secret-tool invocation to read token from keyring.
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

DEFAULT_HOST = "https://code.ffmpeg.org"
DEFAULT_REPO = "FFmpeg/FFmpeg"
DEFAULT_BOT_REVIEWER = "Forgejo_Fairy"

REPO_PATTERN = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
LOGIN_PATTERN = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*$")


def validate_host(raw: str) -> str:
    """Validate an HTTPS URL without path or trailing slash."""
    if not isinstance(raw, str) or not raw:
        raise ValueError("invalid FFMPEG_FORGEJO_HOST: empty host")
    if raw.endswith("/"):
        raise ValueError(f"invalid FFMPEG_FORGEJO_HOST (trailing slash forbidden): {raw!r}")
    parsed = urllib.parse.urlsplit(raw)
    if parsed.scheme != "https":
        raise ValueError(f"invalid FFMPEG_FORGEJO_HOST (scheme must be https): {raw!r}")
    if not parsed.netloc:
        raise ValueError(f"invalid FFMPEG_FORGEJO_HOST (missing network location): {raw!r}")
    if parsed.path:
        raise ValueError(f"invalid FFMPEG_FORGEJO_HOST (path forbidden): {raw!r}")
    if parsed.query or parsed.fragment:
        raise ValueError(f"invalid FFMPEG_FORGEJO_HOST (query/fragment forbidden): {raw!r}")
    return raw


def validate_repo(raw: str) -> str:
    """Validate an owner/name repository path."""
    if not isinstance(raw, str) or not REPO_PATTERN.fullmatch(raw):
        raise ValueError(f"invalid FFMPEG_FORGEJO_REPO: {raw!r}")
    return raw


def validate_bot_reviewer(raw: str) -> str:
    """Validate a bot reviewer account login."""
    if not isinstance(raw, str) or not LOGIN_PATTERN.fullmatch(raw):
        raise ValueError(f"invalid FFMPEG_FORGEJO_BOT_REVIEWER: {raw!r}")
    return raw


def validate_login(raw: str) -> str:
    """Validate a user account login."""
    if not isinstance(raw, str) or not LOGIN_PATTERN.fullmatch(raw):
        raise ValueError(f"invalid FFMPEG_FORGEJO_LOGIN: {raw!r}")
    return raw


def host() -> str:
    """Return the validated forge host URL."""
    return validate_host(os.environ.get("FFMPEG_FORGEJO_HOST", DEFAULT_HOST))


def repo() -> str:
    """Return the validated repository path."""
    return validate_repo(os.environ.get("FFMPEG_FORGEJO_REPO", DEFAULT_REPO))


def bot_reviewer() -> str:
    """Return the validated bot reviewer login."""
    return validate_bot_reviewer(
        os.environ.get("FFMPEG_FORGEJO_BOT_REVIEWER", DEFAULT_BOT_REVIEWER)
    )


def keyring_service(target_host: str | None = None) -> str:
    """Derive the desktop keyring service name from the forge host URL."""
    h = target_host if target_host is not None else host()
    return urllib.parse.urlsplit(h).netloc


def authority(url: str) -> tuple[str | None, int | None]:
    """Return a normalized hostname and effective HTTPS port for URL comparisons."""
    parsed = urllib.parse.urlsplit(url)
    port = parsed.port
    if port is None and parsed.scheme == "https":
        port = 443
    return parsed.hostname, port


def api_base(target_host: str | None = None) -> str:
    """Return the root v1 API URL for the configured forge host."""
    h = target_host if target_host is not None else host()
    return f"{h}/api/v1"


def git_url(target_host: str | None = None, target_repo: str | None = None) -> str:
    """Return the upstream Git clone URL for the configured forge repository."""
    h = target_host if target_host is not None else host()
    r = target_repo if target_repo is not None else repo()
    return f"{h}/{r}.git"


def pull_url_pattern(
    target_host: str | None = None, target_repo: str | None = None
) -> re.Pattern[str]:
    """Match configured repository pull URLs, capturing host, owner, name, and number."""
    h = target_host if target_host is not None else host()
    owner, name = validate_repo(target_repo if target_repo is not None else repo()).split("/")
    return re.compile(rf"({re.escape(h)})/({re.escape(owner)})/({re.escape(name)})/pulls/(\d+)")


def _lookup_keyring_token(service: str) -> str:
    """Query Secret Service for the forge token of service."""
    try:
        proc = subprocess.run(  # nosec B603 B607 - fixed secret-tool lookup argv.
            ["secret-tool", "lookup", "service", service],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(
            f"failed to read keyring token for {service} (set FFMPEG_FORGEJO_LOGIN): {exc}"
        ) from exc
    if proc.returncode != 0:
        raise RuntimeError(
            f"keyring lookup for service {service!r} failed with return code {proc.returncode} "
            "(set FFMPEG_FORGEJO_LOGIN)"
        )
    token = proc.stdout.strip()
    if not token:
        raise RuntimeError(
            f"no keyring token found for service {service!r} (set FFMPEG_FORGEJO_LOGIN)"
        )
    return token


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect when a request carries the forge token."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: PLR0913, PLR0917
        """Let HTTP error processing reject every redirect without another request."""
        return None


def _fetch_user_login(base_url: str, token: str) -> str:
    """Fetch user profile from /user and return validated login."""
    url = f"{base_url}/user"
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"token {token}", "Accept": "application/json"},
    )
    opener = urllib.request.build_opener(NoRedirectHandler())
    try:
        with opener.open(req, timeout=10) as response:  # nosec B310 - forge API URL
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError) as exc:
        if isinstance(exc, urllib.error.HTTPError):
            exc.close()
        raise RuntimeError(
            f"failed to fetch /user from {url} (set FFMPEG_FORGEJO_LOGIN): {exc}"
        ) from exc
    if not isinstance(payload, dict) or "login" not in payload:
        raise RuntimeError(f"malformed response from {url} (set FFMPEG_FORGEJO_LOGIN)")
    return validate_login(payload["login"])


def login() -> str:
    """Return user login from FFMPEG_FORGEJO_LOGIN or discover it via /user API."""
    env_login = os.environ.get("FFMPEG_FORGEJO_LOGIN")
    if env_login is not None:
        return validate_login(env_login)
    service = keyring_service()
    token = _lookup_keyring_token(service)
    return _fetch_user_login(api_base(), token)


HOST: str
REPO: str
BOT_REVIEWER: str


def __getattr__(name: str) -> Any:
    """Resolve dynamic forge settings from environment."""
    if name == "HOST":
        return host()
    if name == "REPO":
        return repo()
    if name == "BOT_REVIEWER":
        return bot_reviewer()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
