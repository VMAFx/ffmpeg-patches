#!/usr/bin/env python3
"""List own or commented open FFmpeg threads awaiting lusoris after Fairy's word.

Reads are not atomic. A reply posted between read_from and read_to may
show as owed; a rerun clears it.
"""

import datetime
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

API = "https://code.ffmpeg.org/api/v1/repos/FFmpeg/FFmpeg"
API_HOST = "code.ffmpeg.org"
FEED = "https://code.ffmpeg.org/api/v1/users/lusoris/activities/feeds"


class RefusedURL(ValueError):
    """Identify a URL rejected before any network request."""


def get(path):
    """Decode a deadline-bound API GET, raising RefusedURL for unsafe URLs."""
    url = path if urllib.parse.urlsplit(path).scheme else API + path
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != API_HOST:
        raise RefusedURL(url)
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.load(r)


def paginated(path, max_pages=1000, older="entries"):
    """Read bounded 50-row pages, warning if the last permitted page is full."""
    result = []
    separator = "&" if "?" in path else "?"
    for page in range(1, max_pages + 1):
        batch = get(f"{path}{separator}limit=50&page={page}")
        if not isinstance(batch, list):
            raise ValueError(f"expected a list from {path}")
        result.extend(batch)
        if len(batch) < 50:
            return result
    print(
        f"warning: older {older} were not read; {path} reached {max_pages} full pages",
        file=sys.stderr,
    )
    return result


def activity_number(row):
    """Extract a matching comment's thread number; report malformed activity rows."""
    try:
        if row["op_type"] not in {"comment_issue", "comment_pull"}:
            return None
        if row["repo"]["full_name"] != "FFmpeg/FFmpeg":
            return None
        content = json.loads(row["content"])
        if (
            not isinstance(content, list)
            or len(content) != 2
            or not isinstance(content[0], str)
            or not re.fullmatch(r"[1-9][0-9]*", content[0])
            or not isinstance(content[1], str)
        ):
            raise ValueError('expected ["<number>", "<comment>"]')
        return int(content[0])
    except (KeyError, TypeError, ValueError) as error:
        print(f"warning: unparsable activity row {row!r}: {error}", file=sys.stderr)
        return None


def commented_numbers():
    """Return thread numbers from at most ten pages of lusoris' own activities."""
    rows = paginated(FEED + "?only-performed-by=true", max_pages=10, older="comments")
    return {number for row in rows if (number := activity_number(row)) is not None}


def thread_kind(issue):
    """Classify a thread as a PR only when its pull-request metadata is populated."""
    return "pr" if issue.get("pull_request") else "issue"


def _open_issue_kind(number):
    """Fetch an issue by number; return its thread kind if open, or None."""
    try:
        issue = get(f"/issues/{number}")
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
        error.close()
        return None
    if not isinstance(issue, dict):
        raise TypeError("expected a dict for issue")
    return thread_kind(issue) if issue["state"] == "open" else None


def discover_threads():
    """Dedupe own open PRs/issues and open threads found through comment activity."""
    pulls = paginated("/pulls?state=open&poster=lusoris")
    threads = {}
    for p in pulls:
        if not isinstance(p, dict):
            raise TypeError("expected a dict for pull")
        if p["user"]["login"] == "lusoris":
            threads[p["number"]] = "pr"
    issues = paginated("/issues?type=issues&state=open&created_by=lusoris")
    for issue in issues:
        if not isinstance(issue, dict):
            raise TypeError("expected a dict for issue")
        threads.setdefault(issue["number"], thread_kind(issue))
    for number in sorted(commented_numbers() - threads.keys()):
        if (kind := _open_issue_kind(number)) is not None:
            threads[number] = kind
    return threads


def _parse_time(timestamp):
    """Parse an RFC3339 timestamp into a UTC datetime."""
    dt = datetime.datetime.fromisoformat(timestamp)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=datetime.UTC)
    return dt.astimezone(datetime.UTC)


def _newest_ts(ts1, ts2):
    """Return the newer of two timestamps, or the non-empty one, or empty string."""
    if not ts1:
        return ts2
    if not ts2:
        return ts1
    return max(ts1, ts2, key=_parse_time)


def latest(items, who, key):
    """Return the newest timestamp key(i) of items by who with a non-empty body, or ""."""
    ts = [key(i) for i in items if i["user"]["login"] == who and (i.get("body") or "").strip()]
    return max(ts, key=_parse_time) if ts else ""


CLASS_RE = re.compile(
    r"^### Classification\s*\n+\s*-\s*<details><summary><b>([A-Za-z_ ]+?)\s*\u2014", re.M
)
OK_HEADINGS = {"### Classification", "### Resolved issues"}


def approve_only(body):
    """True when a Fairy review is classified plain approve and holds no finding section.

    minor_issues_approve and any other label stay listed; an unparsable body stays listed.
    """
    m = CLASS_RE.search(body or "")
    if not m or m.group(1).strip().lower() != "approve":
        return False
    return set(re.findall(r"^#{1,6}\s.*$", body, re.M)) <= OK_HEADINGS


def owed(comments, reviews):
    """Return the newest Fairy word, newest lusoris word, and whether a reply is owed."""
    if not isinstance(comments, list):
        raise TypeError("expected a list of comments")
    if not isinstance(reviews, list):
        raise TypeError("expected a list of reviews")
    words = [
        r
        for r in reviews
        if r["state"] != "REQUEST_REVIEW"
        and not (r["user"]["login"] == "Forgejo_Fairy" and approve_only(r.get("body")))
    ]
    fairy = _newest_ts(
        latest(words, "Forgejo_Fairy", lambda r: r["submitted_at"]),
        latest(comments, "Forgejo_Fairy", lambda c: c["created_at"]),
    )
    mine = _newest_ts(
        latest(words, "lusoris", lambda r: r["submitted_at"]),
        latest(comments, "lusoris", lambda c: c["created_at"]),
    )
    unanswered = bool(fairy and (not mine or _parse_time(fairy) > _parse_time(mine)))
    return fairy, mine, unanswered


def _fixture_word(who, body, timestamp):
    """Build a network-free comment or review fixture."""
    return {
        "user": {"login": who},
        "body": body,
        "created_at": timestamp,
        "submitted_at": timestamp,
        "state": "COMMENTED",
    }


def _owed_fixtures(head):
    """Return planted issue and PR turn-rule cases."""
    fairy = _fixture_word("Forgejo_Fairy", "finding", "2026-11-02T00:00:00Z")
    mine = _fixture_word("lusoris", "reply", "2026-11-01T00:00:00Z")
    newer = dict(mine, created_at="2026-11-03T00:00:00Z")
    empty = dict(fairy, body=" \n")
    approve = dict(fairy, body=head % "approve")
    minor = dict(fairy, body=head % "minor_issues_approve")
    return [
        ([mine, fairy], [], (fairy["created_at"], mine["created_at"], True)),
        ([newer, fairy], [], (fairy["created_at"], newer["created_at"], False)),
        ([empty], [], ("", "", False)),
        ([mine], [approve], ("", mine["created_at"], False)),
        ([], [minor], (minor["submitted_at"], "", True)),
        ([], [dict(fairy, state="REQUEST_REVIEW")], ("", "", False)),
        (
            [fairy, dict(mine, created_at=fairy["created_at"])],
            [],
            (fairy["created_at"], fairy["created_at"], False),
        ),
    ]


def self_test():
    """Check planted classification and turn-rule cases; return 1 on any mismatch."""
    head = "### Classification\n\n- <details><summary><b>%s \u2014 x.</b></summary>\n"
    cases = [
        (head % "approve", True),
        (head % "Approve", True),
        ("### Resolved issues\n1. ok\n" + head % "approve", True),
        (head % "minor_issues_approve", False),
        ("### Minor issues\n1. nit\n" + head % "approve", False),
        ("no classification", False),
        ("", False),
        (head % "approve" + "## Major issues\n1. bug\n", False),
        (head % "approve" + "#### Findings\n1. bug\n", False),
        (head % "approve" + "### Resolved issues\n1. ok\n", True),
    ]
    bad = [(b, want) for b, want in cases if approve_only(b) != want]
    for b, want in bad:
        print(f"self-test FAIL want={want}: {b[:60]!r}", file=sys.stderr)
    owed_bad = [
        (want, owed(comments, reviews))
        for comments, reviews, want in _owed_fixtures(head)
        if owed(comments, reviews) != want
    ]
    for want, actual in owed_bad:
        print(f"self-test FAIL owed want={want!r} actual={actual!r}", file=sys.stderr)
    return 1 if bad or owed_bad else 0


def utc_now():
    """Return the current UTC timestamp formatted as ISO 8601."""
    return datetime.datetime.now(datetime.UTC).isoformat()


def main(argv):
    """Read all review pages, print owed PR/issue counts, and return an exit status.

    Reads are not atomic. A reply posted between read_from and read_to may
    show as owed; a rerun clears it.
    """
    if argv == ["--self-test"]:
        return self_test()
    try:
        read_from = utc_now()
        threads = discover_threads()
        out = []
        for number, kind in threads.items():
            comments = get(f"/issues/{number}/comments")
            reviews = paginated(f"/pulls/{number}/reviews") if kind == "pr" else []
            fairy, mine, unanswered = owed(comments, reviews)
            if unanswered:
                out.append(f"{number}\t{kind}\tfairy={fairy}\tlusoris={mine or '-'}")
        read_to = utc_now()
    except RefusedURL as error:
        print(f"fairy_unanswered: refused URL: {error}", file=sys.stderr)
        return 1
    except (KeyError, TypeError, ValueError) as error:
        print(f"fairy_unanswered: malformed API response: {error}", file=sys.stderr)
        return 1
    except OSError as error:
        print(f"error: {error}", file=sys.stderr)
        if isinstance(error, urllib.error.HTTPError):
            error.close()
        return 1
    if out:
        print("\n".join(out))
    prs = sum(kind == "pr" for kind in threads.values())
    print(
        f"checked_prs={prs} checked_issues={len(threads) - prs} unanswered={len(out)} "
        f"read_from={read_from} read_to={read_to}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
