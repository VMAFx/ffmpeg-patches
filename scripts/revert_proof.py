#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Measure whether a regression test fails when a fix commit's source paths are reverted."""

from __future__ import annotations

import argparse
import json
import math
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path

try:
    from scripts import upstream_lint
    from scripts.upstream_lint import is_source_path
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from scripts import upstream_lint
    from scripts.upstream_lint import is_source_path

DEFAULT_TIMEOUT = 1800
MAX_TIMEOUT = 7200
GIT_TIMEOUT = 60


class _PreflightError(ValueError):
    """Input or repository state error that exits with status 2."""


class _HeadTestError(RuntimeError):
    """The test command did not exit 0 at HEAD; exits with status 3."""


class _ReverseApplyError(RuntimeError):
    """The commit's source diff did not reverse-apply cleanly; exits with status 4."""


class _RestoreError(RuntimeError):
    """The work tree could not be restored to HEAD; exits with status 5."""


@dataclass(frozen=True, slots=True)
class ProofConfig:
    """Validated command-line options for a single revert-proof run."""

    tree: Path
    commit: str
    record: Path
    timeout: float
    command: list[str]


def git_out(tree: Path, *args: str) -> str:
    """Run a bounded Git command in tree or raise _PreflightError."""
    result = upstream_lint._run(["git", *args], tree, timeout=GIT_TIMEOUT)
    if result.returncode != 0:
        detail = result.stdout.strip() or f"rc={result.returncode}"
        raise _PreflightError(f"git {shlex.join(args)}: {detail}")
    return result.stdout


def tracked_status(tree: Path) -> str:
    """Return porcelain status output for tracked files only."""
    return git_out(tree, "status", "--porcelain", "--untracked-files=no")


def build_parser() -> argparse.ArgumentParser:
    """Construct the argument parser for revert_proof.py."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tree", type=Path, required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--record", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=float(DEFAULT_TIMEOUT))
    parser.add_argument("command", nargs="*")
    return parser


def parse_config(argv: list[str] | None) -> ProofConfig:
    """Parse and validate CLI options and the test command after --."""
    raw = list(sys.argv[1:] if argv is None else argv)
    if "--" in raw:
        sep = raw.index("--")
        head_args, tail_cmd = raw[:sep], raw[sep + 1 :]
    else:
        head_args, tail_cmd = raw, []
    args = build_parser().parse_args(head_args)
    command = [*args.command, *tail_cmd]
    if not math.isfinite(args.timeout) or args.timeout <= 0 or args.timeout > MAX_TIMEOUT:
        raise _PreflightError(
            f"--timeout must be between 0 and {MAX_TIMEOUT} seconds: {args.timeout}"
        )
    if not command or not command[0]:
        raise _PreflightError("test command is empty")
    return ProofConfig(
        tree=args.tree.resolve(),
        commit=args.commit,
        record=args.record,
        timeout=args.timeout,
        command=command,
    )


def prepare_tree(tree: Path, raw_commit: str) -> tuple[str, str, str, list[str]]:
    """Verify clean tracked tree, commit ancestry and changed source paths."""
    if not tree.is_dir():
        raise _PreflightError(f"work tree not found: {tree}")
    if tracked_status(tree):
        raise _PreflightError(f"dirty tree: uncommitted changes to tracked files in {tree}")
    head = git_out(tree, "rev-parse", "--verify", "HEAD^{commit}").strip()
    head_tree = git_out(tree, "rev-parse", "--verify", "HEAD^{tree}").strip()
    commit = git_out(
        tree, "rev-parse", "--verify", "--end-of-options", f"{raw_commit}^{{commit}}"
    ).strip()
    ancestor = upstream_lint._run(
        ["git", "merge-base", "--is-ancestor", commit, head], tree, timeout=GIT_TIMEOUT
    )
    if ancestor.returncode != 0:
        raise _PreflightError(f"commit {commit} is not an ancestor of HEAD ({head})")
    names = git_out(tree, "diff-tree", "--no-commit-id", "--name-only", "-r", "-z", commit).split(
        "\0"
    )
    if len(names) > upstream_lint.MAX_PATHS:
        raise _PreflightError(f"commit paths exceed MAX_PATHS={upstream_lint.MAX_PATHS}")
    source_paths = sorted(
        {path for path in names[: upstream_lint.MAX_PATHS] if path and is_source_path(path)}
    )
    if not source_paths:
        raise _PreflightError(f"commit {commit} changes no source path")
    return commit, head, head_tree, source_paths


def run_test_command(command: list[str], tree: Path, timeout: float) -> int | str:
    """Run the test command without a shell and return its exit code or 'timeout'."""
    try:
        result = upstream_lint._run(command, tree, timeout=timeout, allow_any_executable=True)
    except OSError as error:
        return 127 if error.errno else 1
    if result.timed_out:
        return "timeout"
    return result.returncode


def restore_tree(tree: Path, absent_at_head: list[str]) -> None:
    """Restore tracked files to HEAD and verify tracked status is empty."""
    for relpath in absent_at_head[: upstream_lint.MAX_PATHS]:
        target = tree / relpath
        if target.is_file() or target.is_symlink():
            try:
                target.unlink()
            except OSError as error:
                raise _RestoreError(f"failed to restore tree {tree}: {error}") from error
    reset = upstream_lint._run(["git", "reset", "--hard", "HEAD"], tree, timeout=GIT_TIMEOUT)
    status = upstream_lint._run(
        ["git", "status", "--porcelain", "--untracked-files=no"], tree, timeout=GIT_TIMEOUT
    )
    if reset.returncode != 0 or status.returncode != 0 or status.stdout:
        raise _RestoreError(f"failed to restore tree {tree}")


def measure_reverted(
    config: ProofConfig, commit: str, head: str, source_paths: list[str]
) -> int | str:
    """Reverse-apply source paths of commit, run test command, and always restore tree."""
    patch = git_out(
        config.tree,
        "diff-tree",
        "--no-commit-id",
        "-r",
        "-p",
        "--binary",
        "--no-color",
        "--no-ext-diff",
        "--no-textconv",
        "--no-renames",
        "--src-prefix=a/",
        "--dst-prefix=b/",
        commit,
        "--",
        *source_paths,
    )
    present = set(
        filter(
            None,
            git_out(
                config.tree, "ls-tree", "--name-only", "-r", "-z", head, "--", *source_paths
            ).split("\0"),
        )
    )
    absent = [path for path in source_paths[: upstream_lint.MAX_PATHS] if path not in present]
    try:
        applied = upstream_lint._run(
            ["git", "apply", "-R", "-"], config.tree, data=patch, timeout=GIT_TIMEOUT
        )
        if applied.returncode != 0:
            raise _ReverseApplyError(f"reverse apply failed for {commit}: {applied.stdout.strip()}")
        return run_test_command(config.command, config.tree, config.timeout)
    finally:
        restore_tree(config.tree, absent)


def run_proof(config: ProofConfig) -> None:
    """Execute run 1 at HEAD, run 2 with source paths reverted, and write the record."""
    commit, head, head_tree, source_paths = prepare_tree(config.tree, config.commit)
    rc_head = run_test_command(config.command, config.tree, config.timeout)
    if rc_head != 0:
        raise _HeadTestError(f"test fails at head (rc={rc_head})")
    rc_reverted = measure_reverted(config, commit, head, source_paths)
    payload = {
        "commit": commit,
        "head": head,
        "head_tree": head_tree,
        "command": config.command,
        "rc_head": rc_head,
        "rc_reverted": rc_reverted,
        "reverted_paths": source_paths,
    }
    config.record.parent.mkdir(parents=True, exist_ok=True)
    config.record.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    """Run revert_proof CLI and map each failure class to its exit code."""
    try:
        run_proof(parse_config(argv))
    except _RestoreError as error:
        print(f"revert_proof: {error}", file=sys.stderr)
        return 5
    except _ReverseApplyError as error:
        print(f"revert_proof: {error}", file=sys.stderr)
        return 4
    except _HeadTestError as error:
        print(f"revert_proof: {error}", file=sys.stderr)
        return 3
    except (_PreflightError, OSError) as error:
        print(f"revert_proof: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
