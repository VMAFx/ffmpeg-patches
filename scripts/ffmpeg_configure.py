#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Run FFmpeg configure with a filtered environment; its set dump enters config.log."""

from __future__ import annotations

import argparse
import os
import re
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
MAX_KEEP = 32
SENSITIVE_NAME = re.compile(r"TOKEN|SECRET|PASSWORD|PASSWD|KEY|CREDENTIAL", re.IGNORECASE)


def allowed_environment(environ: Mapping[str, str], keep: Sequence[str]) -> dict[str, str]:
    """Select bounded, approved names and reject sensitive keep requests."""
    if len(keep) > MAX_KEEP:
        raise ValueError(f"at most {MAX_KEEP} --keep options are allowed")
    for name in keep:
        if SENSITIVE_NAME.search(name):
            raise ValueError(f"--keep refuses sensitive name {name!r}")
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
    result = subprocess.run(  # nosec B603 - fixed configure path, argument list, timeout.
        [str(Path(parsed.source).resolve() / "configure"), *args[separator + 1 :]],
        cwd=build,
        env=environment,
        timeout=1800,
        check=False,
        stdin=subprocess.DEVNULL,
    )
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
