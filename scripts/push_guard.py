#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Refuse pushes that revive a closed pull request or skip a bot review body.

At most 256 pull request lookups may start within a 120-second check deadline,
and review reads end by that deadline. Remote configuration queries share a
30-second deadline.

    push_guard.py REMOTE_NAME REMOTE_URL
        Read Git pre-push ref updates from stdin. For each branch update,
        query its GitHub pull requests through gh, newest first (fewer than
        100). Non-GitHub remotes, tags and branch deletions pass without
        querying GitHub. For each open pull request, read its reviews and
        issue comments in the pull request's base repository (at most 10
        pages of 100 each) and refuse while a bot review with a body has no
        later human review or comment.

    push_guard.py --from-config
        Check each update against every GitHub repository in configured remote
        expanded push URLs because lefthook cannot safely pass the pushed remote.
        A matching closed PR head in another repository also refuses the push
        (fail closed); rename the branch to proceed.
        Known gap: a push to a bare URL absent from all configured remotes
        is not seen in this mode.
        The guard reads PR state and reviews once before Git updates the ref,
        so it narrows but cannot close a race with a merge or a bot review
        that lands during the push.

Exit status: 0 when allowed, 1 for a closed pull request's absent or unchanged
head branch or an open pull request with an unanswered bot review body, 2 when
the guard cannot determine whether the push is safe.
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
from datetime import datetime
from typing import TextIO
from urllib.parse import quote, urlsplit

MAX_UPDATES = 1000
MAX_REPOSITORIES = 32
MAX_API_REQUESTS = 256
CHECK_DEADLINE = 120
MAX_REMOTES = 1000
MAX_CONFIG_ENTRIES = 1000
CONFIG_DEADLINE = 10
CONFIG_TOTAL_DEADLINE = 30
STDIN_DEADLINE = 10
READ_DEADLINE = 30.0
MAX_ACTIVITY_PAGES = 10
ACTIVITY_PAGE_SIZE = 100
MAX_HEAD_PULLS = 100
# Tuples, not sets: an unhashable value from the API must fail the check, not raise TypeError.
SUBMITTED_REVIEW_STATES = ("APPROVED", "CHANGES_REQUESTED", "COMMENTED", "DISMISSED")
ACCOUNT_TYPES = ("User", "Bot")
# Budget 4096 UTF-8 bytes per update across the complete input.
MAX_STDIN_BYTES = MAX_UPDATES * 4096
SCP_REMOTE = re.compile(r"(?:[^/@:\s]+@)?([A-Za-z0-9_.-]+):(.+)")
REPOSITORY_PATH = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
GITHUB_HOSTS = {"github.com", "www.github.com"}
GH_HOSTNAME = "github.com"
REMOTE_SCHEMES = {"https", "http", "ssh", "git", "git+ssh", "ssh+git"}
SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
APIReader = Callable[[str, str], str]
ActivityReader = Callable[[str, int, float], tuple[list, list]]
ConfigReader = Callable[[], dict[str, list[str]]]


@dataclass(frozen=True)
class PullRequest:
    number: int
    state: str
    head_sha: str
    merged: bool
    repository: str = ""


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


def git_remote(args: list[str], cwd: str | None, timeout: float) -> str:
    """Run a local remote query with a deadline and a named failure reason."""
    command = ["git", "remote", *args]
    label = "git remote" + (f" get-url for {args[-1]!r}" if args else "")
    try:
        result = subprocess.run(  # nosec B603 B607 - argv data, no shell.
            command,
            cwd=cwd,
            timeout=timeout,
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


def read_remote_config(
    cwd: str | None = None, clock: Callable[[], float] = time.monotonic
) -> dict[str, list[str]]:
    """Ask Git for expanded push URLs, including insteadOf and pushInsteadOf."""
    start = clock()

    def timeout() -> float:
        remaining = CONFIG_TOTAL_DEADLINE - (clock() - start)
        if remaining <= 0:
            raise ValueError(f"remote config deadline of {CONFIG_TOTAL_DEADLINE} s passed")
        return min(CONFIG_DEADLINE, remaining)

    names = remote_lines(git_remote([], cwd, timeout()), MAX_REMOTES, "remotes")
    remotes = {}
    for index in range(MAX_REMOTES):
        if index == len(names):
            break
        name = names[index]
        remotes[name] = remote_lines(
            git_remote(["get-url", "--push", "--all", name], cwd, timeout()),
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
    seen: set[str] = set()
    for index in range(MAX_CONFIG_ENTRIES):
        if index == len(urls):
            break
        repository = validated_repository(urls[index])
        if repository is not None and repository.casefold() not in seen:
            if len(seen) == MAX_REPOSITORIES:
                raise ValueError(f"more than {MAX_REPOSITORIES} GitHub repositories")
            seen.add(repository.casefold())
            repositories.append(repository)
    return repositories


def _gh_api(endpoint: str, timeout: float = 20.0) -> str:
    """Run a gh api call with fixed timeout and argument shape."""
    result = subprocess.run(  # nosec B603 B607 - fixed gh command; encoded head query, no shell.
        ["gh", "api", endpoint, "--hostname", GH_HOSTNAME],
        timeout=timeout,
        check=False,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    if result.returncode:
        raise ValueError(result.stderr.strip() or f"gh api exited {result.returncode}")
    return result.stdout


def _call_timeout(deadline: float, target: str) -> float:
    """Return a per-call timeout of at most 20 s, raising once the read deadline has passed."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError(f"read deadline passed before {target}")
    return min(remaining, 20)


def _pulls_query(repository: str, owner: str, branch: str, timeout: float = 20.0) -> str:
    """Query a repository's pull requests for one head, newest first."""
    endpoint = (
        f"repos/{repository}/pulls?head={owner}:{quote(branch, safe='/')}"
        f"&state=all&sort=created&direction=desc&per_page={MAX_HEAD_PULLS}"
    )
    return _gh_api(endpoint, timeout=timeout)


def _fork_parent(repository: str, payload: dict) -> str:
    """Extract and validate the parent repository name from a fork payload."""
    parent = payload.get("parent")
    if not isinstance(parent, dict):
        raise ValueError(f"fork {repository} has no readable parent repository")
    full_name = parent.get("full_name")
    if not isinstance(full_name, str) or not REPOSITORY_PATH.fullmatch(full_name):
        raise ValueError(f"fork {repository} has no readable parent repository")
    return full_name


def read_pull_requests(repository: str, branch: str) -> str:
    """Read the PRs for this head, newest first, checking fork parent repositories first."""
    deadline = time.monotonic() + READ_DEADLINE
    owner = repository.split("/", 1)[0]
    meta_endpoint = f"repos/{repository}"
    meta_raw = _gh_api(meta_endpoint, timeout=_call_timeout(deadline, meta_endpoint))
    meta = json.loads(meta_raw)
    if not isinstance(meta, dict) or not isinstance(meta.get("fork"), bool):
        raise ValueError(f"repository {repository} has no valid fork field")
    if not meta["fork"]:
        return _pulls_query(
            repository, owner, branch, timeout=_call_timeout(deadline, f"repos/{repository}/pulls")
        )
    parent = _fork_parent(repository, meta)
    parent_stdout = _pulls_query(
        parent, owner, branch, timeout=_call_timeout(deadline, f"repos/{parent}/pulls")
    )
    parent_list = json.loads(parent_stdout)
    if not isinstance(parent_list, list):
        raise ValueError("expected a pull request list")
    if parent_list:
        return parent_stdout
    return _pulls_query(
        repository, owner, branch, timeout=_call_timeout(deadline, f"repos/{repository}/pulls")
    )


def pull_requests(raw: str) -> list[PullRequest]:
    """Validate every PR in the response so malformed data cannot silently allow a push."""
    payload = json.loads(raw)
    if not isinstance(payload, list):
        raise ValueError("expected a pull request list")
    if len(payload) >= MAX_HEAD_PULLS:
        raise ValueError(f"a full page of {MAX_HEAD_PULLS} pull requests for one head")
    prs: list[PullRequest] = []
    for pr in payload:
        if not isinstance(pr, dict):
            raise ValueError("expected a pull request object")
        fields = _pull_request_fields(pr)
        prs.append(PullRequest(*fields, _base_repository(pr, fields[1])))
    return prs


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


def _base_repository(pr: dict, state: str) -> str:
    """Read the base repository that holds an open pull request's reviews."""
    base = pr.get("base")
    repo = base.get("repo") if isinstance(base, dict) else None
    name = repo.get("full_name") if isinstance(repo, dict) else None
    if isinstance(name, str) and REPOSITORY_PATH.fullmatch(name):
        return name
    if state == "open":
        raise ValueError("invalid pull request base repository")
    return ""


def _read_pages(endpoint: str, deadline: float) -> list:
    """Read a list endpoint page by page, failing closed past the page bound or deadline."""
    entries: list = []
    for page in range(1, MAX_ACTIVITY_PAGES + 1):
        query = f"{endpoint}?per_page={ACTIVITY_PAGE_SIZE}&page={page}"
        batch = json.loads(_gh_api(query, timeout=_call_timeout(deadline, query)))
        if not isinstance(batch, list):
            raise ValueError(f"expected a list from {endpoint}")
        entries.extend(batch)
        if len(batch) < ACTIVITY_PAGE_SIZE:
            return entries
    raise ValueError(f"more than {MAX_ACTIVITY_PAGES} pages from {endpoint}")


def read_review_activity(
    repository: str, number: int, budget: float = READ_DEADLINE
) -> tuple[list, list]:
    """Read a pull request's reviews and issue comments within one read budget in seconds."""
    deadline = time.monotonic() + min(READ_DEADLINE, budget)
    reviews = _read_pages(f"repos/{repository}/pulls/{number}/reviews", deadline)
    comments = _read_pages(f"repos/{repository}/issues/{number}/comments", deadline)
    return reviews, comments


def _author_is_bot(entry: object) -> bool:
    """Whether a review or comment author is a bot account, rejecting malformed authors."""
    user = entry.get("user") if isinstance(entry, dict) else None
    login, kind = (user.get("login"), user.get("type")) if isinstance(user, dict) else (None, None)
    if not isinstance(login, str) or not login or kind not in ACCOUNT_TYPES:
        raise ValueError("invalid review or comment author")
    return login.endswith("[bot]") or kind == "Bot"


def _entry_time(entry: dict, field: str) -> datetime:
    """Parse a timezone-aware review or comment timestamp."""
    value = entry.get(field)
    if isinstance(value, str) and value.endswith("Z"):
        # Python 3.10, the hook interpreter floor, rejects a Z suffix in fromisoformat.
        value = value[:-1] + "+00:00"
    stamp = datetime.fromisoformat(value) if isinstance(value, str) else None
    if stamp is None or stamp.tzinfo is None:
        raise ValueError(f"invalid {field} timestamp")
    return stamp


def _bot_review_id(review: dict) -> int | None:
    """Return the id of a bot review that carries a body, or None for an empty body."""
    body, review_id = review.get("body"), review.get("id")
    if body is not None and not isinstance(body, str):
        raise ValueError("invalid review body")
    if isinstance(review_id, bool) or not isinstance(review_id, int):
        raise ValueError("invalid review id")
    return review_id if body and body.strip() else None


def _review_submitted(review: dict) -> bool:
    """Whether a review was submitted; a pending draft was not, and other states are rejected."""
    state = review.get("state")
    if state == "PENDING":
        return False
    if state not in SUBMITTED_REVIEW_STATES:
        raise ValueError("invalid review state")
    return True


def unanswered_bot_reviews(reviews: list, comments: list) -> list[int]:
    """Return ids of bot review bodies that no later human review or issue comment follows."""
    answers = [_entry_time(c, "created_at") for c in comments if not _author_is_bot(c)]
    bodies: list[tuple[int, datetime]] = []
    for review in reviews:
        bot = _author_is_bot(review)
        if not _review_submitted(review):
            continue
        if not bot:
            answers.append(_entry_time(review, "submitted_at"))
            continue
        review_id = _bot_review_id(review)
        if review_id is not None:
            bodies.append((review_id, _entry_time(review, "submitted_at")))
    latest = max(answers, default=None)
    return [review_id for review_id, stamp in bodies if latest is None or latest <= stamp]


def check_reviews(branch: str, pr: PullRequest, activity: ActivityReader) -> int:
    """Refuse while a bot review body on the open pull request has no human answer."""
    try:
        unanswered = unanswered_bot_reviews(*activity(pr.repository, pr.number, READ_DEADLINE))
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        print(
            f"push_guard: cannot read reviews of PR #{pr.number} for {branch}: {exc}; "
            "refusing (fail closed)",
            file=sys.stderr,
        )
        return 2
    if unanswered:
        ids = ", ".join(str(review_id) for review_id in unanswered)
        print(
            f"push_guard: refusing branch {branch}: PR #{pr.number} in {pr.repository} has "
            f"unanswered bot review bodies ({ids}); answer them in a comment first",
            file=sys.stderr,
        )
        return 1
    return 0


def _closed_pull_request(branch: str, remote_sha: str, newest: PullRequest) -> bool:
    """Report and refuse a push that would revive the branch of the newest, closed pull request."""
    if newest.state != "closed" or (set(remote_sha) != {"0"} and remote_sha != newest.head_sha):
        return False
    disposition = "merged" if newest.merged else "closed-unmerged"
    print(
        f"push_guard: refusing branch {branch}: PR #{newest.number} is closed ({disposition})",
        file=sys.stderr,
    )
    return True


def check_branch(
    repository: str,
    branch: str,
    remote_sha: str,
    reader: APIReader,
    activity: ActivityReader = read_review_activity,
) -> int:
    """Check one pushed branch against its newest pull request and every open one."""
    try:
        prs = pull_requests(reader(repository, branch))
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        print(
            f"push_guard: cannot read pull requests for {branch}: {exc}; refusing (fail closed)",
            file=sys.stderr,
        )
        return 2
    if prs and _closed_pull_request(branch, remote_sha, prs[0]):
        return 1
    for pr in prs:
        status = check_reviews(branch, pr, activity) if pr.state == "open" else 0
        if status:
            return status
    return 0


def check_update(
    repository: str,
    line: str,
    reader: APIReader,
    activity: ActivityReader = read_review_activity,
) -> int:
    """Check one pre-push update line, skipping deletions and non-branch refs."""
    _, local_sha, remote_ref, remote_sha = line.split()
    if not SHA.fullmatch(local_sha) or not SHA.fullmatch(remote_sha):
        raise ValueError("invalid pre-push SHA")
    if set(local_sha) == {"0"} or not remote_ref.startswith("refs/heads/"):
        return 0
    branch = remote_ref.removeprefix("refs/heads/")
    return check_branch(repository, branch, remote_sha, reader, activity)


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
        _check_deadline(clock, start)
        requests += 1
        return reader(repository, branch)

    return bounded_reader


def _check_deadline(clock: Callable[[], float], start: float) -> float:
    """Raise once the shared check deadline has passed; return the seconds left."""
    left = CHECK_DEADLINE - (clock() - start)
    if left < 0:
        raise ValueError(f"check deadline of {CHECK_DEADLINE} s passed")
    return left


def bounded_activity_reader(
    reader: ActivityReader, clock: Callable[[], float], start: float
) -> ActivityReader:
    """Start no review read after the shared check deadline, and end each read by it."""

    def bounded_reader(repository: str, number: int, budget: float) -> tuple[list, list]:
        """Read the pull request's review activity within the time left before the deadline."""
        return reader(repository, number, min(budget, _check_deadline(clock, start)))

    return bounded_reader


def check_updates(
    repositories: list[str],
    reader: APIReader,
    clock: Callable[[], float] = time.monotonic,
    activity: ActivityReader = read_review_activity,
) -> int:
    """Check at most MAX_UPDATES ref updates, failing closed on input or API errors."""
    start = clock()
    bounded_reader = bounded_api_reader(reader, clock, start)
    bounded_activity = bounded_activity_reader(activity, clock, start)
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
                status = check_update(
                    repositories[repository_index], line, bounded_reader, bounded_activity
                )
                if status:
                    return status
    except (OSError, ValueError) as exc:
        print(f"push_guard: invalid ref updates: {exc}; refusing (fail closed)", file=sys.stderr)
    return 2


def main(
    argv: list[str],
    reader: APIReader = read_pull_requests,
    config_reader: ConfigReader = read_remote_config,
    activity_reader: ActivityReader = read_review_activity,
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
    return check_updates(repositories, reader, activity=activity_reader)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
