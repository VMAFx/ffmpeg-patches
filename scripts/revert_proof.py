#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Measure whether a regression test fails when a fix commit's source paths are reverted.

Each command runs in its own clean checkout of captured HEAD in detached scratch outside
the caller; PWD names scratch, absolute paths to tools are allowed, and missing
caller-only ignored or untracked inputs cause exit 3 without a record.
The module itself never writes to the caller work tree or index; the test command
is trusted and runs with the rights of the user, so it can reach the caller by
absolute path or through the shared Git directory.
Afterwards the module detects only a moved head commit of the recorded ref or
current HEAD; a record binds content (commit and tree), not provenance.
Tracked changes that Git status shows are refused, and cleanup is attempted after
every outcome the module survives; input errors mean exit 2, HEAD failure or
movement or a command launch failure means exit 3 without a record,
reverse-apply failure means exit 4, and scratch removal
failure means exit 5 and names its path; a changed record parent or oversized
encoded record means exit 2 without publication; an older record is removed before runs
start, output must be outside the caller work tree and Git administration, and
the temporary parent must be outside the caller work tree, selected without probe writes.
Scratch checkout reads stored objects without replace refs, populates all committed
paths even for sparse callers, and suppresses setup hooks only; test commands keep hooks.
Root fix commits are compared against the empty tree; reversing their source
patch removes the added source files in scratch; conflicts or pathspecs selecting
non-source paths exit 4. Only sha1 and sha256 object formats are accepted in preflight.
Parents hidden by shallow history, grafts or replace refs cause exit 2 before
scratch work or old-record removal; fetch missing history or restore the parents.
Commands above upstream_lint.MAX_COMMAND_ARGS are refused with exit 2 before
scratch work; commands at the cap are accepted.
Non-UTF-8 filenames round-trip through filesystem surrogate escapes, stored as
JSON escapes in UTF-8 records and restored unchanged by the reader.
Human output renders lone surrogates as visible backslash-u escapes without
changing Git arguments, stdin or stored records; literal escape spellings can
look identical, so distinguish them using stored JSON or raw Git path bytes.
Diagnostics and parser messages also escape characters outside the destination
stream's encoding; broken or closed output streams retain the decided exit status.
At executable exit, a failed original stdout or stderr flush removes that stream
from interpreter cleanup; replacement streams remain owned by their callers.
Parser help, usage and errors stay plain with colour detection disabled.
Help wrapping may still query the original standard output's terminal size.
Record files use UTF-8 with ASCII JSON escapes, independently of terminal encoding.
End-to-end filename support is claimed only for UTF-8 filesystem encodings;
other filesystem encodings retain native decode/encode behavior but are unverified.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import shlex
import shutil
import sys
import tempfile
import uuid
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


def git_result(tree: Path, *args: str) -> upstream_lint.RunResult:
    """Read stored Git objects, except checkout-free registration, with checked diagnostics."""
    options = (
        ["-c", "advice.graftFileDeprecated=false"]
        if args and args[0] in ("rev-parse", "diff-tree", "--no-optional-locks")
        else []
    )
    # Worktree registration with --no-checkout reads no tree or blob content.
    if not (args[:2] == ("worktree", "add") and "--no-checkout" in args):
        options.append("--no-replace-objects")
    result = upstream_lint._run(["git", *options, *args], tree, timeout=GIT_TIMEOUT)
    if result.returncode != 0 or result.timed_out:
        detail = result.stdout.strip() or f"rc={result.returncode}"
        raise _PreflightError(f"git {shlex.join(args)}: {detail}")
    return result


def git_out(tree: Path, *args: str) -> str:
    """Run bounded Git and return decoded stdout or raise _PreflightError."""
    return git_result(tree, *args).stdout


def tracked_status(tree: Path) -> str:
    """Read tracked porcelain status without refreshing the caller index."""
    return git_out(tree, "--no-optional-locks", "status", "--porcelain", "--untracked-files=no")


def build_parser() -> argparse.ArgumentParser:
    """Construct the argument parser for revert_proof.py."""
    parser = upstream_lint.DisplayArgumentParser(
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
    root = Path(git_out(tree, "rev-parse", "--show-toplevel").removesuffix("\n")).resolve()
    if tree != root:
        raise _PreflightError(f"--tree must name the work tree root {root}: {tree}")
    return root


def _check_object_format(tree: Path) -> None:
    """Refuse unsupported object formats before old-record removal or measurements."""
    object_format = git_out(tree, "rev-parse", "--show-object-format").strip()
    if object_format not in ("sha1", "sha256"):
        raise _PreflightError(f"unsupported Git object format: {object_format}")


def prepare_tree(tree: Path, raw_commit: str) -> tuple[str, str, str, list[str], str]:
    """Validate object format, clean history, ancestry and the source path set."""
    _check_object_format(tree)
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
    return commit, head, head_tree, _source_paths(tree, commit), head_ref


def _source_paths(tree: Path, commit: str) -> list[str]:
    """Return the bounded sorted source path set changed by a validated commit."""
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
    return source_paths


def run_test_command(command: list[str], tree: Path, timeout: float) -> int | str:
    """Return a started process exit code, timeout, or a distinct launch-error sentinel."""
    try:
        result = upstream_lint._run(command, tree, timeout=timeout, allow_any_executable=True)
    except OSError:
        return "launch-error"
    if not result.started:
        return "launch-error"
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
    """Select a writable directory without probes and refuse caller-contained parents."""
    candidates = [os.environ[name] for name in ("TMPDIR", "TEMP", "TMP") if os.environ.get(name)]
    candidates.extend(("/tmp", "/var/tmp", "/usr/tmp"))
    for candidate in candidates:
        path = Path(candidate)
        if path.is_dir() and os.access(path, os.W_OK | os.X_OK):
            parent = path.resolve()
            if parent.is_relative_to(tree):
                raise _PreflightError(
                    f"temporary directory must be outside the caller tree: {parent}"
                )
            return parent
    raise _PreflightError(f"no usable temporary directory; candidates: {candidates!r}")


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


def scratch_registered(tree: Path, scratch: Path) -> bool:
    """Check exact owned scratch registration with unambiguous porcelain paths."""
    fields = git_out(tree, "worktree", "list", "--porcelain", "-z").split("\0")
    if len(fields) > upstream_lint.MAX_PATHS:
        raise _PreflightError("worktree listing exceeds MAX_PATHS")
    return f"worktree {scratch}" in fields[: upstream_lint.MAX_PATHS]


def remove_scratch(tree: Path, scratch: Path) -> None:
    """Force-remove only owned scratch, including locked registrations, mapping failures to 5."""
    failures: list[str] = []
    try:
        if scratch_registered(tree, scratch):
            cleanup_git(tree, failures, "worktree", "remove", "--force", "--force", str(scratch))
    except (_PreflightError, OSError, RuntimeError, KeyboardInterrupt) as error:
        failures.append(f"scratch registration lookup: {error}")
    try:
        shutil.rmtree(scratch.parent)
    except (OSError, KeyboardInterrupt) as error:
        failures.append(f"temporary directory removal: {error}")
    if failures:
        raise _ScratchRemovalError(f"scratch removal failed for {scratch}: {'; '.join(failures)}")


def _check_record_location(config: ProofConfig, path: Path) -> None:
    """Exclude exact caller and Git paths for both the record and its open directory."""
    directories = [config.tree]
    for option in ("--absolute-git-dir", "--git-common-dir"):
        directory = Path(git_out(config.tree, "rev-parse", option).removesuffix("\n"))
        directories.append((config.tree / directory).resolve())
    for directory in directories[:3]:
        if path.absolute().is_relative_to(directory) or path.resolve().is_relative_to(directory):
            raise _PreflightError(
                f"--record must be outside the caller tree and Git administration: {path}"
            )


def clear_record(config: ProofConfig, descriptor: int | None = None) -> None:
    """Remove an old record by name through its validated directory descriptor."""
    path = config.record
    _check_record_location(config, path)
    with contextlib.ExitStack() as resources:
        if descriptor is None:
            descriptor = _open_record_directory(config)
            resources.callback(os.close, descriptor)
        try:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(path.name, dir_fd=descriptor)
        except OSError as error:
            raise _PreflightError(f"cannot remove old record {path}: {error}") from error


def _open_record_directory(config: ProofConfig) -> int:
    """Require directory-relative operations, then pin and validate the output directory."""
    # CPython lists rename rather than its replace alias in supports_dir_fd.
    if not {os.open, os.unlink} <= os.supports_dir_fd or not (
        {os.rename, os.replace} & os.supports_dir_fd
    ):
        raise _PreflightError(
            "record write requires dir_fd support for os.open and os.replace and os.unlink"
        )
    config.record.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(config.record.parent, flags)
    try:
        resolved = Path(os.path.realpath(config.record.parent))
        opened, named = os.fstat(descriptor), os.stat(resolved)
        if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
            raise _PreflightError("record directory changed while opening")
        _check_record_location(config, resolved)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _verify_record_directory(config: ProofConfig, descriptor: int) -> None:
    """Require both the requested parent and its resolved path to name the pinned directory."""
    parent = config.record.parent
    try:
        opened = os.fstat(descriptor)
        expected = (opened.st_dev, opened.st_ino)
        for named in (os.lstat(parent), os.stat(os.path.realpath(parent))):
            if (named.st_dev, named.st_ino) != expected:
                raise _PreflightError(f"record directory changed: {parent}")
    except OSError as error:
        raise _PreflightError(f"record directory changed: {parent}: {error}") from error


def _write_record(descriptor: int, name: str, payload: dict[str, object]) -> None:
    """Bound encoded JSON before publishing through the pinned directory; clean failed writes."""
    encoded = (json.dumps(payload, indent=2) + "\n").encode("utf-8")
    if len(encoded) > upstream_lint.MAX_RECORD_BYTES:
        raise _PreflightError(f"record exceeds MAX_RECORD_BYTES={upstream_lint.MAX_RECORD_BYTES}")
    temporary = f".revert-proof-{uuid.uuid4().hex}.tmp"
    file_descriptor = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=descriptor
    )
    try:
        with os.fdopen(file_descriptor, "wb") as stream:
            stream.write(encoded)
        os.replace(temporary, name, src_dir_fd=descriptor, dst_dir_fd=descriptor)
    except NotImplementedError as error:
        raise _PreflightError("record write requires dir_fd support for os.replace") from error
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=descriptor)


def _source_patch(
    config: ProofConfig, commit: str, source_paths: list[str]
) -> upstream_lint.RunResult:
    """Reject extra paths selected by recursive literal pathspecs before building the patch."""
    options = ["diff-tree", "--root", "--no-commit-id", "-r", "--no-renames"]
    selection = [commit, "--", *(f":(literal){p}" for p in source_paths[: upstream_lint.MAX_PATHS])]
    names = git_out(config.tree, *options, "--name-only", "-z", *selection).split("\0")
    extras = sorted(set(filter(None, names)) - set(source_paths))
    if extras:
        raise _ReverseApplyError(
            f"reverse patch includes paths outside source_paths: {extras[:10]!r} "
            f"({len(extras)} extra path(s))"
        )
    return git_result(
        config.tree,
        *options,
        "-p",
        "--binary",
        "--no-color",
        "--no-ext-diff",
        "--no-textconv",
        "--src-prefix=a/",
        "--dst-prefix=b/",
        *selection,
    )


def measure_reverted(
    config: ProofConfig,
    commit: str,
    source_paths: list[str],
) -> int | str:
    """Validate the exact reverse patch path set, apply it and run the same command."""
    patch = _source_patch(config, commit, source_paths)
    applied = upstream_lint._run(
        ["git", "apply", "--whitespace=nowarn", "--no-ignore-whitespace", "-R", "-"],
        config.tree,
        data=patch.stdout_bytes,
        timeout=GIT_TIMEOUT,
    )
    if applied.returncode != 0 or applied.timed_out:
        raise _ReverseApplyError(f"reverse apply failed for {commit}: {applied.stdout.strip()}")
    result = run_test_command(config.command, config.tree, config.timeout)
    if result == "launch-error":
        raise _HeadTestError("test launch-error on reverted checkout (no verdict)")
    return result


def verify_caller_head(tree: Path, head: str, head_ref: str) -> None:
    """Refuse a moved or missing recorded caller branch or current HEAD with exit 3."""
    for reference in (head_ref, "HEAD"):
        result = upstream_lint._run(
            ["git", "rev-parse", "--verify", f"{reference}^{{commit}}"], tree, timeout=GIT_TIMEOUT
        )
        current = result.stdout.strip() if result.returncode == 0 else "<invalid>"
        if result.timed_out or current != head:
            raise _HeadTestError(f"HEAD moved from {head} to {current} ({reference}) in {tree}")


def measure_scratch(
    config: ProofConfig,
    head: str,
    commit: str | None = None,
    source_paths: list[str] | None = None,
) -> int | str:
    """Measure in a full stored-object checkout with setup hooks disabled and always clean it."""
    directory = Path(
        tempfile.mkdtemp(prefix="ffmpeg-revert-proof-", dir=scratch_parent(config.tree))
    )
    scratch = directory / "tree"
    scratch_config = ProofConfig(
        scratch, config.commit, config.record, config.timeout, config.command
    )
    try:
        git_out(config.tree, "worktree", "add", "--detach", "--no-checkout", str(scratch), head)
        hooks = directory / "empty-hooks"
        hooks.mkdir()
        git_out(
            scratch,
            "-c",
            f"core.hooksPath={hooks}",
            "-c",
            "core.sparseCheckout=false",
            "-c",
            "core.sparseCheckoutCone=false",
            "-c",
            "index.sparse=false",
            "reset",
            "--hard",
            head,
        )
        if commit is not None:
            return measure_reverted(scratch_config, commit, source_paths or [])
        result = run_test_command(config.command, scratch, config.timeout)
        return verify_head_run(scratch, result, head)
    finally:
        remove_scratch(config.tree, scratch)


def run_proof(config: ProofConfig) -> None:
    """Remove the old record through a pinned parent and publish only if it stays named."""
    config = ProofConfig(
        worktree_root(config.tree), config.commit, config.record, config.timeout, config.command
    )
    commit, head, head_tree, source_paths, head_ref = prepare_tree(config.tree, config.commit)
    scratch_parent(config.tree)
    _check_record_location(config, config.record)
    descriptor = _open_record_directory(config)
    try:
        clear_record(config, descriptor)
        rc_head = measure_scratch(config, head)
        rc_reverted = measure_scratch(config, head, commit, source_paths)
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
        _verify_record_directory(config, descriptor)
        _write_record(descriptor, config.record.name, payload)
    finally:
        os.close(descriptor)


def _error_exit(error: Exception, status: int) -> int:
    """Display producer diagnostics safely while retaining their failure status."""
    upstream_lint.emit_text(f"revert_proof: {error}\n", sys.stderr)
    return status


def main(argv: list[str] | None = None) -> int:
    """Run revert_proof CLI and map each failure class to its exit code."""
    try:
        run_proof(parse_config(argv))
    except _ScratchRemovalError as error:
        return _error_exit(error, 5)
    except _ReverseApplyError as error:
        return _error_exit(error, 4)
    except _HeadTestError as error:
        return _error_exit(error, 3)
    except (_PreflightError, OSError, UnicodeError) as error:
        return _error_exit(error, 2)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        upstream_lint.finish_process_output()
