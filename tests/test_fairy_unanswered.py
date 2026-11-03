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
        "state": "COMMENT",
    }


def _routes():
    """Build mixed threads with paged reviews, stale rows, and missing rows."""
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
        "/pulls/1/reviews?limit=50&page=1": [],
        "/pulls/4/reviews?limit=50&page=1": [_word()],
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

    def test_pending_fairy_review_ignored(self):
        """An unsubmitted Fairy draft with a body creates no reply obligation."""
        review = dict(_word(), state="PENDING", submitted_at=None)
        self.assertEqual(fairy.owed([], [review]), ("", "", False))

    def test_pending_lusoris_review_ignored(self):
        """An unsubmitted reply cannot clear an outstanding Fairy comment."""
        review = dict(_word("lusoris"), state="PENDING", submitted_at=None)
        self.assertEqual(fairy.owed([_word()], [review]), (NEW, "", True))

    def test_unknown_review_state_ignored(self):
        """Unknown and null review states are silent even with a timestamp."""
        for state in ("FOO", None, "REQUEST_REVIEW"):
            with self.subTest(state=state):
                review = dict(_word(), state=state)
                self.assertEqual(fairy.owed([], [review]), ("", "", False))

    def test_submitted_review_invalid_timestamp_reported(self):
        """Counted states reject non-string timestamps, including approve-only bodies."""
        approve = "### Classification\n\n- <details><summary><b>approve — x.</b></summary>\n"
        for body in ("finding", approve):
            for timestamp in (None, 0, False, [], {}):
                with self.subTest(body=body, timestamp=timestamp):
                    review = dict(_word(), body=body, submitted_at=timestamp)
                    with self.assertRaises((TypeError, ValueError)):
                        fairy.owed([], [review])
                    output, errors = io.StringIO(), io.StringIO()
                    with (
                        unittest.mock.patch.object(
                            fairy, "discover_threads", return_value={1: "pr"}
                        ),
                        unittest.mock.patch.object(fairy, "get", return_value=[]),
                        unittest.mock.patch.object(fairy, "paginated", return_value=[review]),
                        contextlib.redirect_stdout(output),
                        contextlib.redirect_stderr(errors),
                    ):
                        self.assertEqual(fairy.main([]), 1)
                    self.assertEqual(output.getvalue(), "")
                    self.assertIn("malformed API response", errors.getvalue())

    def test_submitted_review_states_and_empty_timestamp(self):
        """Every counted state needs a non-empty submission timestamp."""
        for state in ("APPROVED", "COMMENT", "REQUEST_CHANGES"):
            with self.subTest(state=state):
                review = dict(_word(), state=state)
                self.assertEqual(fairy.owed([], [review]), (NEW, "", True))
                self.assertEqual(fairy.owed([], [dict(review, submitted_at="")]), ("", "", False))

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
            unittest.mock.patch.object(
                fairy, "utc_now", side_effect=["2026-11-03T00:00:00Z", "2026-11-03T00:00:01Z"]
            ),
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
        self.assertEqual(
            errors.getvalue(),
            "checked_prs=2 checked_issues=2 unanswered=3 "
            "read_from=2026-11-03T00:00:00Z read_to=2026-11-03T00:00:01Z\n",
        )
        self.assertEqual(request.call_args_list.count(unittest.mock.call("/issues/3")), 1)

    def _review_pages_output(self, first, second):
        """Run a PR against a full default page and a final explicit review page."""
        routes = {
            "/issues/1/comments": [],
            "/pulls/1/reviews": first,
            "/pulls/1/reviews?limit=50&page=1": first,
            "/pulls/1/reviews?limit=50&page=2": second,
        }
        output, errors = io.StringIO(), io.StringIO()
        with (
            unittest.mock.patch.object(fairy, "discover_threads", return_value={1: "pr"}),
            unittest.mock.patch.object(fairy, "get", side_effect=routes.__getitem__),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(errors),
        ):
            self.assertEqual(fairy.main([]), 0)
        return output.getvalue(), errors.getvalue()

    def test_reviews_second_page_fairy_owed(self):
        """Fairy's newest review after fifty older replies must be reported owed."""
        output, errors = self._review_pages_output([_word("lusoris", OLD)] * 50, [_word()])
        self.assertEqual(output, f"1\tpr\tfairy={NEW}\tlusoris={OLD}\n")
        self.assertIn("checked_prs=1 checked_issues=0 unanswered=1", errors)

    def test_reviews_second_page_reply_clears_owed(self):
        """A reply after fifty older Fairy reviews must clear the owed thread."""
        output, errors = self._review_pages_output([_word(timestamp=OLD)] * 50, [_word("lusoris")])
        self.assertEqual(output, "")
        self.assertIn("checked_prs=1 checked_issues=0 unanswered=0", errors)

    def test_main_refused_feed_url_reported(self):
        """Refused feed URLs fail distinctly from malformed JSON before network I/O."""
        read_pages = fairy.paginated
        for url in ("http://code.ffmpeg.org/activities", "https://evil.example/activities"):
            with self.subTest(url=url):
                output, errors = io.StringIO(), io.StringIO()

                def pages(path, feed_url=url, **kwargs):
                    """Skip discovery lists while exercising the real feed GET."""
                    return read_pages(path, **kwargs) if path.startswith(feed_url) else []

                with (
                    unittest.mock.patch.object(fairy, "FEED", url),
                    unittest.mock.patch.object(fairy, "paginated", side_effect=pages),
                    unittest.mock.patch.object(fairy.urllib.request, "urlopen") as request,
                    contextlib.redirect_stdout(output),
                    contextlib.redirect_stderr(errors),
                ):
                    self.assertEqual(fairy.main([]), 1)
                self.assertEqual(output.getvalue(), "")
                self.assertEqual(
                    errors.getvalue(),
                    f"fairy_unanswered: refused URL: {url}"
                    "?only-performed-by=true&limit=50&page=1\n",
                )
                self.assertNotIn("malformed", errors.getvalue())
                request.assert_not_called()

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
                expected_urls = [
                    f"{FEED}?only-performed-by=true&limit=50&page={page}" for page in range(1, 11)
                ]
                self.assertEqual(
                    [call.args[0] for call in request.call_args_list],
                    expected_urls,
                )
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

    def test_get_refuses_non_https_and_foreign_host(self):
        """Unsafe URLs raise a distinct ValueError subtype before network I/O."""
        with unittest.mock.patch.object(fairy.urllib.request, "urlopen") as mock_open:
            for bad_url in (
                "http://code.ffmpeg.org/api/v1/repos/FFmpeg/FFmpeg/pulls",
                "https://evil.example/pulls",
            ):
                with self.subTest(bad_url=bad_url), self.assertRaises(fairy.RefusedURL) as caught:
                    fairy.get(bad_url)
                self.assertIsInstance(caught.exception, ValueError)
                self.assertEqual(str(caught.exception), bad_url)
            mock_open.assert_not_called()

    def test_timestamp_comparison_rfc3339(self):
        """Compare timestamps by UTC datetime rather than lexicographic string order."""
        fairy_word = _word("Forgejo_Fairy", "2026-11-03T10:00:00+02:00")
        mine_word = _word("lusoris", "2026-11-03T09:00:00Z")
        fairy_ts, mine_ts, owed = fairy.owed([fairy_word, mine_word], [])
        self.assertEqual(fairy_ts, "2026-11-03T10:00:00+02:00")
        self.assertEqual(mine_ts, "2026-11-03T09:00:00Z")
        self.assertFalse(owed)

        words = [
            _word("lusoris", "2026-11-03T09:00:00Z"),
            _word("lusoris", "2026-11-03T09:00:00.5Z"),
        ]
        self.assertEqual(
            fairy.latest(words, "lusoris", lambda c: c["created_at"]),
            "2026-11-03T09:00:00.5Z",
        )

    def test_malformed_comment_row_without_user(self):
        """A comment row missing 'user' exits 1 with malformed response message."""
        routes = {
            "/pulls?state=open&poster=lusoris&limit=50&page=1": [],
            "/issues?type=issues&state=open&created_by=lusoris&limit=50&page=1": [{"number": 1}],
            FEED + "?only-performed-by=true&limit=50&page=1": [],
            "/issues/1/comments": [{"body": "no user field", "created_at": NEW}],
        }
        output, errors = io.StringIO(), io.StringIO()
        with (
            unittest.mock.patch.object(fairy, "get", side_effect=routes.__getitem__),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(errors),
        ):
            self.assertEqual(fairy.main([]), 1)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("fairy_unanswered: malformed API response: 'user'", errors.getvalue())

    def test_malformed_reviews_payload_dict_not_list(self):
        """A reviews payload that is a dict instead of a list exits 1 with message."""
        routes = {
            "/pulls?state=open&poster=lusoris&limit=50&page=1": [
                {"number": 1, "user": {"login": "lusoris"}}
            ],
            "/issues?type=issues&state=open&created_by=lusoris&limit=50&page=1": [],
            FEED + "?only-performed-by=true&limit=50&page=1": [],
            "/issues/1/comments": [],
            "/pulls/1/reviews?limit=50&page=1": {"message": "Not Found"},
        }
        output, errors = io.StringIO(), io.StringIO()
        with (
            unittest.mock.patch.object(fairy, "get", side_effect=routes.__getitem__),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(errors),
        ):
            self.assertEqual(fairy.main([]), 1)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("fairy_unanswered: malformed API response:", errors.getvalue())

    def test_malformed_issue_row_missing_number(self):
        """An issue row missing 'number' exits 1 with malformed response message."""
        routes = {
            "/pulls?state=open&poster=lusoris&limit=50&page=1": [],
            "/issues?type=issues&state=open&created_by=lusoris&limit=50&page=1": [
                {"state": "open"}
            ],
            FEED + "?only-performed-by=true&limit=50&page=1": [],
        }
        output, errors = io.StringIO(), io.StringIO()
        with (
            unittest.mock.patch.object(fairy, "get", side_effect=routes.__getitem__),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(errors),
        ):
            self.assertEqual(fairy.main([]), 1)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("fairy_unanswered: malformed API response: 'number'", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
