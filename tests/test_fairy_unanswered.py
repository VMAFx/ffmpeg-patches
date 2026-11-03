# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Network-free fixtures for Fairy turn detection and Forgejo discovery."""

import contextlib
import io
import json
import unittest.mock
import urllib.error

from scripts import fairy_unanswered as fairy

FEED = "https://code.ffmpeg.org/api/v1/users/lusoris/activities/feeds"
OLD = "2026-11-01T00:00:00Z"
NEW = "2026-11-02T00:00:00Z"


def _activity(number, op="comment_issue"):
    """Build an activity-feed comment row."""
    return {
        "op_type": op,
        "repo": {"full_name": "FFmpeg/FFmpeg"},
        "content": json.dumps([str(number), "reply"]),
    }


def _word(who="Forgejo_Fairy", timestamp=NEW):
    """Build a comment or review with a body and both timestamp fields."""
    return {
        "user": {"login": who},
        "body": "finding",
        "created_at": timestamp,
        "submitted_at": timestamp,
        "state": "COMMENTED",
    }


def _routes():
    """Build mixed own and commented threads, including stale and missing rows."""
    return {
        "/pulls?state=open&poster=lusoris&limit=50&page=1": [
            {"number": 1, "user": {"login": "lusoris"}},
            {"number": 99, "user": {"login": "other"}},
        ],
        "/issues?type=issues&state=open&created_by=lusoris&limit=50&page=1": [{"number": 2}],
        FEED + "?only-performed-by=true&limit=50&page=1": [
            _activity(1),
            _activity(2),
            _activity(3),
            _activity(3),
            _activity(4, "comment_pull"),
            _activity(5),
            _activity(6),
        ],
        "/issues/3": {"number": 3, "state": "open"},
        "/issues/4": {"number": 4, "state": "open", "pull_request": {"merged": False}},
        "/issues/5": {"number": 5, "state": "closed"},
        "/issues/6": urllib.error.HTTPError("fixture", 404, "missing", {}, io.BytesIO()),
        "/issues/1/comments": [_word(timestamp=OLD), _word("lusoris")],
        "/issues/2/comments": [_word()],
        "/issues/3/comments": [_word()],
        "/issues/4/comments": [],
        "/pulls/1/reviews": [],
        "/pulls/4/reviews": [_word()],
    }


class FairyUnansweredTest(unittest.TestCase):
    """Exercise public behavior with network access forbidden."""

    def setUp(self):
        """Fail immediately if a fixture accidentally attempts network I/O."""
        network = unittest.mock.patch.object(
            fairy.urllib.request, "urlopen", side_effect=AssertionError("network forbidden")
        )
        network.start()
        self.addCleanup(network.stop)

    def test_self_test(self):
        """All planted fixtures pass through both supported entry points."""
        self.assertEqual(fairy.self_test(), 0)
        self.assertEqual(fairy.main(["--self-test"]), 0)

    def test_owed_always_false_mutation_fails(self):
        """Suppressing every owed thread must be detected by a planted fixture."""
        errors = io.StringIO()
        with (
            unittest.mock.patch.object(fairy, "owed", return_value=("", "", False)),
            contextlib.redirect_stderr(errors),
        ):
            self.assertEqual(fairy.self_test(), 1)
        self.assertIn("self-test FAIL owed", errors.getvalue())

    def test_mixed_thread_discovery(self):
        """Dedupe own and commented threads; skip closed/404; review only PRs."""
        routes = _routes()

        def fetch(path):
            """Serve only expected fixture endpoints."""
            response = routes[path]
            if isinstance(response, Exception):
                raise response
            return response

        output, errors = io.StringIO(), io.StringIO()
        with (
            unittest.mock.patch.object(fairy, "get", side_effect=fetch) as request,
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(errors),
        ):
            self.assertEqual(fairy.main([]), 0)
        self.assertEqual(
            output.getvalue(),
            f"2\tissue\tfairy={NEW}\tlusoris=-\n"
            f"3\tissue\tfairy={NEW}\tlusoris=-\n"
            f"4\tpr\tfairy={NEW}\tlusoris=-\n",
        )
        self.assertEqual(errors.getvalue(), "checked_prs=2 checked_issues=2 unanswered=3\n")
        self.assertEqual(request.call_args_list.count(unittest.mock.call("/issues/3")), 1)

    def test_discovery_thread_kind(self):
        """Classify null, populated, and missing PR metadata on both discovery paths."""
        cases = (
            ({"pull_request": None}, "issue"),
            ({"pull_request": {"merged": False}}, "pr"),
            ({}, "issue"),
        )
        for metadata, expected in cases:
            for from_activity in (False, True):
                with self.subTest(metadata=metadata, from_activity=from_activity):
                    issue = {"number": 3, "state": "open", **metadata}
                    routes = {
                        "/pulls?state=open&poster=lusoris&limit=50&page=1": [],
                        "/issues?type=issues&state=open&created_by=lusoris&limit=50&page=1": []
                        if from_activity
                        else [issue],
                        FEED + "?only-performed-by=true&limit=50&page=1": [_activity(3)]
                        if from_activity
                        else [],
                        "/issues/3": issue,
                    }
                    with unittest.mock.patch.object(fairy, "get", side_effect=routes.__getitem__):
                        self.assertEqual(fairy.discover_threads(), {3: expected})

    def test_feed_page_boundary(self):
        """Only a full tenth page warns that older comments were not read."""
        for last_size in (49, 50):
            with self.subTest(last_size=last_size):
                pages = [[_activity(3)] * 50 for _ in range(9)]
                pages.append([_activity(4)] * last_size)
                errors = io.StringIO()
                with (
                    unittest.mock.patch.object(fairy, "get", side_effect=pages) as request,
                    contextlib.redirect_stderr(errors),
                ):
                    self.assertEqual(fairy.commented_numbers(), {3, 4})
                self.assertEqual(request.call_count, 10)
                self.assertEqual(
                    "older comments were not read" in errors.getvalue(), last_size == 50
                )

    def test_malformed_activity_reported(self):
        """Malformed matching rows are visible; unrelated activity is ignored."""
        bad = [
            dict(_activity(3), content=content)
            for content in ("{", "[]", '["oops", "text"]', '["3", null]', "null")
        ]
        bad.append(None)
        unrelated = dict(_activity(8), repo={"full_name": "other/repo"})
        rows = bad + [unrelated, _activity(9, "create_issue"), _activity(4)]
        errors = io.StringIO()
        with (
            unittest.mock.patch.object(fairy, "get", return_value=rows),
            contextlib.redirect_stderr(errors),
        ):
            self.assertEqual(fairy.commented_numbers(), {4})
        for row in bad:
            self.assertIn(repr(row), errors.getvalue())
        self.assertEqual(len(errors.getvalue().splitlines()), len(bad))

    def test_non_404_error_fails(self):
        """HTTP failures other than 404 must not silently discard a thread."""
        routes = _routes()
        routes["/issues/6"].close()
        routes["/issues/6"] = urllib.error.HTTPError("fixture", 500, "failed", {}, io.BytesIO())

        def fetch(path):
            """Serve fixture data or raise the planted HTTP error."""
            response = routes[path]
            if isinstance(response, Exception):
                raise response
            return response

        errors = io.StringIO()
        with (
            unittest.mock.patch.object(fairy, "get", side_effect=fetch),
            contextlib.redirect_stderr(errors),
        ):
            self.assertEqual(fairy.main([]), 1)
        self.assertIn("500", errors.getvalue())

    def test_get_timeout(self):
        """Every repository and feed GET uses the required 30-second timeout."""
        for path, url in (("/issues/3", fairy.API + "/issues/3"), (FEED, FEED)):
            with unittest.mock.patch.object(
                fairy.urllib.request, "urlopen", return_value=io.BytesIO(b"[]")
            ) as request:
                self.assertEqual(fairy.get(path), [])
            request.assert_called_once_with(url, timeout=30)
