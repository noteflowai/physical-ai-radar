"""The robots.txt that the runs read is kept (`robots_store.RobotsTexts`), and the refusals that are new with `robots_rules`
are reported (`robots_newly_denied`), with no request beyond the ones a run makes anyway.

The store tests use a fake clock and a temporary directory. The reader tests use injected getters, the fake Firecrawl and the
test TLS server, and count what was asked. What this does not show: the real sites' files, or a real run.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from pairadar import fetch, heat, robots_store  # noqa: E402
from pairadar.config import Config  # noqa: E402
from static_fixture import HOST, Reply, TlsTestCase  # noqa: E402
from test_page_watch import FakeFirecrawl, LISTING_V1, page  # noqa: E402
from test_page_watch import sources as firecrawl_sources  # noqa: E402
from test_page_watch_static import LISTING, ROBOTS_OK, V1, sources as static_sources  # noqa: E402
from test_sources_mainstream import config_with, rss  # noqa: E402

ORIGIN = f"https://{HOST}"
OTHER = "https://other.example"
T0 = datetime(2026, 11, 16, 0, 0, tzinfo=timezone.utc)
DAY = date(2026, 11, 16)
ATOM_RULE = b"User-agent: *\nDisallow: /*.atom$\n"        # urllib.robotparser reads this as a plain prefix: no match


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


def silent():
    return contextlib.redirect_stdout(io.StringIO())


class TextsStoreTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(tmp.cleanup)
        self.directory = Path(os.path.realpath(tmp.name)) / "state"
        self.clock = Clock()

    def store(self, writable=True):
        return robots_store.RobotsStore(self.directory, writable=writable, clock=self.clock)

    def texts(self):
        return robots_store.RobotsTexts(self.directory, writable=False, clock=self.clock)

    def test_a_file_that_was_read_is_kept_with_its_time_size_and_sha256(self):
        raw = b"User-agent: *\nDisallow: /a\n"
        self.store().record_text(ORIGIN, raw)
        self.assertEqual(self.texts().lookup(ORIGIN),
                         {"fetched_at": T0.isoformat(), "sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw),
                          "body": raw.decode(), "omitted": None})
        path = self.directory / "robots-texts.json"
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_only_the_latest_file_of_a_host_is_kept(self):
        self.store().record_text(ORIGIN, b"User-agent: *\nDisallow: /old\n")
        self.clock.now = T0 + timedelta(hours=7)
        self.store().record_text(ORIGIN, b"User-agent: *\nDisallow: /new\n")
        entry = self.texts().lookup(ORIGIN)
        self.assertEqual((entry["body"], entry["fetched_at"]), ("User-agent: *\nDisallow: /new\n", self.clock.now.isoformat()))
        record = json.loads((self.directory / "robots-texts.json").read_text())
        self.assertEqual(list(record["origins"]), [ORIGIN])

    def test_a_round_that_may_not_write_writes_nothing(self):
        self.store(writable=False).record_text(ORIGIN, b"User-agent: *\n")
        self.assertFalse(self.directory.exists())

    def test_a_file_over_64_kib_keeps_only_its_sha256_and_size(self):
        raw = b"# " + b"x" * robots_store.MAX_TEXT_BYTES + b"\n"
        self.store().record_text(ORIGIN, raw)
        self.assertEqual(self.texts().lookup(ORIGIN),
                         {"fetched_at": T0.isoformat(), "sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw),
                          "body": None, "omitted": "too-large"})
        exactly = b"#" * robots_store.MAX_TEXT_BYTES                      # the limit itself still keeps the text
        self.store().record_text(OTHER, exactly)
        self.assertEqual(self.texts().lookup(OTHER)["body"], exactly.decode())

    def test_a_file_that_is_not_utf_8_keeps_the_sha256_of_its_bytes_and_says_why(self):
        raw = b"User-agent: *\nDisallow: /\xff\xfe\n"
        self.store().record_text(ORIGIN, raw)
        entry = self.texts().lookup(ORIGIN)
        self.assertEqual((entry["sha256"], entry["size"], entry["body"], entry["omitted"]),
                         (hashlib.sha256(raw).hexdigest(), len(raw), None, "not-utf-8"))

    def test_the_text_is_exactly_the_bytes_that_were_read(self):
        raw = "\ufeffUser-agent: *\r\nDisallow: /é\r\n".encode()
        self.store().record_text(ORIGIN, raw)
        entry = self.texts().lookup(ORIGIN)
        self.assertEqual((entry["body"].encode(), entry["sha256"]), (raw, hashlib.sha256(raw).hexdigest()))

    def test_it_is_a_separate_file_and_never_touches_the_waits_in_the_state_file(self):
        store = self.store()
        store.record_backoff(ORIGIN, "http-429", 172_800)
        before = (self.directory / "robots-state.json").read_bytes()
        store.record_text(ORIGIN, b"User-agent: *\nAllow: /\n")
        store.record_text(OTHER, b"User-agent: *\nAllow: /\n")
        self.assertEqual((self.directory / "robots-state.json").read_bytes(), before)
        self.assertEqual(self.store().lookup(ORIGIN), ("backoff", "http-429"))      # the text decides nothing
        self.assertIsNone(self.store().lookup(OTHER))

    def test_the_state_file_does_not_write_or_read_the_texts_file(self):
        store = self.store()
        store.record_rules(ORIGIN, "User-agent: *\nAllow: /\n")
        self.assertFalse((self.directory / "robots-texts.json").exists())

    def test_at_most_64_hosts_are_kept_and_the_oldest_goes_first(self):
        store = self.store()
        for number in range(robots_store.TEXTS_MAX_ORIGINS + 2):
            self.clock.now = T0 + timedelta(minutes=number)
            store.record_text(f"https://h{number}.example", b"User-agent: *\nAllow: /\n")
        kept = json.loads((self.directory / "robots-texts.json").read_text())["origins"]
        self.assertEqual(len(kept), robots_store.TEXTS_MAX_ORIGINS)
        self.assertNotIn("https://h0.example", kept)
        self.assertNotIn("https://h1.example", kept)
        self.assertIn("https://h65.example", kept)

    def test_a_file_that_does_not_say_what_the_module_writes_is_ignored_and_replaced(self):
        self.store().record_text(ORIGIN, b"User-agent: *\nAllow: /\n")
        path = self.directory / "robots-texts.json"
        record = json.loads(path.read_text())
        for name, change in {"a body that is not the sha256": lambda e: e.update(body="User-agent: *\nDisallow: /\n"),
                             "an extra key": lambda e: e.update(extra=1),
                             "a size that is wrong": lambda e: e.update(size=3),
                             "a missing text with no reason": lambda e: e.update(body=None),
                             "a text with a reason": lambda e: e.update(omitted="too-large")}.items():
            with self.subTest(name):
                bad = json.loads(json.dumps(record))
                change(bad["origins"][ORIGIN])
                path.write_text(json.dumps(bad))
                path.chmod(0o600)
                self.assertIsNone(self.texts().lookup(ORIGIN))
        path.write_text(json.dumps(record))
        path.chmod(0o600)
        self.assertIsNotNone(self.texts().lookup(ORIGIN))

    def test_a_file_that_is_too_loose_or_a_link_is_not_read(self):
        self.store().record_text(ORIGIN, b"User-agent: *\nAllow: /\n")
        path = self.directory / "robots-texts.json"
        path.chmod(0o644)
        self.assertIsNone(self.texts().lookup(ORIGIN))
        path.chmod(0o600)
        self.assertIsNotNone(self.texts().lookup(ORIGIN))
        copy = self.directory / "real.json"
        path.rename(copy)
        path.symlink_to(copy)
        self.assertIsNone(self.texts().lookup(ORIGIN))

    def test_a_directory_that_is_not_private_is_neither_read_nor_written(self):
        self.store().record_text(ORIGIN, b"User-agent: *\nAllow: /\n")
        self.directory.chmod(0o755)
        self.assertIsNone(self.texts().lookup(ORIGIN))
        with silent():
            self.store().record_text(OTHER, b"User-agent: *\nAllow: /\n")
        self.directory.chmod(0o700)
        self.assertIsNone(self.texts().lookup(OTHER))

    def test_an_origin_that_is_not_an_https_host_is_not_kept(self):
        for origin in ("http://x.example", "https://x.example/path", "x.example", ""):
            with self.subTest(origin):
                self.store().record_text(origin, b"User-agent: *\n")
        self.assertFalse((self.directory / "robots-texts.json").exists())

    def test_a_write_that_fails_is_reported_and_changes_nothing_else(self):
        store = self.store()
        with mock.patch.object(robots_store, "_sync_chain", side_effect=OSError("disk")), silent() as out:
            store.record_text(ORIGIN, b"User-agent: *\nAllow: /\n")
        self.assertIn("robots state not written", out.getvalue())


class FeedReaderTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(tmp.cleanup)
        self.directory = Path(os.path.realpath(tmp.name)) / "state"
        self.clock = Clock()

    def store(self, writable=True):
        return robots_store.RobotsStore(self.directory, writable=writable, clock=self.clock)

    def cache(self, answers, store="default"):
        asked = []

        def getter(url):
            asked.append(url)
            return answers[url.split("/robots.txt")[0]]
        store = self.store() if store == "default" else store
        return fetch.RobotsCache(getter=getter, store=store), asked

    def verdict(self, cache, url):
        with mock.patch.object(fetch.time, "sleep"), silent():
            return cache.verdict(url)

    def test_a_file_the_feeds_read_is_kept_and_nothing_else_is_asked(self):
        cache, asked = self.cache({"https://a.example": (200, ATOM_RULE)})
        self.assertEqual(self.verdict(cache, "https://a.example/x.atom"), fetch.DISALLOW)
        self.assertEqual(self.verdict(cache, "https://a.example/y"), fetch.ALLOW)
        self.assertEqual(asked, ["https://a.example/robots.txt"])
        entry = robots_store.RobotsTexts(self.directory, writable=False).lookup("https://a.example")
        self.assertEqual((entry["body"], entry["sha256"]), (ATOM_RULE.decode(), hashlib.sha256(ATOM_RULE).hexdigest()))

    def test_a_file_that_cannot_be_judged_is_kept_too_by_its_bytes(self):
        raw = b"User-agent: *\nDisallow: /a%zz\n"
        cache, _ = self.cache({"https://a.example": (200, raw)})
        self.assertEqual(self.verdict(cache, "https://a.example/x"), fetch.UNREACHABLE)
        self.assertEqual(robots_store.RobotsTexts(self.directory, writable=False).lookup("https://a.example")["sha256"],
                         hashlib.sha256(raw).hexdigest())

    def test_an_answer_that_is_not_a_file_is_not_kept(self):
        for status, body in ((403, b"no"), (404, b"gone"), (429, b""), (500, b"oops"), (None, None)):
            with self.subTest(status):
                cache, _ = self.cache({"https://a.example": (status, body)})
                self.verdict(cache, "https://a.example/x")
        self.assertFalse((self.directory / "robots-texts.json").exists())

    def test_a_round_that_may_not_write_keeps_nothing_and_decides_the_same(self):
        cache, _ = self.cache({"https://a.example": (200, ATOM_RULE)}, store=self.store(writable=False))
        self.assertEqual(self.verdict(cache, "https://a.example/x.atom"), fetch.DISALLOW)
        self.assertFalse(self.directory.exists())

    def test_without_a_store_nothing_is_kept_and_the_verdict_is_the_same(self):
        cache, _ = self.cache({"https://a.example": (200, ATOM_RULE)}, store=None)
        self.assertEqual(self.verdict(cache, "https://a.example/x.atom"), fetch.DISALLOW)

    def test_a_feed_the_wildcard_rule_refuses_and_the_old_reader_allowed_is_newly_denied(self):
        answers = {"https://k.example": (200, ATOM_RULE),
                   "https://j.example": (200, b"User-agent: *\nDisallow: /action/\n"),
                   "https://f.example": (403, b"no"), "https://u.example": (503, b"")}
        config = config_with(feeds=[
            {"id": "gh", "url": "https://k.example/o/r/releases.atom", "evidence": "O"},
            {"id": "plain", "url": "https://j.example/action/feed", "evidence": "O"},
            {"id": "forbidden", "url": "https://f.example/feed", "evidence": "O"},
            {"id": "down", "url": "https://u.example/feed", "evidence": "O"},
            {"id": "fine", "url": "https://k.example/feed", "evidence": "O"}])
        cache, asked = self.cache(answers)
        skipped, reasons, newly = [], {}, {}
        with mock.patch.object(fetch, "http_get", return_value=rss(("A robot", "https://k.example/a", "robot"))) as get, \
                mock.patch.object(fetch.time, "sleep"), silent():
            fetch.fetch_feeds(config, robots=cache, skipped=skipped, reasons=reasons, newly_denied=newly)
        self.assertEqual(sorted(skipped), ["down", "forbidden", "gh", "plain"])
        self.assertEqual(newly, {"gh": fetch.DISALLOW})                   # `plain` was refused before too; the others are not files
        self.assertEqual(sorted(asked), sorted(f"{origin}/robots.txt" for origin in answers))        # one request per host
        self.assertNotIn("https://k.example/o/r/releases.atom", [call.args[0] for call in get.call_args_list])

    def test_a_file_that_cannot_be_judged_makes_a_feed_the_old_reader_read_newly_denied_as_unreachable(self):
        config = config_with(feeds=[{"id": "odd", "url": "https://k.example/feed", "evidence": "O"}])
        cache, _ = self.cache({"https://k.example": (200, b"User-agent: *\nDisallow: /a%zz\n")})
        skipped, newly = [], {}
        with mock.patch.object(fetch, "http_get", return_value=None), mock.patch.object(fetch.time, "sleep"), silent():
            fetch.fetch_feeds(config, robots=cache, skipped=skipped, newly_denied=newly)
        self.assertEqual((skipped, newly), (["odd"], {"odd": fetch.UNREACHABLE}))

    def test_a_signal_the_new_reader_refuses_and_the_old_allowed_is_newly_denied(self):
        cache, _ = self.cache({"https://api.example": (200, b"User-agent: *\nDisallow: /*/stars$\n")})
        signals = heat.Signals()
        with mock.patch.object(fetch.time, "sleep"), silent():
            heat._json("https://api.example/v1/stars", "stars", signals, cache, lambda url: b"[]")
        self.assertEqual((signals.robots_skipped, signals.robots_newly_denied), (["stars"], {"stars": fetch.DISALLOW}))

    def test_the_report_and_its_dictionary_carry_the_refusals(self):
        report = fetch.FetchReport(robots_newly_denied={"b": "disallowed", "a": "refused"})
        self.assertEqual(list(report.to_dict()["robots_newly_denied"].items()), [("a", "refused"), ("b", "disallowed")])
        self.assertEqual(fetch.FetchReport().to_dict()["robots_newly_denied"], {})


class PageReaderTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(tmp.cleanup)
        self.directory = Path(os.path.realpath(tmp.name)) / "state"
        self.store = robots_store.RobotsStore(self.directory, writable=True, clock=Clock())

    def robots(self, fake, text, status=200):
        fake.serve("https://www.figure.ai/robots.txt",
                   {"success": True, "data": {"rawHtml": text, "metadata": {"statusCode": status}}})

    def test_a_page_the_wildcard_rule_refuses_and_the_old_reader_allowed_is_newly_denied_and_not_requested(self):
        raw = firecrawl_sources()
        raw["page_watch"] = raw["page_watch"][:1]
        with FakeFirecrawl() as fake, mock.patch.dict(os.environ, {fetch.FIRECRAWL_ENV: fake.url}):
            self.robots(fake, "User-agent: *\nDisallow: /*s$\n")
            fake.serve("https://www.figure.ai/news", page(LISTING_V1))
            with silent():
                result = fetch.fetch_page_watch(Config(sources=raw, taxonomy={}, glossary={}, baseline={}), DAY, None, self.store)
            self.assertEqual((fake.scraped(), result.robots_skipped, result.robots_newly_denied),
                             ([], ["figure"], ["figure"]))
        entry = robots_store.RobotsTexts(self.directory, writable=False).lookup("https://www.figure.ai")
        self.assertEqual(entry["body"], "User-agent: *\nDisallow: /*s$\n")

    def test_a_page_both_readers_refuse_is_not_newly_denied(self):
        raw = firecrawl_sources()
        raw["page_watch"] = raw["page_watch"][:1]
        for text in ("User-agent: *\nDisallow: /news\n", "User-agent: FirecrawlAgent\nDisallow: /\n"):
            with self.subTest(text), FakeFirecrawl() as fake, mock.patch.dict(os.environ, {fetch.FIRECRAWL_ENV: fake.url}):
                self.robots(fake, text)
                with silent():
                    result = fetch.fetch_page_watch(Config(sources=raw, taxonomy={}, glossary={}, baseline={}), DAY, None)
                self.assertEqual((result.robots_skipped, result.robots_newly_denied), (["figure"], []))

    def test_an_answer_that_is_not_a_file_is_neither_kept_nor_newly_denied(self):
        for status in (403, 404, 429, 500):
            with self.subTest(status), FakeFirecrawl() as fake:
                self.robots(fake, "", status)
                cache = fetch._PageRobots(self.store)
                allowed = fetch._robots_allows({"endpoint": fake.url, "timeout": 5}, "https://www.figure.ai/news", cache)
                self.assertEqual((allowed, fetch._page_previously_allowed(cache, "https://www.figure.ai/news")),
                                 (status == 404, False))
        self.assertFalse((self.directory / "robots-texts.json").exists())

    def test_a_plain_dictionary_as_the_cache_still_works(self):
        with FakeFirecrawl() as fake:
            self.robots(fake, "User-agent: *\nAllow: /\n")
            self.assertTrue(fetch._robots_allows({"endpoint": fake.url, "timeout": 5}, "https://www.figure.ai/news", {}))
        self.assertFalse(fetch._page_previously_allowed({}, "https://www.figure.ai/news"))


class UnsupportedUrlTests(unittest.TestCase):
    """04's N1: a URL with a `%` that is not an escape cannot be judged. That URL is refused (`unsupported-url`); the host's
    other URLs, the other feeds and the other pages of the same run are read as before."""

    VALID = b"User-agent: *\nDisallow: /private\n"

    def test_the_cache_refuses_that_url_alone_and_does_not_ask_again(self):
        asked = []
        cache = fetch.RobotsCache(getter=lambda url: asked.append(url) or (200, self.VALID))
        with mock.patch.object(fetch.time, "sleep"), silent():
            got = [cache.verdict(url) for url in ("https://k.example/feed%zz.xml", "https://k.example/feed",
                                                  "https://k.example/a%", "https://k.example/private/x",
                                                  "https://k.example/ok%41")]
        self.assertEqual(got, [fetch.UNSUPPORTED_URL, fetch.ALLOW, fetch.UNSUPPORTED_URL, fetch.DISALLOW, fetch.ALLOW])
        self.assertEqual(asked, ["https://k.example/robots.txt"])

    def test_one_feed_with_such_a_url_does_not_stop_the_others(self):
        config = config_with(feeds=[{"id": "bad", "url": "https://k.example/feed%zz.xml", "evidence": "O", "topical": True},
                                    {"id": "before", "url": "https://j.example/feed", "evidence": "O", "topical": True},
                                    {"id": "after", "url": "https://k.example/feed", "evidence": "O", "topical": True}])
        requested = []

        def get(url, *a, **k):
            requested.append(url)
            return rss(("A robot", f"{url}/a", "robot"))
        skipped, reasons, newly = [], {}, {}
        cache = fetch.RobotsCache(getter=lambda url: (200, self.VALID))
        with mock.patch.object(fetch, "http_get", get), mock.patch.object(fetch.time, "sleep"), silent():
            items, failed = fetch.fetch_feeds(config, robots=cache, skipped=skipped, reasons=reasons, newly_denied=newly)
        self.assertEqual((skipped, reasons, failed), (["bad"], {"bad": fetch.UNSUPPORTED_URL}, []))
        self.assertEqual(sorted({entry.source_id for entry in items}), ["after", "before"])
        self.assertEqual(requested, ["https://j.example/feed", "https://k.example/feed"])        # the bad URL was never requested
        self.assertEqual(newly, {"bad": fetch.UNSUPPORTED_URL})                                    # the old reader would have read it

    def test_a_signal_with_such_a_url_is_skipped_and_the_next_is_read(self):
        cache = fetch.RobotsCache(getter=lambda url: (200, self.VALID))
        signals = heat.Signals()
        with mock.patch.object(fetch.time, "sleep"), silent():
            self.assertIsNone(heat._json("https://api.example/v1/s%zz", "bad", signals, cache, lambda url: b"[]"))
            self.assertEqual(heat._json("https://api.example/v1/ok", "ok", signals, cache, lambda url: b"[1]"), [1])
        self.assertEqual((signals.robots_skipped, signals.robots_reasons), (["bad"], {"bad": fetch.UNSUPPORTED_URL}))

    def test_a_page_with_such_a_url_is_refused_and_the_other_page_of_the_run_is_read(self):
        raw = firecrawl_sources()
        figure = raw["page_watch"][0]
        raw["page_watch"] = [{**figure, "id": "figure-bad", "url": "https://www.figure.ai/news%zz"}, figure]
        with FakeFirecrawl() as fake, mock.patch.dict(os.environ, {fetch.FIRECRAWL_ENV: fake.url}):
            fake.serve("https://www.figure.ai/robots.txt",
                       {"success": True, "data": {"rawHtml": "User-agent: *\nAllow: /\n", "metadata": {"statusCode": 200}}})
            fake.serve("https://www.figure.ai/news", page(LISTING_V1))
            with silent():
                result = fetch.fetch_page_watch(Config(sources=raw, taxonomy={}, glossary={}, baseline={}), DAY, None)
            self.assertEqual(fake.scraped(), ["https://www.figure.ai/news"])
        self.assertEqual((result.robots_skipped, result.checked, result.failed), (["figure-bad"], ["figure"], []))
        self.assertEqual(result.sources["figure-bad"]["reason"], fetch.UNSUPPORTED_URL)
        self.assertEqual(result.sources["figure"]["outcome"], "checked")

    def test_the_page_check_alone_refuses_that_url_and_a_plain_dictionary_cache_still_works(self):
        for cache in (fetch._PageRobots(), {}):
            with self.subTest(type(cache).__name__), FakeFirecrawl() as fake:
                fake.serve("https://www.figure.ai/robots.txt",
                           {"success": True, "data": {"rawHtml": "User-agent: *\nAllow: /\n", "metadata": {"statusCode": 200}}})
                settings = {"endpoint": fake.url, "timeout": 5}
                self.assertFalse(fetch._robots_allows(settings, "https://www.figure.ai/news%zz", cache))
                self.assertTrue(fetch._robots_allows(settings, "https://www.figure.ai/news", cache))
                self.assertEqual(len(fake.requests), 1)                                           # robots.txt, asked once
                if isinstance(cache, fetch._PageRobots):
                    self.assertEqual(cache.unsupported, {"https://www.figure.ai/news%zz"})


class Answer:
    """What `fetch._urlopen` (urllib's opener) gives `http_get`: a status, the URL it came from after redirects, headers and a body."""

    def __init__(self, url, body, content_type="text/plain; charset=utf-8", status=200):
        self.status, self._url, self._body = status, url, body
        self.headers = {} if content_type is None else {"Content-Type": content_type}

    def geturl(self):
        return self._url

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


HTML_LOGIN = b"<html><body>Sign in to continue private-capture-marker</body></html>"


class ResponseIdentityTests(unittest.TestCase):
    """01's F1: a 2xx answer is a robots.txt only if it came from that origin's /robots.txt and is not an HTML page. Anything
    else makes the host unreachable for the run and keeps no text. A real empty robots.txt is still an empty robots.txt."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(tmp.cleanup)
        self.directory = Path(os.path.realpath(tmp.name)) / "state"
        self.store = robots_store.RobotsStore(self.directory, writable=True, clock=Clock())

    def kept(self, origin="https://a.example"):
        return robots_store.RobotsTexts(self.directory, writable=False).lookup(origin)

    def through_urlopen(self, answer, url="https://a.example/feed"):
        cache = fetch.RobotsCache(store=self.store)
        with mock.patch.object(fetch, "_urlopen", lambda request, timeout=None: answer), \
                mock.patch.object(fetch.time, "sleep"), silent():
            return cache, cache.verdict(url)

    def test_an_html_login_page_answered_as_robots_txt_is_unreachable_and_not_kept(self):
        for content_type in ("text/html", "text/html; charset=utf-8", "TEXT/HTML"):
            with self.subTest(content_type):
                cache, verdict = self.through_urlopen(Answer("https://a.example/robots.txt", HTML_LOGIN, content_type))
                self.assertEqual(verdict, fetch.UNREACHABLE)
                self.assertIsNone(self.kept())
        self.assertFalse((self.directory / "robots-texts.json").exists())
        self.assertTrue(cache.previously_allowed("https://a.example/feed"))     # the old reader read it as "no rules": shown

    def test_a_redirect_to_another_path_or_host_is_not_the_robots_txt(self):
        for final in ("https://a.example/login", "https://a.example/robots.txt.html", "https://login.a.example/robots.txt",
                      "https://b.example/robots.txt", "http://a.example/robots.txt", "https://a.example/",
                      "https://a.example/sub/robots.txt"):
            with self.subTest(final):
                _, verdict = self.through_urlopen(Answer(final, b"User-agent: *\nAllow: /\n", "text/plain"))
                self.assertEqual(verdict, fetch.UNREACHABLE)
        self.assertFalse((self.directory / "robots-texts.json").exists())

    def test_another_media_type_than_text_plain_is_not_a_robots_txt(self):
        for content_type in ("application/octet-stream", "text/xml", "application/xhtml+xml", "application/json", ""):
            with self.subTest(content_type):
                _, verdict = self.through_urlopen(Answer("https://a.example/robots.txt", b"User-agent: *\n", content_type))
                self.assertEqual(verdict, fetch.UNREACHABLE)
        self.assertFalse((self.directory / "robots-texts.json").exists())

    def test_text_plain_from_robots_txt_with_or_without_a_type_is_read_and_kept(self):
        body = b"User-agent: *\nDisallow: /private\n"
        for name, answer in {"with parameters": Answer("https://a.example/robots.txt", body, "text/plain; charset=UTF-8"),
                             "no content type": Answer("https://a.example/robots.txt", body, None),
                             "plain": Answer("https://a.example/robots.txt", body, "text/plain")}.items():
            with self.subTest(name):
                cache, verdict = self.through_urlopen(answer, "https://a.example/private/x")
                self.assertEqual(verdict, fetch.DISALLOW)
                self.assertEqual(self.kept()["body"], body.decode())

    def test_a_real_empty_robots_txt_allows_everything_and_is_kept_as_empty(self):
        _, verdict = self.through_urlopen(Answer("https://a.example/robots.txt", b"", "text/plain"))
        self.assertEqual(verdict, fetch.ALLOW)
        entry = self.kept()
        self.assertEqual((entry["body"], entry["size"], entry["sha256"]), ("", 0, hashlib.sha256(b"").hexdigest()))

    def test_a_feed_on_such_a_host_is_skipped_unreachable_not_requested_and_the_others_are_read(self):
        config = config_with(feeds=[{"id": "login", "url": "https://a.example/feed", "evidence": "O"},
                                    {"id": "fine", "url": "https://b.example/feed", "evidence": "O"}])
        requested = []

        def urlopen(request, timeout=None):
            requested.append(request.full_url)
            if request.full_url == "https://a.example/robots.txt":
                return Answer("https://a.example/login", HTML_LOGIN, "text/html")
            if request.full_url == "https://b.example/robots.txt":
                return Answer("https://b.example/robots.txt", b"User-agent: *\nAllow: /\n", "text/plain")
            return Answer(request.full_url, rss(("A robot", "https://b.example/a", "robot")), "application/rss+xml")
        skipped, reasons, newly = [], {}, {}
        with mock.patch.object(fetch, "_urlopen", urlopen), mock.patch.object(fetch.time, "sleep"), silent():
            items, failed = fetch.fetch_feeds(config, robots=fetch.RobotsCache(store=self.store), skipped=skipped,
                                              reasons=reasons, newly_denied=newly)
        self.assertEqual((skipped, reasons, failed), (["login"], {"login": fetch.UNREACHABLE}, []))
        self.assertNotIn("https://a.example/feed", requested)
        self.assertEqual([entry.source_id for entry in items], ["fine"])
        self.assertEqual(newly, {"login": fetch.UNREACHABLE})
        self.assertIsNone(self.kept("https://a.example"))
        self.assertIsNotNone(self.kept("https://b.example"))


class FirecrawlIdentityTests(unittest.TestCase):
    """01's F1 for the Firecrawl pages (contentType and sourceURL take part), and the unpaired surrogate of F2."""

    URL = "https://www.figure.ai/robots.txt"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(tmp.cleanup)
        self.directory = Path(os.path.realpath(tmp.name)) / "state"
        self.store = robots_store.RobotsStore(self.directory, writable=True, clock=Clock())

    def allowed(self, raw_html, metadata, source=True):
        cache = fetch._PageRobots(self.store)
        with FakeFirecrawl() as fake:
            fake.serve(self.URL, {"success": True, "data": {"rawHtml": raw_html, "metadata": metadata}}, source=source)
            return fetch._robots_allows({"endpoint": fake.url, "timeout": 5}, "https://www.figure.ai/news", cache), cache

    def kept(self):
        return robots_store.RobotsTexts(self.directory, writable=False).lookup("https://www.figure.ai")

    def test_01s_counterexample_an_html_login_page_with_a_login_source_is_refused_and_not_kept(self):
        allowed, cache = self.allowed("<html>Sign in private-capture-marker</html>",
                                      {"statusCode": 200, "contentType": "text/html; charset=utf-8",
                                       "sourceURL": "https://www.figure.ai/login"})
        self.assertFalse(allowed)
        self.assertIsNone(self.kept())
        self.assertFalse((self.directory / "robots-texts.json").exists())
        self.assertTrue(fetch._page_previously_allowed(cache, "https://www.figure.ai/news"))       # shown as newly denied

    def test_each_of_the_two_is_enough_to_refuse(self):
        text = "User-agent: *\nAllow: /\n"
        cases = {"html with the right source": {"statusCode": 200, "contentType": "text/html", "sourceURL": self.URL},
                 "text with a login source": {"statusCode": 200, "contentType": "text/plain",
                                              "sourceURL": "https://www.figure.ai/login"},
                 "text with another host as the source": {"statusCode": 200, "contentType": "text/plain",
                                                          "sourceURL": "https://www.example.com/robots.txt"},
                 "a final url that is another page": {"statusCode": 200, "contentType": "text/plain", "sourceURL": self.URL,
                                                      "url": "https://www.figure.ai/login"},
                 "another media type": {"statusCode": 200, "contentType": "application/json", "sourceURL": self.URL},
                 "a source that is not text": {"statusCode": 200, "contentType": "text/plain", "sourceURL": 5}}
        for name, metadata in cases.items():
            with self.subTest(name):
                allowed, _ = self.allowed(text, metadata)
                self.assertFalse(allowed)
        self.assertFalse((self.directory / "robots-texts.json").exists())

    def test_a_reply_that_names_no_source_is_not_confirmed(self):
        allowed, _ = self.allowed("User-agent: *\nAllow: /\n", {"statusCode": 200, "contentType": "text/plain"}, source=False)
        self.assertFalse(allowed)
        self.assertIsNone(self.kept())

    def test_the_robots_txt_itself_is_read_and_kept_with_or_without_a_content_type(self):
        for name, metadata in {"with parameters": {"statusCode": 200, "contentType": "text/plain; charset=utf-8",
                                                   "sourceURL": self.URL, "url": self.URL},
                               "no content type": {"statusCode": 200, "sourceURL": self.URL}}.items():
            with self.subTest(name):
                allowed, _ = self.allowed("User-agent: *\nDisallow: /other\n", metadata)
                self.assertTrue(allowed)
                self.assertEqual(self.kept()["body"], "User-agent: *\nDisallow: /other\n")

    def test_a_real_empty_robots_txt_is_still_empty_and_allows(self):
        allowed, _ = self.allowed("", {"statusCode": 200, "contentType": "text/plain", "sourceURL": self.URL})
        self.assertTrue(allowed)
        self.assertEqual((self.kept()["body"], self.kept()["size"]), ("", 0))

    def test_text_with_an_unpaired_surrogate_is_refused_without_raising_and_not_kept(self):
        allowed, _ = self.allowed("User-agent: *\nAllow: /\n\ud800", {"statusCode": 200, "contentType": "text/plain",
                                                                    "sourceURL": self.URL})
        self.assertFalse(allowed)
        self.assertFalse((self.directory / "robots-texts.json").exists())

    def test_a_page_run_with_the_login_reply_reads_no_page_and_keeps_nothing(self):
        raw = firecrawl_sources()
        raw["page_watch"] = raw["page_watch"][:1]
        with FakeFirecrawl() as fake, mock.patch.dict(os.environ, {fetch.FIRECRAWL_ENV: fake.url}):
            fake.serve(self.URL, {"success": True, "data": {"rawHtml": "<html>login</html>", "metadata": {
                "statusCode": 200, "contentType": "text/html", "sourceURL": "https://www.figure.ai/login"}}})
            fake.serve("https://www.figure.ai/news", page(LISTING_V1))
            with silent():
                result = fetch.fetch_page_watch(Config(sources=raw, taxonomy={}, glossary={}, baseline={}), DAY, None, self.store)
            self.assertEqual(fake.scraped(), [])
        self.assertEqual((result.robots_skipped, result.checked, result.robots_newly_denied), (["figure"], [], ["figure"]))
        self.assertFalse((self.directory / "robots-texts.json").exists())


class HtmlAndIdentityEdgeTests(unittest.TestCase):
    """01's round 2 (D1, D2): a body that is an HTML page is not a robots.txt whatever its Content-Type says or does not say;
    Firecrawl must name the final address; an address that cannot be parsed refuses that one source and raises nothing."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(tmp.cleanup)
        self.directory = Path(os.path.realpath(tmp.name)) / "state"
        self.store = robots_store.RobotsStore(self.directory, writable=True, clock=Clock())

    def no_file(self):
        self.assertFalse((self.directory / "robots-texts.json").exists())

    def feeds(self, answer, url="https://a.example/feed"):
        cache = fetch.RobotsCache(store=self.store)
        with mock.patch.object(fetch, "_urlopen", lambda request, timeout=None: answer), \
                mock.patch.object(fetch.time, "sleep"), silent():
            return cache.verdict(url)

    def firecrawl(self, raw_html, metadata, source=False):
        cache = fetch._PageRobots(self.store)
        with FakeFirecrawl() as fake:
            fake.serve("https://www.figure.ai/robots.txt",
                       {"success": True, "data": {"rawHtml": raw_html, "metadata": metadata}}, source=source)
            return fetch._robots_allows({"endpoint": fake.url, "timeout": 5}, "https://www.figure.ai/news", cache)

    def test_d1_html_with_no_content_type_is_refused_and_not_kept(self):
        self.assertEqual(self.feeds(Answer("https://a.example/robots.txt", HTML_LOGIN, None)), fetch.UNREACHABLE)
        self.no_file()

    def test_d1_html_is_refused_whatever_the_content_type_says(self):
        for content_type in (None, "text/plain", "text/plain; charset=utf-8"):
            with self.subTest(content_type):
                self.assertEqual(self.feeds(Answer("https://a.example/robots.txt", HTML_LOGIN, content_type)),
                                 fetch.UNREACHABLE)
        self.no_file()

    def test_d1_every_way_a_body_looks_like_html(self):
        bodies = {"starts with a tag": b"<p>Sign in</p>", "after white space": b"  \r\n\t<div>x</div>",
                  "after a byte order mark": b"\xef\xbb\xbf<html>", "an html tag later": b"Sorry, moved.\n<HTML lang=en>",
                  "a doctype later": b"Sorry.\n<!DocType html>", "a head later": b"Sorry.\n<Head><title>x</title>",
                  "a body later": b"Sorry.\n<BODY>", "an xml feed": b'<?xml version="1.0"?><rss/>'}
        for name, body in bodies.items():
            with self.subTest(name):
                self.assertTrue(fetch._looks_like_html(body), name)
                self.assertTrue(fetch._looks_like_html(body.decode("utf-8")), name)
                self.assertEqual(self.feeds(Answer("https://a.example/robots.txt", body, "text/plain")), fetch.UNREACHABLE)
        self.no_file()

    def test_d1_what_is_not_html(self):
        for name, body in {"empty": b"", "only white space": b" \n", "a robots.txt": b"User-agent: *\nDisallow: /a\n",
                           "a comment that names html": b"# html pages are fine\nUser-agent: *\nAllow: /\n",
                           "a sitemap line": b"Sitemap: https://a.example/s.xml\n",
                           "a greater-than sign": b"User-agent: *\nDisallow: /a>b\n"}.items():
            with self.subTest(name):
                self.assertFalse(fetch._looks_like_html(body), name)

    def test_d1_the_controls_are_unchanged(self):
        empty = self.feeds(Answer("https://a.example/robots.txt", b"", "text/plain"))
        self.assertEqual(empty, fetch.ALLOW)
        self.assertEqual(robots_store.RobotsTexts(self.directory, writable=False).lookup("https://a.example")["body"], "")
        normal = self.feeds(Answer("https://a.example/robots.txt", b"User-agent: *\nDisallow: /private\n", None),
                            "https://a.example/private/x")
        self.assertEqual(normal, fetch.DISALLOW)
        self.assertEqual(self.feeds(Answer("https://a.example/robots.txt", b"User-agent: *\nAllow: /\n", None),
                                    "https://a.example/feed%zz.xml"), fetch.UNSUPPORTED_URL)               # N1: that URL alone

    def test_d1_firecrawl_html_with_only_a_source_url_is_refused_and_not_kept(self):
        for name, metadata in {"source only, no type": {"statusCode": 200, "sourceURL": "https://www.figure.ai/robots.txt"},
                               "source only, text/plain": {"statusCode": 200, "contentType": "text/plain",
                                                           "sourceURL": "https://www.figure.ai/robots.txt"}}.items():
            with self.subTest(name):
                self.assertFalse(self.firecrawl("<html><body>Sign in</body></html>", metadata))
        self.no_file()

    def test_d1_firecrawl_needs_the_final_address_even_for_a_real_robots_txt(self):
        text = "User-agent: *\nAllow: /\n"
        self.assertFalse(self.firecrawl(text, {"statusCode": 200, "contentType": "text/plain",
                                               "sourceURL": "https://www.figure.ai/robots.txt"}))     # final address unknown
        self.no_file()
        self.assertTrue(self.firecrawl(text, {"statusCode": 200, "contentType": "text/plain",
                                              "sourceURL": "https://www.figure.ai/robots.txt",
                                              "url": "https://www.figure.ai/robots.txt"}))
        self.assertEqual(robots_store.RobotsTexts(self.directory, writable=False).lookup("https://www.figure.ai")["body"], text)

    def test_d1_firecrawl_html_with_a_robots_looking_address_and_type_is_still_refused(self):
        self.assertFalse(self.firecrawl("<!doctype html><title>x</title>", {
            "statusCode": 200, "contentType": "text/plain", "sourceURL": "https://www.figure.ai/robots.txt",
            "url": "https://www.figure.ai/robots.txt"}))
        self.no_file()

    def test_d2_an_address_that_cannot_be_parsed_refuses_that_source_and_raises_nothing(self):
        for final in ("https://[invalid/robots.txt", "https://[::1/robots.txt", "http://[x]/robots.txt", "https://a.example:abc/robots.txt"):
            with self.subTest(final):
                self.assertEqual(self.feeds(Answer(final, b"User-agent: *\nAllow: /\n", "text/plain")), fetch.UNREACHABLE)
        for metadata in ({"sourceURL": "https://[invalid/robots.txt", "url": "https://www.figure.ai/robots.txt"},
                         {"sourceURL": "https://www.figure.ai/robots.txt", "url": "https://[invalid/robots.txt"},
                         {"sourceURL": "https://[invalid/robots.txt", "url": "https://[invalid/robots.txt"}):
            with self.subTest(metadata):
                self.assertFalse(self.firecrawl("User-agent: *\nAllow: /\n", {"statusCode": 200, **metadata}))
        self.no_file()

    def test_d2_any_other_identity_is_not_confirmed_and_never_raises(self):
        for final in (None, 5, b"https://a.example/robots.txt", ["https://a.example/robots.txt"], "", "\x00", "https://a.example"):
            with self.subTest(repr(final)):
                self.assertFalse(fetch._is_robots_reply("https://a.example", final, None))
        self.assertTrue(fetch._is_robots_reply("https://a.example", "https://a.example/robots.txt", None))

    def test_d2_the_other_feeds_of_the_run_are_still_read(self):
        config = config_with(feeds=[{"id": "bad", "url": "https://a.example/feed", "evidence": "O"},
                                    {"id": "fine", "url": "https://b.example/feed", "evidence": "O"}])

        def urlopen(request, timeout=None):
            if request.full_url == "https://a.example/robots.txt":
                return Answer("https://[invalid/robots.txt", b"User-agent: *\nAllow: /\n", "text/plain")
            if request.full_url == "https://b.example/robots.txt":
                return Answer("https://b.example/robots.txt", b"User-agent: *\nAllow: /\n", "text/plain")
            return Answer(request.full_url, rss(("A robot", "https://b.example/a", "robot")), "application/rss+xml")
        skipped, reasons = [], {}
        with mock.patch.object(fetch, "_urlopen", urlopen), mock.patch.object(fetch.time, "sleep"), silent():
            items, failed = fetch.fetch_feeds(config, robots=fetch.RobotsCache(store=self.store), skipped=skipped,
                                              reasons=reasons)
        self.assertEqual((skipped, reasons, failed), (["bad"], {"bad": fetch.UNREACHABLE}, []))
        self.assertEqual([entry.source_id for entry in items], ["fine"])

    def test_d2_a_page_run_with_an_unparseable_address_reads_no_page_and_the_run_goes_on(self):
        raw = firecrawl_sources()
        raw["page_watch"] = raw["page_watch"][:1]
        with FakeFirecrawl() as fake, mock.patch.dict(os.environ, {fetch.FIRECRAWL_ENV: fake.url}):
            fake.serve("https://www.figure.ai/robots.txt", {"success": True, "data": {"rawHtml": "User-agent: *\nAllow: /\n",
                       "metadata": {"statusCode": 200, "sourceURL": "https://[invalid/robots.txt",
                                    "url": "https://www.figure.ai/robots.txt"}}}, source=False)
            fake.serve("https://www.figure.ai/news", page(LISTING_V1))
            with silent():
                result = fetch.fetch_page_watch(Config(sources=raw, taxonomy={}, glossary={}, baseline={}), DAY, None, self.store)
            self.assertEqual(fake.scraped(), [])
        self.assertEqual((result.robots_skipped, result.failed), (["figure"], []))
        self.no_file()


class StaticReaderTests(TlsTestCase):
    def setUp(self) -> None:
        super().setUp()
        tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(tmp.cleanup)
        self.directory = Path(os.path.realpath(tmp.name)) / "state"
        self.clock = Clock()
        self.server.serve("/news", Reply(200, V1))

    def run_once(self, store):
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.dict(os.environ, {fetch.FIRECRAWL_ENV: "http://127.0.0.1:9", "GITHUB_ACTIONS": ""}))
            stack.enter_context(mock.patch.object(fetch, "STATIC_FETCHER_FACTORY", self.fetcher))
            stack.enter_context(silent())
            return fetch.fetch_page_watch(Config(sources=static_sources(LISTING), taxonomy={}, glossary={}, baseline={}),
                                          DAY, None, store)

    def test_the_file_a_static_source_read_is_kept_with_its_sha256(self):
        self.server.serve("/robots.txt", ROBOTS_OK)
        store = robots_store.RobotsStore(self.directory, writable=True, clock=self.clock)
        result = self.run_once(store)
        self.assertEqual(result.checked, ["figure"])
        entry = robots_store.RobotsTexts(self.directory, writable=False).lookup(f"https://{HOST}")
        raw = ROBOTS_OK.body if isinstance(ROBOTS_OK.body, bytes) else ROBOTS_OK.body.encode()
        self.assertEqual((entry["sha256"], entry["body"]), (hashlib.sha256(raw).hexdigest(), raw.decode()))

    def test_a_round_that_may_not_write_keeps_nothing(self):
        self.server.serve("/robots.txt", ROBOTS_OK)
        self.run_once(robots_store.RobotsStore(self.directory, writable=False, clock=self.clock))
        self.assertFalse(self.directory.exists())

    def test_a_file_that_was_remembered_for_the_day_is_not_asked_for_and_the_text_is_not_rewritten(self):
        self.server.serve("/robots.txt", ROBOTS_OK)
        store = robots_store.RobotsStore(self.directory, writable=True, clock=self.clock)
        self.run_once(store)
        before = (self.directory / "robots-texts.json").read_bytes()
        paths = len(self.server.paths())
        self.clock.now = T0 + timedelta(hours=5)
        self.run_once(robots_store.RobotsStore(self.directory, writable=True, clock=self.clock))
        self.assertEqual(self.server.paths()[paths:], ["/news"])                  # no new robots request
        self.assertEqual((self.directory / "robots-texts.json").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
