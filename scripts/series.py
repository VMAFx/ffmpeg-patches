#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Check, apply and export the FFmpeg patch series.

    series.py check
        Lint base.env, series.txt, patches/*.patch and their metadata.
    series.py apply TREE [--expect-base] [--keep-going] [--report FILE]
        Apply the series with `git am --3way` onto the clean FFmpeg
        checkout TREE and print one status per patch.
    series.py export TREE
        Regenerate patches/ and series.txt from the commits on TREE after
        the pinned base commit.
    series.py warnings TREE LOG [--report FILE] [--base REF]
        Fail on every compiler warning in LOG that names a file the
        commits on TREE after the pinned base change; count the others.
        On a tree that applies the series to another upstream commit,
        --base names that commit.
    series.py notes VERSION REPOSITORY
        Print release notes: the pinned base, every patch with its status
        and upstream link, and how to verify and apply the release.

Exit status: 0 on success, 1 on findings or a failed apply, 2 when the
tool cannot run (unreadable series files, unusable checkout).

Every git call runs without global or system configuration, so user
settings (apply.whitespace, am.keepcr, format.*, hooks) cannot change what
is applied or exported.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE_KEYS = ("FFMPEG_REMOTE", "FFMPEG_TAG", "FFMPEG_COMMIT")
STATUSES = ("unsubmitted", "submitted", "merged", "rejected")
METADATA_KEYS = {"status", "upstream", "consumers", "origin", "notes"}
PATCH_NAME = re.compile(r"^(\d{4})-([A-Za-z0-9._-]+)\.patch$")
FROM_LINE = re.compile(r"^From [0-9a-f]{40} Mon Sep 17 00:00:00 2001$")
COMMIT = re.compile(r"^[0-9a-f]{40}$")
WARNING = re.compile(r"(^|\s)warning(\s|#|:)", re.IGNORECASE)
LOCATION = re.compile(r"^(?P<path>[^:\s]+):\d+(:\d+)?: ")
GIT_IDENTITY = ("-c", "user.name=ffmpeg-patches", "-c", "user.email=ffmpeg-patches@invalid")
# Deadline for one git call (HISS-02); a 3-way am of the largest patch takes seconds.
GIT_TIMEOUT = 600
GIT_POLICY = ("-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false")
FORMAT_PATCH = (
    "format-patch",
    "--zero-commit",
    "--full-index",
    "--no-signature",
    "--no-numbered",
)


Results = list[tuple[str, str]]


class SeriesError(Exception):
    """The series or the FFmpeg checkout is unusable; main() exits 2."""


@dataclass
class Findings:
    """Errors fail the check; warnings are printed and pass."""

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def error(self, where: str, message: str) -> None:
        self.errors.append(f"{where}: {message}")

    def warn(self, where: str, message: str) -> None:
        self.warnings.append(f"{where}: {message}")


def git_env() -> dict[str, str]:
    env = dict(os.environ)
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["LC_ALL"] = "C"
    for key in ("GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "GIT_COMMITTER_DATE"):
        env.pop(key, None)
    return env


def git(tree: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(tree), *GIT_IDENTITY, *GIT_POLICY, *args],
        env=git_env(),
        capture_output=True,
        text=True,
        check=check,
        timeout=GIT_TIMEOUT,
    )


def read_base(root: Path, findings: Findings) -> dict[str, str]:
    path = root / "base.env"
    if not path.is_file():
        findings.error("base.env", "missing")
        return {}
    values = {}
    for number, raw in enumerate(path.read_text().splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep or key not in BASE_KEYS or key in values:
            findings.error(f"base.env:{number}", f"unexpected line {line!r}")
            continue
        values[key] = value
    for key in BASE_KEYS:
        if not values.get(key):
            findings.error("base.env", f"{key} is not set")
    if values.get("FFMPEG_COMMIT") and not COMMIT.match(values["FFMPEG_COMMIT"]):
        findings.error("base.env", "FFMPEG_COMMIT is not a full 40-hex commit")
    return values


def read_series(root: Path, findings: Findings) -> list[str]:
    path = root / "series.txt"
    if not path.is_file():
        findings.error("series.txt", "missing")
        return []
    names: list[str] = []
    for number, line in enumerate(path.read_text().splitlines(), 1):
        name = line.strip()
        if not name or name.startswith("#"):
            continue
        if name in names:
            findings.error(f"series.txt:{number}", f"{name} is listed twice")
            continue
        names.append(name)
    return names


def check_patch_name(name: str, position: int, findings: Findings) -> None:
    match = PATCH_NAME.match(name)
    if not match:
        findings.error(name, "name is not NNNN-slug.patch")
    elif int(match.group(1)) != position:
        findings.error(name, f"number differs from its series position {position:04d}")


def check_patch_form(path: Path, findings: Findings) -> None:
    """Require `git format-patch` form: zero From line, headers, sign-off, diff."""
    text = path.read_text(errors="replace")
    lines = text.splitlines()
    name = path.name
    if not lines or not FROM_LINE.match(lines[0]):
        findings.error(name, "first line is not a format-patch From line (--zero-commit)")
    header, _, rest = text.partition("\n\n")
    for prefix in ("From: ", "Date: ", "Subject: [PATCH] "):
        if f"\n{prefix}" not in header:
            findings.error(name, f"header lacks '{prefix.strip()}'")
    message = rest.split("\n---\n", 1)[0]
    if "\nSigned-off-by: " not in f"\n{message}":
        findings.error(name, "commit message lacks a Signed-off-by trailer")
    if "\ndiff --git " not in text:
        findings.error(name, "no diff --git section")


def check_metadata(path: Path, findings: Findings) -> None:
    name = path.name
    try:
        data = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as exc:
        findings.error(name, f"invalid TOML: {exc}")
        return
    for key in sorted(set(data) - METADATA_KEYS):
        findings.error(name, f"unknown key {key!r}")
    status = data.get("status")
    if status not in STATUSES:
        findings.error(name, f"status must be one of {', '.join(STATUSES)}")
    upstream = data.get("upstream", "")
    if status != "unsubmitted" and not str(upstream).startswith("https://"):
        findings.error(name, f"status {status!r} needs an https upstream link")
    consumers = data.get("consumers")
    if (
        not isinstance(consumers, list)
        or not consumers
        or not all(isinstance(c, str) and c for c in consumers)
    ):
        findings.error(name, "consumers must be a non-empty list of names")
    if status == "merged":
        findings.warn(name, "merged upstream: drop it once the pinned base contains it")


def check_tree(root: Path, names: list[str], findings: Findings) -> None:
    """Every listed patch exists with metadata, and nothing unlisted lies around."""
    patches = root / "patches"
    listed = set(names)
    for position, name in enumerate(names, 1):
        check_patch_name(name, position, findings)
        path = patches / name
        if not path.is_file():
            findings.error(name, "listed in series.txt but missing from patches/")
            continue
        check_patch_form(path, findings)
        sidecar = path.with_suffix(".toml")
        if sidecar.is_file():
            check_metadata(sidecar, findings)
        else:
            findings.error(name, f"no metadata file patches/{sidecar.name}")
    for path in sorted(patches.glob("*.patch")) if patches.is_dir() else []:
        if path.name not in listed:
            findings.error(path.name, "in patches/ but not listed in series.txt")
    for path in sorted(patches.glob("*.toml")) if patches.is_dir() else []:
        if path.with_suffix(".patch").name not in listed:
            findings.error(path.name, "metadata without a listed patch")


def check(root: Path) -> Findings:
    findings = Findings()
    read_base(root, findings)
    names = read_series(root, findings)
    if not names and not findings.errors:
        findings.error("series.txt", "lists no patch")
    check_tree(root, names, findings)
    return findings


def am_in_progress(tree: Path) -> bool:
    result = git(tree, "rev-parse", "--path-format=absolute", "--git-path", "rebase-apply")
    return Path(result.stdout.strip()).exists()


def require_clean(tree: Path, base: dict[str, str], expect_base: bool) -> None:
    if git(tree, "status", "--porcelain", "--untracked-files=no").stdout.strip():
        raise SeriesError(f"{tree}: working tree has local changes")
    if am_in_progress(tree):
        raise SeriesError(f"{tree}: a git am session is in progress")
    head = git(tree, "rev-parse", "HEAD").stdout.strip()
    if expect_base and head != base["FFMPEG_COMMIT"]:
        raise SeriesError(f"{tree}: HEAD is {head}, base.env pins {base['FFMPEG_COMMIT']}")


def added_runs(text: str) -> dict[str, list[list[str]]]:
    """Map each file the patch writes to its runs of consecutive added lines."""
    runs: dict[str, list[list[str]]] = {}
    path: str | None = None
    run: list[str] = []
    for line in text.splitlines():
        if path and line.startswith("+") and not line.startswith("+++ "):
            run.append(line[1:].rstrip())
            continue
        if run and path:
            runs.setdefault(path, []).append(run)
        run = []
        if line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else None
    if run and path:
        runs.setdefault(path, []).append(run)
    return runs


def contains_run(lines: list[str], run: list[str]) -> bool:
    width = len(run)
    return any(lines[i : i + width] == run for i in range(len(lines) - width + 1))


def content_present(tree: Path, patch: Path) -> bool:
    """True when every run of added lines already stands in the tree's files.

    Upstream often lands a fix with different surrounding context, where
    neither a reverse apply nor a 3-way merge recognises it; the added
    lines themselves still match.
    """
    runs = added_runs(patch.read_text(errors="replace"))
    for path, file_runs in runs.items():
        target = tree / path
        if not target.is_file():
            return False
        lines = [line.rstrip() for line in target.read_text(errors="replace").splitlines()]
        if not all(contains_run(lines, run) for run in file_runs):
            return False
    return bool(runs)


def apply_one(tree: Path, patch: Path) -> str:
    """Apply one patch: 'applied', 'upstream' (already in the tree) or 'conflict'."""
    forward = git(tree, "apply", "--check", str(patch), check=False).returncode == 0
    if not forward and content_present(tree, patch):
        return "upstream"
    if git(tree, "am", "--3way", str(patch), check=False).returncode == 0:
        return "applied"
    if am_in_progress(tree):
        git(tree, "am", "--abort", check=False)
    return "conflict"


def apply_series(root: Path, tree: Path, expect_base: bool, keep_going: bool) -> Results:
    findings = Findings()
    base = read_base(root, findings)
    names = read_series(root, findings)
    if findings.errors:
        raise SeriesError("\n".join(findings.errors))
    require_clean(tree, base, expect_base)
    results: Results = []
    stopped = False
    for name in names:
        if stopped:
            results.append((name, "not-tried"))
            continue
        status = apply_one(tree, root / "patches" / name)
        results.append((name, status))
        stopped = status == "conflict" and not keep_going
    return results


def apply_failed(results: Results, expect_base: bool) -> bool:
    """A conflict always fails; a patch already in the pinned base is a broken series."""
    statuses = {status for _, status in results}
    return "conflict" in statuses or (expect_base and "upstream" in statuses)


def write_report(results: Results, tree: Path, report: Path | None) -> None:
    head = git(tree, "rev-parse", "--short=12", "HEAD").stdout.strip()
    rows = [f"| `{name}` | {status} |" for name, status in results]
    for name, status in results:
        print(f"{status:10} {name}")
    if report is None:
        return
    lines = [f"Series applied onto FFmpeg `{head}` (HEAD after the run).", ""]
    lines += ["| Patch | Result |", "| :-- | :-- |", *rows, ""]
    with report.open("a") as out:
        out.write("\n".join(lines) + "\n")


def slug(name: str) -> str | None:
    match = PATCH_NAME.match(Path(name).with_suffix(".patch").name)
    return match.group(2) if match else None


def renamed_sidecars(patches: Path, new_names: list[str]) -> None:
    """Carry metadata across renumbering: NNNN-slug.toml follows its slug."""
    by_slug = {slug(path.name): path for path in patches.glob("*.toml")}
    for name in new_names:
        target = (patches / name).with_suffix(".toml")
        source = by_slug.get(slug(name))
        if source and source != target and not target.exists():
            source.rename(target)
        if not target.exists():
            print(f"note: write metadata patches/{target.name}", file=sys.stderr)


def write_series(root: Path, names: list[str]) -> None:
    path = root / "series.txt"
    old = path.read_text().splitlines() if path.is_file() else []
    header = [line for line in old if line.startswith("#")]
    path.write_text("\n".join([*header, *names]) + "\n")


def pinned_commit(root: Path) -> str:
    """FFMPEG_COMMIT from base.env; an unusable base.env is a tool error."""
    findings = Findings()
    base = read_base(root, findings)
    if findings.errors:
        raise SeriesError("\n".join(findings.errors))
    return base["FFMPEG_COMMIT"]


def require_descendant(tree: Path, commit: str, message: str) -> None:
    """Raise SeriesError with MESSAGE unless HEAD of TREE descends from COMMIT."""
    if git(tree, "merge-base", "--is-ancestor", commit, "HEAD", check=False).returncode:
        raise SeriesError(f"{tree}: {message}")


def export_series(root: Path, tree: Path) -> list[str]:
    commit = pinned_commit(root)
    require_descendant(tree, commit, f"HEAD does not descend from the pinned base {commit}")
    patches = root / "patches"
    patches.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as scratch:
        git(tree, *FORMAT_PATCH, "-o", scratch, f"{commit}..HEAD")
        new = sorted(Path(scratch).glob("*.patch"))
        names = [p.name for p in new]
        for old in patches.glob("*.patch"):
            if old.name not in names:
                old.unlink()
        for path in new:
            (patches / path.name).write_bytes(path.read_bytes())
    renamed_sidecars(patches, names)
    write_series(root, names)
    return names


def series_files(root: Path, tree: Path, base: str | None = None) -> set[str]:
    """Paths the commits on TREE after BASE (default: the pinned base) change.

    BASE must be an ancestor of HEAD: on a tree that applies the series to a
    newer upstream, the pinned base would count upstream changes as the
    series' own.
    """
    if base is None:
        base = pinned_commit(root)
    resolved = git(tree, "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}", check=False)
    if resolved.returncode:
        raise SeriesError(f"{tree}: base {base!r} is not a commit")
    commit = resolved.stdout.strip()
    require_descendant(
        tree,
        commit,
        f"base {base!r} is not an ancestor of HEAD; "
        "pass --base with the upstream commit the series was applied to",
    )
    names = git(tree, "diff", "--name-only", commit, "HEAD").stdout
    return {name for name in names.splitlines() if name}


def triage_warnings(lines: list[str], files: set[str]) -> tuple[list[str], int]:
    """Split compiler warning lines into those in FILES and a count of the rest.

    A build prints a source as given to the compiler: relative to the build
    directory, through the source path, or absolute. A path matches a file
    when it is that file or ends in "/" plus it.
    """
    own: list[str] = []
    other = 0
    for line in lines:
        if not WARNING.search(line):
            continue
        match = LOCATION.match(line)
        path = match.group("path") if match else ""
        if path and any(path == f or path.endswith(f"/{f}") for f in files):
            own.append(line)
        else:
            other += 1
    return own, other


def warnings_gate(
    root: Path, tree: Path, log: Path, report: Path | None, base: str | None = None
) -> int:
    lines = log.read_text(errors="replace").splitlines()
    own, other = triage_warnings(lines, series_files(root, tree, base))
    for line in own:
        print(line)
    summary = (
        f"{len(own)} compiler warning(s) in files the series changes (gated); "
        f"{other} elsewhere in FFmpeg (counted, not gated)."
    )
    print(summary)
    if report is not None:
        with report.open("a") as out:
            out.write(summary + "\n")
    return 1 if own else 0


def patch_subject(path: Path) -> str:
    """The Subject header without its [PATCH] prefix, unfolded."""
    header = path.read_text(errors="replace").split("\n\n", 1)[0]
    match = re.search(r"^Subject: \[PATCH\] (.*(?:\n .*)*)", header, re.MULTILINE)
    return " ".join(match.group(1).split()) if match else path.name


def note_row(root: Path, number: int, name: str) -> str:
    path = root / "patches" / name
    meta = tomllib.loads(path.with_suffix(".toml").read_text())
    upstream = meta.get("upstream", "")
    link = f"[link]({upstream})" if upstream else "none"
    consumers = ", ".join(meta.get("consumers", []))
    return f"| {number:04d} | {patch_subject(path)} | {meta['status']} | {link} | {consumers} |"


def release_notes(root: Path, version: str, repository: str) -> str:
    findings = Findings()
    base = read_base(root, findings)
    names = read_series(root, findings)
    if findings.errors:
        raise SeriesError("\n".join(findings.errors))
    signer = (
        f"https://github.com/{repository}/.github/workflows/release-build.yml@refs/tags/{version}"
    )
    lines = [
        f"FFmpeg base: `{base['FFMPEG_TAG']}` (`{base['FFMPEG_COMMIT']}`).",
        "",
        "| # | Patch | Status | Upstream | Needed by |",
        "| :-- | :-- | :-- | :-- | :-- |",
        *(note_row(root, number, name) for number, name in enumerate(names, 1)),
        "",
        "Verify the release files:",
        "",
        "```bash",
        "cosign verify-blob --bundle SHA256SUMS.sigstore.json \\",
        f"  --certificate-identity '{signer}' \\",
        "  --certificate-oidc-issuer https://token.actions.githubusercontent.com SHA256SUMS",
        "sha256sum --check SHA256SUMS",
        f"gh attestation verify ffmpeg-patches-{version}.tar.gz --repo {repository}",
        "```",
        "",
        "Apply the patches in `series.txt` order with `git am --3way` onto the base, "
        "or run `scripts/series.py apply --expect-base <ffmpeg-tree>` from the tarball.",
    ]
    return "\n".join(lines) + "\n"


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--root", type=Path, default=ROOT, help="series repository root")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="lint the series and its metadata")
    apply = sub.add_parser("apply", help="apply the series onto an FFmpeg checkout")
    apply.add_argument("tree", type=Path)
    apply.add_argument("--expect-base", action="store_true", help="require HEAD == FFMPEG_COMMIT")
    apply.add_argument("--keep-going", action="store_true", help="try every patch after a conflict")
    apply.add_argument("--report", type=Path, help="append a Markdown table to this file")
    export = sub.add_parser("export", help="regenerate patches/ from an FFmpeg checkout")
    export.add_argument("tree", type=Path)
    warn = sub.add_parser("warnings", help="gate compiler warnings in the series' files")
    warn.add_argument("tree", type=Path)
    warn.add_argument("log", type=Path)
    warn.add_argument("--report", type=Path, help="append the summary to this file")
    warn.add_argument("--base", help="upstream commit under the series (default: FFMPEG_COMMIT)")
    notes = sub.add_parser("notes", help="print release notes for a tag")
    notes.add_argument("version")
    notes.add_argument("repository", help="owner/name on GitHub")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    """Exit 0 on success, 1 on findings or a failed apply, 2 when the tool cannot run."""
    args = parse_args(argv)
    try:
        return run(args)
    except (SeriesError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        detail = getattr(exc, "stderr", None) or ""
        print(f"error: {exc}\n{detail}".rstrip(), file=sys.stderr)
        return 2


def run(args: argparse.Namespace) -> int:
    if args.command == "check":
        findings = check(args.root)
        for line in findings.warnings:
            print(f"warning: {line}")
        for line in findings.errors:
            print(f"error: {line}", file=sys.stderr)
        return 1 if findings.errors else 0
    if args.command == "apply":
        results = apply_series(args.root, args.tree, args.expect_base, args.keep_going)
        write_report(results, args.tree, args.report)
        return 1 if apply_failed(results, args.expect_base) else 0
    if args.command == "notes":
        print(release_notes(args.root, args.version, args.repository), end="")
        return 0
    if args.command == "warnings":
        return warnings_gate(args.root, args.tree, args.log, args.report, args.base)
    for name in export_series(args.root, args.tree):
        print(name)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
