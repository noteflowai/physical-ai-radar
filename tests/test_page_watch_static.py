"""page_watch with sources fetched statically (pairadar.static_fetch), end to end over real local TLS.

A run goes through fetch.fetch_page_watch exactly as the radar runs it; only the fetcher factory is replaced by one that
resolves every name to the test server's loopback address (which only a test policy admits) and trusts the per-run test
certificate. What these tests do not show: anything about the real sites, the host network, or the egress rules.
"""
from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from pairadar import cli, fetch, static_fetch  # noqa: E402
from pairadar.config import Config  # noqa: E402
from static_fixture import HOST, Reply, TlsTestCase  # noqa: E402
from test_page_watch import FakeFirecrawl, ROBOTS_OPEN, page as firecrawl_page  # noqa: E402

DAY = date(2026, 10, 21)
ROBOTS_OK = Reply(200, "User-agent: *\nAllow: /\n", ctype="text/plain")
LISTING = {"id": "figure", "url": f"https://{HOST}/news", "mode": "listing", "link_prefix": "/news/", "hosts": [HOST],
           "evidence": "O", "weight": 1.1, "publisher": "Figure AI News", "topical": True, "fetcher": "static"}
PAGE = {"id": "watch-iso", "url": "https://alt.good.test/standard", "mode": "page", "evidence": "O", "weight": 1.0,
        "publisher": "ISO", "topical": True, "fetcher": "static"}
REASONS = set("""url-invalid address-blocked redirect content-type content-encoding too-large unframed request-limit dns-failed
dns-no-ipv4 resolver-stuck connect-failed peer-mismatch tls-failed timeout budget truncated protocol-error http-403 http-404
http-410 http-429 http-4xx http-5xx http-other challenge empty-page no-links robots-unreadable robots-disallowed
firecrawl-unavailable page-failed not-reached""".split()) | {""}


def listing(*cards) -> str:
    body = "".join(f'<div class="c"><a href="{h}"><h3>{t}</h3></a><p>{x}</p></div>' for h, t, x in cards)
    return f"<html><head><title>News</title></head><body><main><h1>News</h1>{body}</main></body></html>"


V1 = listing(("/news/helix-3", "Introducing Helix 3", "Oct 20, 2026 · Our next model."),
             ("/news/series-d", "Figure raises Series D", "An older announcement."))
V2 = listing(("/news/f04-second", "F.04 ships to a second customer", "A short teaser about the deployment."),
             ("/news/helix-3", "Introducing Helix 3", "Oct 20, 2026 · Our next model."),
             ("/news/series-d", "Figure raises Series D", "An older announcement."))


def article(*extra: str) -> str:
    paragraphs = [f"Paragraph {i}: the regulation applies to machinery placed on the market and sets out requirements."
                  for i in range(8)] + list(extra)
    return ("<html><head><title>Standard</title></head><body><main>"
            + "".join(f"<p>{p}</p>" for p in paragraphs) + "</main></body></html>")


def sources(*entries: dict, **settings) -> dict:
    return {"filters": {"max_age_days": 30},
            "page_watch_settings": {"enabled": True, "endpoint": "http://127.0.0.1:9", "timeout_seconds": 5,
                                    "budget_seconds": 120, "max_entries": 12, "max_items_per_source": 3, **settings},
            "page_watch": [copy.deepcopy(entry) for entry in entries]}


class StaticWatchTests(TlsTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.server.serve("/robots.txt", ROBOTS_OK)

    def watch(self, raw, state=None, endpoint="http://127.0.0.1:9", factory=None, patches=()):
        stack = contextlib.ExitStack()
        with stack:
            stack.enter_context(patch.dict(os.environ, {fetch.FIRECRAWL_ENV: endpoint}))
            stack.enter_context(patch.object(fetch, "STATIC_FETCHER_FACTORY", factory or self.fetcher))
            for item in patches:
                stack.enter_context(item)
            out = stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            result = fetch.fetch_page_watch(Config(sources=raw, taxonomy={}, glossary={}, baseline={}), DAY, state)
        self.out = out.getvalue()
        return result

    def urls(self, result):
        return [item.url for item in result.items]

    # --- reading ---------------------------------------------------------------------------------------
    def test_a_static_listing_is_read_seeded_and_a_new_link_is_offered(self):
        self.server.serve("/news", Reply(200, V1))
        first = self.watch(sources(LISTING))
        self.assertEqual(self.server.paths(), ["/robots.txt", "/news"])             # nothing else is requested
        self.assertEqual((first.checked, first.failed, first.robots_skipped), (["figure"], [], []))
        self.assertEqual(first.static_checked, ["figure"])
        self.assertEqual(self.urls(first), [f"https://{HOST}/news/helix-3"])      # the dated one; the undated old one is seeded
        record = first.state["sources"]["figure"]
        self.assertEqual(record["extractor"], "static-1")
        self.assertNotIn("migrated_from", record)
        self.assertEqual(first.sources["figure"], {"fetcher": "static", "outcome": "checked", "reason": ""})
        self.server.serve("/news", Reply(200, V2))
        second = self.watch(sources(LISTING), first.state)
        self.assertIn(f"https://{HOST}/news/f04-second", self.urls(second))
        self.assertNotIn(f"https://{HOST}/news/series-d", self.urls(second))

    def test_a_static_page_is_compared_with_its_own_baseline(self):
        self.server.serve("/standard", Reply(200, article()))
        first = self.watch(sources(PAGE))
        self.assertEqual(first.checked, ["watch-iso"])
        self.assertEqual(self.urls(first), [])                                      # first sight of a page: a baseline, no item
        self.server.serve("/standard", Reply(200, article("Paragraph new: a brand new application date was announced.")))
        second = self.watch(sources(PAGE), first.state)
        self.assertEqual(len(second.items), 1)
        self.assertIn("brand new application date", second.items[0].summary)
        self.assertNotIn("Paragraph 3", second.items[0].summary)
        third = self.watch(sources(PAGE), second.state)
        self.assertEqual(third.items, [])

    # --- robots.txt through the same fetcher ---------------------------------------------------------------
    def test_robots_decides_before_the_page_is_requested(self):
        cases = {
            "disallow all": (Reply(200, "User-agent: *\nDisallow: /\n", ctype="text/plain"), "robots", "robots-disallowed", True),
            "disallow our token": (Reply(200, "User-agent: physical-ai-radar\nDisallow: /news\n", ctype="text/plain"), "robots", "robots-disallowed", True),
            "allow": (Reply(200, "User-agent: *\nDisallow: /private\n", ctype="text/plain"), "checked", "", False),
            "404 means none": (Reply(404, "no", ctype="text/html"), "checked", "", False),
            "410 means none": (Reply(410, "gone", ctype="text/html"), "checked", "", False),
            "401": (Reply(401, "login", ctype="text/html"), "unavailable", "robots-unreadable", True),
            "403": (Reply(403, "denied", ctype="text/html"), "unavailable", "robots-unreadable", True),
            "429": (Reply(429, "slow", ctype="text/html"), "unavailable", "robots-unreadable", True),
            "500": (Reply(500, "boom", ctype="text/html"), "unavailable", "robots-unreadable", True),
            "challenge 202": (Reply(202, "<html>wait</html>"), "unavailable", "robots-unreadable", True),
            "redirect": (Reply(301, "", headers={"Location": "https://good.test/other"}), "unavailable", "robots-unreadable", True),
            "html page as robots": (Reply(200, "<html>Welcome</html>", ctype="text/html"), "unavailable", "robots-unreadable", True),
            "truncated": (Reply(200, "User-agent", declare=500, ctype="text/plain"), "unavailable", "robots-unreadable", True),
            "gzip": (Reply(200, "x", ctype="text/plain", headers={"Content-Encoding": "gzip"}), "unavailable", "robots-unreadable", True),
        }
        details = {"401": "http-4xx", "403": "http-403", "429": "http-429", "500": "http-5xx", "challenge 202": "http-other",
                   "redirect": "redirect", "html page as robots": "content-type", "truncated": "truncated",
                   "gzip": "content-encoding"}
        for name, (robots, outcome, why, page_untouched) in cases.items():
            with self.subTest(name):
                self.server.requests.clear()
                self.server.serve("/robots.txt", robots)
                self.server.serve("/news", Reply(200, V1))
                result = self.watch(sources(LISTING))
                self.assertEqual(result.sources["figure"]["outcome"], outcome)
                self.assertEqual(result.sources["figure"]["reason"], why)
                self.assertEqual(result.sources["figure"].get("detail"), details.get(name))      # the fixed code underneath
                self.assertEqual("/news" not in self.server.paths(), page_untouched)
                self.assertEqual(result.checked == ["figure"], outcome == "checked")
                self.assertEqual(result.robots_skipped == ["figure"], outcome == "robots")
                if outcome == "unavailable":
                    self.assertEqual(result.failed, ["figure"])

    def test_robots_that_cannot_be_looked_up_say_why_in_a_fixed_code(self):
        def broken(host):
            raise OSError("no such host 10.9.9.9 secret")
        result = self.watch(sources(LISTING), factory=lambda: self.fetcher(resolver=broken))
        self.assertEqual(result.sources["figure"],
                         {"fetcher": "static", "outcome": "unavailable", "reason": "robots-unreadable", "detail": "dns-failed"})
        self.assertNotIn("10.9.9.9", self.out)
        self.assertNotIn("secret", self.out)
        self.assertIn("robots-unreadable (dns-failed)", self.out)

    def test_robots_is_read_once_per_origin_in_a_run(self):
        other = dict(LISTING, id="figure-two", url=f"https://{HOST}/blog", link_prefix="/blog/")
        self.server.serve("/news", Reply(200, V1))
        self.server.serve("/blog", Reply(200, listing(("/blog/a-post", "A blog post title", "Oct 20, 2026 · hello"))))
        result = self.watch(sources(LISTING, other))
        self.assertEqual(self.server.paths().count("/robots.txt"), 1)
        self.assertEqual(sorted(result.checked), ["figure", "figure-two"])

    def test_an_unreadable_robots_is_remembered_for_the_run(self):
        other = dict(LISTING, id="figure-two", url=f"https://{HOST}/blog", link_prefix="/blog/")
        self.server.serve("/robots.txt", Reply(403, "no", ctype="text/html"))
        result = self.watch(sources(LISTING, other))
        self.assertEqual(self.server.paths(), ["/robots.txt"])                      # the second source did not ask again
        self.assertEqual(sorted(result.failed), ["figure", "figure-two"])

    # --- a 200 that is not the page; the baseline stays -------------------------------------------------
    def test_pages_that_are_not_the_page_leave_the_baseline_alone(self):
        self.server.serve("/news", Reply(200, V1))
        base = self.watch(sources(LISTING))
        self.assertTrue(base.state["sources"]["figure"])
        challenge = ("<html><head><title>Just a moment...</title></head><body>"
                     "<script src='/cdn-cgi/challenge-platform/x.js'></script></body></html>")
        bad = {
            "challenge": (Reply(200, challenge), "unavailable", "challenge"),
            "empty shell": (Reply(200, "<html><body><div id=root></div><script src=/a.js></script></body></html>"), "unavailable", "empty-page"),
            "no links": (Reply(200, "<html><body><main><h1>News</h1><p>Nothing to show yet, check back soon.</p></main></body></html>"),
                         "unavailable", "no-links"),
            "links outside the prefix": (Reply(200, listing(("/careers", "Careers at the company", "Join us"))), "unavailable", "no-links"),
            "403": (Reply(403, "Access Denied"), "unavailable", "http-403"),
            "429": (Reply(429, "slow down"), "unavailable", "http-429"),
            "500": (Reply(500, "boom"), "unavailable", "http-5xx"),
            "truncated": (Reply(200, V1[:50], declare=len(V1)), "unavailable", "truncated"),
            "redirect": (Reply(302, "", headers={"Location": "http://169.254.169.254/latest/meta-data/"}), "blocked", "redirect"),
            "wrong type": (Reply(200, V1, ctype="application/json"), "blocked", "content-type"),
            "gzip": (Reply(200, V1, headers={"Content-Encoding": "gzip"}), "blocked", "content-encoding"),
            "too large": (Reply(200, "x", declare=static_fetch.MAX_PAGE_BYTES + 1), "blocked", "too-large"),
        }
        for name, (reply, outcome, why) in bad.items():
            with self.subTest(name):
                self.server.serve("/news", reply)
                result = self.watch(sources(LISTING), copy.deepcopy(base.state))
                self.assertEqual(result.sources["figure"], {"fetcher": "static", "outcome": outcome, "reason": why})
                self.assertEqual((result.checked, result.failed), ([], ["figure"]))
                self.assertEqual(result.items, [])
                self.assertEqual(result.state["sources"]["figure"], base.state["sources"]["figure"])    # untouched, not emptied
                self.assertEqual(cli.page_watch_status(result.summary()), "all-failed")

    def test_page_mode_with_an_empty_body_is_not_a_page(self):
        self.server.serve("/standard", Reply(200, "<html><head><title>ISO</title></head><body><main><p>Loading</p></main></body></html>"))
        result = self.watch(sources(PAGE))
        self.assertEqual(result.sources["watch-iso"]["reason"], "empty-page")
        self.assertEqual(result.state["sources"], {})

    def test_a_refused_address_is_blocked_and_never_connected(self):
        factory = lambda: self.fetcher(policy=static_fetch.address_allowed)
        self.server.serve("/news", Reply(200, V1))
        result = self.watch(sources(LISTING), factory=factory)
        self.assertEqual(result.sources["figure"],
                         {"fetcher": "static", "outcome": "unavailable", "reason": "robots-unreadable", "detail": "address-blocked"})
        self.assertEqual(self.server.accepted, 0)
        direct = factory()
        with self.assertRaises(static_fetch.StaticFetchError) as caught:
            direct.get(LISTING["url"], direct.budget(5))
        self.assertEqual(caught.exception.reason, "address-blocked")

    # --- mixed runs, configuration -----------------------------------------------------------------------
    def firecrawl_entry(self):
        return {"id": "unitree", "url": "https://www.unitree.com/news", "mode": "listing", "link_prefix": "/news",
                "hosts": ["unitree.com"], "evidence": "O", "weight": 1.0, "publisher": "Unitree", "topical": True}

    def test_firecrawl_being_down_does_not_stop_the_static_sources(self):
        self.server.serve("/news", Reply(200, V1))
        for endpoint in ("http://127.0.0.1:9", "http://10.0.0.5:3002"):
            with self.subTest(endpoint):
                result = self.watch(sources(self.firecrawl_entry(), LISTING), endpoint=endpoint)
                self.assertTrue(result.unavailable)
                self.assertEqual(result.checked, ["figure"])
                self.assertEqual(result.sources["unitree"], {"fetcher": "firecrawl", "outcome": "not-attempted",
                                                             "reason": "firecrawl-unavailable"})
                self.assertEqual(cli.page_watch_status(result.summary()), "ok")        # the floor: one page was read
                self.assertEqual(result.summary()["planned"], ["unitree", "figure"])

    def test_a_run_of_only_firecrawl_sources_is_exactly_what_it_was(self):
        result = self.watch(sources(self.firecrawl_entry()), endpoint="http://127.0.0.1:9")
        self.assertTrue(result.unavailable)
        self.assertEqual(cli.page_watch_status(result.summary()), "firecrawl-unavailable")
        self.assertNotIn("planned", result.summary())
        self.assertNotIn("sources", result.summary())
        self.assertEqual(self.server.accepted, 0)

    def test_firecrawl_and_static_sources_in_one_run(self):
        self.server.serve("/news", Reply(200, V1))
        with FakeFirecrawl() as fc:
            fc.serve("https://www.unitree.com/robots.txt", ROBOTS_OPEN)
            fc.serve("https://www.unitree.com/news", firecrawl_page(
                "[Unitree G2 launch](https://www.unitree.com/news/g2)\nOct 20, 2026 · A launch."))
            result = self.watch(sources(self.firecrawl_entry(), LISTING), endpoint=fc.url)
        self.assertEqual(result.checked, ["unitree", "figure"])
        self.assertEqual(result.sources["unitree"]["fetcher"], "firecrawl")
        self.assertEqual({item.source_id for item in result.items}, {"unitree", "figure"})
        self.assertNotIn("extractor", result.state["sources"]["unitree"])             # the Firecrawl baseline keeps its shape
        self.assertEqual(result.state["sources"]["figure"]["extractor"], "static-1")

    def test_an_unknown_fetcher_is_invalid_and_never_falls_back(self):
        for value in ("scraperapi", "STATIC", "Static", "", None, 5, ["static"], "firecrawl,static"):
            with self.subTest(value=value):
                raw = sources(dict(LISTING, fetcher=value))
                self.assertTrue(any("fetcher must be one of" in line for line in fetch.page_watch_problems(raw)), value)
                with FakeFirecrawl() as fc:
                    result = self.watch(raw, endpoint=fc.url)
                    self.assertEqual(fc.requests, [])
                self.assertEqual((result.attempted, result.checked, result.failed), ([], [], []))
                self.assertEqual(self.server.accepted, 0)

    def test_without_a_fetcher_key_a_source_is_firecrawl_and_the_repository_ships_none_static(self):
        entry = {k: v for k, v in LISTING.items() if k != "fetcher"}
        self.assertEqual(fetch.page_watch_problems(sources(entry)), [])
        with FakeFirecrawl() as fc:
            fc.serve(entry["url"], firecrawl_page("[Introducing Helix 3](/news/helix-3)\nOct 20, 2026 · hi"))
            fc.serve(f"https://{HOST}/robots.txt", ROBOTS_OPEN)
            result = self.watch(sources(entry), endpoint=fc.url)
            self.assertEqual(result.checked, ["figure"])
            self.assertTrue(fc.requests)
        self.assertEqual(self.server.accepted, 0)
        shipped = json.loads((ROOT / "data" / "sources.json").read_text(encoding="utf-8"))
        self.assertEqual([e["id"] for e in shipped["page_watch"] if e.get("fetcher", "firecrawl") != "firecrawl"], [])
        self.assertEqual(fetch.page_watch_problems(shipped), [])

    def test_the_run_budget_leaves_later_sources_marked_not_attempted(self):
        self.server.serve("/news", Reply(200, V1))
        self.server.serve("/standard", Reply(200, article()))
        real, calls = fetch.time.monotonic, [0]

        def clock() -> float:                       # the run's own checks see time jump after the first source
            calls[0] += 1
            return real() + (10 ** 6 if calls[0] > 2 else 0)

        result = self.watch(sources(LISTING, PAGE), patches=[patch.object(fetch.time, "monotonic", clock)])
        self.assertEqual(result.checked, ["figure"])
        self.assertEqual(result.sources["watch-iso"], {"fetcher": "static", "outcome": "not-attempted", "reason": "budget"})

    def test_the_run_budget_also_ends_a_source_that_is_being_read(self):
        self.server.serve("/news", Reply(200, "x" * 400, drip=0.2))                 # a slow body from the first source
        self.server.serve("/standard", Reply(200, article()))
        real = fetch.page_watch_settings

        def short(config):
            return {**real(config), "budget": 1.5}

        started = time.monotonic()
        result = self.watch(sources(LISTING, PAGE), patches=[patch.object(fetch, "page_watch_settings", short)])
        self.assertLess(time.monotonic() - started, 6)
        self.assertEqual(result.sources["figure"]["reason"], "budget")
        self.assertEqual(result.sources["watch-iso"], {"fetcher": "static", "outcome": "not-attempted", "reason": "budget"})
        self.assertEqual(result.checked, [])

    # --- baseline migration --------------------------------------------------------------------------------
    def firecrawl_baseline(self):
        links = [{"url": f"https://{HOST}/news/series-d", "first_seen": "2026-09-01", "seeded": True}]
        return {"schema": fetch.PAGE_STATE_SCHEMA, "sources": {"figure": {
            "digest": "olddigest", "since": "2026-09-01", "changed": "2026-10-01", "links": links}}}

    def test_a_firecrawl_baseline_is_reseeded_on_the_first_static_read(self):
        page = listing(("/news/series-d", "Figure raises Series D", "An older announcement."),
                       ("/news/archive-1", "An old post from the archive", "Nothing dated here."),
                       ("/news/archive-2", "Another old post from the archive", "Nothing dated here either."),
                       ("/news/helix-3", "Introducing Helix 3", "Oct 20, 2026 · Our next model."))
        self.server.serve("/news", Reply(200, page))
        first = self.watch(sources(LISTING), self.firecrawl_baseline())
        record = first.state["sources"]["figure"]
        self.assertEqual(self.urls(first), [f"https://{HOST}/news/helix-3"])         # no flood of archive posts
        self.assertEqual(record["extractor"], "static-1")
        self.assertEqual(record["migrated_from"], {"extractor": "firecrawl", "digest": "olddigest", "on": "2026-10-21"})
        self.assertEqual(record["since"], "2026-09-01")                              # the history is kept
        by_url = {link["url"]: link for link in record["links"]}
        self.assertEqual(by_url[f"https://{HOST}/news/series-d"]["first_seen"], "2026-09-01")
        self.assertTrue(by_url[f"https://{HOST}/news/archive-1"]["seeded"])
        # the second run is an ordinary comparison: no new migration, the record is kept, a real new link is offered
        second = self.watch(sources(LISTING), first.state)
        self.assertEqual(second.state["sources"].get("figure", record)["migrated_from"], record["migrated_from"])
        newer = listing(("/news/brand-new", "A brand new announcement", "Fresh."), ("/news/series-d", "Figure raises Series D", "x"),
                        ("/news/archive-1", "An old post from the archive", "x"), ("/news/archive-2", "Another old post from the archive", "x"),
                        ("/news/helix-3", "Introducing Helix 3", "Oct 20, 2026 · Our next model."))
        self.server.serve("/news", Reply(200, newer))
        third = self.watch(sources(LISTING), first.state)
        self.assertIn(f"https://{HOST}/news/brand-new", self.urls(third))
        self.assertNotIn(f"https://{HOST}/news/archive-1", self.urls(third))

    def test_a_firecrawl_page_baseline_is_reseeded_not_reported_as_all_new(self):
        old = {"schema": fetch.PAGE_STATE_SCHEMA, "sources": {"watch-iso": {
            "digest": "olddigest", "since": "2026-09-01", "changed": "2026-10-01", "lines": ["0123456789abcdef"]}}}
        self.server.serve("/standard", Reply(200, article()))
        first = self.watch(sources(PAGE), old)
        self.assertEqual(first.items, [])                                             # eight "new" lines are not news
        record = first.state["sources"]["watch-iso"]
        self.assertEqual((record["extractor"], record["migrated_from"]["extractor"]), ("static-1", "firecrawl"))
        self.server.serve("/standard", Reply(200, article("Paragraph new: a real change after the migration.")))
        second = self.watch(sources(PAGE), first.state)
        self.assertEqual(len(second.items), 1)
        self.assertIn("real change after the migration", second.items[0].summary)

    def test_going_back_to_firecrawl_reseeds_a_static_baseline(self):
        self.server.serve("/news", Reply(200, V1))
        static = self.watch(sources(LISTING))
        entry = {k: v for k, v in LISTING.items() if k != "fetcher"}
        with FakeFirecrawl() as fc:
            fc.serve(entry["url"], firecrawl_page("[Introducing Helix 3](/news/helix-3)\nOct 20, 2026 · hi\n"
                                                  "[An old post from the archive](/news/archive-1)\n"))
            fc.serve(f"https://{HOST}/robots.txt", ROBOTS_OPEN)
            back = self.watch(sources(entry), copy.deepcopy(static.state), endpoint=fc.url)
        record = back.state["sources"]["figure"]
        self.assertNotIn("extractor", record)                                          # a Firecrawl-shaped record again
        self.assertEqual(record["migrated_from"]["extractor"], "static-1")
        self.assertNotIn(f"https://{HOST}/news/archive-1", self.urls(back))           # not offered as news

    def test_a_failed_read_after_migration_does_not_change_the_baseline(self):
        base = self.firecrawl_baseline()
        self.server.serve("/news", Reply(503, "later"))
        result = self.watch(sources(LISTING), copy.deepcopy(base))
        self.assertEqual(result.state["sources"]["figure"], base["sources"]["figure"])

    # --- evidence and logs ---------------------------------------------------------------------------------
    def test_evidence_reports_each_planned_source_with_fixed_codes_only(self):
        self.server.serve("/news", Reply(200, V1))
        self.server.serve("/standard", Reply(500, "SECRET-SERVER-TEXT"))
        entries = [self.firecrawl_entry(), LISTING, PAGE]
        ticks = None
        result = self.watch(sources(*entries), endpoint="http://127.0.0.1:9")
        with tempfile.TemporaryDirectory() as tmp:
            path = cli.write_page_watch_evidence({"page_watch": result.summary()}, "2026-10-21", Path(tmp))
            text = path.read_text(encoding="utf-8")
        record = json.loads(text)
        self.assertEqual(record["status"], "ok")
        self.assertEqual(record["sources_checked"], 1)
        self.assertEqual(record["sources_planned"], 3)
        self.assertEqual(record["sources_unavailable"], ["watch-iso"])
        self.assertEqual(record["sources_not_attempted"], ["unitree"])
        self.assertEqual(record["sources"]["watch-iso"], {"fetcher": "static", "outcome": "unavailable", "reason": "http-5xx"})
        self.assertEqual(record["sources"]["unitree"]["reason"], "firecrawl-unavailable")
        for note in record["sources"].values():
            self.assertIn(note["reason"], REASONS)
        for leak in ("SECRET-SERVER-TEXT", "https://", HOST, "Helix", "127.0.0.1"):
            self.assertNotIn(leak, text)

    def test_the_run_log_has_ids_and_codes_not_urls_or_server_text(self):
        self.server.serve("/news", Reply(500, "SECRET-SERVER-TEXT with https://internal.example/path?token=abc"))
        self.watch(sources(LISTING))
        self.assertIn("figure", self.out)
        self.assertIn("http-5xx", self.out)
        for leak in ("SECRET", "token=abc", "internal.example", HOST):
            self.assertNotIn(leak, self.out)

    def test_a_planned_source_nothing_recorded_for_is_not_attempted_not_missing(self):
        block = {"enabled": True, "attempted": ["a", "c"], "checked": ["a"], "failed": ["c"], "robots_skipped": [],
                 "unavailable": False, "items": 0, "planned": ["a", "b", "c"], "static_checked": ["a"],
                 "sources": {"a": {"fetcher": "static", "outcome": "checked", "reason": ""},
                             "c": {"fetcher": "static", "outcome": "unavailable", "reason": "robots-unreadable", "detail": "http-403"}}}
        with tempfile.TemporaryDirectory() as tmp:
            record = json.loads(cli.write_page_watch_evidence({"page_watch": block}, "2026-10-21", Path(tmp)).read_text(encoding="utf-8"))
        self.assertEqual(record["sources"]["b"], {"fetcher": "", "outcome": "not-attempted", "reason": "not-reached"})
        self.assertEqual(record["sources"]["c"]["detail"], "http-403")
        self.assertNotIn("detail", record["sources"]["a"])
        self.assertEqual(record["sources_not_attempted"], ["b"])
        self.assertEqual(record["sources_planned"], 3)

    def test_a_legacy_evidence_record_has_no_per_source_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = cli.write_page_watch_evidence(
                {"page_watch": {"enabled": True, "attempted": ["a"], "checked": ["a"], "failed": [], "robots_skipped": [],
                                "unavailable": False, "items": 0}}, "2026-10-21", Path(tmp))
            record = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(record["status"], "ok")
        for key in ("sources", "sources_planned", "sources_blocked", "sources_unavailable", "sources_not_attempted"):
            self.assertNotIn(key, record)


if __name__ == "__main__":
    unittest.main()
