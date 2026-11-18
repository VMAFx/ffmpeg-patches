#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Measure whether a regression test fails when a fix commit's source paths are reverted.

Both commands run in a clean checkout of captured HEAD in detached scratch outside
the caller; PWD names scratch, absolute paths to tools are allowed, and missing
caller-only ignored or untracked inputs cause exit 3 without a record.
The module itself never writes to the caller work tree or index; the test command
is trusted and runs with the rights of the user, so it can reach the caller by
absolute path or through the shared Git directory.
Afterwards the module detects only a moved head commit of the recorded ref or
current HEAD; a record binds content (commit and tree), not provenance.
Tracked changes that Git status shows are refused, and cleanup is attempted after
every outcome the module survives; input errors mean exit 2, HEAD failure or
movement means exit 3, reverse-apply failure means exit 4, and scratch removal
failure means exit 5 and names its path; an older record is removed before runs
start, output must be outside the caller work tree and Git administration, and
the temporary parent must be outside the caller work tree.
Root fix commits are compared against the empty tree; reversing their source
patch removes the added source files in scratch, with conflicts still exiting 4.
Parents hidden by shallow history, grafts or replace refs cause exit 2 before
scratch work or old-record removal; fetch missing history or restore the parents.
Commands above upstream_lint.MAX_COMMAND_ARGS are refused with exit 2 before
scratch work; commands at the cap are accepted.
"""

from __future__ import annotations

import argparse
import json
import math
import shlex
import shutil
import sys
import tempfile
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
    """The HEAD command or caller HEAD check failed; exits with status 3."""


class _ReverseApplyError(RuntimeError):
    """The commit's source diff did not reverse-apply cleanly; exits with status 4."""


class _ScratchRemovalError(RuntimeError):
    """The scratch worktree could not be removed; exits with status 5."""


@dataclass(frozen=True, slots=True)
class ProofConfig:
    """Validated command-line options for a single revert-proof run."""

    tree: Path
    commit: str
    record: Path
    timeout: float
    command: list[str]


def git_out(tree: Path, *args: str) -> str:
    """Run bounded Git, suppressing advice in machine reads, or raise _PreflightError."""
    options = (
        ["--no-advice"]
        if args and args[0] in ("rev-parse", "diff-tree", "--no-optional-locks")
        else []
    )
    result = upstream_lint._run(["git", *options, *args], tree, timeout=GIT_TIMEOUT)
    if result.returncode != 0 or result.timed_out:
        detail = result.stdout.strip() or f"rc={result.returncode}"
        raise _PreflightError(f"git {shlex.join(args)}: {detail}")
    return result.stdout


def tracked_status(tree: Path) -> str:
    """Read tracked porcelain status without refreshing the caller index."""
    return git_out(tree, "--no-optional-locks", "status", "--porcelain", "--untracked-files=no")


def build_parser() -> argparse.ArgumentParser:
    """Construct the argument parser for revert_proof.py."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
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
    if len(command) > upstream_lint.MAX_COMMAND_ARGS:
        raise _PreflightError(
            f"command arguments exceed MAX_COMMAND_ARGS={upstream_lint.MAX_COMMAND_ARGS}: "
            f"{len(command)}"
        )
    return ProofConfig(
        tree=args.tree.resolve(),
        commit=args.commit,
        record=args.record,
        timeout=args.timeout,
        command=command,
    )


def worktree_root(tree: Path) -> Path:
    """Resolve the caller root and refuse a repository subdirectory."""
    tree = tree.resolve()
    if not tree.is_dir():
        raise _PreflightError(f"work tree not found: {tree}")
    root = Path(git_out(tree, "rev-parse", "--show-toplevel").strip()).resolve()
    if tree != root:
        raise _PreflightError(f"--tree must name the work tree root {root}: {tree}")
    return root


def prepare_tree(tree: Path, raw_commit: str) -> tuple[str, str, str, list[str], str]:
    """Refuse tracked changes or hidden parents and validate ancestry and source paths."""
    if not tree.is_dir():
        raise _PreflightError(f"work tree not found: {tree}")
    if tracked_status(tree):
        raise _PreflightError(
            f"dirty tree: uncommitted changes to tracked files that git status shows in {tree}; "
            "the proof is about the commit, not uncommitted work"
        )
    head_ref = git_out(tree, "rev-parse", "--symbolic-full-name", "HEAD").strip()
    head = git_out(tree, "rev-parse", "--verify", f"{head_ref}^{{commit}}").strip()
    head_tree = git_out(tree, "rev-parse", "--verify", f"{head}^{{tree}}").strip()
    commit = git_out(
        tree, "rev-parse", "--verify", "--end-of-options", f"{raw_commit}^{{commit}}"
    ).strip()
    try:
        history_error = upstream_lint._commit_history_error(tree, commit)
    except RuntimeError as error:
        raise _PreflightError(str(error)) from error
    if history_error:
        raise _PreflightError(history_error)
    ancestor = upstream_lint._run(
        ["git", "merge-base", "--is-ancestor", commit, head], tree, timeout=GIT_TIMEOUT
    )
    if ancestor.returncode != 0:
        raise _PreflightError(f"commit {commit} is not an ancestor of HEAD ({head})")
    names = list(
        filter(
            None,
            git_out(
                tree,
                "diff-tree",
                "--root",
                "--no-commit-id",
                "--name-only",
                "--no-renames",
                "-r",
                "-z",
                commit,
            ).split("\0"),
        )
    )
    if len(names) > upstream_lint.MAX_PATHS:
        raise _PreflightError(
            f"commit paths exceed MAX_PATHS={upstream_lint.MAX_PATHS}: {len(names)}"
        )
    source_paths = sorted(
        {path for path in names[: upstream_lint.MAX_PATHS] if path and is_source_path(path)}
    )
    if not source_paths:
        raise _PreflightError(f"commit {commit} changes no source path")
    return commit, head, head_tree, source_paths, head_ref


def run_test_command(command: list[str], tree: Path, timeout: float) -> int | str:
    """Run the test command without a shell and return its exit code or 'timeout'."""
    try:
        result = upstream_lint._run(command, tree, timeout=timeout, allow_any_executable=True)
    except OSError as error:
        return 127 if error.errno else 1
    if result.timed_out:
        return "timeout"
    return result.returncode


def verify_head_run(scratch: Path, rc_head: int | str, head: str) -> int:
    """Require a passing HEAD command that left the scratch checkout at its commit."""
    if rc_head != 0:
        raise _HeadTestError(f"test fails at head (rc={rc_head}) in clean checkout {scratch}")
    if git_out(scratch, "rev-parse", "--verify", "HEAD^{commit}").strip() != head:
        raise _HeadTestError(f"test moved scratch HEAD in {scratch}")
    if tracked_status(scratch):
        raise _HeadTestError(f"test modified tracked files at head in {scratch}")
    return 0


def scratch_parent(tree: Path) -> Path:
    """Refuse a temporary parent at or below the resolved caller root."""
    parent = Path(tempfile.gettempdir()).resolve()
    if parent.is_relative_to(tree):
        raise _PreflightError(f"temporary directory must be outside the caller tree: {parent}")
    return parent


def cleanup_git(tree: Path, failures: list[str], *args: str) -> None:
    """Collect a cleanup Git failure without skipping the remaining cleanup steps."""
    try:
        result = upstream_lint._run(["git", *args], tree, timeout=GIT_TIMEOUT)
        if result.returncode != 0 or result.timed_out:
            failures.append(
                f"git {shlex.join(args)}: {result.stdout.strip()} (rc={result.returncode})"
            )
    except (OSError, RuntimeError, KeyboardInterrupt) as error:
        failures.append(f"git {shlex.join(args)}: {error}")


def remove_scratch(tree: Path, scratch: Path) -> None:
    """Remove only owned scratch and its directory, mapping cleanup failures to 5."""
    failures: list[str] = []
    cleanup_git(tree, failures, "worktree", "remove", "--force", str(scratch))
    try:
        shutil.rmtree(scratch.parent)
    except (OSError, KeyboardInterrupt) as error:
        failures.append(f"temporary directory removal: {error}")
    if failures:
        raise _ScratchRemovalError(f"scratch removal failed for {scratch}: {'; '.join(failures)}")


def clear_record(config: ProofConfig) -> None:
    """Exclude caller work tree and Git administration before removing an old record."""
    path = config.record
    directories = [config.tree]
    for option in ("--absolute-git-dir", "--git-common-dir"):
        directory = Path(git_out(config.tree, "rev-parse", option).strip())
        directories.append((config.tree / directory).resolve())
    for directory in directories[:3]:
        if path.absolute().is_relative_to(directory) or path.resolve().is_relative_to(directory):
            raise _PreflightError(
                f"--record must be outside the caller tree and Git administration: {path}"
            )
    try:
        path.unlink(missing_ok=True)
    except OSError as error:
        raise _PreflightError(f"cannot remove old record {path}: {error}") from error


def measure_reverted(
    config: ProofConfig,
    commit: str,
    source_paths: list[str],
) -> int | str:
    """Reverse-apply only fix source paths in scratch and run the same test command."""
    patch = git_out(
        config.tree,
        "diff-tree",
        "--root",
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
    applied = upstream_lint._run(
        ["git", "apply", "-R", "-"], config.tree, data=patch, timeout=GIT_TIMEOUT
    )
    if applied.returncode != 0 or applied.timed_out:
        raise _ReverseApplyError(f"reverse apply failed for {commit}: {applied.stdout.strip()}")
    return run_test_command(config.command, config.tree, config.timeout)


def verify_caller_head(tree: Path, head: str, head_ref: str) -> None:
    """Refuse a moved or missing recorded caller branch or current HEAD with exit 3."""
    for reference in (head_ref, "HEAD"):
        result = upstream_lint._run(
            ["git", "rev-parse", "--verify", f"{reference}^{{commit}}"], tree, timeout=GIT_TIMEOUT
        )
        current = result.stdout.strip() if result.returncode == 0 else "<invalid>"
        if result.timed_out or current != head:
            raise _HeadTestError(f"HEAD moved from {head} to {current} ({reference}) in {tree}")


def run_proof(config: ProofConfig) -> None:
    """Run both measurements in detached scratch, remove it, then publish the record."""
    config = ProofConfig(
        worktree_root(config.tree), config.commit, config.record, config.timeout, config.command
    )
    commit, head, head_tree, source_paths, head_ref = prepare_tree(config.tree, config.commit)
    parent = scratch_parent(config.tree)
    clear_record(config)
    directory = Path(tempfile.mkdtemp(prefix="ffmpeg-revert-proof-", dir=parent))
    scratch = directory / "tree"
    scratch_config = ProofConfig(scratch, commit, config.record, config.timeout, config.command)
    try:
        git_out(config.tree, "worktree", "add", "--detach", str(scratch), head)
        raw_head = run_test_command(config.command, scratch, config.timeout)
        rc_head = verify_head_run(scratch, raw_head, head)
        rc_reverted = measure_reverted(scratch_config, commit, source_paths)
    finally:
        remove_scratch(config.tree, scratch)
    verify_caller_head(config.tree, head, head_ref)
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
    except _ScratchRemovalError as error:
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
