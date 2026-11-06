#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Run FFmpeg configure with a filtered environment; its set dump enters config.log."""

from __future__ import annotations

import argparse
import os
import subprocess  # nosec B404 - configure is a fixed path, no shell, with timeout.
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

ALLOWED = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "TMPDIR",
    "PKG_CONFIG_PATH",
    "PKG_CONFIG_LIBDIR",
    "CC",
    "CXX",
    "CFLAGS",
    "CXXFLAGS",
    "LDFLAGS",
    "AR",
    "NM",
    "RANLIB",
    "STRIP",
    "SOURCE_DATE_EPOCH",
    "CCACHE_DIR",
)
KEEPABLE: tuple[str, ...] = (
    "CPPFLAGS",
    "ASFLAGS",
    "OBJCFLAGS",
    "HOSTCFLAGS",
    "HOSTLDFLAGS",
    "HOSTCPPFLAGS",
    "LC_CTYPE",
    "TZ",
    "CCACHE_BASEDIR",
    "CCACHE_NOHASHDIR",
    "CCACHE_SLOPPINESS",
    "CCACHE_MAXSIZE",
)
MAX_KEEP = 32
TIMEOUT_S = 1800


def allowed_environment(environ: Mapping[str, str], keep: Sequence[str]) -> dict[str, str]:
    """Select bounded, explicitly approved environment names."""
    if len(keep) > MAX_KEEP:
        raise ValueError(f"at most {MAX_KEEP} --keep options are allowed")
    if any(name not in KEEPABLE for name in keep):
        raise ValueError(f"--keep accepts only: {', '.join(KEEPABLE)}")
    names = (*ALLOWED, *keep)
    return {name: environ[name] for name in names if name in environ}


def main(argv: list[str] | None = None) -> int:
    """Run configure in BUILD_DIR with a filtered environment and deadline."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep", action="append", default=[], metavar="NAME")
    parser.add_argument("source", metavar="SRC_DIR")
    parser.add_argument("build", metavar="BUILD_DIR")
    args = sys.argv[1:] if argv is None else argv
    if "--" not in args:
        print("missing -- before configure arguments", file=sys.stderr)
        return 2
    separator = args.index("--")
    parsed = parser.parse_args(args[:separator])
    try:
        environment = allowed_environment(os.environ, parsed.keep)
    except ValueError as error:
        print(error, file=sys.stderr)
        return 2
    build = Path(parsed.build)
    build.mkdir(parents=True, exist_ok=True)
    try:
        result = subprocess.run(  # nosec B603 - fixed configure path, argument list, timeout.
            [str(Path(parsed.source).resolve() / "configure"), *args[separator + 1 :]],
            cwd=build,
            env=environment,
            timeout=TIMEOUT_S,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        print(f"configure exceeded the {TIMEOUT_S} s deadline", file=sys.stderr)
        return 124
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
