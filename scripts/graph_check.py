#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Find the conversion filters FFmpeg inserted on its own into a filtergraph.

    graph_check.py GRAPHS.json [--allow FILTER]...
        GRAPHS.json is what `ffmpeg ... -print_graphs_file GRAPHS.json
        -print_graphs_format json` writes (FFmpeg 8.0 and later). Print one
        line per auto-inserted filter and refuse when there is any, except
        for the filter names given with --allow.

When two linked filters share no format, libavfilter inserts a converter named
auto_<filter>_<n> (libavfilter/avfiltergraph.c, "auto_%s_%d"), usually scale
for video and aresample for audio. On a hardware path such a converter means a
silent software conversion; the graph dump makes it visible. A filter the user
named auto_<name>_<n> with the @ syntax is reported too; --allow does not help
there, so pick another name.

Exit status: 0 when no auto-inserted filter is left, 1 when one is, 2 when the
file cannot be read as a graph dump.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# ffmpeg's graph printer prefixes every filter instance name with G<graph>_.
AUTO = re.compile(r"G[0-9]+_auto_([a-z0-9_]+)_[0-9]+")


def pads(flt: dict) -> list[dict]:
    """Input and output pads of one filter entry of the dump."""
    return list(flt.get("filter_inputs", [])) + list(flt.get("filter_outputs", []))


def formats(flt: dict, side: str) -> str:
    """The pad formats on one side ("filter_inputs" or "filter_outputs")."""
    shown = [f"{p.get('format')} {p.get('width')}x{p.get('height')}" for p in flt.get(side, [])]
    return ", ".join(shown) or "?"


def describe(flt: dict, fid: str) -> str:
    """One report line: instance, and the formats on either side of it."""
    return f"{fid}: {formats(flt, 'filter_inputs')} -> {formats(flt, 'filter_outputs')}"


def auto_filters(dump: dict) -> list[tuple[str, str]]:
    """Every auto-inserted filter of the dump as (filter name, report line)."""
    found = []
    for graph in dump["graphs"]:
        for flt in graph["filters"]:
            ids = {p["filter_id"] for p in pads(flt) if "filter_id" in p}
            for fid in sorted(ids):
                match = AUTO.fullmatch(fid)
                if match and match.group(1) == flt.get("filter_name"):
                    found.append((flt["filter_name"], describe(flt, fid)))
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("graphs", type=Path, help="JSON written by -print_graphs_file")
    parser.add_argument("--allow", action="append", default=[], metavar="FILTER")
    args = parser.parse_args(argv)
    try:
        found = auto_filters(json.loads(args.graphs.read_text()))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"graph_check: {args.graphs}: not a graph dump: {exc!r}", file=sys.stderr)
        return 2
    refused = [line for name, line in found if name not in args.allow]
    for line in refused:
        print(f"auto-inserted {line}")
    return 1 if refused else 0


if __name__ == "__main__":
    sys.exit(main())
