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

`handed-over` passes when a series patch declares the consumer and contains
every added-line run at the same file path, but is absent from its known --pin
NAME=TAG in --series-dir (default: this repository). It warns the consumer to
drop the patch on its next pin bump and continues the stack without changing
the tree for that patch.

Exit status: 0 when every stack applies, 1 when a stack breaks, 2 when the
tool cannot run (unclean tree, missing or empty series.txt, missing patch).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tomllib
from pathlib import Path

import series

Results = list[tuple[str, str, str]]
SERIES_DIR = Path(__file__).resolve().parent.parent
PASSING = {"applied", "handed-over"}


def consumer(text: str) -> tuple[str, Path]:
    name, separator, directory = text.partition("=")
    if not separator or not name or not directory:
        raise argparse.ArgumentTypeError(f"{text!r} is not NAME=DIR")
    # git am runs in TREE (git -C), so a relative DIR would name the wrong file.
    return name, Path(directory).resolve()


def pin(text: str) -> tuple[str, str]:
    name, separator, tag = text.partition("=")
    if not separator or not name or not tag:
        raise argparse.ArgumentTypeError(f"{text!r} is not NAME=TAG")
    return name, tag


def validate_pins(root: Path, pins: dict[str, str]) -> None:
    for name, tag in pins.items():
        if series.git(root, "cat-file", "-e", f"{tag}^{{commit}}", check=False).returncode:
            raise series.SeriesError(f"{name}: unknown series pin {tag}")


def covers_patch(text: str, runs: dict[str, list[list[str]]]) -> bool:
    added = series.added_runs(text)
    for path, file_runs in runs.items():
        if not all(
            any(series.contains_run(added_run, run) for added_run in added.get(path, []))
            for run in file_runs
        ):
            return False
    return bool(runs)


def pin_covers_patch(root: Path, tag: str, runs: dict[str, list[list[str]]]) -> bool:
    """Check every patch at the pin, regardless of its current series number."""
    paths = series.git(root, "ls-tree", "--name-only", tag, "patches/").stdout.splitlines()
    for path in paths:
        if Path(path).parent != Path("patches") or not path.endswith(".patch"):
            continue
        text = series.git(root, "show", f"{tag}:{path}").stdout
        if covers_patch(text, runs):
            return True
    return False


def handed_over(root: Path, name: str, patch: Path, tag: str | None) -> Path | None:
    """Find a declared, fully covering series patch that the consumer has not pinned."""
    if tag is None:
        return None
    runs = series.added_runs(patch.read_text(encoding="utf-8", errors="replace"))
    for candidate in sorted((root / "patches").glob("[0-9][0-9][0-9][0-9]-*.patch")):
        sidecar = candidate.with_suffix(".toml")
        if not sidecar.is_file():
            continue
        metadata = tomllib.loads(sidecar.read_text(encoding="utf-8"))
        consumers = metadata.get("consumers", [])
        if not isinstance(consumers, list) or name not in consumers:
            continue
        if not covers_patch(candidate.read_text(encoding="utf-8", errors="replace"), runs):
            continue
        return None if pin_covers_patch(root, tag, runs) else candidate
    return None


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


def apply_stack(tree: Path, name: str, patches: list[Path], root: Path, tag: str | None) -> Results:
    """Apply one consumer's stack; stop at the first patch that does not apply."""
    results: Results = []
    stopped = False
    for patch in patches:
        if stopped:
            results.append((name, patch.name, "not-tried"))
            continue
        status = series.apply_one(tree, patch)
        label = patch.name
        if status == "upstream":
            shared = handed_over(root, name, patch, tag)
            status = "handed-over" if shared else "in-series"
            if shared:
                label = f"{patch.name} -> {shared.name}"
        results.append((name, label, status))
        stopped = status not in PASSING
    return results


def apply_consumers(
    tree: Path,
    consumers: list[tuple[str, Path]],
    root: Path = SERIES_DIR,
    pins: dict[str, str] | None = None,
) -> Results:
    pins = pins or {}
    validate_pins(root, pins)
    series.require_clean(tree, {}, expect_base=False)
    stacks = [(name, stack(directory)) for name, directory in consumers]
    tip = series.git(tree, "rev-parse", "HEAD").stdout.strip()
    results: Results = []
    try:
        for name, patches in stacks:
            results += apply_stack(tree, name, patches, root, pins.get(name))
            series.git(tree, "checkout", "-q", "-f", "--detach", tip)
    finally:
        series.git(tree, "checkout", "-q", "-f", "--detach", tip, check=False)
    return results


def write_report(results: Results, report: Path | None) -> None:
    for name, patch, status in results:
        print(f"{name}: {status:10} {patch}")
        if status == "handed-over":
            print(f"::warning::{name} must drop {patch} on its next pin bump")
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
    parser.add_argument("--series-dir", type=Path, default=SERIES_DIR)
    parser.add_argument("--pin", type=pin, action="append", default=[], metavar="NAME=TAG")
    args = parser.parse_args(argv)
    try:
        results = apply_consumers(args.tree, args.consumers, args.series_dir, dict(args.pin))
    except (
        series.SeriesError,
        subprocess.SubprocessError,
        OSError,
        UnicodeDecodeError,
        tomllib.TOMLDecodeError,
    ) as exc:
        detail = getattr(exc, "stderr", None) or ""
        print(f"error: {exc}\n{detail}".rstrip(), file=sys.stderr)
        return 2
    write_report(results, args.report)
    broken = sorted({name for name, _, status in results if status not in PASSING})
    for name in broken:
        print(f"::error title=Consumer stack broken::{name} no longer applies on the series")
    return 1 if broken else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
