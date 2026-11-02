#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Refuse pushes that revive the head branch of a closed pull request.

At most 256 pull request lookups may start within a 120-second check deadline.

    push_guard.py REMOTE_NAME REMOTE_URL
        Read Git pre-push ref updates from stdin. For each branch update,
        query its newest GitHub pull request through gh. Non-GitHub remotes,
        tags and branch deletions pass without querying GitHub.

    push_guard.py --from-config
        Check each update against every GitHub repository in configured remote
        expanded push URLs because lefthook cannot safely pass the pushed remote.
        A matching closed PR head in another repository also refuses the push
        (fail closed); rename the branch to proceed.
        Known gap: a push to a bare URL absent from all configured remotes
        is not seen in this mode.
        The guard reads PR state once before Git updates the ref, so it narrows
        but cannot close a race with a merge that happens during the push.

Exit status: 0 when allowed, 1 for a closed pull request's absent or unchanged
head branch, 2 when the guard cannot determine whether the push is safe.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import subprocess  # nosec B404 - fixed git/gh argv, no shell, explicit timeout.
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TextIO
from urllib.parse import quote, urlsplit

MAX_UPDATES = 1000
MAX_REPOSITORIES = 32
MAX_API_REQUESTS = 256
CHECK_DEADLINE = 120
MAX_REMOTES = 1000
MAX_CONFIG_ENTRIES = 1000
CONFIG_DEADLINE = 10
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
ConfigReader = Callable[[], dict[str, list[str]]]


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


def git_remote(args: list[str], cwd: str | None) -> str:
    """Run a local remote query with a deadline and a named failure reason."""
    command = ["git", "remote", *args]
    label = "git remote" + (f" get-url for {args[-1]!r}" if args else "")
    try:
        result = subprocess.run(  # nosec B603 B607 - argv data, no shell.
            command,
            cwd=cwd,
            timeout=CONFIG_DEADLINE,
            check=False,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f"{label} failed: {type(exc).__name__}") from exc
    if result.returncode:
        raise ValueError(f"{label} exited {result.returncode}")
    return result.stdout


def remote_lines(raw: str, limit: int, label: str) -> list[str]:
    """Parse bounded newline records without stripping URL or name whitespace."""
    lines = raw.removesuffix("\n").split("\n", limit) if raw else []
    if len(lines) > limit:
        raise ValueError(f"more than {limit} {label}")
    return lines


def read_remote_config(cwd: str | None = None) -> dict[str, list[str]]:
    """Ask Git for expanded push URLs, including insteadOf and pushInsteadOf."""
    names = remote_lines(git_remote([], cwd), MAX_REMOTES, "remotes")
    remotes = {}
    for index in range(MAX_REMOTES):
        if index == len(names):
            break
        name = names[index]
        remotes[name] = remote_lines(
            git_remote(["get-url", "--push", "--all", name], cwd),
            MAX_CONFIG_ENTRIES,
            "remote push URLs",
        )
    return remotes


def validated_repository(url: str) -> str | None:
    """Reject ambiguous GitHub hosts rather than skipping their updates."""
    repository = github_repository(url)
    host, _ = remote_location(url)
    if repository is None and "github" in host:
        raise ValueError(f"cannot tell whether {redact_remote(url)} is GitHub")
    return repository


def config_repositories(reader: ConfigReader) -> list[str]:
    """Collect distinct repositories from bounded, expanded remote push URLs."""
    remotes = reader()
    if len(remotes) > MAX_REMOTES:
        raise ValueError(f"more than {MAX_REMOTES} remotes")
    urls: list[str] = []
    entries = iter(remotes.values())
    for _ in range(MAX_REMOTES):
        remote_urls = next(entries, None)
        if remote_urls is None:
            break
        if len(urls) + len(remote_urls) > MAX_CONFIG_ENTRIES:
            raise ValueError(f"more than {MAX_CONFIG_ENTRIES} remote push URLs")
        urls.extend(remote_urls)
    repositories: list[str] = []
    for index in range(MAX_CONFIG_ENTRIES):
        if index == len(urls):
            break
        repository = validated_repository(urls[index])
        if repository is not None and repository not in repositories:
            if len(repositories) == MAX_REPOSITORIES:
                raise ValueError(f"more than {MAX_REPOSITORIES} GitHub repositories")
            repositories.append(repository)
    return repositories


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


def parse_arguments(argv: list[str]) -> argparse.Namespace:
    """Accept either config mode or the original pair of Git arguments."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--from-config", action="store_true")
    parser.add_argument("remote_name", nargs="?")
    parser.add_argument("remote_url", nargs="?")
    args = parser.parse_args(argv)
    if args.from_config:
        if args.remote_name is not None or args.remote_url is not None:
            parser.error("--from-config cannot be combined with remote arguments")
    elif args.remote_name is None or args.remote_url is None:
        parser.error("provide --from-config or both REMOTE_NAME and REMOTE_URL")
    return args


def bounded_api_reader(reader: APIReader, clock: Callable[[], float], start: float) -> APIReader:
    """Limit actual API calls across all updates and repositories."""
    requests = 0

    def bounded_reader(repository: str, branch: str) -> str:
        nonlocal requests
        if requests >= MAX_API_REQUESTS:
            raise ValueError(f"more than {MAX_API_REQUESTS} pull request lookups")
        if clock() - start > CHECK_DEADLINE:
            raise ValueError(f"check deadline of {CHECK_DEADLINE} s passed")
        requests += 1
        return reader(repository, branch)

    return bounded_reader


def check_updates(
    repositories: list[str], reader: APIReader, clock: Callable[[], float] = time.monotonic
) -> int:
    """Check at most MAX_UPDATES ref updates, failing closed on input or API errors."""
    bounded_reader = bounded_api_reader(reader, clock, clock())
    try:
        if len(repositories) > MAX_REPOSITORIES:
            raise ValueError(f"more than {MAX_REPOSITORIES} GitHub repositories")
        updates = read_stdin()
        for index in range(MAX_UPDATES + 1):
            line = updates.readline()
            if not line:
                return 0
            if index == MAX_UPDATES:
                raise ValueError(f"more than {MAX_UPDATES} pre-push updates")
            for repository_index in range(MAX_REPOSITORIES):
                if repository_index == len(repositories):
                    break
                status = check_update(repositories[repository_index], line, bounded_reader)
                if status:
                    return status
    except (OSError, ValueError) as exc:
        print(f"push_guard: invalid ref updates: {exc}; refusing (fail closed)", file=sys.stderr)
    return 2


def main(
    argv: list[str],
    reader: APIReader = read_pull_requests,
    config_reader: ConfigReader = read_remote_config,
) -> int:
    """Resolve repositories without shell interpolation, then check stdin once."""
    args = parse_arguments(argv)
    try:
        if args.from_config:
            repositories = config_repositories(config_reader)
        else:
            repository = validated_repository(args.remote_url)
            repositories = [repository] if repository is not None else []
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        reason = "cannot read remote configuration: " if args.from_config else ""
        print(f"push_guard: {reason}{exc}; refusing (fail closed)", file=sys.stderr)
        return 2
    if not repositories:
        print("push_guard: non-GitHub remote; skipping", file=sys.stderr)
        return 0
    return check_updates(repositories, reader)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
