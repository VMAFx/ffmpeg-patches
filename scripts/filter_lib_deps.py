#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Find FFmpeg filters whose cross-library calls lack configure dependencies."""

from __future__ import annotations

import argparse
import collections
import os
import re
import stat
import sys
import tomllib
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

MAX_SOURCE_FILES = 4096
MAX_BYTES_PER_FILE = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024
MAX_CLOSURE_STEPS = 100_000

LIBRARIES = ("avformat", "avcodec", "swscale", "swresample")
SOURCE_SUFFIXES = {".c", ".h", ".cpp", ".cc", ".m", ".mm", ".cu", ".cl", ".S", ".asm", ".glsl"}
OBJECT_SOURCE_SUFFIXES = (".c", ".cpp", ".cc", ".m", ".mm", ".cu", ".S", ".asm")
CALL = re.compile(
    r"\b(avio_\w+|avformat_\w+|avcodec_\w+|av_packet_\w+|sws_\w+|swr_\w+|"
    r"avpriv_elbg_\w+|av_find_input_format|av_find_best_stream|av_seek_frame|"
    r"av_read_frame|av_dump_format|av_guess_\w+|av_write_\w+|av_interleaved_\w+|"
    r"av_new_packet|av_get_packet|av_append_packet|av_bsf_\w+)\s*\("
)
ASSIGNMENT = re.compile(r'^([A-Za-z0-9_]+)="([^"\n]*)"$', re.MULTILINE)
OBJECTS = re.compile(r"^OBJS-\$\(CONFIG_([A-Za-z0-9_]+)\)\s*\+=\s*(.*)$", re.MULTILINE)
OBJECT_GROUPS = re.compile(
    r"^([A-Za-z0-9_-]+)-\$\(CONFIG_[A-Za-z0-9_]+\)\s*\+=\s*(.*)$", re.MULTILINE
)
OBJECT_GROUP_REFERENCE = re.compile(r"\$\(([A-Za-z0-9_-]+)-yes\)")
OBJECT_FILE = re.compile(r"[A-Za-z0-9_./-]+\.o\b")
REGISTERED = re.compile(r"^extern const FFFilter ff_[a-z0-9]+_([a-z0-9_]+);", re.MULTILINE)
CONDITIONAL = re.compile(r'enabled\s+([a-z0-9_]+)_filter\b.*?\bprepend\s+avfilter_deps\s+"([^"]*)"')
PREPROCESSOR = re.compile(r"^\s*#\s*(if|ifdef|ifndef|elif|else|endif)\b(.*)$")


class InputError(RuntimeError):
    """Report malformed or over-bound scanner input."""


@dataclass(frozen=True)
class CallSite:
    """Identify one source call and its target library."""

    path: str
    line: int
    symbol: str
    library: str


@dataclass(frozen=True)
class ExceptionEntry:
    """Hold one validated, expiring dependency-gap exception."""

    filter_name: str
    library: str
    reason: str
    category: str
    entered: date
    expires: date


@dataclass(frozen=True)
class Gap:
    """Hold one filter/library gap at its first call site."""

    filter_name: str
    library: str
    site: CallSite
    declared: bool
    linked: bool


def _read_regular(path: Path, limit: int, label: str) -> bytes:
    """Read at most limit bytes from one regular file or raise an input error."""
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    try:
        with open(os.open(path, flags), "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise InputError(f"not a regular file: {path}")
            payload = handle.read(limit + 1)
    except OSError as error:
        raise InputError(f"cannot read {path}: {error}") from error
    if len(payload) > limit:
        raise InputError(f"{label} exceeds {limit} bytes: {path}")
    return payload


@dataclass
class ReadBudget:
    """Track bounded bytes read from one input tree."""

    total: int = 0

    def text(self, path: Path) -> str:
        """Read one bounded UTF-8 file or raise an input error."""
        payload = _read_regular(path, MAX_BYTES_PER_FILE, "file")
        self.total += len(payload)
        if self.total > MAX_TOTAL_BYTES:
            raise InputError(f"input exceeds {MAX_TOTAL_BYTES} total bytes")
        return payload.decode("utf-8", errors="replace")


def _clean(text: str) -> str:
    return re.sub(
        r'/\*.*?\*/|//[^\n]*|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'',
        lambda match: re.sub(r"[^\n]", " ", match[0]),
        text,
        flags=re.DOTALL,
    )


def _library(symbol: str) -> str:
    if symbol.startswith("sws_"):
        return "swscale"
    if symbol.startswith("swr_"):
        return "swresample"
    if symbol.startswith(("avcodec_", "av_packet_", "avpriv_elbg_", "av_bsf_")):
        return "avcodec"
    if symbol == "av_new_packet":
        return "avcodec"
    return "avformat"


def _closure(starts: list[str], declarations: dict[str, list[str]]) -> set[str]:
    seen: set[str] = set()
    work = list(starts)
    position = 0
    steps = 0
    while position < len(work):
        if steps >= MAX_CLOSURE_STEPS:
            raise InputError(f"dependency closure exceeds {MAX_CLOSURE_STEPS} steps")
        item = work[position]
        position += 1
        steps += 1
        if item in seen:
            continue
        seen.add(item)
        work.extend(declarations.get(f"{item}_deps", ()))
        work.extend(declarations.get(f"{item}_select", ()))
    return seen


def _parse_configure(text: str) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    declarations = {name: value.split() for name, value in ASSIGNMENT.findall(text)}
    conditionals: dict[str, list[str]] = collections.defaultdict(list)
    for name, libraries in CONDITIONAL.findall(text):
        conditionals[name].extend(libraries.split())
    return declarations, dict(conditionals)


def _source_paths(root: Path) -> list[Path]:
    source_root = root / "libavfilter"
    if not source_root.is_dir():
        raise InputError(f"missing directory: {source_root}")
    paths = sorted(path for path in source_root.rglob("*") if path.suffix in SOURCE_SUFFIXES)
    if len(paths) > MAX_SOURCE_FILES:
        raise InputError(f"source count exceeds {MAX_SOURCE_FILES}: {len(paths)}")
    return paths


def _read_sources(root: Path, budget: ReadBudget) -> dict[str, str]:
    sources = {}
    for path in _source_paths(root):
        relative = path.relative_to(root).as_posix()
        sources[relative] = budget.text(path)
    return sources


def _conditional_object_groups(text: str) -> dict[str, set[str]]:
    """Collect literal objects from conditional groups in one Makefile."""
    groups: dict[str, set[str]] = collections.defaultdict(set)
    # Nested NAME-yes references are intentionally not expanded.
    for name, value in OBJECT_GROUPS.findall(text):
        groups[name].update(OBJECT_FILE.findall(value))
    return dict(groups)


def _read_object_map(root: Path, budget: ReadBudget) -> dict[str, set[str]]:
    """Map configure symbols to literal and one-level grouped objects."""
    makefiles = sorted((root / "libavfilter").rglob("Makefile"))
    if root / "libavfilter" / "Makefile" not in makefiles:
        raise InputError(f"missing file: {root / 'libavfilter' / 'Makefile'}")
    object_map: dict[str, set[str]] = collections.defaultdict(set)
    for makefile in makefiles:
        text = budget.text(makefile).replace("\\\n", " ")
        groups = _conditional_object_groups(text)
        for config, value in OBJECTS.findall(text):
            object_map[config].update(OBJECT_FILE.findall(value))
            for name in OBJECT_GROUP_REFERENCE.findall(value):
                object_map[config].update(groups.get(name, ()))
    return dict(object_map)


def _object_sources(objects: set[str], sources: dict[str, str]) -> set[str]:
    paths = set()
    for object_name in objects:
        stem = f"libavfilter/{object_name[:-2]}"
        for suffix in OBJECT_SOURCE_SUFFIXES:
            candidate = f"{stem}{suffix}"
            if candidate in sources:
                paths.add(candidate)
                break
    return paths


def _fallback_sources(name: str, sources: dict[str, str]) -> set[str]:
    definition = re.compile(rf"\bconst FFFilter ff_[a-z0-9]+_{re.escape(name)}\s*=")
    return {path for path, text in sources.items() if definition.search(_clean(text))}


def _filter_sources(
    registry: str,
    declarations: dict[str, list[str]],
    object_map: dict[str, set[str]],
    sources: dict[str, str],
) -> dict[str, set[str]]:
    direct = {key[:-7].lower() for key in object_map if key.endswith("_FILTER")}
    names = direct | set(REGISTERED.findall(registry))
    ownership = {}
    for name in sorted(names):
        configs = _closure([f"{name}_filter"], declarations)
        objects = set()
        for config in configs:
            objects.update(object_map.get(config.upper(), ()))
        paths = _object_sources(objects, sources)
        if name not in direct:
            paths.update(_fallback_sources(name, sources))
        ownership[name] = paths
    return ownership


def _guard(kind: str, expression: str) -> tuple[str, str] | None:
    exact = expression.strip()
    library = re.fullmatch(r"CONFIG_(AVFORMAT|AVCODEC|SWSCALE|SWRESAMPLE)", exact)
    if kind in {"if", "ifdef"} and library:
        return "library", library.group(1).lower()
    filter_guard = re.fullmatch(r"CONFIG_([A-Z0-9_]+)_FILTER", exact)
    if kind == "if" and filter_guard:
        return "filter", filter_guard.group(1).lower()
    return None


def _line_guards(text: str, path: str) -> list[tuple[tuple[str, str] | None, ...]]:
    stack: list[tuple[str, str] | None] = []
    guards = []
    for line_number, line in enumerate(text.splitlines(), 1):
        directive = PREPROCESSOR.match(line)
        if directive:
            kind, expression = directive.groups()
            if kind in {"if", "ifdef", "ifndef"}:
                stack.append(_guard(kind, expression))
            elif kind in {"elif", "else"}:
                if not stack:
                    raise InputError(f"unmatched #{kind}: {path}:{line_number}")
                stack[-1] = _guard(kind, expression) if kind == "elif" else None
            elif not stack:
                raise InputError(f"unmatched #endif: {path}:{line_number}")
            else:
                stack.pop()
        guards.append(tuple(stack))
    if stack:
        raise InputError(f"unterminated preprocessor branch: {path}")
    return guards


def _is_guarded(guards: tuple[tuple[str, str] | None, ...], name: str, library: str) -> bool:
    for guard in guards:
        if guard == ("library", library):
            return True
        if guard and guard[0] == "filter" and guard[1] != name:
            return True
    return False


def _calls(path: str, text: str, name: str, avformat_names: set[str]) -> list[CallSite]:
    cleaned = _clean(text)
    guards = _line_guards(cleaned, path)
    calls = []
    for match in CALL.finditer(cleaned):
        symbol = match.group(1)
        if symbol.startswith(("av_write_", "av_guess_", "av_interleaved_")) and (
            symbol not in avformat_names
        ):
            continue
        line = cleaned.count("\n", 0, match.start()) + 1
        library = _library(symbol)
        if not _is_guarded(guards[line - 1], name, library):
            calls.append(CallSite(path, line, symbol, library))
    return calls


def _find_gaps(
    ownership: dict[str, set[str]],
    sources: dict[str, str],
    declarations: dict[str, list[str]],
    conditionals: dict[str, list[str]],
    avformat_names: set[str],
) -> tuple[list[Gap], int]:
    gaps = []
    call_count = 0
    for name, paths in sorted(ownership.items()):
        by_library: dict[str, list[CallSite]] = collections.defaultdict(list)
        for path in sorted(paths):
            for call in _calls(path, sources[path], name, avformat_names):
                by_library[call.library].append(call)
                call_count += 1
        declared = _closure([f"{name}_filter"], declarations)
        linked = _closure(conditionals.get(name, []), declarations)
        for library, calls in sorted(by_library.items()):
            if library not in declared or library not in linked:
                gaps.append(Gap(name, library, calls[0], library in declared, library in linked))
    return gaps, call_count


def _exception_date(value: object, field: str) -> date:
    if isinstance(value, datetime) or not isinstance(value, date):
        raise InputError(f"exception {field} must be an unquoted YYYY-MM-DD date")
    return value


def _exception_entry(raw: object, position: int) -> ExceptionEntry:
    if not isinstance(raw, dict):
        raise InputError(f"exception {position} must be a table")
    common = {"filter", "library", "reason", "category", "entered"}
    allowed = common | {"interval_days"}
    unknown = set(raw) - allowed
    if unknown:
        raise InputError(f"exception {position} unknown field: {sorted(unknown)[0]}")
    missing = common - set(raw)
    if missing:
        raise InputError(f"exception {position} missing field: {sorted(missing)[0]}")
    strings = {field: raw[field] for field in ("filter", "library", "reason", "category")}
    if any(not isinstance(value, str) or not value for value in strings.values()):
        raise InputError(f"exception {position} string fields must be non-empty")
    if strings["library"] not in LIBRARIES:
        raise InputError(f"exception {position} bad library: {strings['library']}")
    category = strings["category"]
    if category not in {"A", "B", "C", "D"}:
        raise InputError(f"exception {position} bad category: {category}")
    entered = _exception_date(raw["entered"], "entered")
    try:
        expires = entered + _exception_interval(raw, position, category)
    except OverflowError as error:
        raise InputError(f"exception {position} expiry out of range") from error
    return ExceptionEntry(
        strings["filter"],
        strings["library"],
        strings["reason"],
        category,
        entered,
        expires,
    )


def _exception_interval(raw: dict[str, object], position: int, category: str) -> timedelta:
    if category == "A":
        value = raw.get("interval_days")
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise InputError(f"exception {position} category A needs positive interval_days")
        return timedelta(days=value)
    if "interval_days" in raw:
        raise InputError(f"exception {position} interval_days is category A only")
    return timedelta(days={"B": 3, "C": 10, "D": 120}[category])


def _read_exceptions(path: Path) -> list[ExceptionEntry]:
    try:
        payload = _read_regular(path, MAX_BYTES_PER_FILE, "exceptions file")
        parsed = tomllib.loads(payload.decode("utf-8")) if payload else {}
    except InputError:
        raise
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
        raise InputError(f"cannot read exceptions {path}: {error}") from error
    unknown = set(parsed) - {"exceptions"}
    if unknown:
        raise InputError(f"exceptions file unknown field: {sorted(unknown)[0]}")
    raw_entries = parsed.get("exceptions", [])
    if not isinstance(raw_entries, list):
        raise InputError("exceptions must be an array of tables")
    entries = [_exception_entry(raw, position) for position, raw in enumerate(raw_entries, 1)]
    keys = [(entry.filter_name, entry.library) for entry in entries]
    if len(keys) != len(set(keys)):
        raise InputError("duplicate filter/library exception")
    return entries


def _report(
    gaps: list[Gap], exceptions: list[ExceptionEntry], today: date, filters: int, calls: int
) -> int:
    entries = {(entry.filter_name, entry.library): entry for entry in exceptions}
    gap_keys = {(gap.filter_name, gap.library) for gap in gaps}
    expired = [entry for entry in exceptions if entry.expires < today]
    stale = [entry for entry in exceptions if (entry.filter_name, entry.library) not in gap_keys]
    excepted = 0
    print("| filter | file:line (symbol) | library | declared | linked | verdict |")
    print("|---|---|---|---|---|---|")
    for gap in gaps:
        entry = entries.get((gap.filter_name, gap.library))
        live = entry is not None and entry.expires >= today
        verdict = (
            f"excepted ({entry.category}, expires {entry.expires.isoformat()})" if live else "gap"
        )
        excepted += int(live)
        print(
            f"| {gap.filter_name} | {gap.site.path}:{gap.site.line} ({gap.site.symbol}) | "
            f"lib{gap.library} | {'yes' if gap.declared else 'no'} | "
            f"{'yes' if gap.linked else 'no'} | {verdict} |"
        )
    print(f"filters={filters} calls={calls} gaps={len(gaps)} excepted={excepted}")
    _report_exception_errors(expired, stale)
    return int(excepted != len(gaps) or bool(expired) or bool(stale))


def _report_exception_errors(expired: list[ExceptionEntry], stale: list[ExceptionEntry]) -> None:
    for entry in expired:
        print(
            f"expired exception: {entry.filter_name} lib{entry.library} "
            f"(expired {entry.expires.isoformat()})",
            file=sys.stderr,
        )
    for entry in stale:
        print(f"stale exception: {entry.filter_name} lib{entry.library}", file=sys.stderr)


def _scan(root: Path, exceptions: list[ExceptionEntry], today: date) -> int:
    budget = ReadBudget()
    configure = budget.text(root / "configure")
    registry = budget.text(root / "libavfilter" / "allfilters.c")
    avformat_header = budget.text(root / "libavformat" / "avformat.h")
    declarations, conditionals = _parse_configure(configure)
    object_map = _read_object_map(root, budget)
    sources = _read_sources(root, budget)
    ownership = _filter_sources(registry, declarations, object_map, sources)
    avformat_names = set(re.findall(r"\b(av\w+)\s*\(", _clean(avformat_header)))
    gaps, calls = _find_gaps(ownership, sources, declarations, conditionals, avformat_names)
    return _report(gaps, exceptions, today, len(ownership), calls)


def _date_argument(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"invalid YYYY-MM-DD date: {value}") from error


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--exceptions",
        type=Path,
        default=Path(__file__).with_name("filter_lib_deps_exceptions.toml"),
    )
    parser.add_argument("--today", type=_date_argument, default=date.today())
    parser.add_argument("tree", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Scan one FFmpeg tree and return 0, 1 for gaps, or 2 for input errors."""
    try:
        arguments = _parser().parse_args(argv)
    except SystemExit as error:
        return int(error.code)
    try:
        exceptions = _read_exceptions(arguments.exceptions)
        return _scan(arguments.tree, exceptions, arguments.today)
    except InputError as error:
        print(f"filter-lib-deps: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
