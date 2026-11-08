#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Check whether the ffmpeg-forgejo helper is a fresh, executable symlink."""

from __future__ import annotations

import argparse
import os
import subprocess  # nosec B404 - bounded Git subprocess call.
import sys
from pathlib import Path


def read_origin_helper(repo: Path) -> bytes | None:
    """Read scripts/ffmpeg-forgejo at origin/main or report an unreadable error."""
    cmd = ["git", "-C", str(repo), "show", "origin/main:scripts/ffmpeg-forgejo"]
    try:
        proc = subprocess.run(  # nosec B603 B607 - fixed Git executable and explicit argv list.
            cmd,
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        print(
            f"cannot read origin/main:scripts/ffmpeg-forgejo in {repo}: {error}",
            file=sys.stderr,
        )
        return None

    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace").strip()
        print(
            f"cannot read origin/main:scripts/ffmpeg-forgejo in {repo}: {err}",
            file=sys.stderr,
        )
        return None

    return proc.stdout


def check_symlink_target(path: Path, repo: Path) -> int:
    """Verify that a symlink target exists and matches repo origin/main."""
    target = path.resolve()
    if not target.exists():
        print(f"dangling symlink: {path} -> {target}", file=sys.stderr)
        return 1

    origin_bytes = read_origin_helper(repo)
    if origin_bytes is None:
        return 1

    try:
        target_bytes = target.read_bytes()
    except OSError as error:
        print(f"cannot read target {target}: {error}", file=sys.stderr)
        return 1

    if target_bytes != origin_bytes:
        print(f"stale: {target} differs from origin/main", file=sys.stderr)
        return 1
    if not os.access(target, os.X_OK):
        print(f"not executable: {target}; run: chmod +x {target}", file=sys.stderr)
        return 1

    print(f"helper ok: {path} -> {target}")
    return 0


def check_install(path: Path, repo: Path) -> int:
    """Check whether the helper executable at path is a symlink matching repo origin/main."""
    if not path.is_symlink() and not path.exists():
        print(f"helper not installed at {path}; skipped")
        return 0

    if not path.is_symlink():
        msg = (
            f"installed copy, not a symlink: {path}; "
            f"replace with: ln -sf {repo}/scripts/ffmpeg-forgejo {path}"
        )
        print(msg, file=sys.stderr)
        return 1

    return check_symlink_target(path, repo)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments for the helper installation checker."""
    parser = argparse.ArgumentParser(
        description="Check whether the ffmpeg-forgejo helper is a fresh, executable symlink."
    )
    repo_default = Path(__file__).resolve().parent.parent
    path_default = Path.home() / ".local" / "bin" / "ffmpeg-forgejo"
    parser.add_argument(
        "--path",
        type=Path,
        default=path_default,
        help="Path to the installed helper executable (default: %(default)s)",
    )
    parser.add_argument(
        "--repo",
        type=Path,
        default=repo_default,
        help="Path to the repository root (default: %(default)s)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run the helper installation check and exit with the outcome code."""
    args = parse_args(argv)
    path = args.path.expanduser()
    repo = args.repo.resolve()
    return check_install(path, repo)


if __name__ == "__main__":
    sys.exit(main())
