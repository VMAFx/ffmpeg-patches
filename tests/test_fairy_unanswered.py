# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Network-free fixtures for Fairy turn detection and Forgejo discovery."""

import contextlib
import io
import json
import unittest.mock
import urllib.error
import urllib.parse

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
        fairy.SETTINGS: {"max_response_items": 50},
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
        """Forbid network I/O through both global and restricted openers."""
        if hasattr(fairy, "PAGE_CACHE"):
            fairy.PAGE_CACHE.clear()
        network = unittest.mock.patch.object(
            fairy.urllib.request, "urlopen", side_effect=AssertionError("network forbidden")
        )
        network.start()
        self.addCleanup(network.stop)
        transport = unittest.mock.patch.object(
            fairy.urllib.request.AbstractHTTPHandler,
            "do_open",
            side_effect=AssertionError("network forbidden"),
        )
        transport.start()
        self.addCleanup(transport.stop)

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
            fairy.SETTINGS: {"max_response_items": 50},
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
                    unittest.mock.patch.object(fairy, "discover_page_size", return_value=50),
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
                        fairy.SETTINGS: {"max_response_items": 50},
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
                    unittest.mock.patch.dict(fairy.PAGE_CACHE, {"page_size": 50}),
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
            unittest.mock.patch.dict(fairy.PAGE_CACHE, {"page_size": 50}),
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
        """Every GET uses the restricted opener with a 30-second timeout."""
        for path, url in (
            ("/issues/3", fairy.API + "/issues/3"),
            (FEED, FEED),
            (fairy.SETTINGS, fairy.SETTINGS),
        ):
            with (
                unittest.mock.patch.object(
                    fairy.OPENER, "open", return_value=io.BytesIO(b"[]")
                ) as request,
                unittest.mock.patch.object(fairy.urllib.request, "urlopen") as unrestricted,
            ):
                self.assertEqual(fairy.get(path), [])
            request.assert_called_once_with(url, timeout=30)
            unrestricted.assert_not_called()

    def test_redirect_refuses_http(self):
        """A same-host redirect cannot downgrade the connection to HTTP."""
        url = "http://code.ffmpeg.org/x"
        handler = fairy.RestrictedRedirectHandler()
        request = fairy.urllib.request.Request(FEED)
        with self.assertRaises(fairy.RefusedURL) as caught:
            handler.redirect_request(request, None, 302, "Found", {}, url)
        self.assertEqual(str(caught.exception), url)

    def test_redirect_refuses_foreign_host(self):
        """An HTTPS redirect cannot leave the allow-listed API host."""
        url = "https://evil.example/x"
        handler = fairy.RestrictedRedirectHandler()
        request = fairy.urllib.request.Request(FEED)
        with self.assertRaises(fairy.RefusedURL) as caught:
            handler.redirect_request(request, None, 302, "Found", {}, url)
        self.assertEqual(str(caught.exception), url)

    def test_redirect_allows_api_host_https(self):
        """An HTTPS redirect within the API host produces the next request."""
        url = "https://code.ffmpeg.org/api/v1/x"
        handler = fairy.RestrictedRedirectHandler()
        request = fairy.urllib.request.Request(FEED)
        redirected = handler.redirect_request(request, None, 302, "Found", {}, url)
        self.assertEqual(redirected.full_url, url)

    def test_opener_excludes_unsafe_handlers(self):
        """Only HTTPS transport is installed; local and other transports stay absent."""
        forbidden = (
            fairy.urllib.request.HTTPHandler,
            fairy.urllib.request.FileHandler,
            fairy.urllib.request.FTPHandler,
            fairy.urllib.request.DataHandler,
        )
        for handler in fairy.OPENER.handlers:
            with self.subTest(handler=type(handler).__name__):
                self.assertNotIsInstance(handler, forbidden)
        self.assertTrue(
            any(isinstance(h, fairy.urllib.request.HTTPSHandler) for h in fairy.OPENER.handlers)
        )

    def test_opener_refuses_http(self):
        """Direct HTTP opening fails before any transport can read a response."""
        with self.assertRaises(urllib.error.URLError):
            fairy.OPENER.open("http://code.ffmpeg.org/x", timeout=30)

    def test_redirect_refusal_reported_by_main(self):
        """Real redirect processing preserves main's distinct refused-URL report."""
        url = "https://evil.example/x"
        response = unittest.mock.Mock(code=302, msg="Found")
        response.info.return_value = {"location": url}
        output, errors = io.StringIO(), io.StringIO()
        with (
            unittest.mock.patch.object(
                fairy.urllib.request.HTTPSHandler, "https_open", return_value=response
            ) as transport,
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(errors),
        ):
            self.assertEqual(fairy.main([]), 1)
        self.assertEqual(transport.call_count, 1)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(errors.getvalue(), f"fairy_unanswered: refused URL: {url}\n")

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
            fairy.SETTINGS: {"max_response_items": 50},
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
            fairy.SETTINGS: {"max_response_items": 50},
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
            fairy.SETTINGS: {"max_response_items": 50},
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

    def test_page_size_positive_max_50_unchanged_behaviour(self):
        """A server setting of 50 retains 50-row pagination limit and stop test."""
        routes = {
            fairy.SETTINGS: {"max_response_items": 50},
            "/pulls?limit=50&page=1": [{"number": i} for i in range(50)],
            "/pulls?limit=50&page=2": [{"number": 51}],
        }
        calls = []

        def fake_get(path):
            calls.append(path)
            return routes[path]

        with unittest.mock.patch.object(fairy, "get", side_effect=fake_get):
            rows = fairy.paginated("/pulls")
            rows2 = fairy.paginated("/pulls")
        self.assertEqual(len(rows), 51)
        self.assertEqual(len(rows2), 51)
        self.assertEqual(calls.count(fairy.SETTINGS), 1)
        self.assertEqual(
            calls,
            [
                fairy.SETTINGS,
                "/pulls?limit=50&page=1",
                "/pulls?limit=50&page=2",
                "/pulls?limit=50&page=1",
                "/pulls?limit=50&page=2",
            ],
        )

    def test_page_size_regression_max_20_reads_all_over_three_pages(self):
        """A server setting of 20 reads 45 items across 3 pages instead of stopping at 20."""
        total_items = [{"id": i} for i in range(1, 46)]
        calls = []

        def fake_get(path):
            calls.append(path)
            if path == fairy.SETTINGS:
                return {"max_response_items": 20}
            parsed = urllib.parse.urlsplit(path)
            qs = urllib.parse.parse_qs(parsed.query)
            limit = int(qs.get("limit", [50])[0])
            page = int(qs.get("page", [1])[0])
            effective_limit = min(limit, 20)
            start = (page - 1) * effective_limit
            return total_items[start : start + effective_limit]

        with unittest.mock.patch.object(fairy, "get", side_effect=fake_get):
            rows = fairy.paginated("/items")
        self.assertEqual(len(rows), 45)
        self.assertEqual(
            calls,
            [
                fairy.SETTINGS,
                "/items?limit=20&page=1",
                "/items?limit=20&page=2",
                "/items?limit=20&page=3",
            ],
        )

    def test_page_size_boundary_max_1(self):
        """A server setting of 1 reads single-row pages and stops on an empty page."""
        routes = {
            fairy.SETTINGS: {"max_response_items": 1},
            "/items?limit=1&page=1": [{"id": 1}],
            "/items?limit=1&page=2": [{"id": 2}],
            "/items?limit=1&page=3": [],
        }
        calls = []

        def fake_get(path):
            calls.append(path)
            return routes[path]

        with unittest.mock.patch.object(fairy, "get", side_effect=fake_get):
            self.assertEqual(fairy.discover_page_size(), 1)
            rows = fairy.paginated("/items")
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            calls,
            [
                fairy.SETTINGS,
                "/items?limit=1&page=1",
                "/items?limit=1&page=2",
                "/items?limit=1&page=3",
            ],
        )

    def test_page_size_boundary_max_51_capped_at_50(self):
        """A server setting of 51 is capped at the client maximum of 50."""
        routes = {
            fairy.SETTINGS: {"max_response_items": 51},
            "/items?limit=50&page=1": [{"id": i} for i in range(50)],
            "/items?limit=50&page=2": [],
        }
        calls = []

        def fake_get(path):
            calls.append(path)
            return routes[path]

        with unittest.mock.patch.object(fairy, "get", side_effect=fake_get):
            self.assertEqual(fairy.discover_page_size(), 50)
            rows = fairy.paginated("/items")
        self.assertEqual(len(rows), 50)
        self.assertEqual(
            calls,
            [
                fairy.SETTINGS,
                "/items?limit=50&page=1",
                "/items?limit=50&page=2",
            ],
        )

    def test_page_size_cleared_per_run_on_module_reuse(self):
        """When the module is reused, a lowered page size is read fresh and all rows are read."""
        run = 1
        items_run2 = [{"number": i, "user": {"login": "lusoris"}} for i in range(1, 46)]
        calls = []

        def fake_get(path):
            calls.append(path)
            if path == fairy.SETTINGS:
                return {"max_response_items": 50 if run == 1 else 20}
            if path.startswith("/pulls?"):
                server_max = 50 if run == 1 else 20
                items = [] if run == 1 else items_run2
                parsed = urllib.parse.urlsplit(path)
                qs = urllib.parse.parse_qs(parsed.query)
                limit = int(qs.get("limit", [50])[0])
                page = int(qs.get("page", [1])[0])
                effective_limit = min(limit, server_max)
                start = (page - 1) * effective_limit
                return items[start : start + effective_limit]
            return []

        errors_run1, errors_run2 = io.StringIO(), io.StringIO()
        with unittest.mock.patch.object(fairy, "get", side_effect=fake_get):
            with (
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(errors_run1),
            ):
                rc1 = fairy.main([])
            run = 2
            with (
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(errors_run2),
            ):
                rc2 = fairy.main([])

        self.assertEqual(rc1, 0)
        self.assertEqual(rc2, 0)
        self.assertEqual(calls.count(fairy.SETTINGS), 2)
        self.assertIn("checked_prs=45", errors_run2.getvalue())
        self.assertIn("/pulls?state=open&poster=lusoris&limit=20&page=3", calls)

    def test_page_size_negative_invalid_settings_reported(self):
        """Invalid max_response_items values raise ValueError and exit 1 in main."""
        cases = (
            ("bool true", {"max_response_items": True}),
            ("zero", {"max_response_items": 0}),
            ("string", {"max_response_items": "50"}),
            ("missing key", {}),
        )
        for label, payload in cases:
            with self.subTest(case=label):
                fairy.PAGE_CACHE.clear()
                with (
                    unittest.mock.patch.object(fairy, "get", return_value=payload),
                    self.assertRaises(ValueError),
                ):
                    fairy.discover_page_size()

                calls = []

                def fake_get(path, payload=payload, calls=calls):
                    calls.append(path)
                    if path == fairy.SETTINGS:
                        return payload
                    return []

                output, errors = io.StringIO(), io.StringIO()
                with (
                    unittest.mock.patch.object(fairy, "get", side_effect=fake_get),
                    contextlib.redirect_stdout(output),
                    contextlib.redirect_stderr(errors),
                ):
                    self.assertEqual(fairy.main([]), 1)
                self.assertEqual(output.getvalue(), "")
                self.assertIn("malformed API response", errors.getvalue())
                self.assertEqual(calls, [fairy.SETTINGS])


if __name__ == "__main__":
    unittest.main()
