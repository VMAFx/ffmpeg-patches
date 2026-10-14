#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Apply each consumer's FFmpeg patch stack on top of the applied series.

    consumer_stacks.py TREE NAME=DIR [NAME=DIR ...] [--report FILE]
        TREE is an FFmpeg checkout with the series applied, clean. For each
        consumer, starting from TREE's HEAD every time, apply the patches
        DIR/series.txt lists (blank lines and # comments skipped) with
        `git am --3way`, the way the consumer's own build does, and print
        one status per patch: `applied`, `conflict`, or `in-series` (the
        patch no longer applies and its added lines already stand in the
        tree, so the consumer must retire it). TREE is left at its HEAD.

Exit status: 0 when every stack applies, 1 when a stack breaks, 2 when the
tool cannot run (unclean tree, missing or empty series.txt, missing patch).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import series

Results = list[tuple[str, str, str]]


def consumer(text: str) -> tuple[str, Path]:
    name, separator, directory = text.partition("=")
    if not separator or not name or not directory:
        raise argparse.ArgumentTypeError(f"{text!r} is not NAME=DIR")
    return name, Path(directory)


def stack(directory: Path) -> list[Path]:
    """The patches DIR/series.txt lists, in order; refuse an unusable list."""
    listing = directory / "series.txt"
    try:
        lines = listing.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise series.SeriesError(f"{listing}: {exc.strerror}") from exc
    names = [line.strip() for line in lines if line.strip() and not line.startswith("#")]
    if not names:
        raise series.SeriesError(f"{listing}: lists no patch")
    missing = [name for name in names if not (directory / name).is_file()]
    if missing:
        raise series.SeriesError(f"{listing}: listed patch missing: {', '.join(missing)}")
    return [directory / name for name in names]


def apply_stack(tree: Path, name: str, patches: list[Path]) -> Results:
    """Apply one consumer's stack; stop at the first patch that does not apply."""
    results: Results = []
    stopped = False
    for patch in patches:
        if stopped:
            results.append((name, patch.name, "not-tried"))
            continue
        status = series.apply_one(tree, patch)
        status = "in-series" if status == "upstream" else status
        results.append((name, patch.name, status))
        stopped = status != "applied"
    return results


def apply_consumers(tree: Path, consumers: list[tuple[str, Path]]) -> Results:
    series.require_clean(tree, {}, expect_base=False)
    stacks = [(name, stack(directory)) for name, directory in consumers]
    tip = series.git(tree, "rev-parse", "HEAD").stdout.strip()
    results: Results = []
    try:
        for name, patches in stacks:
            results += apply_stack(tree, name, patches)
            series.git(tree, "checkout", "-q", "-f", "--detach", tip)
    finally:
        series.git(tree, "checkout", "-q", "-f", "--detach", tip, check=False)
    return results


def write_report(results: Results, report: Path | None) -> None:
    for name, patch, status in results:
        print(f"{name}: {status:10} {patch}")
    if report is None:
        return
    rows = [f"| {name} | `{patch}` | {status} |" for name, patch, status in results]
    lines = ["Consumer stacks on top of the series.", "", "| Consumer | Patch | Result |"]
    lines += ["| :-- | :-- | :-- |", *rows, ""]
    with report.open("a", encoding="utf-8") as out:
        out.write("\n".join(lines) + "\n")


def main(argv: list[str]) -> int:
    """Exit 0 when every stack applies, 1 when a stack breaks, 2 when the tool cannot run."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("tree", type=Path)
    parser.add_argument("consumers", type=consumer, nargs="+", metavar="NAME=DIR")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    try:
        results = apply_consumers(args.tree, args.consumers)
    except (series.SeriesError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", None) or ""
        print(f"error: {exc}\n{detail}".rstrip(), file=sys.stderr)
        return 2
    write_report(results, args.report)
    broken = sorted({name for name, _, status in results if status != "applied"})
    for name in broken:
        print(f"::error title=Consumer stack broken::{name} no longer applies on the series")
    return 1 if broken else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
