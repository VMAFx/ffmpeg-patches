#!/usr/bin/env python3
"""List own open FFmpeg PRs whose newest Fairy review/comment with a body is newer than lusoris' newest word."""
import json, re, sys, urllib.request
API = "https://code.ffmpeg.org/api/v1/repos/FFmpeg/FFmpeg"
def get(path):
    """Return the decoded JSON of one Forgejo API GET below the FFmpeg repository."""
    with urllib.request.urlopen(API + path, timeout=30) as r:
        return json.load(r)
def latest(items, who, key):
    """Return the newest timestamp key(i) of items by who with a non-empty body, or ""."""
    ts = [key(i) for i in items if i["user"]["login"] == who and (i.get("body") or "").strip()]
    return max(ts) if ts else ""
CLASS_RE = re.compile(r"^### Classification\s*\n+\s*-\s*<details><summary><b>([A-Za-z_ ]+?)\s*\u2014", re.M)
OK_HEADINGS = {"### Classification", "### Resolved issues"}

def approve_only(body):
    """True when a Fairy review is classified plain approve and holds no finding section.

    minor_issues_approve and any other label stay listed; an unparsable body stays listed.
    """
    m = CLASS_RE.search(body or "")
    if not m or m.group(1).strip().lower() != "approve":
        return False
    return set(re.findall(r"^### .*$", body, re.M)) <= OK_HEADINGS

def self_test():
    """Check approve_only() against planted bodies; return 1 on any mismatch, else 0."""
    head = "### Classification\n\n- <details><summary><b>%s \u2014 x.</b></summary>\n"
    cases = [(head % "approve", True), (head % "Approve", True),
             ("### Resolved issues\n1. ok\n" + head % "approve", True),
             (head % "minor_issues_approve", False),
             ("### Minor issues\n1. nit\n" + head % "approve", False),
             ("no classification", False), ("", False)]
    bad = [(b, want) for b, want in cases if approve_only(b) != want]
    for b, want in bad:
        print(f"self-test FAIL want={want}: {b[:60]!r}", file=sys.stderr)
    return 1 if bad else 0

def main(argv):
    """Print owed PR lines; return the process exit status."""
    if argv == ["--self-test"]:
        return self_test()
    pulls = []
    for page in range(1, 6):
        batch = get(f"/pulls?state=open&poster=lusoris&limit=50&page={page}")
        pulls += [p for p in batch if p["user"]["login"] == "lusoris"]
        if len(batch) < 50:
            break
    out = []
    for p in pulls:
        n = p["number"]
        revs = [r for r in get(f"/pulls/{n}/reviews") if r["state"] != "REQUEST_REVIEW"
                and not (r["user"]["login"] == "Forgejo_Fairy" and approve_only(r.get("body")))]
        coms = get(f"/issues/{n}/comments")
        fairy = max(latest(revs, "Forgejo_Fairy", lambda r: r["submitted_at"]),
                    latest(coms, "Forgejo_Fairy", lambda c: c["created_at"]))
        mine = max(latest(revs, "lusoris", lambda r: r["submitted_at"]),
                   latest(coms, "lusoris", lambda c: c["created_at"]))
        if fairy and fairy > mine:
            out.append(f"{n}\tfairy={fairy}\tlusoris={mine or '-'}")
    print("\n".join(out))
    print(f"checked={len(pulls)} unanswered={len(out)}", file=sys.stderr)
    return 0

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
