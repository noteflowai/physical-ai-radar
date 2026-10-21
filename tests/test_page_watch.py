"""page_watch: official pages without a feed, read only through a loopback Firecrawl.

No real Firecrawl is contacted. Most cases run against a fake Firecrawl on an
ephemeral 127.0.0.1 port, so the HTTP path, the redirect refusal and the
unavailable handling are the real ones; nothing leaves the machine.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import socket
import sys
import tempfile
import threading
import unittest
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pairadar import cli, fetch, health  # noqa: E402
from pairadar.config import Config, load_config  # noqa: E402
from pairadar.distill import prefilter  # noqa: E402

DAY = date(2026, 10, 21)

LISTING_V1 = """# News
[![cover](https://www.figure.ai/img/a.png)](https://www.figure.ai/news/helix-3)
[Introducing Helix 3](https://www.figure.ai/news/helix-3)
Oct 20, 2026 · Our next model.
[Figure raises Series D](/news/series-d)
[Read more](https://www.figure.ai/news/series-d)
[Careers](https://www.figure.ai/careers)
"""
LISTING_V2 = """# News
[F.04 ships to a second customer <script>alert(1)</script>](https://www.figure.ai/news/f04-second-customer)
A short teaser about the deployment.
[Introducing Helix 3](https://www.figure.ai/news/helix-3)
Oct 20, 2026 · Our next model.
[Figure raises Series D](/news/series-d)
[Elsewhere](https://evil.example.com/news/x)
[Script](javascript:alert(1))
"""


def page(markdown: str, status: int = 200, change: str | None = "new", **meta) -> dict:
    data = {"markdown": markdown, "metadata": {"statusCode": status, **meta}}
    if change:
        data["changeTracking"] = {"previousScrapeAt": None, "changeStatus": change,
                                  "visibility": "visible"}
    return {"success": True, "data": data}


ROBOTS_OPEN = {"success": True, "data": {"rawHtml": "User-agent: *\nAllow: /\n",
                                         "metadata": {"statusCode": 200}}}


def sources(**settings) -> dict:
    return {
        "feeds": [{"id": "deepmind", "url": "https://deepmind.google/blog/rss.xml",
                   "evidence": "O", "weight": 1.2}],
        "standards_watch": [{"id": "iso-1", "title": "ISO 1 — Safety of robots",
                             "url": "https://www.iso.org/standard/1.html", "status_note": "x"}],
        "page_watch_settings": {"enabled": True, "endpoint": "http://127.0.0.1:9",
                                "timeout_seconds": 5, "budget_seconds": 60, "max_entries": 12,
                                "max_items_per_source": 3, **settings},
        "page_watch": [
            {"id": "figure", "url": "https://www.figure.ai/news", "mode": "listing",
             "link_prefix": "/news/", "hosts": ["figure.ai"], "evidence": "O", "weight": 1.1,
             "publisher": "Figure AI News", "topical": True},
            {"id": "watch-iso-1", "standards_watch": "iso-1", "mode": "page",
             "evidence": "O", "weight": 1.0, "publisher": "ISO", "topical": True},
        ],
    }


def config_with(raw: dict) -> Config:
    return Config(sources=raw, taxonomy={}, glossary={}, baseline={})


class FakeFirecrawl:
    """A stand-in for the local Firecrawl: answers /v2/scrape from a URL -> reply map."""

    def __init__(self) -> None:
        self.replies: dict[str, tuple[int, dict]] = {}
        self.requests: list[dict] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.requests.append({"path": self.path, **body})
                status, reply = owner.replies.get(body["url"], (200, {"success": False,
                                                                       "error": "not served"}))
                if status in (301, 302, 307):
                    self.send_response(status)
                    self.send_header("Location", "http://169.254.169.254/latest/meta-data/")
                    self.end_headers()
                    return
                payload = reply if isinstance(reply, bytes) else json.dumps(reply).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "FakeFirecrawl":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.server.shutdown()
        self.server.server_close()

    def serve(self, url: str, reply: dict, status: int = 200) -> None:
        self.replies[url] = (status, reply)

    def scraped(self) -> list[str]:
        return [request["url"] for request in self.requests if not request["url"].endswith("/robots.txt")]


def watch(raw: dict, endpoint: str, state: dict | None = None):
    with patch.dict(os.environ, {fetch.FIRECRAWL_ENV: endpoint}), \
            contextlib.redirect_stdout(io.StringIO()) as out:
        result = fetch.fetch_page_watch(config_with(raw), DAY, state)
    return result, out.getvalue()


class SchemaTest(unittest.TestCase):
    def test_the_committed_page_watch_is_sound(self) -> None:
        raw = load_config().sources
        self.assertEqual(fetch.page_watch_problems(raw), [])
        ids = {entry["id"] for entry in raw["page_watch"]}
        self.assertTrue({"figure", "1x", "physical-intelligence", "unitree"} <= ids)
        settings = raw["page_watch_settings"]
        self.assertEqual(settings["endpoint"], "http://127.0.0.1:3002")
        self.assertTrue(settings["enabled"])

    def test_standards_pages_are_referenced_not_repeated(self) -> None:
        raw = load_config().sources
        standards = {watch["id"]: watch["url"] for watch in raw["standards_watch"]}
        referenced = {entry["standards_watch"] for entry in raw["page_watch"] if "standards_watch" in entry}
        self.assertEqual(referenced, set(standards), "every standards page is watched")
        own_urls = {entry["url"] for entry in raw["page_watch"] if "url" in entry}
        self.assertFalse(own_urls & set(standards.values()), "a standards URL is written twice")
        resolved = {entry["standards_watch"]: entry["url"]
                    for entry in fetch._resolve_page_watch(raw) if "standards_watch" in entry}
        self.assertEqual(resolved, standards)

    def test_bad_entries_are_reported(self) -> None:
        cases = {
            "evidence must be O": {"evidence": "M"},
            "public https URL": {"url": "http://www.figure.ai/news"},
            "already used": {"id": "deepmind"},
            "link_prefix": {"link_prefix": "news"},
            "weight must be": {"weight": 0},
        }
        for needle, change in cases.items():
            with self.subTest(needle):
                raw = sources()
                raw["page_watch"][0].update(change)
                problems = fetch.page_watch_problems(raw)
                self.assertTrue(any(needle in line for line in problems), problems)
        for url in ("https://169.254.169.254/latest", "https://localhost/news", "https://[::1]/x"):
            raw = sources()
            raw["page_watch"][0]["url"] = url
            self.assertTrue(fetch.page_watch_problems(raw), url)
        raw = sources()
        raw["page_watch"][1]["standards_watch"] = "missing"
        self.assertTrue(any("no standards_watch" in line for line in fetch.page_watch_problems(raw)))
        raw = sources()
        raw["page_watch"][1]["url"] = "https://www.iso.org/standard/1.html"
        self.assertTrue(any("not both" in line for line in fetch.page_watch_problems(raw)))

    def test_an_invalid_entry_is_left_out_not_fatal(self) -> None:
        raw = sources()
        raw["page_watch"][0]["evidence"] = "M"
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual([entry["id"] for entry in fetch._resolve_page_watch(raw)], ["watch-iso-1"])


class LoopbackTest(unittest.TestCase):
    def test_only_loopback_endpoints_are_accepted(self) -> None:
        self.assertEqual(fetch.loopback_endpoint("http://127.0.0.1:3002"), "http://127.0.0.1:3002")
        self.assertEqual(fetch.loopback_endpoint("http://localhost:3002/"), "http://localhost:3002")
        self.assertEqual(fetch.loopback_endpoint("http://[::1]:3002"), "http://[::1]:3002")
        for refused in ("http://10.0.0.5:3002", "http://169.254.169.254", "https://api.firecrawl.dev",
                        "http://127.0.0.2:3002", "http://localhost.evil.com:3002",
                        "http://user:pw@127.0.0.1:3002", "http://127.0.0.1:3002/v2",
                        "http://127.0.0.1:3002?x=1", "ftp://127.0.0.1", "http://0.0.0.0:3002",
                        "http://127.0.0.1:99999", "", None):
            self.assertIsNone(fetch.loopback_endpoint(refused), refused)

    def test_a_non_loopback_endpoint_skips_the_channel_without_a_request(self) -> None:
        def refuse(*args, **kwargs):
            raise AssertionError("no request may leave for a non-loopback endpoint")

        with patch.object(fetch._FIRECRAWL_OPENER, "open", refuse):
            result, out = watch(sources(), "https://api.firecrawl.dev")
        self.assertTrue(result.unavailable)
        self.assertEqual((result.items, result.attempted), ([], []))
        self.assertIn("refusing a Firecrawl endpoint", out)
        with self.assertRaises(fetch.FirecrawlUnavailable), patch.object(fetch._FIRECRAWL_OPENER, "open", refuse):
            fetch.firecrawl_post("http://10.1.2.3:3002", "/v2/scrape", {}, 5)

    def test_the_default_endpoint_and_its_override(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(fetch.FIRECRAWL_ENV, None)
            self.assertEqual(fetch.page_watch_settings(config_with(sources(endpoint=None)))["endpoint"],
                             fetch.DEFAULT_FIRECRAWL)
        with patch.dict(os.environ, {fetch.FIRECRAWL_ENV: "http://localhost:4000"}):
            self.assertEqual(fetch.page_watch_settings(config_with(sources()))["endpoint"],
                             "http://localhost:4000")


class ChangeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.raw = sources()
        self.raw["page_watch"] = self.raw["page_watch"][:1]

    def serve_listing(self, fake: FakeFirecrawl, markdown: str, change: str | None = "new") -> None:
        fake.serve("https://www.figure.ai/robots.txt", ROBOTS_OPEN)
        fake.serve("https://www.figure.ai/news", page(markdown, change=change))

    def test_the_request_is_markdown_with_change_tracking(self) -> None:
        with FakeFirecrawl() as fake:
            self.serve_listing(fake, LISTING_V1)
            watch(self.raw, fake.url)
        request = next(r for r in fake.requests if r["url"] == "https://www.figure.ai/news")
        self.assertEqual(request["path"], "/v2/scrape")
        self.assertEqual(request["formats"][0], "markdown")
        self.assertEqual(request["formats"][1]["type"], "changeTracking")
        self.assertEqual(request["formats"][1]["tag"], fetch.CHANGE_TAG)
        self.assertEqual((request["proxy"], request["maxAge"]), ("basic", 0))

    def test_first_sight_only_dated_links_become_items(self) -> None:
        with FakeFirecrawl() as fake:
            self.serve_listing(fake, LISTING_V1)
            result, _ = watch(self.raw, fake.url)
        self.assertEqual([(i.url, i.published) for i in result.items],
                         [("https://www.figure.ai/news/helix-3", "2026-10-20")])
        item = result.items[0]
        self.assertEqual((item.title, item.evidence, item.source_id, item.topical),
                         ("Introducing Helix 3", "O", "figure", True))
        links = result.state["sources"]["figure"]["links"]
        self.assertEqual([link["url"] for link in links],
                         ["https://www.figure.ai/news/helix-3", "https://www.figure.ai/news/series-d"])
        self.assertTrue(all(link["first_seen"] == DAY.isoformat() and link["seeded"] for link in links))
        self.assertEqual((result.attempted, result.checked), (["figure"], ["figure"]))

    def test_changed_listing_yields_new_first_party_links_first(self) -> None:
        with FakeFirecrawl() as fake:
            self.serve_listing(fake, LISTING_V1)
            first, _ = watch(self.raw, fake.url)
            self.serve_listing(fake, LISTING_V2, change="changed")
            second, _ = watch(self.raw, fake.url, first.state)
        # The new link first (never offered); the dated helix-3 is still inside the age
        # window and offered again (the repeat guard downstream drops it once published);
        # the undated series-d from the first look and off-site links never are.
        self.assertEqual([i.url for i in second.items], ["https://www.figure.ai/news/f04-second-customer",
                                                         "https://www.figure.ai/news/helix-3"])
        item = second.items[0]
        self.assertEqual(item.published, DAY.isoformat(), "undated new link: first seen today")
        self.assertNotIn("<script>", item.title)
        self.assertTrue(item.summary.startswith("A short teaser about the deployment."), item.summary)
        self.assertLessEqual(len(item.summary), fetch.MAX_SUMMARY_CHARS + 2)

    def test_unchanged_empty_same_and_removed_give_nothing_new(self) -> None:
        undated = "[Figure raises Series D](/news/series-d)\n[Another old post](/news/old)\n"
        with FakeFirecrawl() as fake:
            self.serve_listing(fake, undated)
            first, _ = watch(self.raw, fake.url)
            again, _ = watch(self.raw, fake.url, first.state)
            fake.serve("https://www.figure.ai/news", {"success": True, "data": {
                "markdown": "", "metadata": {"statusCode": 200},
                "changeTracking": {"changeStatus": "same"}}})
            same, _ = watch(self.raw, fake.url, first.state)
            fake.serve("https://www.figure.ai/news", page("gone", status=404, change="removed"))
            removed, _ = watch(self.raw, fake.url, first.state)
        self.assertEqual((first.items, again.items, same.items, removed.items), ([], [], [], []))
        self.assertEqual(again.state, first.state, "an unchanged page leaves the baseline as it was")
        self.assertEqual(removed.failed, ["figure"])
        self.assertEqual(removed.state["sources"]["figure"], first.state["sources"]["figure"])

    def test_items_are_capped_per_source(self) -> None:
        self.raw["page_watch_settings"]["max_items_per_source"] = 2
        many = "\n".join(f"[Post number {n}](https://www.figure.ai/news/p{n}) Oct 1{n}, 2026" for n in range(6))
        with FakeFirecrawl() as fake:
            self.serve_listing(fake, many)
            result, _ = watch(self.raw, fake.url)
        self.assertEqual([i.url for i in result.items],
                         ["https://www.figure.ai/news/p0", "https://www.figure.ai/news/p1"])

    def test_a_changed_page_is_one_item_at_the_page(self) -> None:
        raw = sources()
        raw["page_watch"] = raw["page_watch"][1:]
        with FakeFirecrawl() as fake:
            fake.serve("https://www.iso.org/robots.txt", ROBOTS_OPEN)
            fake.serve("https://www.iso.org/standard/1.html", page("# ISO 1\nStage: 30.20 Committee draft"))
            first, _ = watch(raw, fake.url)
            fake.serve("https://www.iso.org/standard/1.html",
                       page("# ISO 1\nStage: 40.20 DIS ballot initiated\n", change="changed"))
            second, _ = watch(raw, fake.url, first.state)
        self.assertEqual(first.items, [], "an undated first look only records the baseline")
        self.assertEqual(len(second.items), 1)
        item = second.items[0]
        self.assertEqual((item.url, item.title, item.source_id),
                         ("https://www.iso.org/standard/1.html", "ISO 1 — Safety of robots", "watch-iso-1"))
        self.assertIn("40.20 DIS ballot initiated", item.summary)
        self.assertNotIn("30.20", item.summary)


class RobotsTest(unittest.TestCase):
    def test_a_disallowed_page_is_never_scraped(self) -> None:
        raw = sources()
        with FakeFirecrawl() as fake:
            fake.serve("https://www.figure.ai/robots.txt",
                       {"success": True, "data": {"rawHtml": "User-agent: *\nDisallow: /news\n",
                                                  "metadata": {"statusCode": 200}}})
            fake.serve("https://www.iso.org/robots.txt",
                       {"success": True, "data": {"rawHtml": "", "metadata": {"statusCode": 404}}})
            fake.serve("https://www.figure.ai/news", page(LISTING_V1))
            fake.serve("https://www.iso.org/standard/1.html", page("ISO page"))
            result, out = watch(raw, fake.url)
        self.assertEqual(fake.scraped(), ["https://www.iso.org/standard/1.html"])
        self.assertEqual((result.robots_skipped, result.failed, result.checked), (["figure"], [], ["watch-iso-1"]))
        self.assertIn("robots.txt disallows", out)

    def test_an_unreadable_robots_file_disallows(self) -> None:
        raw = sources()
        raw["page_watch"] = raw["page_watch"][:1]
        with FakeFirecrawl() as fake:
            fake.serve("https://www.figure.ai/robots.txt",
                       {"success": True, "data": {"rawHtml": "", "metadata": {"statusCode": 503}}})
            fake.serve("https://www.figure.ai/news", page(LISTING_V1))
            result, _ = watch(raw, fake.url)
        self.assertEqual((fake.scraped(), result.items, result.robots_skipped), ([], [], ["figure"]))


def closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class UnavailableTest(unittest.TestCase):
    def test_connection_refused_skips_the_channel_and_keeps_the_baseline(self) -> None:
        state = {"schema": fetch.PAGE_STATE_SCHEMA, "sources": {"figure": {"digest": "x", "links": ["u"]}}}
        result, out = watch(sources(), f"http://127.0.0.1:{closed_port()}", state)
        self.assertTrue(result.unavailable)
        self.assertEqual((result.items, result.attempted, result.failed), ([], [], []))
        self.assertEqual(result.state["sources"], state["sources"])
        self.assertIn("unavailable", out)

    def test_a_5xx_or_a_redirect_is_the_service_being_down(self) -> None:
        for status in (502, 302):
            with self.subTest(status=status), FakeFirecrawl() as fake:
                fake.serve("https://www.figure.ai/robots.txt", {"success": False}, status=status)
                result, _ = watch(sources(), fake.url)
                self.assertTrue(result.unavailable)
                self.assertEqual(len(fake.requests), 1, "the channel stops at the first sign")

    def test_the_daily_run_goes_on_and_health_records_the_channel(self) -> None:
        feed = (b'<?xml version="1.0"?><rss><channel><item><title>A humanoid robot</title>'
                b'<link>https://deepmind.google/a</link><pubDate>Mon, 20 Oct 2026 10:00:00 GMT</pubDate>'
                b'</item></channel></rss>')
        raw = sources()
        with patch.object(fetch, "http_get", lambda url, *a, **k: feed), \
                patch.dict(os.environ, {fetch.FIRECRAWL_ENV: f"http://127.0.0.1:{closed_port()}"}), \
                contextlib.redirect_stdout(io.StringIO()):
            report = fetch.fetch_all(config_with(raw), reference=DAY)
        summary = report.to_dict()
        self.assertEqual((summary["answered"], summary["failed"], summary["unavailable"]),
                         (1, [], ["page_watch"]))
        self.assertFalse(cli.degraded(summary, offline=False))
        runs = [{"date": f"2026-10-{day}", "failed": [], "unavailable": ["page_watch"]} for day in (19, 20, 21)]
        verdict = health.report(runs, DAY)
        self.assertEqual(verdict["unavailable"], {"page_watch": 3})
        self.assertEqual((verdict["struggling"], verdict["failures"]), ([], {}))

    def test_build_context_records_unavailable_in_the_run_log(self) -> None:
        report = fetch.FetchReport(attempted=["deepmind"], unavailable=["page_watch"])
        with patch.object(cli, "fetch_all", return_value=report) as fetched, \
                patch.object(cli, "load_history", return_value=[]), \
                contextlib.redirect_stdout(io.StringIO()):
            ctx = cli.build_context(load_config(), offline=False, limit=8, day="2026-10-21")
        self.assertIn("page_state", fetched.call_args.kwargs)
        self.assertEqual(ctx["history"][-1]["unavailable"], ["page_watch"])
        self.assertEqual(ctx["fetch"]["unavailable"], ["page_watch"])
        self.assertIsNone(ctx["page_state"], "a round that read no page keeps the baseline file")


def three_sources() -> dict:
    """figure, then 1x, then the ISO page: a middle entry to fail."""
    raw = sources()
    raw["page_watch"].insert(1, {"id": "1x", "url": "https://www.1x.tech/discover", "mode": "listing",
                                 "link_prefix": "/discover/", "hosts": ["1x.tech"], "evidence": "O",
                                 "weight": 1.0, "publisher": "1X", "topical": True})
    return raw


SITE_ERROR = {"success": False, "code": "SCRAPE_SITE_ERROR",
              "error": 'The URL failed to load in the browser with error code "ERR_CONNECTION_RESET".'}


class ServiceOrPageTest(unittest.TestCase):
    """R154-02: a site failing inside Firecrawl is that page's failure, not the service."""

    def serve_all(self, fake: FakeFirecrawl) -> None:
        for origin in ("https://www.figure.ai", "https://www.1x.tech", "https://www.iso.org"):
            fake.serve(origin + "/robots.txt", ROBOTS_OPEN)
        fake.serve("https://www.figure.ai/news", page(LISTING_V1))
        fake.serve("https://www.1x.tech/discover", page("[NEO update](/discover/neo) Oct 19, 2026"))
        fake.serve("https://www.iso.org/standard/1.html", page("# ISO 1\nStage: 30.20"))

    def test_a_500_with_a_firecrawl_error_code_fails_only_that_page(self) -> None:
        for code, reply in ((500, SITE_ERROR), (500, {**SITE_ERROR, "code": "SCRAPE_SSL_ERROR"}),
                            (500, {**SITE_ERROR, "code": "SCRAPE_ALL_ENGINES_FAILED"}),
                            (408, {"success": False, "code": "SCRAPE_TIMEOUT", "error": "timed out"})):
            with self.subTest(code=code, firecrawl=reply["code"]), FakeFirecrawl() as fake:
                self.serve_all(fake)
                fake.serve("https://www.1x.tech/discover", reply, status=code)
                result, out = watch(three_sources(), fake.url)
                self.assertFalse(result.unavailable, out)
                self.assertEqual(result.attempted, ["figure", "1x", "watch-iso-1"])
                self.assertEqual((result.checked, result.failed), (["figure", "watch-iso-1"], ["1x"]))
                self.assertEqual(fake.scraped(), ["https://www.figure.ai/news", "https://www.1x.tech/discover",
                                                  "https://www.iso.org/standard/1.html"])

    def test_what_still_means_the_service_is_down(self) -> None:
        cases = {"UNKNOWN_ERROR": (500, {"success": False, "code": "UNKNOWN_ERROR", "error": "x"}),
                 "no code": (500, {"success": False, "error": "x"}),
                 "not JSON": (500, b"<html>Internal Server Error</html>"),
                 "503 with a code": (503, SITE_ERROR), "502": (502, SITE_ERROR), "504": (504, SITE_ERROR)}
        for name, (code, reply) in cases.items():
            with self.subTest(name), FakeFirecrawl() as fake:
                self.serve_all(fake)
                fake.serve("https://www.1x.tech/discover", reply, status=code)
                result, _ = watch(three_sources(), fake.url)
                self.assertTrue(result.unavailable)
                self.assertEqual((result.attempted, result.checked, result.failed), (["figure"], ["figure"], []))
                self.assertNotIn("https://www.iso.org/standard/1.html", fake.scraped(), "the channel stopped")

    def test_consecutive_5xx_page_failures_stop_the_channel(self) -> None:
        self.assertEqual(fetch.MAX_CONSECUTIVE_5XX, 3)
        raw = three_sources()
        raw["page_watch"].append({**raw["page_watch"][0], "id": "figure-2", "url": "https://www.figure.ai/blog",
                                  "link_prefix": "/blog/"})
        with FakeFirecrawl() as fake:
            self.serve_all(fake)
            for url in ("https://www.figure.ai/news", "https://www.1x.tech/discover",
                        "https://www.iso.org/standard/1.html", "https://www.figure.ai/blog"):
                fake.serve(url, SITE_ERROR, status=500)
            result, out = watch(raw, fake.url)
        self.assertTrue(result.unavailable)
        self.assertEqual((result.failed, result.checked), (["figure", "1x"], []))
        self.assertIn("3 consecutive HTTP 5xx", out)
        self.assertNotIn("https://www.figure.ai/blog", fake.scraped())

    def test_a_page_that_answers_resets_the_count(self) -> None:
        with FakeFirecrawl() as fake:
            self.serve_all(fake)
            fake.serve("https://www.figure.ai/news", SITE_ERROR, status=500)
            fake.serve("https://www.iso.org/standard/1.html", SITE_ERROR, status=500)
            result, _ = watch(three_sources(), fake.url)
        self.assertFalse(result.unavailable)
        self.assertEqual((result.checked, result.failed), (["1x"], ["figure", "watch-iso-1"]))


class BaselineSemanticsTest(unittest.TestCase):
    """R154-05: recording a link in the baseline never consumes it."""

    def setUp(self) -> None:
        self.raw = sources()
        self.raw["page_watch"] = self.raw["page_watch"][:1]
        self.old = "\n".join(f"[Old post {i}](/news/old-{i}) Jan {i}, 2026" for i in range(1, 4))

    def test_round_three_still_offers_round_two_overflow(self) -> None:
        new = "\n".join(f"[Figure post {i} humanoid](/news/new-{i}) Oct 1{i}, 2026" for i in range(1, 6))
        undated = "\n".join(f"[Undated humanoid note {i}](/news/u-{i})" for i in range(1, 3))
        with FakeFirecrawl() as fake:
            fake.serve("https://www.figure.ai/robots.txt", ROBOTS_OPEN)
            fake.serve("https://www.figure.ai/news", page(self.old))
            first, _ = watch(self.raw, fake.url)
            fake.serve("https://www.figure.ai/news", page(new + "\n" + self.old + "\n" + undated))
            second, _ = watch(self.raw, fake.url, first.state)
            third, _ = watch(self.raw, fake.url, second.state)
            fourth, _ = watch(self.raw, fake.url, third.state)
        tail = lambda result: [i.url.rsplit("/", 1)[1] for i in result.items]  # noqa: E731
        self.assertEqual(first.items, [], "old posts outside the age window are recorded, not offered")
        self.assertEqual(tail(second), ["new-1", "new-2", "new-3"])
        # Everything is in the baseline already, and none of it is lost.
        recorded = [link["url"].rsplit("/", 1)[1] for link in second.state["sources"]["figure"]["links"]]
        self.assertEqual(len(recorded), 10)
        self.assertEqual(tail(third), ["new-4", "new-5", "u-1"], "round 2's overflow, offered in round 3")
        self.assertEqual(tail(fourth), ["u-2", "new-1", "new-2"], "then the least recently offered")
        undated_item = third.items[2]
        self.assertEqual(undated_item.published, DAY.isoformat(), "first seen today")

    def test_the_age_window_bounds_what_is_offered(self) -> None:
        stale = "[Figure post from last year](/news/stale) Oct 1, 2025"
        with FakeFirecrawl() as fake:
            fake.serve("https://www.figure.ai/robots.txt", ROBOTS_OPEN)
            fake.serve("https://www.figure.ai/news", page(self.old))
            first, _ = watch(self.raw, fake.url)
            fake.serve("https://www.figure.ai/news", page(stale + "\n" + self.old))
            second, _ = watch(self.raw, fake.url, first.state)
        # Newly listed but stating an old date: offered, dated as the page says, and the
        # prefilter's age gate is what drops it.
        self.assertEqual([i.published for i in second.items], ["2025-10-01"])
        old_state = {**first.state, "sources": {"figure": {**first.state["sources"]["figure"],
                                                           "links": [{"url": "https://www.figure.ai/news/x",
                                                                      "first_seen": "2026-08-01"}]}}}
        with FakeFirecrawl() as fake:
            fake.serve("https://www.figure.ai/robots.txt", ROBOTS_OPEN)
            fake.serve("https://www.figure.ai/news", page("[An undated humanoid post](/news/x)"))
            aged, _ = watch(self.raw, fake.url, old_state)
        self.assertEqual(aged.items, [], "first seen more than max_age_days ago: no longer offered")

    def test_a_changed_page_with_an_old_published_time_survives_the_prefilter(self) -> None:
        raw = sources()
        raw["page_watch"] = raw["page_watch"][1:]
        before = "# ISO 1 humanoid robot safety\nHigh-risk obligations apply from 2027-12-02."
        after = before + "\nCouncil adopts its position on the humanoid robot safety postponement."
        with FakeFirecrawl() as fake:
            fake.serve("https://www.iso.org/robots.txt", ROBOTS_OPEN)
            fake.serve("https://www.iso.org/standard/1.html", page(before, publishedTime="2024-07-12T00:00:00Z"))
            first, _ = watch(raw, fake.url)
            fake.serve("https://www.iso.org/standard/1.html", page(after, publishedTime="2024-07-12T00:00:00Z"))
            second, _ = watch(raw, fake.url, first.state)
            third, _ = watch(raw, fake.url, second.state)
        self.assertEqual([i.published for i in first.items], ["2024-07-12"], "a first look keeps the page date")
        self.assertEqual([i.published for i in second.items], [DAY.isoformat()])
        self.assertIn("Council adopts its position", second.items[0].summary)
        config = load_config()
        with contextlib.redirect_stdout(io.StringIO()):
            kept = prefilter(second.items, config, DAY)
            dropped = prefilter(first.items, config, DAY)
        self.assertEqual((len(kept), len(dropped)), (1, 0))
        self.assertEqual(third.items, [])

    def test_update_stamps_on_a_page_are_not_a_change(self) -> None:
        raw = sources()
        raw["page_watch"] = raw["page_watch"][1:]
        body = "# ISO 1\nStage: 30.20 Committee draft\n"
        with FakeFirecrawl() as fake:
            fake.serve("https://www.iso.org/robots.txt", ROBOTS_OPEN)
            fake.serve("https://www.iso.org/standard/1.html", page(body + "Last updated: 3 October 2026\n© 2026 ISO"))
            first, _ = watch(raw, fake.url)
            fake.serve("https://www.iso.org/standard/1.html", page(body + "Last updated: 20 October 2026\n© 2027 ISO"))
            stamped, _ = watch(raw, fake.url, first.state)
            fake.serve("https://www.iso.org/standard/1.html", page(body.replace("30.20", "40.20")))
            staged, _ = watch(raw, fake.url, first.state)
        self.assertEqual(stamped.items, [])
        self.assertEqual(stamped.state, first.state, "an update stamp alone does not rewrite the baseline")
        self.assertEqual(len(staged.items), 1, "a stage code is a number, and it is the news")

    def test_a_bare_url_baseline_from_before_is_read(self) -> None:
        state = {"schema": fetch.PAGE_STATE_SCHEMA, "sources": {"figure": {
            "digest": "x", "since": "2026-10-01", "links": ["https://www.figure.ai/news/series-d"]}}}
        with FakeFirecrawl() as fake:
            fake.serve("https://www.figure.ai/robots.txt", ROBOTS_OPEN)
            fake.serve("https://www.figure.ai/news", page("[Figure raises Series D](/news/series-d)"))
            result, _ = watch(self.raw, fake.url, state)
        self.assertEqual(result.items, [])


class BaselineFileTest(unittest.TestCase):
    def test_atomic_save_and_corrupt_load(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "radar" / "page-watch.json"
            self.assertEqual(cli.load_page_state(path), {})
            cli.save_page_state(path, {"schema": fetch.PAGE_STATE_SCHEMA, "sources": {}})
            self.assertEqual(json.loads(path.read_text())["schema"], fetch.PAGE_STATE_SCHEMA)
            self.assertEqual([p.name for p in path.parent.iterdir()], ["page-watch.json"])
            self.assertEqual(path.stat().st_mode & 0o777, 0o644)
            for broken in ('{"schema": "x", "sour', "[1, 2]", "\xff\xfe"):
                path.write_text(broken)
                with contextlib.redirect_stdout(io.StringIO()) as out:
                    self.assertEqual(cli.load_page_state(path), {})
                self.assertIn("rebuilding", out.getvalue())
            with patch.object(cli.json, "dump", side_effect=RuntimeError("disk")):
                with self.assertRaises(RuntimeError):
                    cli.save_page_state(path, {"x": 1})
            self.assertEqual([p.name for p in path.parent.iterdir()], ["page-watch.json"], "no temp left")

    def test_an_unchanged_round_leaves_the_file_alone(self) -> None:
        report = fetch.FetchReport(attempted=["figure"])
        report.page_state = {"schema": fetch.PAGE_STATE_SCHEMA, "sources": {"figure": {"digest": "d"}}}
        with patch.object(cli, "fetch_all", return_value=report), \
                patch.object(cli, "load_history", return_value=[]), \
                patch.object(cli, "load_page_state", return_value=dict(report.page_state)), \
                contextlib.redirect_stdout(io.StringIO()):
            ctx = cli.build_context(load_config(), offline=False, limit=8, day="2026-10-21")
        self.assertIsNone(ctx["page_state"])


class TextTest(unittest.TestCase):
    def test_dates_written_on_listings(self) -> None:
        cases = {"Oct 20, 2026": "2026-10-20", "20 October 2026": "2026-10-20",
                 "MAR 18 '25": "2025-03-18", "2026-10-01": "2026-10-01", "2026年10月9日": "2026-10-09",
                 "Dec 1, 2030": "", "no date": ""}
        for text, expected in cases.items():
            self.assertEqual(fetch.listing_date(text, DAY), expected, text)

    def test_links_stay_on_the_publisher_and_its_prefix(self) -> None:
        links = fetch.listing_links(LISTING_V2, "https://www.figure.ai/news", ["figure.ai"], "/news/", DAY)
        self.assertEqual([link["url"] for link in links], [
            "https://www.figure.ai/news/f04-second-customer", "https://www.figure.ai/news/helix-3",
            "https://www.figure.ai/news/series-d"])
        for link in links:
            self.assertNotIn("<", link["title"])

    def test_page_text_is_cut_to_length(self) -> None:
        markdown = f"[{'x ' * 190}](https://www.figure.ai/news/long)\n{'word ' * 2000}"
        link = fetch.listing_links(markdown, "https://www.figure.ai/news", [], "/news/", DAY)[0]
        self.assertLessEqual(len(link["title"]), fetch.MAX_TITLE_CHARS + 2)
        self.assertLessEqual(len(link["summary"]), fetch.MAX_SUMMARY_CHARS + 2)


if __name__ == "__main__":
    unittest.main()
