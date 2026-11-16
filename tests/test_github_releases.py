"""GitHub releases read from the API (feed `kind: github-releases`), not from the atom feeds github.com's robots.txt disallows.

The parser and the fetch_feeds tests use fixed replies; the reader tests use a local HTTP server to show what the real opener
does with a 301, a 429 and an oversized reply. What this does not show: the real api.github.com (its fields, its limits, a
real redirect), or how many requests a day the radar may make before the unauthenticated limit is reached.
"""
from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import threading
import unittest
import urllib.parse
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from pairadar import fetch, health  # noqa: E402
from pairadar.config import Item, load_config  # noqa: E402
from pairadar.distill import enrich, prefilter  # noqa: E402
from test_sources_mainstream import config_with, rss  # noqa: E402

NOTES = "private-capture-marker release notes that must never reach an item"
REPO = "huggingface/lerobot"
URL = fetch.GITHUB_API.format(repo=REPO)


def release(tag="v0.4.0", name="Version 0.4.0", repo=REPO, published="2026-11-10T09:30:00Z", **extra):
    return {"tag_name": tag, "name": name, "html_url": f"https://github.com/{repo}/releases/tag/{tag}",
            "published_at": published, "created_at": published, "draft": False, "prerelease": False, "body": NOTES, **extra}


def payload(*releases) -> bytes:
    return json.dumps(list(releases)).encode()


def feed(**extra):
    return {"id": "github-lerobot", "url": URL, "kind": "github-releases", "repo": REPO, "evidence": "O",
            "topical": True, "max_entries": 1, "title_prefix": "LeRobot", "publisher": "LeRobot releases (GitHub)", **extra}


def silent():
    return contextlib.redirect_stdout(io.StringIO())


class ParserTests(unittest.TestCase):
    def test_a_release_gives_its_title_link_and_day_and_never_its_notes(self):
        out = fetch.parse_github_releases(payload(release()), REPO)
        self.assertEqual(out, [("Version 0.4.0", f"https://github.com/{REPO}/releases/tag/v0.4.0", "", "2026-11-10")])
        self.assertNotIn(NOTES, repr(out))

    def test_a_release_with_no_name_is_titled_by_its_tag(self):
        for name in (None, "", "   ", 5):
            with self.subTest(name):
                self.assertEqual(fetch.parse_github_releases(payload(release(name=name)), REPO)[0][0], "v0.4.0")

    def test_a_time_with_an_offset_is_folded_to_the_utc_day(self):
        out = fetch.parse_github_releases(payload(release(published="2026-11-10T23:30:00-05:00")), REPO)
        self.assertEqual(out[0][3], "2026-11-11")

    def test_newest_first(self):
        out = fetch.parse_github_releases(payload(release("v1", published="2026-10-01T00:00:00Z"),
                                                  release("v3", published="2026-11-03T00:00:00Z"),
                                                  release("v2", published="2026-10-15T00:00:00Z")), REPO)
        self.assertEqual([entry[0] for entry in out], ["Version 0.4.0"] * 3)
        self.assertEqual([entry[3] for entry in out], ["2026-11-03", "2026-10-15", "2026-10-01"])

    def test_a_draft_is_skipped_and_is_not_a_bad_reply(self):
        self.assertEqual(fetch.parse_github_releases(payload(release(draft=True)), REPO), [])
        # a draft has no published time or link yet: nothing else about it is checked
        self.assertEqual(fetch.parse_github_releases(payload({"draft": True, "tag_name": None, "html_url": None,
                                                              "published_at": None}), REPO), [])
        out = fetch.parse_github_releases(payload(release("v2", draft=True), release("v1")), REPO)
        self.assertEqual([entry[1].rsplit("/", 1)[1] for entry in out], ["v1"])

    # 01 F2 and F4: what is not a well-formed release of this repository is a bad reply (None), not "no release" and not an Item
    BAD_RELEASES = {
        "an empty object": {},
        "no draft key": {k: v for k, v in release().items() if k != "draft"},
        "draft as text": release(draft="true"), "draft as a number": release(draft=1), "draft null": release(draft=None),
        "no tag": release(tag=""), "tag not text": release(tag=7), "tag missing": {k: v for k, v in release().items() if k != "tag_name"},
        "tag with spaces round it": release(tag=" v1 "), "tag with an empty segment": release(tag="a//b"),
        "tag that is a dot segment": release(tag="a/../b"), "tag that is a dot": release(tag=".."),
        "no time": release(published=None), "time as a number": release(published=1731231000), "time not a date": release(published="soon"),
        "time without an offset": release(published="2026-11-10T09:30:00"), "time before GitHub existed": release(published="1999-01-01T00:00:00Z"),
        "time in the far future": release(published="9999-12-31T23:59:59Z"),
        "no link": {**release(), "html_url": None}, "link not text": {**release(), "html_url": 5},
        "another repository": release(repo="someone/else"),
        "a prefix of the repository's name": release(repo="huggingface/lerobot-evil"),
        "another host": {**release(), "html_url": f"https://evil.example/{REPO}/releases/tag/v0.4.0"},
        "a host with a prefix": {**release(), "html_url": f"https://github.com.evil.example/{REPO}/releases/tag/v0.4.0"},
        "userinfo": {**release(), "html_url": f"https://x@github.com/{REPO}/releases/tag/v0.4.0"},
        "a port": {**release(), "html_url": f"https://github.com:443/{REPO}/releases/tag/v0.4.0"},
        "plain http": {**release(), "html_url": f"http://github.com/{REPO}/releases/tag/v0.4.0"},
        "a query": {**release(), "html_url": f"https://github.com/{REPO}/releases/tag/v0.4.0?x=1"},
        "a fragment": {**release(), "html_url": f"https://github.com/{REPO}/releases/tag/v0.4.0#x"},
        "a parameter": {**release(), "html_url": f"https://github.com/{REPO}/releases/tag/v0.4.0;x"},
        "not the releases pages": {**release(), "html_url": f"https://github.com/{REPO}/issues/1"},
        "a release path that is not a tag page": {**release(), "html_url": f"https://github.com/{REPO}/releases/not-a-release"},
        "the releases list": {**release(), "html_url": f"https://github.com/{REPO}/releases"},
        "another tag's page": {**release(), "html_url": f"https://github.com/{REPO}/releases/tag/v0.3.0"},
        "a dot segment that leaves the repository": {
            **release(), "html_url": f"https://github.com/{REPO}/releases/../../../other/repo/issues/1"},
        "a dot segment after tag": {**release(), "html_url": f"https://github.com/{REPO}/releases/tag/v0.4.0/.."},
        "an encoded dot segment": {**release(), "html_url": f"https://github.com/{REPO}/releases/%2e%2e/%2e%2e/other/repo/issues/1"},
        "an encoded dot segment as the tag": {**release(tag=".."), "html_url": f"https://github.com/{REPO}/releases/tag/%2e%2e"},
        "an encoded slash in the repository": {**release(), "html_url": "https://github.com/huggingface%2Flerobot/releases/tag/v0.4.0"},
        "a double slash": {**release(), "html_url": f"https://github.com//{REPO}/releases/tag/v0.4.0"},
        "a backslash": {**release(), "html_url": f"https://github.com/{REPO}\\..\\releases/tag/v0.4.0"},
        "a space": {**release(), "html_url": f"https://github.com/{REPO}/releases/tag/v0.4.0 x"},
        "a newline": {**release(), "html_url": f"https://github.com/{REPO}/releases/tag/v0.4.0\n"},
        "not ascii": {**release(), "html_url": f"https://github.com/{REPO}/releases/tag/v0.4.\u2024"},
        "an address that cannot be parsed": {**release(), "html_url": "https://["},
    }

    def test_what_is_not_a_well_formed_release_of_this_repository_is_a_bad_reply(self):
        for name, one in self.BAD_RELEASES.items():
            with self.subTest(name):
                self.assertIsNone(fetch.parse_github_releases(payload(one), REPO))

    def test_one_bad_release_among_good_ones_makes_the_whole_reply_bad(self):
        self.assertIsNone(fetch.parse_github_releases(payload(release("v2"), {}), REPO))

    def test_a_key_twice_anywhere_is_a_bad_reply(self):
        good = json.dumps(release()).encode()
        for name, body in {
                "draft true then false": b'[{"tag_name":"v1","name":"v1","html_url":"https://github.com/huggingface/lerobot/releases/tag/v1",'
                                         b'"published_at":"2026-11-10T09:30:00Z","draft":true,"draft":false}]',
                "draft false then true": b'[{"draft":false,"draft":true,"tag_name":"v1"}]',
                "tag twice": b'[{"tag_name":"v1","tag_name":"v2","draft":false}]',
                "a key twice in an unused part": b'[{"draft":true,"body":"a","body":"b"}]',
                "two objects, one with a repeat": b"[" + good + b',{"draft":true,"draft":true}]'}.items():
            with self.subTest(name):
                self.assertIsNone(fetch.parse_github_releases(body, REPO))
        self.assertEqual(len(fetch.parse_github_releases(b"[" + good + b"]", REPO)), 1)

    def test_a_legal_tag_in_its_percent_encoded_and_plain_forms_is_accepted(self):
        cases = {"v1.0+rc1": "v1.0%2Brc1", "release/v2": "release/v2", "v3": "v3", "ver 4": "ver%204", "v5%": "v5%25",
                 "ünï": "%C3%BCn%C3%AF"}
        for tag, encoded in cases.items():
            with self.subTest(tag):
                one = {**release(tag), "html_url": f"https://github.com/{REPO}/releases/tag/{encoded}"}
                out = fetch.parse_github_releases(payload(one), REPO)
                self.assertEqual(len(out), 1, out)
                self.assertEqual(out[0][1], one["html_url"])

    # 01 F4-R2 and F2-R2 (the follow-up to the second round): the link is read as strict UTF-8, and every entry is checked.
    NOT_UTF8_LINKS = {"a lone 0xFF": "%FF", "a lone 0xFE": "%FE", "an overlong slash": "%C0%AF", "a surrogate": "%ED%A0%80"}

    def test_a_link_that_is_not_utf8_is_not_the_tag_its_lossy_decoding_spells(self):
        for name, encoded in self.NOT_UTF8_LINKS.items():
            with self.subTest(name):
                lossy = urllib.parse.unquote(encoded)                  # what the default decoding made of it: U+FFFD only
                self.assertEqual(set(lossy), {"\ufffd"})
                one = {**release(lossy), "html_url": f"https://github.com/{REPO}/releases/tag/{encoded}"}
                self.assertIsNone(fetch.parse_github_releases(payload(one), REPO))

    def test_a_tag_that_really_has_the_replacement_character_is_still_accepted_in_its_utf8_form(self):
        for tag, encoded in {"\ufffd": "%EF%BF%BD", "a\ufffdb": "a%EF%BF%BDb"}.items():
            with self.subTest(tag):
                one = {**release(tag), "html_url": f"https://github.com/{REPO}/releases/tag/{encoded}"}
                self.assertEqual(len(fetch.parse_github_releases(payload(one), REPO)), 1)
        # and the plain character in the link is not ASCII, so it was never accepted
        one = {**release("\ufffd"), "html_url": f"https://github.com/{REPO}/releases/tag/\ufffd"}
        self.assertIsNone(fetch.parse_github_releases(payload(one), REPO))

    def many(self, count, *, bad_at=None):
        """`count` well-formed releases with distinct tags, and `{}` in place of the one at `bad_at`."""
        return payload(*({} if index == bad_at else release(f"v{index}") for index in range(count)))

    def test_a_bad_entry_anywhere_in_the_reply_is_a_bad_reply_not_only_among_the_first_thirty(self):
        cap = fetch.MAX_RELEASES_KEPT
        for position in (0, 1, cap - 1, cap, cap + 1, 100):
            with self.subTest(position):
                self.assertIsNone(fetch.parse_github_releases(self.many(101, bad_at=position), REPO))
        # a bad field, not only a missing object, and after the cap
        tail = [release(f"v{index}") for index in range(cap)] + [release("v-last", published="soon")]
        self.assertIsNone(fetch.parse_github_releases(payload(*tail), REPO))

    def test_a_longer_reply_is_kept_to_the_cap_after_it_has_been_checked(self):
        cap = fetch.MAX_RELEASES_KEPT
        self.assertEqual(len(fetch.parse_github_releases(self.many(cap), REPO)), cap)
        self.assertEqual(len(fetch.parse_github_releases(self.many(cap + 40), REPO)), cap)
        # a draft inside the first entries still counts as one of them, as before
        drafts = payload(release("d", draft=True), *(release(f"v{index}") for index in range(cap + 5)))
        self.assertEqual(len(fetch.parse_github_releases(drafts, REPO)), cap - 1)

    def test_the_failure_is_a_result_not_an_exception(self):
        # 01 F1: an unparseable address, and a time whose UTC day does not exist, used to raise out of fetch_feeds
        for name, one in {"address": {**release(), "html_url": "https://["},
                          "address with a bad port": {**release(), "html_url": "https://github.com:99999999/x"},
                          "year 1 with an offset": release(published="0001-01-01T00:00:00+14:00"),
                          "year 9999 with an offset": release(published="9999-12-31T23:59:59-14:00")}.items():
            with self.subTest(name):
                self.assertIsNone(fetch.parse_github_releases(payload(one), REPO))
        # the edge of the range is turned into a bad reply even where the plausibility bounds would not have caught it
        with mock.patch.object(fetch, "_utc_day", side_effect=OverflowError):
            self.assertIsNone(fetch.parse_github_releases(payload(release()), REPO))

    def test_deeply_nested_json_is_a_bad_reply(self):
        for depth in (1100, 2000, 100000):
            with self.subTest(depth):
                self.assertIsNone(fetch.parse_github_releases(b"[" * depth + b"]" * depth, REPO))

    def test_the_repository_name_is_compared_without_case(self):
        out = fetch.parse_github_releases(payload(release(repo="isaac-sim/IsaacLab")), "isaac-sim/isaaclab")
        self.assertEqual(len(out), 1)

    def test_an_empty_list_is_an_answer_and_anything_else_is_not(self):
        self.assertEqual(fetch.parse_github_releases(b"[]", REPO), [])
        for name, body in {"an object": b'{"message": "Not Found"}', "text": b"not json", "an html page": b"<html></html>",
                           "a list of strings": b'["a"]', "empty": b"", "not utf-8": b"[\xff]", "null": b"null"}.items():
            with self.subTest(name):
                self.assertIsNone(fetch.parse_github_releases(body, REPO))

    def test_a_long_name_is_cut(self):
        out = fetch.parse_github_releases(payload(release(name="x" * 5000)), REPO)
        self.assertEqual(len(out[0][0]), fetch.MAX_TITLE_CHARS)


class Handler(BaseHTTPRequestHandler):
    seen: list[tuple[str, str]] = []
    reply: tuple[int, dict[str, str], bytes] = (200, {}, b"[]")

    def do_GET(self) -> None:  # noqa: N802
        type(self).seen.append((self.path, self.headers.get("User-Agent", "")))
        status, headers, body = type(self).reply
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass


class ReaderTests(unittest.TestCase):
    """`github_get` with the real opener against a local server."""

    def setUp(self):
        Handler.seen = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (self.server.shutdown(), self.server.server_close()))

    def get(self, status, headers=None, body=b"[]"):
        Handler.reply = (status, headers or {}, body)
        report: dict = {}
        with silent():
            return fetch.github_get(self.base + "/repos/x/y/releases?per_page=1", report), report

    def test_a_200_is_read_with_the_radars_user_agent_and_one_request(self):
        body, report = self.get(200, body=b'[{"a": 1}]')
        self.assertEqual((body, report["status"]), (b'[{"a": 1}]', 200))
        self.assertEqual(Handler.seen, [("/repos/x/y/releases?per_page=1", fetch.USER_AGENT)])

    def test_a_redirect_is_not_followed_and_is_reported_as_it_was_answered(self):
        for status in (301, 302, 307, 308):
            with self.subTest(status):
                Handler.seen = []
                body, report = self.get(status, {"Location": self.base + "/repositories/1/releases?per_page=1"})
                self.assertEqual((body, report["status"]), (None, status))
                self.assertEqual(len(Handler.seen), 1)                 # the Location was never asked for
                self.assertEqual(fetch._github_failure(report["status"], report), "moved")

    def test_a_refusal_an_error_and_no_answer_are_one_attempt_each(self):
        for status, expected in ((403, "forbidden"), (429, "rate-limited"), (404, "not-found"), (500, "unavailable"),
                                 (503, "unavailable"), (410, "unavailable")):
            with self.subTest(status):
                Handler.seen = []
                body, report = self.get(status)
                self.assertEqual((body, report["status"], len(Handler.seen)), (None, status, 1))
                self.assertEqual(fetch._github_failure(status, report), expected)
        self.assertEqual(fetch._github_failure(None, {}), "unavailable")

    def test_a_403_is_a_rate_limit_only_when_github_says_so(self):
        # 04 N3: GitHub answers 403 for a rate limit and for a plain refusal; X-RateLimit-Remaining and Retry-After tell them apart
        cases = {"no header": ({}, "forbidden"), "primary limit used up": ({"X-RateLimit-Remaining": "0"}, "rate-limited"),
                 "limit left": ({"X-RateLimit-Remaining": "57"}, "forbidden"),
                 "not a number": ({"X-RateLimit-Remaining": "x"}, "forbidden"),
                 "secondary limit": ({"Retry-After": "60"}, "rate-limited")}
        for name, (headers, expected) in cases.items():
            with self.subTest(name):
                _, report = self.get(403, headers)
                self.assertEqual(fetch._github_failure(403, report), expected)
        _, report = self.get(404, {"X-RateLimit-Remaining": "0"})
        self.assertEqual(fetch._github_failure(404, report), "not-found")           # the headers only matter for a 403
        _, report = self.get(429)
        self.assertEqual(fetch._github_failure(429, report), "rate-limited")        # a 429 is a limit whatever it carries

    def test_an_error_reply_is_closed_when_it_has_been_read(self):
        # 04 N2: the HTTPError holds the connection until it is closed
        closed = []

        class Body(io.BytesIO):
            def close(self):
                closed.append(True)
                super().close()

        error = fetch.urllib.error.HTTPError("https://api.github.com/x", 500, "boom", {}, Body(b"x"))
        opener = mock.Mock()
        opener.open.side_effect = error
        report: dict = {}
        with mock.patch.object(fetch.urllib.request, "build_opener", return_value=opener), silent():
            self.assertIsNone(fetch.github_get("https://api.github.com/x", report))
        self.assertEqual((report["status"], closed), (500, [True]))

    def test_an_error_with_no_headers_is_still_an_error_and_is_closed(self):
        error = fetch.urllib.error.HTTPError("https://api.github.com/x", 403, "no", None, io.BytesIO(b"x"))
        opener = mock.Mock()
        opener.open.side_effect = error
        report: dict = {}
        with mock.patch.object(fetch.urllib.request, "build_opener", return_value=opener), silent():
            self.assertIsNone(fetch.github_get("https://api.github.com/x", report))
        self.assertEqual((report["status"], report["rate_limited"]), (403, False))

    def test_a_reply_over_the_limit_is_not_read_as_an_answer(self):
        body, report = self.get(200, body=b"[" + b" " * fetch.MAX_API_BYTES + b"]")
        self.assertIsNone(body)
        self.assertEqual(fetch._github_failure(report["status"], report), "bad-reply")

    def test_nothing_listening_is_no_answer(self):
        self.server.shutdown()
        self.server.server_close()
        report: dict = {}
        with silent():
            self.assertIsNone(fetch.github_get(self.base + "/x", report))
        self.assertIsNone(report["status"])


class FeedTests(unittest.TestCase):
    def read(self, feeds, reply, robots=None, config=None):
        calls = []

        def get(url, report):
            calls.append(url)
            status, body = reply(url) if callable(reply) else reply
            report["status"] = status
            return body
        skipped, reasons, failures = [], {}, {}
        config = config or config_with(feeds=feeds)
        with mock.patch.object(fetch, "github_get", get), mock.patch.object(fetch.time, "sleep"), silent():
            items, failed = fetch.fetch_feeds(config, robots=robots, skipped=skipped, reasons=reasons, failures=failures)
        return items, failed, skipped, failures, calls

    def test_the_newest_release_becomes_one_item_with_no_notes(self):
        items, failed, _, failures, calls = self.read(
            [feed()], (200, payload(release("v0.4.0", "v0.4.0", published="2026-11-10T09:30:00Z"),
                                    release("v0.3.0", "v0.3.0", published="2026-09-01T00:00:00Z"))))
        self.assertEqual((failed, failures, calls), ([], {}, [URL]))
        self.assertEqual(len(items), 1)                                         # max_entries 1
        item = items[0]
        self.assertEqual((item.title, item.url, item.summary, item.published, item.source_id, item.evidence, item.topical),
                         ("LeRobot v0.4.0", f"https://github.com/{REPO}/releases/tag/v0.4.0", "", "2026-11-10",
                          "github-lerobot", "O", True))
        self.assertEqual(item.publisher, "LeRobot releases (GitHub)")
        self.assertNotIn(NOTES, repr(items))

    def test_a_repository_with_no_release_yet_is_answered_and_gives_nothing(self):
        items, failed, _, failures, _ = self.read([feed()], (200, b"[]"))
        self.assertEqual((items, failed, failures), ([], [], {}))

    def test_a_301_is_not_followed_and_is_moved_and_the_other_feeds_are_still_read(self):
        other = {"id": "blog", "url": "https://k.example/feed", "evidence": "M", "topical": True}
        config = config_with(feeds=[feed(), other])
        calls = []

        def moved(url, report):
            calls.append(url)
            report["status"] = 301
            return None
        with mock.patch.object(fetch, "github_get", moved), \
                mock.patch.object(fetch, "http_get", return_value=rss(("A robot", "https://k.example/a", "robot"))), \
                mock.patch.object(fetch.time, "sleep"), silent():
            failures: dict = {}
            items, failed = fetch.fetch_feeds(config, failures=failures)
        self.assertEqual((failed, failures, calls), (["github-lerobot"], {"github-lerobot": "moved"}, [URL]))
        self.assertEqual([item.source_id for item in items], ["blog"])

    def test_every_failure_has_one_fixed_reason(self):
        for status, body, expected in ((403, None, "forbidden"), (429, None, "rate-limited"), (404, None, "not-found"),
                                       (500, None, "unavailable"), (None, None, "unavailable"),
                                       (200, b'{"message": "x"}', "bad-reply"), (200, b"not json", "bad-reply")):
            with self.subTest(status=status, body=body):
                _, failed, _, failures, _ = self.read([feed()], (status, body))
                self.assertEqual((failed, failures), (["github-lerobot"], {"github-lerobot": expected}))
                self.assertIn(expected, fetch.GITHUB_FAILURES)

    # 01 F1, F2, F4 through the real consumer: a reply that is not a well-formed release of the repository is the feed's
    # `bad-reply`; it is not retried, it does not stop the feeds after it, and nothing of it becomes an Item.
    OTHER = {"id": "blog", "url": "https://k.example/feed", "evidence": "M", "topical": True}

    def read_with_a_feed_after(self, body):
        calls = []

        def get(url, report):
            calls.append(url)
            report["status"] = 200
            return body

        failures: dict = {}
        with mock.patch.object(fetch, "github_get", get), \
                mock.patch.object(fetch, "http_get", return_value=rss(("A robot", "https://k.example/a", "robot"))) as ordinary, \
                mock.patch.object(fetch.time, "sleep"), silent():
            items, failed = fetch.fetch_feeds(config_with(feeds=[feed(), self.OTHER]), failures=failures)
        return items, failed, failures, calls, ordinary

    def test_every_malformed_or_hostile_release_is_a_bad_reply_and_the_next_feed_is_still_read(self):
        for name, one in ParserTests.BAD_RELEASES.items():
            with self.subTest(name):
                items, failed, failures, calls, ordinary = self.read_with_a_feed_after(payload(one))
                self.assertEqual((failed, failures, calls), (["github-lerobot"], {"github-lerobot": "bad-reply"}, [URL]))
                self.assertEqual([item.source_id for item in items], ["blog"])
                ordinary.assert_called_once()

    def test_the_reply_01_used_for_the_exceptions_does_not_stop_the_run(self):
        for name, one in {"unparseable link": {**release(), "html_url": "https://["},
                          "time with an offset at the edge": release(published="0001-01-01T00:00:00+14:00")}.items():
            with self.subTest(name):
                items, failed, failures, _, ordinary = self.read_with_a_feed_after(payload(one))
                self.assertEqual((failed, failures), (["github-lerobot"], {"github-lerobot": "bad-reply"}))
                ordinary.assert_called_once()

    def test_duplicate_keys_and_an_empty_object_are_bad_replies_not_no_release(self):
        for name, body in {"empty object": b"[{}]", "draft twice": b'[{"draft":true,"draft":false,"tag_name":"v1"}]',
                           "draft as text": payload(release(draft="true"))}.items():
            with self.subTest(name):
                items, failed, failures, _, _ = self.read_with_a_feed_after(body)
                self.assertEqual((failed, failures), (["github-lerobot"], {"github-lerobot": "bad-reply"}))
                self.assertEqual([item.source_id for item in items], ["blog"])

    def test_a_link_that_is_not_utf8_or_a_bad_entry_after_the_first_thirty_is_a_bad_reply_through_the_real_consumer(self):
        # 01 F4-R2, F2-R2: the same inputs, through fetch_feeds: a bad reply, no Item for the feed, the next feed still read
        valid = [release(f"v{index}") for index in range(fetch.MAX_RELEASES_KEPT)]
        cases = {name: payload(one) for name, one in {
            "a lone 0xFF": {**release(urllib.parse.unquote("%FF")), "html_url": f"https://github.com/{REPO}/releases/tag/%FF"},
            "an overlong slash": {**release(urllib.parse.unquote("%C0%AF")),
                                  "html_url": f"https://github.com/{REPO}/releases/tag/%C0%AF"},
        }.items()}
        cases["an empty object as the 31st"] = payload(*valid, {})
        cases["an empty object as the 32nd"] = payload(*valid, release("v-more"), {})
        for name, body in cases.items():
            with self.subTest(name):
                items, failed, failures, calls, ordinary = self.read_with_a_feed_after(body)
                self.assertEqual((failed, failures, calls), (["github-lerobot"], {"github-lerobot": "bad-reply"}, [URL]))
                self.assertEqual([item.source_id for item in items], ["blog"])
                ordinary.assert_called_once()

    def test_legal_releases_still_become_items_after_the_stricter_reading(self):
        for name, body in {"a tag with a plus": payload({**release("v1.0+rc1"), "html_url":
                                                            f"https://github.com/{REPO}/releases/tag/v1.0%2Brc1"}),
                           "no name": payload(release(name="")),
                           "an offset": payload(release(published="2026-11-10T23:30:00-05:00"))}.items():
            with self.subTest(name):
                items, failed, failures, _, _ = self.read_with_a_feed_after(body)
                self.assertEqual((failed, failures), ([], {}))
                self.assertEqual(sorted(item.source_id for item in items), ["blog", "github-lerobot"])

    def test_a_bad_reply_reaches_the_health_report_and_a_real_empty_list_does_not(self):
        # the feed and health sides together: a bad reply that kept coming would be named for a person, a repository with
        # no release yet would not
        def days(failures):
            return [{"date": (date(2026, 11, 16) + timedelta(days=offset)).isoformat(), "count": 8,
                     "failed": ["github-lerobot"] if failures else [], "fallback": [],
                     **({"failed_reasons": dict(failures)} if failures else {})} for offset in range(5)]

        _, failed_bad, _, failures_bad, _ = self.read([feed()], (200, b"[{}]"))
        _, failed_empty, _, failures_empty, _ = self.read([feed()], (200, b"[]"))
        self.assertEqual((failed_bad, failures_bad), (["github-lerobot"], {"github-lerobot": "bad-reply"}))
        self.assertEqual((failed_empty, failures_empty), ([], {}))
        releases = frozenset({"github-lerobot"})
        self.assertEqual(health.release_feeds_failing(days(failures_bad), date(2026, 11, 20), releases=releases),
                         [{"source": "github-lerobot", "failed_days": 5, "reason": "bad-reply"}])
        self.assertEqual(health.release_feeds_failing(days({}), date(2026, 11, 20), releases=releases), [])

    BAD_CONFIGS = {"another url": feed(url="https://evil.example/repos/x/releases"),
                   "the atom feed": feed(url=f"https://github.com/{REPO}/releases.atom"),
                   "another repo in the url": feed(url=fetch.GITHUB_API.format(repo="someone/else")),
                   "no repo": {k: v for k, v in feed().items() if k != "repo"},
                   "a repo that is not owner/name": feed(repo="../../etc", url=URL),
                   "a repo with a query": feed(repo="a/b?x=1", url=URL),
                   "more pages": feed(url=URL.replace("per_page=1", "per_page=100")),
                   "an unexpected host and path": feed(url="https://unexpected.example/private/feed"),
                   "plain http": feed(url=URL.replace("https://", "http://")),
                   "a port": feed(url=URL.replace("api.github.com", "api.github.com:8443")),
                   "userinfo": feed(url=URL.replace("api.github.com", "x@api.github.com")),
                   "a trailing newline in the repo": feed(repo=REPO + "\n", url=fetch.GITHUB_API.format(repo=REPO + "\n")),
                   "an unknown kind": feed(kind="something-else"),
                   **{f"dot component {repo!r}": feed(repo=repo, url=fetch.GITHUB_API.format(repo=repo))
                      for repo in ("../repo", "owner/..", "./repo", "owner/.", "./.", "../..")}}

    def test_a_feed_whose_url_is_not_the_one_built_from_its_repo_is_bad_config_and_nothing_is_requested(self):
        for name, bad in self.BAD_CONFIGS.items():
            with self.subTest(name):
                _, failed, _, failures, calls = self.read([bad], (200, payload(release())))
                self.assertEqual((failed, failures, calls), (["github-lerobot"], {"github-lerobot": "bad-config"}, []))

    def test_a_bad_configuration_asks_for_no_robots_txt_and_waits_for_nothing(self):
        # 01 F3: the refusal is decided from the configuration, before robots.txt, the spacing wait and the API
        for name, bad in self.BAD_CONFIGS.items():
            with self.subTest(name):
                asked = []

                def getter(url, asked=asked):
                    asked.append(url)
                    return 200, b"User-agent: *\nAllow: /\n"

                with mock.patch.object(fetch, "polite") as polite:
                    _, failed, skipped, failures, calls = self.read([bad], (200, payload(release())),
                                                                    robots=fetch.RobotsCache(getter=getter))
                self.assertEqual((asked, calls, skipped), ([], [], []))
                polite.assert_not_called()
                self.assertEqual((failed, failures), (["github-lerobot"], {"github-lerobot": "bad-config"}))

    def test_a_good_configuration_still_asks_the_api_hosts_robots_txt_first_and_only_then_the_api(self):
        order = []

        def getter(url):
            order.append(("robots", url))
            return 404, None

        def get(url, report):
            order.append(("api", url))
            report["status"] = 200
            return payload(release())

        with mock.patch.object(fetch, "github_get", get), mock.patch.object(fetch.time, "sleep"), silent():
            items, failed = fetch.fetch_feeds(config_with(feeds=[feed()]), robots=fetch.RobotsCache(getter=getter),
                                              failures={})
        self.assertEqual(order, [("robots", "https://api.github.com/robots.txt"), ("api", URL)])
        self.assertEqual((len(items), failed), (1, []))

    def test_an_unknown_kind_is_not_read_as_a_feed(self):
        _, failed, _, failures, calls = self.read([feed(kind="something-else")], (200, payload(release())))
        self.assertEqual((failed, failures, calls), (["github-lerobot"], {"github-lerobot": "bad-config"}, []))

    def test_robots_txt_of_the_api_host_still_decides(self):
        cases = {"a rule for the path": (200, b"User-agent: *\nDisallow: /repos/\n", fetch.DISALLOW),
                 "forbidden": (403, b"no", fetch.FORBIDDEN), "unreachable": (503, b"", fetch.UNREACHABLE)}
        for name, (status, body, verdict) in cases.items():
            with self.subTest(name):
                cache = fetch.RobotsCache(getter=lambda url, s=status, b=body: (s, b))
                _, failed, skipped, failures, calls = self.read([feed()], (200, payload(release())), robots=cache)
                self.assertEqual((failed, skipped, failures, calls), ([], ["github-lerobot"], {}, []))

    def test_no_robots_txt_on_the_api_host_allows_it_as_it_did_on_11_16(self):
        cache = fetch.RobotsCache(getter=lambda url: (404, None))
        items, failed, skipped, _, calls = self.read([feed()], (200, payload(release())), robots=cache)
        self.assertEqual((len(items), failed, skipped, calls), (1, [], [], [URL]))

    def test_a_disabled_feed_is_not_read(self):
        _, failed, _, _, calls = self.read([feed(enabled=False)], (200, payload(release())))
        self.assertEqual((failed, calls), ([], []))

    def test_the_report_carries_the_reasons(self):
        report = fetch.FetchReport(failed_reasons={"b": "moved", "a": "not-found"})
        self.assertEqual(list(report.to_dict()["failed_reasons"].items()), [("a", "not-found"), ("b", "moved")])
        self.assertEqual(fetch.FetchReport().to_dict()["failed_reasons"], {})


class ReleaseFeedHealthTest(unittest.TestCase):
    """A release feed read from the GitHub API cannot be repaired by the repair job: its verifier parses RSS/Atom and accepts a
    change of the feed's `url` alone, while a renamed repository needs `repo` and `url` changed together. So a feed that stays
    dark is reported for a person (with its reason) and never wakes the model every night."""

    TODAY = date(2026, 11, 20)

    def runs(self, failed_by_day: dict[int, list[str]], reasons: dict[str, str] | None = None) -> list[dict]:
        runs = []
        for offset in range(5):
            day = date(2026, 11, 16) + timedelta(days=offset)
            entry = {"date": day.isoformat(), "count": 8, "failed": list(failed_by_day.get(offset, [])), "fallback": []}
            if offset == 4 and reasons:
                entry["failed_reasons"] = dict(reasons)
            runs.append(entry)
        return runs

    def test_a_release_feed_that_stays_dark_is_reported_with_its_reason_and_never_struggling(self):
        runs = self.runs({offset: ["github-isaaclab", "plain-feed"] for offset in range(5)}, {"github-isaaclab": "moved"})
        with mock.patch.object(health, "release_feed_ids", return_value=frozenset({"github-isaaclab"})), \
                mock.patch.object(health, "page_watch_ids", return_value=frozenset()):
            verdict = health.report(runs, self.TODAY)
        self.assertEqual([entry["source"] for entry in verdict["struggling"]], ["plain-feed"])        # an RSS feed still is
        self.assertEqual(verdict["release_feeds_failing"], [{"source": "github-isaaclab", "failed_days": 5, "reason": "moved"}])
        self.assertEqual(verdict["failures"], {"github-isaaclab": 5, "plain-feed": 5})               # still counted as failing

    def test_the_repair_gate_stays_quiet_when_only_a_release_feed_is_dark(self):
        runs = self.runs({offset: ["github-mujoco"] for offset in range(5)}, {"github-mujoco": "rate-limited"})
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(health, "page_watch_ids", return_value=frozenset()), \
                mock.patch.object(health, "release_feed_ids", return_value=frozenset({"github-mujoco"})):
            path = Path(folder) / "history.json"
            path.write_text(json.dumps({"runs": runs}))
            with contextlib.redirect_stdout(io.StringIO()):
                code = health.main(["--history", str(path), "--date", self.TODAY.isoformat(), "--fail-on-struggling"])
        self.assertEqual(code, 0)

    def test_the_same_history_wakes_the_repair_gate_for_an_ordinary_feed(self):
        runs = self.runs({offset: ["plain-feed"] for offset in range(5)})
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(health, "page_watch_ids", return_value=frozenset()), \
                mock.patch.object(health, "release_feed_ids", return_value=frozenset({"github-mujoco"})):
            path = Path(folder) / "history.json"
            path.write_text(json.dumps({"runs": runs}))
            with contextlib.redirect_stdout(io.StringIO()):
                code = health.main(["--history", str(path), "--date", self.TODAY.isoformat(), "--fail-on-struggling"])
        self.assertEqual(code, 1)

    def test_struggling_called_directly_leaves_a_release_feed_out_by_default(self):
        # 04 N1: every production call passes `exclude` through report(); a direct call must not depend on that. The default
        # reads the committed sources, where the three release feeds are `kind: github-releases`.
        runs = self.runs({offset: ["github-mujoco", "github-lerobot", "plain-feed"] for offset in range(5)})
        self.assertEqual([source for source, _ in health.struggling(runs, self.TODAY)], ["plain-feed"])
        self.assertEqual({source for source, _ in health.struggling(runs, self.TODAY, exclude=frozenset())},
                         {"github-mujoco", "github-lerobot", "plain-feed"})

    def test_a_release_feed_is_recognised_by_its_kind_in_the_committed_sources(self):
        self.assertEqual(health.release_feed_ids(), {"github-lerobot", "github-isaaclab", "github-mujoco"})

    def test_a_release_feed_that_answered_today_or_failed_too_few_days_is_not_listed(self):
        releases = frozenset({"github-mujoco"})
        short = self.runs({3: ["github-mujoco"], 4: ["github-mujoco"]})                                # two days: under 3
        recovered = self.runs({0: ["github-mujoco"], 1: ["github-mujoco"], 2: ["github-mujoco"]})      # not in the latest run
        for runs in (short, recovered):
            self.assertEqual(health.release_feeds_failing(runs, self.TODAY, releases=releases), [])

    def test_a_run_log_without_reasons_is_listed_with_none(self):
        runs = self.runs({offset: ["github-mujoco"] for offset in range(5)})
        got = health.release_feeds_failing(runs, self.TODAY, releases=frozenset({"github-mujoco"}))
        self.assertEqual(got, [{"source": "github-mujoco", "failed_days": 5, "reason": None}])


class PipelineTests(unittest.TestCase):
    def test_a_release_with_no_excerpt_passes_the_filters_and_is_enriched(self):
        config = load_config()
        reference = date(2026, 11, 16)
        item = Item(id="github-lerobot:abc", title="LeRobot v0.4.0", url=f"https://github.com/{REPO}/releases/tag/v0.4.0",
                    publisher="LeRobot releases (GitHub)", source_id="github-lerobot", evidence="O", published="2026-11-10",
                    summary="", topical=True)
        kept = prefilter([item], config, reference)
        self.assertEqual([entry.id for entry in kept], ["github-lerobot:abc"])
        self.assertEqual(len(enrich(kept, config, reference)), 1)


class CommittedSourcesTests(unittest.TestCase):
    def test_the_three_release_feeds_are_on_and_read_the_api_not_the_atom_feed(self):
        sources = load_config().sources
        feeds = {entry["id"]: entry for entry in fetch.enabled_feeds(sources)}
        for name in ("github-lerobot", "github-isaaclab", "github-mujoco"):
            with self.subTest(name):
                self.assertIn(name, feeds)
                entry = feeds[name]
                self.assertEqual(entry["kind"], fetch.GITHUB_RELEASES)
                self.assertEqual(entry["url"], fetch.GITHUB_API.format(repo=entry["repo"]))
                self.assertNotIn(".atom", entry["url"])
                self.assertEqual(entry["max_entries"], 1)

    def test_no_enabled_feed_has_an_unknown_kind_or_asks_for_an_atom_file_on_github(self):
        for entry in fetch.enabled_feeds(load_config().sources):
            with self.subTest(entry["id"]):
                self.assertIn(entry.get("kind"), (None, fetch.GITHUB_RELEASES))
                self.assertFalse(entry["url"].startswith("https://github.com/") and entry["url"].endswith(".atom"))

    def test_openpi_is_not_a_release_source(self):
        ids = {entry["id"] for entry in load_config().sources["feeds"]}
        self.assertFalse(any("openpi" in name for name in ids))


if __name__ == "__main__":
    unittest.main()
