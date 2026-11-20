# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Isolate GNU Make children from inherited flags and caller-selected variables."""

from __future__ import annotations

import os

_MAKE_CHANNELS = frozenset({"MAKEFLAGS", "MFLAGS", "GNUMAKEFLAGS", "MAKELEVEL", "MAKEOVERRIDES"})


def make_environment(*extra_names: str) -> dict[str, str]:
    """Copy the environment without Make flag channels or the named fixture inputs."""
    excluded = _MAKE_CHANNELS.union(extra_names)
    return {name: value for name, value in os.environ.items() if name not in excluded}
