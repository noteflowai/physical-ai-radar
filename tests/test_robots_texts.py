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
