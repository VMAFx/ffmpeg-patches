#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Check that the ffmpeg-forgejo helper is a fresh symlink and no stray copy is installed."""

from __future__ import annotations

import argparse
import os
import subprocess  # nosec B404 - bounded Git subprocess call.
import sys
from pathlib import Path

# Ensure repository root is on sys.path for test/tool imports
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.variant_sweep import git_environment  # noqa: E402


def read_origin_helper(repo: Path) -> bytes | None:
    """Read scripts/ffmpeg-forgejo at origin/main or report an unreadable error."""
    cmd = ["git", "-C", str(repo), "show", "origin/main:scripts/ffmpeg-forgejo"]
    try:
        proc = subprocess.run(  # nosec B603 B607 - fixed Git executable and explicit argv list.
            cmd,
            capture_output=True,
            check=False,
            env=git_environment(),
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


def is_repository_link(entry: Path, repo: Path) -> bool:
    """Report whether entry is a symlink that resolves inside repo."""
    return entry.is_symlink() and entry.resolve().is_relative_to(repo)


def installed_files(install: Path, share_dir: Path) -> list[Path]:
    """List ffmpeg-forgejo files beside install and every entry of share_dir, install excluded."""
    siblings = sorted(install.parent.glob("ffmpeg-forgejo*")) if install.parent.is_dir() else []
    shared = sorted(share_dir.iterdir()) if share_dir.is_dir() else []
    return [entry for entry in siblings + shared if entry != install]


def check_stray_copies(install: Path, share_dir: Path, repo: Path) -> int:
    """Report every installed ffmpeg-forgejo file that is not a symlink into repo."""
    try:
        entries = installed_files(install, share_dir)
    except OSError as error:
        print(f"cannot list installed helper files: {error}", file=sys.stderr)
        return 1
    strays = [entry for entry in entries if not is_repository_link(entry, repo)]
    for entry in strays:
        print(f"stray copy, not a symlink into {repo}: {entry}", file=sys.stderr)
    return 1 if strays else 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments for the helper installation checker."""
    parser = argparse.ArgumentParser(
        description=(
            "Check that the ffmpeg-forgejo helper is a fresh symlink and no stray copy is"
            " installed."
        )
    )
    repo_default = Path(__file__).resolve().parent.parent
    path_default = Path.home() / ".local" / "bin" / "ffmpeg-forgejo"
    share_default = Path.home() / ".local" / "share" / "ffmpeg-forgejo"
    parser.add_argument(
        "--path",
        type=Path,
        default=path_default,
        help="Path to the installed helper executable (default: %(default)s)",
    )
    parser.add_argument(
        "--share-dir",
        type=Path,
        default=share_default,
        help="Directory of installed ffmpeg-forgejo support files (default: %(default)s)",
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
    install_rc = check_install(path, repo)
    stray_rc = check_stray_copies(path, args.share_dir.expanduser(), repo)
    return max(install_rc, stray_rc)


if __name__ == "__main__":
    sys.exit(main())
