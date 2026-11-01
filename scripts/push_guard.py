#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Refuse pushes that revive the head branch of a closed pull request.

    push_guard.py REMOTE_NAME REMOTE_URL
        Read Git pre-push ref updates from stdin. For each branch update,
        query its newest GitHub pull request through gh. Non-GitHub remotes,
        tags and branch deletions pass without querying GitHub.

Exit status: 0 when allowed, 1 for a closed pull request's absent or unchanged
head branch, 2 when the guard cannot determine whether the push is safe.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import subprocess  # nosec B404 - fixed gh argv, no shell, explicit timeout.
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import TextIO
from urllib.parse import quote, urlsplit

MAX_UPDATES = 1000
STDIN_DEADLINE = 10
# Budget 4096 UTF-8 bytes per update across the complete input.
MAX_STDIN_BYTES = MAX_UPDATES * 4096
SCP_REMOTE = re.compile(r"(?:[^/@:\s]+@)?([A-Za-z0-9_.-]+):(.+)")
REPOSITORY_PATH = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
GITHUB_HOSTS = {"github.com", "www.github.com"}
GH_HOSTNAME = "github.com"
REMOTE_SCHEMES = {"https", "http", "ssh", "git", "git+ssh", "ssh+git"}
SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
APIReader = Callable[[str, str], str]


@dataclass(frozen=True)
class PullRequest:
    number: int
    state: str
    head_sha: str
    merged: bool


def remote_location(url: str) -> tuple[str, str]:
    """Extract a host and repository path from URL or scp remote syntax."""
    if "://" in url:
        try:
            parsed = urlsplit(url)
            host = parsed.hostname or ""
        except ValueError:
            return "", ""
        if parsed.scheme not in REMOTE_SCHEMES or parsed.query or parsed.fragment:
            return host, ""
        return host, parsed.path.removeprefix("/")
    match = SCP_REMOTE.fullmatch(url)
    return (match.group(1).lower(), match.group(2)) if match else ("", "")


def github_repository(url: str) -> str | None:
    """Extract owner/repo only from supported GitHub remote URL forms."""
    host, path = remote_location(url)
    repository = path.removesuffix("/").removesuffix(".git")
    if host in GITHUB_HOSTS and REPOSITORY_PATH.fullmatch(repository):
        return repository
    return None


def redact_remote(url: str) -> str:
    """Remove userinfo from URL and scp remotes before displaying them."""
    if "://" in url:
        parsed = urlsplit(url)
        return parsed._replace(netloc=parsed.netloc.rsplit("@", 1)[-1]).geturl()
    match = SCP_REMOTE.fullmatch(url)
    return f"{match.group(1)}:{match.group(2)}" if match else url


def read_pull_requests(repository: str, branch: str) -> str:
    """Read only the newest PR for this head, with a twenty-second deadline."""
    owner = repository.split("/", 1)[0]
    endpoint = (
        f"repos/{repository}/pulls?head={owner}:{quote(branch, safe='/')}"
        "&state=all&sort=created&direction=desc&per_page=1"
    )
    result = subprocess.run(  # nosec B603 B607 - fixed gh command; encoded head query, no shell.
        ["gh", "api", endpoint, "--hostname", GH_HOSTNAME],
        timeout=20,
        check=False,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    if result.returncode:
        raise ValueError(result.stderr.strip() or f"gh api exited {result.returncode}")
    return result.stdout


def newest_pull_request(raw: str) -> PullRequest | None:
    """Validate the API response so malformed data cannot silently allow a push."""
    payload = json.loads(raw)
    if not isinstance(payload, list):
        raise ValueError("expected a pull request list")
    if not payload:
        return None
    pr = payload[0]
    if not isinstance(pr, dict):
        raise ValueError("expected a pull request object")
    return PullRequest(*_pull_request_fields(pr))


def _pull_request_fields(pr: dict) -> tuple[int, str, str, bool]:
    """Validate the fields used to decide whether a pull request blocks a push."""
    number, state, head = pr.get("number"), pr.get("state"), pr.get("head")
    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
        raise ValueError("invalid pull request number")
    if state not in ("open", "closed"):
        raise ValueError("invalid pull request state")
    if not isinstance(head, dict):
        raise ValueError("missing pull request head")
    head_sha = head.get("sha")
    if not isinstance(head_sha, str) or not SHA.fullmatch(head_sha):
        raise ValueError("invalid pull request head SHA")
    merged_at = pr.get("merged_at")
    if merged_at is not None and not isinstance(merged_at, str):
        raise ValueError("invalid pull request merge timestamp")
    return number, state, head_sha, merged_at is not None


def check_branch(repository: str, branch: str, remote_sha: str, reader: APIReader) -> int:
    try:
        pr = newest_pull_request(reader(repository, branch))
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        print(
            f"push_guard: cannot read pull requests for {branch}: {exc}; refusing (fail closed)",
            file=sys.stderr,
        )
        return 2
    if (
        pr is not None
        and pr.state == "closed"
        and (set(remote_sha) == {"0"} or remote_sha == pr.head_sha)
    ):
        disposition = "merged" if pr.merged else "closed-unmerged"
        print(
            f"push_guard: refusing branch {branch}: PR #{pr.number} is {pr.state} ({disposition})",
            file=sys.stderr,
        )
        return 1
    return 0


def check_update(repository: str, line: str, reader: APIReader) -> int:
    _, local_sha, remote_ref, remote_sha = line.split()
    if not SHA.fullmatch(local_sha) or not SHA.fullmatch(remote_sha):
        raise ValueError("invalid pre-push SHA")
    if set(local_sha) == {"0"} or not remote_ref.startswith("refs/heads/"):
        return 0
    branch = remote_ref.removeprefix("refs/heads/")
    return check_branch(repository, branch, remote_sha, reader)


def _collect_stdin(stream: TextIO, chunks: list[str], errors: list[Exception]) -> None:
    """Read bounded chunks; read_stdin limits the caller's wait for this worker."""
    total = 0
    try:
        # Each nonempty chunk consumes at least one byte, including short reads.
        for _ in range(MAX_STDIN_BYTES + 1):
            chunk = stream.read(min(4096, MAX_STDIN_BYTES - total + 1))
            if not chunk:
                return
            total += len(chunk.encode("utf-8", errors="surrogatepass"))
            if total > MAX_STDIN_BYTES:
                raise ValueError(f"stdin exceeds {MAX_STDIN_BYTES} byte cap")
            chunks.append(chunk)
    except (OSError, ValueError) as exc:
        errors.append(exc)


def read_stdin() -> io.StringIO:
    """Collect text stdin with a portable deadline and a UTF-8 byte cap."""
    chunks: list[str] = []
    errors: list[Exception] = []
    worker = threading.Thread(target=_collect_stdin, args=(sys.stdin, chunks, errors), daemon=True)
    worker.start()
    worker.join(timeout=STDIN_DEADLINE)
    if worker.is_alive():
        raise ValueError(f"stdin deadline exceeded ({STDIN_DEADLINE} seconds)")
    if errors:
        raise errors[0]
    return io.StringIO("".join(chunks))


def main(argv: list[str], reader: APIReader = read_pull_requests) -> int:
    """Check at most MAX_UPDATES ref updates, failing closed on input or API errors."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("remote_name")
    parser.add_argument("remote_url")
    args = parser.parse_args(argv)
    repository = github_repository(args.remote_url)
    if repository is None:
        host, _ = remote_location(args.remote_url)
        if "github" in host:
            print(
                f"push_guard: cannot tell whether {redact_remote(args.remote_url)} is GitHub; "
                "refusing (fail closed)",
                file=sys.stderr,
            )
            return 2
        print("push_guard: non-GitHub remote; skipping", file=sys.stderr)
        return 0
    try:
        updates = read_stdin()
        for index in range(MAX_UPDATES + 1):
            line = updates.readline()
            if not line:
                return 0
            if index == MAX_UPDATES:
                raise ValueError(f"more than {MAX_UPDATES} pre-push updates")
            status = check_update(repository, line, reader)
            if status:
                return status
    except (OSError, ValueError) as exc:
        print(f"push_guard: invalid ref updates: {exc}; refusing (fail closed)", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
