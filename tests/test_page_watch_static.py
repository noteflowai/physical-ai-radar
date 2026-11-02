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

from pairadar import cli, fetch, robots_rules, static_extract, static_fetch  # noqa: E402
from pairadar.config import Config  # noqa: E402
from static_fixture import HOST, Reply, TlsTestCase  # noqa: E402
from test_page_watch import FakeFirecrawl, ROBOTS_OPEN, page as firecrawl_page  # noqa: E402

DAY = date(2026, 10, 21)
ROBOTS_OK = Reply(200, "User-agent: *\nAllow: /\n", ctype="text/plain")
LISTING = {"id": "figure", "url": f"https://{HOST}/news", "mode": "listing", "link_prefix": "/news/", "hosts": [HOST],
           "evidence": "O", "weight": 1.1, "publisher": "Figure AI News", "topical": True, "fetcher": "static",
           "min_links": 1}
PAGE = {"id": "watch-iso", "url": "https://alt.good.test/standard", "mode": "page", "evidence": "O", "weight": 1.0,
        "publisher": "ISO", "topical": True, "fetcher": "static", "expect_text": "regulation applies",
        "min_text_chars": 300}
REASONS = set("""url-invalid address-blocked redirect content-type content-encoding too-large unframed request-limit dns-failed
dns-no-ipv4 resolver-stuck connect-failed peer-mismatch tls-failed timeout budget truncated protocol-error http-403 http-404
http-410 http-429 http-4xx http-5xx http-other challenge empty-page no-links robots-unreadable robots-disallowed
firecrawl-unavailable page-failed not-reached""".split()) | {""}


def status_of(result) -> str:
    """The freshness status as the CLI computes it: the published block plus the private per-source detail."""
    return cli.page_watch_status({**result.summary(), **result.detail()})


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
                                    "budget_seconds": 120, "max_entries": 12, "max_items_per_source": 3,
                                    "static_mode": "live", **settings},
            "page_watch": [copy.deepcopy(entry) for entry in entries]}


class StaticWatchTests(TlsTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.server.serve("/robots.txt", ROBOTS_OK)

    def watch(self, raw, state=None, endpoint="http://127.0.0.1:9", factory=None, patches=()):
        stack = contextlib.ExitStack()
        with stack:
            stack.enter_context(patch.dict(os.environ, {fetch.FIRECRAWL_ENV: endpoint, "GITHUB_ACTIONS": ""}))   # these tests run on hosted CI too
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
                note = {key: value for key, value in result.sources["figure"].items() if key != "detail"}
                self.assertEqual(note, {"fetcher": "static", "outcome": outcome, "reason": why})
                self.assertEqual((result.checked, result.failed), ([], ["figure"]))
                self.assertEqual(result.items, [])
                self.assertEqual(result.state["sources"]["figure"], base.state["sources"]["figure"])    # untouched, not emptied
                self.assertEqual(status_of(result), "all-failed")

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
                self.assertEqual(status_of(result), "ok")        # the floor: one page was read
                self.assertEqual(result.detail()["planned"], ["unitree", "figure"])

    def test_a_run_of_only_firecrawl_sources_is_exactly_what_it_was(self):
        result = self.watch(sources(self.firecrawl_entry()), endpoint="http://127.0.0.1:9")
        self.assertTrue(result.unavailable)
        self.assertEqual(status_of(result), "firecrawl-unavailable")
        self.assertNotIn("planned", result.summary())
        self.assertNotIn("sources", result.summary())
        self.assertEqual(result.detail(), {})
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

    def test_without_a_fetcher_key_a_source_is_firecrawl(self):
        entry = {k: v for k, v in LISTING.items() if k != "fetcher"}
        self.assertEqual(fetch.page_watch_problems(sources(entry)), [])
        with FakeFirecrawl() as fc:
            fc.serve(entry["url"], firecrawl_page("[Introducing Helix 3](/news/helix-3)\nOct 20, 2026 · hi"))
            fc.serve(f"https://{HOST}/robots.txt", ROBOTS_OPEN)
            result = self.watch(sources(entry), endpoint=fc.url)
            self.assertEqual(result.checked, ["figure"])
            self.assertTrue(fc.requests)
        self.assertEqual(self.server.accepted, 0)

    # Turning a source static, or the whole channel live, is an approval of its own. Editing this list is how a change to
    # data/sources.json shows up in review: the test fails until the approved set here is changed on purpose.
    APPROVED_STATIC = ["physical-intelligence", "unitree"]

    def test_the_repository_ships_exactly_the_approved_static_sources_in_observe_mode(self):
        shipped = json.loads((ROOT / "data" / "sources.json").read_text(encoding="utf-8"))
        self.assertEqual([e["id"] for e in shipped["page_watch"] if e.get("fetcher", "firecrawl") != "firecrawl"], self.APPROVED_STATIC)
        self.assertEqual(fetch.page_watch_problems(shipped), [])                     # each carries the sanity settings static needs
        self.assertEqual(shipped["page_watch_settings"].get("static_mode"), "observe")   # written out, not left to the default
        config = Config(sources=shipped, taxonomy={}, glossary={}, baseline={})
        self.assertEqual(fetch.page_watch_settings(config)["static_mode"], "observe")
        for entry in shipped["page_watch"]:
            if entry.get("fetcher") == "static":
                parsed = static_fetch.parse_url(entry["url"])                             # inside the supported-path policy
                self.assertEqual(parsed.target, "/" + entry["url"].split("/", 3)[3])
                self.assertTrue(entry["link_prefix"].startswith("/"))

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
            path = cli.write_page_watch_evidence({"page_watch": result.summary()}, "2026-10-21", Path(tmp), detail=result.detail())
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

    # --- robots.txt rules, through the real _static_robots -------------------------------------------------
    def verdict(self, body, path="/news", status=200, ctype="text/plain"):
        self.server.requests.clear()
        self.server.serve("/robots.txt", Reply(status, body, ctype=ctype))
        fetcher = self.fetcher()
        return fetch._static_robots(fetcher, f"https://{HOST}{path}", {}, fetcher.budget(20))

    def test_robots_rules_are_applied_as_the_rfc_says(self):
        self.assertEqual(self.verdict("User-agent: *\nAllow: /\nDisallow: /private\n", "/private"), ("disallowed", "robots-disallowed"))
        self.assertEqual(self.verdict("User-agent: *\nAllow: /\nDisallow: /private\n", "/public"), ("allowed", ""))
        self.assertEqual(self.verdict("User-agent: *\nDisallow: /a\nAllow: /a/b\n", "/a/b/c"), ("allowed", ""))
        self.assertEqual(self.verdict("User-agent: physical-ai-radar\nDisallow: /a\n\nUser-agent: physical-ai-radar\nDisallow: /b\n", "/b"),
                         ("disallowed", "robots-disallowed"))
        self.assertEqual(self.verdict("User-agent: *\nDisallow: /*.pdf$\n", "/x.pdf"), ("disallowed", "robots-disallowed"))

    def test_the_reviewers_robots_examples_fetch_no_page(self):
        # 01, R-D-ffc7-01: robotparser allowed all three
        disallowed = ("disallowed", "robots-disallowed")
        self.assertEqual(self.verdict("User-agent: *\nAllow: /\nDisallow: /news\n", "/news"), disallowed)
        self.assertEqual(self.verdict("User-agent: *\nDisallow: /news/*private*\n", "/news/private"), disallowed)
        padding = "#" + "c" * 500_000 + "\n"
        body = "User-agent: *\nAllow: /\n" + padding + "Disallow: /news\n"
        self.assertEqual(len(body.encode()), 500_041)
        self.assertEqual(self.verdict(body, "/news"), disallowed)
        # end to end: the page is never requested and the baseline is as it was
        entry = dict(LISTING, url=f"https://{HOST}/news/private", link_prefix="/news/private/")
        base_state = {"schema": fetch.PAGE_STATE_SCHEMA, "sources": {"figure": {"digest": "d", "since": "2026-09-01",
                                                                                   "changed": "2026-10-01", "links": [],
                                                                                   "extractor": "static-1"}}}
        for robots in ("User-agent: *\nDisallow: /news/*private*\n", "User-agent: *\nAllow: /\nDisallow: /news/private\n", body):
            with self.subTest(robots[:30]):
                self.server.requests.clear()
                self.server.serve("/robots.txt", Reply(200, robots, ctype="text/plain"))
                self.server.serve("/news/private", Reply(200, V1))
                result = self.watch(sources(entry), copy.deepcopy(base_state))
                self.assertEqual(self.server.paths(), ["/robots.txt"])
                self.assertEqual((result.robots_skipped, result.checked), (["figure"], []))
                self.assertEqual(result.state["sources"], base_state["sources"])

    def test_the_last_rule_of_a_long_robots_file_counts(self):
        # 500028 bytes, under the limit: the Disallow at the very end must not be cut off
        body = "#" + "c" * 500_000 + "\nUser-agent: *\nDisallow: /\n"
        self.assertEqual(len(body.encode()), 500_028)
        self.assertEqual(self.verdict(body), ("disallowed", "robots-disallowed"))
        self.server.serve("/news", Reply(200, V1))
        result = self.watch(sources(LISTING, static_mode="live"))
        self.assertEqual((result.robots_skipped, result.checked), (["figure"], []))
        self.assertNotIn("/news", self.server.paths())                                   # the page was never requested

    def test_robots_size_limit_is_exact_and_over_it_nothing_is_guessed(self):
        tail = "\nUser-agent: *\nDisallow: /\n"
        exact = "#" + "c" * (static_fetch.MAX_ROBOTS_BYTES - len(tail) - 1) + tail
        self.assertEqual(len(exact.encode()), static_fetch.MAX_ROBOTS_BYTES)
        self.assertEqual(self.verdict(exact), ("disallowed", "robots-disallowed"))        # exactly the limit is read whole
        self.assertEqual(self.verdict("#" + exact), ("unreadable", "too-large"))          # one byte more is not read at all
        self.assertNotIn("/news", self.server.paths())

    def test_a_robots_file_that_cannot_be_judged_completely_is_unreadable(self):
        many = "User-agent: *\n" + "".join(f"Disallow: /p{i}\n" for i in range(robots_rules.MAX_RULES + 1))
        for name, body in {"too many rules": many, "invalid utf-8": b"User-agent: *\nDisallow: /\xff\xfe\n",
                           "latin-1 accent": "User-agent: *\nDisallow: /caf\xe9\n".encode("latin-1"),
                           "rule too long": "User-agent: *\nDisallow: /" + "a" * robots_rules.MAX_PATTERN + "\n"}.items():
            with self.subTest(name):
                self.assertEqual(self.verdict(body), ("unreadable", "robots-unsupported"))
        result = self.watch(sources(LISTING, static_mode="live"))
        self.assertEqual(result.sources["figure"]["reason"], "robots-unreadable")
        self.assertEqual(result.sources["figure"]["detail"], "robots-unsupported")
        self.assertNotIn("/news", self.server.paths())

    def test_encoded_special_characters_in_a_rule_match_the_literal_ones_end_to_end(self):
        # 01, R-D-4b12-01: both rules were wrongly allowed; the page must not be requested and the baseline stays
        disallowed = ("disallowed", "robots-disallowed")
        self.assertEqual(self.verdict("User-agent: *\nDisallow: /foo%2A\n", "/foo*"), disallowed)
        self.assertEqual(self.verdict("User-agent: *\nDisallow: /foo%24\n", "/foo$"), disallowed)
        self.assertEqual(self.verdict("User-agent: *\nDisallow: /private\n", "/private"), disallowed)
        self.assertEqual(self.verdict("User-agent: *\nDisallow: /a%2Ab\n", "/axb"), ("allowed", ""))     # an encoded * is no wildcard
        base_state = {"schema": fetch.PAGE_STATE_SCHEMA, "sources": {"figure": {"digest": "d", "since": "2026-09-01",
                                                                                   "changed": "2026-10-01", "links": [],
                                                                                   "extractor": "static-1"}}}
        for rule, path in (("/foo%2A", "/foo*"), ("/foo%24", "/foo$"), ("/private", "/private")):
            with self.subTest(rule):
                self.server.requests.clear()
                self.server.serve("/robots.txt", Reply(200, f"User-agent: *\nDisallow: {rule}\n", ctype="text/plain"))
                self.server.serve(path, Reply(200, V1))
                entry = dict(LISTING, url=f"https://{HOST}{path}", link_prefix=path + "/")
                result = self.watch(sources(entry), copy.deepcopy(base_state))
                self.assertEqual(self.server.paths(), ["/robots.txt"])
                self.assertEqual((result.robots_skipped, result.checked), (["figure"], []))
                self.assertEqual(result.state["sources"], base_state["sources"])

    def test_robots_is_judged_per_url_and_cached_per_origin(self):
        self.server.serve("/robots.txt", Reply(200, "User-agent: *\nDisallow: /private\n", ctype="text/plain"))
        fetcher = self.fetcher()
        cache: dict = {}
        budget = fetcher.budget(20)
        self.assertEqual(fetch._static_robots(fetcher, f"https://{HOST}/private/x", cache, budget), ("disallowed", "robots-disallowed"))
        self.assertEqual(fetch._static_robots(fetcher, f"https://{HOST}/news", cache, budget), ("allowed", ""))
        self.assertEqual(self.server.paths().count("/robots.txt"), 1)

    # --- nothing is cut silently; the baseline must be able to hold what it is given ------------------------
    def test_content_over_a_cap_is_refused_and_leaves_the_baseline_alone(self):
        self.server.serve("/news", Reply(200, V1))
        base = self.watch(sources(LISTING))
        links = "".join(f"<p><a href='/news/post-{i}'>A post number {i} title here</a></p>" for i in range(6))
        tail = "<p><a href='/news/latest'>The latest post title here</a></p>"
        pages = {"text over the cap": f"<html><head><title>News</title></head><body><main>{links}<p>{'x' * 200_100}</p>{tail}</main></body></html>",
                 "01's example: a link, 201000 characters, a link":
                     "<html><head><title>News</title></head><body><main><p><a href='/news/first'>The first post title here</a></p>"
                     f"<p>{'x' * 201_000}</p><p><a href='/news/last'>The last post title here</a></p></main></body></html>",
                 "links over the cap": "<html><head><title>News</title></head><body><main>"
                 + "".join(f"<p><a href='/news/p{i}'>Post {i} is here</a></p>" for i in range(static_extract.MAX_ANCHORS + 1))
                 + "</main></body></html>"}
        for name, html in pages.items():
            with self.subTest(name):
                self.server.serve("/news", Reply(200, html))
                result = self.watch(sources(LISTING), copy.deepcopy(base.state))
                self.assertEqual(result.sources["figure"], {"fetcher": "static", "outcome": "blocked", "reason": "content-cap"})
                self.assertEqual((result.checked, result.items), ([], []))
                self.assertEqual(result.state["sources"], base.state["sources"])
                self.assertEqual(result.observations[0]["reason"], "content-cap")

    def test_a_listing_the_baseline_cannot_hold_whole_is_held(self):
        def page(count, first=0):
            return listing(*[(f"/news/post-{i}", f"A post number {i} with a title", "") for i in range(first, first + count)])

        limit = fetch.MAX_STATE_LINKS
        self.server.serve("/news", Reply(200, page(limit + 1)))
        held = self.watch(sources(LISTING))
        self.assertEqual(held.sources["figure"], {"fetcher": "static", "outcome": "blocked", "reason": "too-many-links"})
        self.assertEqual((held.checked, held.items, held.state["sources"]), ([], [], {}))
        # an existing baseline (another extractor's) is kept as it was
        old = self.firecrawl_baseline()
        migrated = self.watch(sources(LISTING), copy.deepcopy(old))
        self.assertEqual((migrated.sources["figure"]["reason"], migrated.state["sources"]), ("too-many-links", old["sources"]))
        # exactly the limit is recorded whole, and the next day's identical page is not news
        self.server.serve("/news", Reply(200, page(limit)))
        first = self.watch(sources(LISTING))
        self.assertEqual(first.checked, ["figure"])
        self.assertEqual(len(first.state["sources"]["figure"]["links"]), limit)
        second = self.watch(sources(LISTING), copy.deepcopy(first.state))
        self.assertEqual(second.items, [])
        # links that left the page are forgotten before any link that is on it
        known = copy.deepcopy(first.state)
        self.server.serve("/news", Reply(200, page(limit - 10, first=10)))
        shifted = self.watch(sources(LISTING), known)
        recorded = {link["url"] for link in shifted.state["sources"]["figure"]["links"]}
        self.assertTrue({f"https://{HOST}/news/post-{i}" for i in range(10, limit)} <= recorded)
        self.assertEqual(self.watch(sources(LISTING), copy.deepcopy(shifted.state)).items, [])

    def test_property_a_source_is_recorded_whole_or_held_never_half(self):
        limit_links, limit_lines = fetch.MAX_STATE_LINKS, fetch.MAX_STATE_LINES
        for count in (1, 2, 150, limit_links - 1, limit_links, limit_links + 1, limit_links + 50):
            with self.subTest(links=count):
                cards = [(f"/news/post-{i}", f"A post number {i} with a title", "") for i in range(count)]
                self.server.serve("/news", Reply(200, listing(*cards)))
                first = self.watch(sources(LISTING))
                second = self.watch(sources(LISTING), copy.deepcopy(first.state))
                if count > limit_links:
                    self.assertEqual((first.checked, first.state["sources"], second.checked), ([], {}, []))
                else:
                    recorded = {link["url"] for link in first.state["sources"]["figure"]["links"]}
                    self.assertEqual(recorded, {f"https://{HOST}/news/post-{i}" for i in range(count)})   # every link, none missing
                    self.assertEqual(second.items, [])                                                    # nothing is news the next day
        for count in (1, 2, 1000, limit_lines - 1, limit_lines, limit_lines + 1):
            with self.subTest(lines=count):
                lines = "".join(f"<p>Line {i} of the regulation applies to machinery</p>" for i in range(count))
                self.server.serve("/standard", Reply(200, f"<html><head><title>S</title></head><body><main>{lines}</main></body></html>"))
                entry = dict(PAGE, min_text_chars=100)
                if count < 5:
                    continue
                first = self.watch(sources(entry))
                second = self.watch(sources(entry), copy.deepcopy(first.state))
                if count > limit_lines:
                    self.assertEqual((first.checked, first.state["sources"], second.checked), ([], {}, []))
                else:
                    self.assertEqual(len(first.state["sources"]["watch-iso"]["lines"]), count)
                    self.assertEqual(second.items, [])

    def test_a_page_the_baseline_cannot_hold_whole_is_held(self):
        def page(count):
            lines = "".join(f"<p>Line {i} of the regulation applies to machinery</p>" for i in range(count))
            return f"<html><head><title>Standard</title></head><body><main>{lines}</main></body></html>"

        limit = fetch.MAX_STATE_LINES
        self.server.serve("/standard", Reply(200, page(limit + 1)))
        held = self.watch(sources(PAGE))
        self.assertEqual(held.sources["watch-iso"], {"fetcher": "static", "outcome": "blocked", "reason": "too-many-lines"})
        self.assertEqual((held.checked, held.state["sources"]), ([], {}))
        self.server.serve("/standard", Reply(200, page(limit)))
        first = self.watch(sources(PAGE))
        self.assertEqual(first.checked, ["watch-iso"])
        self.assertEqual(len(first.state["sources"]["watch-iso"]["lines"]), limit)
        second = self.watch(sources(PAGE), copy.deepcopy(first.state))
        self.assertEqual(second.items, [])                                                # the same page is no change

    # --- observe first ---------------------------------------------------------------------------------
    def test_observe_is_the_default_and_the_run_takes_nothing_from_it(self):
        for mode in (None, "observe", "LIVE", "Live", "live ", "banana", True, 1, ""):
            with self.subTest(mode=mode):
                self.server.requests.clear()
                self.server.serve("/news", Reply(200, V1))
                raw = sources(LISTING)
                if mode is None:
                    del raw["page_watch_settings"]["static_mode"]
                else:
                    raw["page_watch_settings"]["static_mode"] = mode
                prior = copy.deepcopy(self.firecrawl_baseline())
                result = self.watch(raw, copy.deepcopy(prior))
                self.assertEqual(self.server.paths(), ["/robots.txt", "/news"])           # it did look, under every rule
                self.assertEqual((result.attempted, result.checked, result.failed, result.robots_skipped, result.static_checked),
                                 ([], [], [], [], []))
                self.assertEqual(result.items, [])
                self.assertEqual(result.state["sources"], prior["sources"])                  # the baseline is not touched
                self.assertEqual(result.sources["figure"], {"fetcher": "static", "outcome": "observed", "reason": ""})
                observation, = result.observations
                self.assertEqual((observation["mode"], observation["outcome"], observation["http_status"], observation["links"],
                                  observation["bytes"], observation["robots"]), ("observe", "checked", 200, 2, len(V1.encode()), "allowed"))
                self.assertEqual(len(observation["digest"]), 16)
                self.assertEqual(status_of(result), "skipped")         # no freshness claim from an observation

    def test_only_the_exact_word_live_makes_a_static_source_count(self):
        self.server.serve("/news", Reply(200, V1))
        result = self.watch(sources(LISTING, static_mode="live"))
        self.assertEqual((result.checked, result.static_checked), (["figure"], ["figure"]))
        self.assertEqual(result.observations[0]["mode"], "live")
        self.assertEqual(status_of(result), "ok")

    def test_observing_records_what_went_wrong_and_fails_nothing(self):
        challenge = "<html><head><title>Just a moment...</title></head><body><script src='/cdn-cgi/challenge-platform/x.js'></script></body></html>"
        cases = {"challenge": (Reply(200, challenge), "challenge", ""), "403": (Reply(403, "no"), "http-403", ""),
                 "redirect": (Reply(302, "", headers={"Location": "https://other.example/x?y=1"}), "redirect", "other.example")}
        for name, (reply, reason, detail) in cases.items():
            with self.subTest(name):
                self.server.serve("/news", reply)
                result = self.watch(sources(LISTING, static_mode="observe"))
                self.assertEqual((result.failed, result.robots_skipped, result.checked), ([], [], []))
                self.assertFalse(result.unavailable)
                observation, = result.observations
                self.assertEqual((observation["reason"], observation["detail"]), (reason, detail))
                self.assertNotEqual(observation["outcome"], "checked")

    def test_observing_while_firecrawl_is_down_is_still_a_firecrawl_outage(self):
        self.server.serve("/news", Reply(200, V1))
        result = self.watch(sources(self.firecrawl_entry(), LISTING, static_mode="observe"), endpoint="http://127.0.0.1:9")
        self.assertEqual(status_of(result), "firecrawl-unavailable")
        self.assertEqual(result.sources["figure"]["outcome"], "observed")

    def test_a_redirect_is_not_followed_and_only_the_target_name_is_kept(self):
        self.server.serve("/news", Reply(302, "", headers={"Location": "https://other.example/secret/path?token=abc"}))
        result = self.watch(sources(LISTING))
        self.assertEqual(self.server.paths(), ["/robots.txt", "/news"])                    # nothing was requested after the 302
        self.assertEqual(result.sources["figure"], {"fetcher": "static", "outcome": "blocked", "reason": "redirect",
                                                    "detail": "other.example"})
        self.assertIn("redirect (other.example)", self.out)
        for leak in ("secret", "token", "abc"):
            self.assertNotIn(leak, self.out)
            self.assertNotIn(leak, json.dumps(result.summary()))

    # --- the source's own sanity settings -------------------------------------------------------------------
    def test_a_page_must_carry_its_marker_and_enough_text(self):
        self.server.serve("/standard", Reply(200, article()))
        base = self.watch(sources(PAGE))
        self.assertEqual(base.checked, ["watch-iso"])
        for name, entry, why in (("marker", dict(PAGE, expect_text="Directive 2006/42"), "marker-missing"),
                                 ("text", dict(PAGE, min_text_chars=5000), "empty-page")):
            with self.subTest(name):
                result = self.watch(sources(entry), copy.deepcopy(base.state))
                self.assertEqual(result.sources["watch-iso"]["reason"], why)
                self.assertEqual(result.state["sources"], base.state["sources"])
                self.assertEqual(result.items, [])

    def test_a_listing_must_reach_its_minimum_number_of_links(self):
        self.server.serve("/news", Reply(200, V1))                                          # two links
        result = self.watch(sources(dict(LISTING, min_links=3)))
        self.assertEqual((result.sources["figure"]["reason"], result.checked), ("no-links", []))
        self.assertEqual(self.watch(sources(dict(LISTING, min_links=2))).checked, ["figure"])

    def test_a_listing_that_loses_most_of_its_links_is_not_read_until_someone_decides(self):
        cards = [(f"/news/post-{i}", f"A post number {i} with a title", "Oct 01, 2026 · text.") for i in range(10)]
        self.server.serve("/news", Reply(200, listing(*cards)))
        first = self.watch(sources(LISTING))
        self.assertEqual(first.state["sources"]["figure"]["link_count"], 10)
        self.server.serve("/news", Reply(200, listing(*cards[:4])))
        for name, entry, expected in (("dropped", LISTING, "link-drop"), ("reviewed", dict(LISTING, allow_link_drop=True), "")):
            with self.subTest(name):
                result = self.watch(sources(entry), copy.deepcopy(first.state))
                if expected:
                    self.assertEqual((result.sources["figure"]["reason"], result.checked), ("link-drop", []))
                    self.assertEqual(result.state["sources"], first.state["sources"])      # the baseline stays as it was
                else:
                    self.assertEqual(result.checked, ["figure"])
                    self.assertEqual(result.state["sources"]["figure"]["link_count"], 4)
        # exactly half is not a drop; and a baseline another extractor made has no count to compare with
        self.server.serve("/news", Reply(200, listing(*cards[:5])))
        self.assertEqual(self.watch(sources(LISTING), copy.deepcopy(first.state)).checked, ["figure"])
        old = self.firecrawl_baseline()
        old["sources"]["figure"]["link_count"] = 50
        self.assertEqual(self.watch(sources(LISTING), old).checked, ["figure"])
        # a short list on record is not judged: 5 on record, 2 now
        small = copy.deepcopy(first.state)
        small["sources"]["figure"]["link_count"] = 5
        self.server.serve("/news", Reply(200, listing(*cards[:2])))
        self.assertEqual(self.watch(sources(LISTING), small).checked, ["figure"])

    def test_the_first_read_records_the_link_count(self):
        self.server.serve("/news", Reply(200, V1))
        self.assertEqual(self.watch(sources(LISTING)).state["sources"]["figure"]["link_count"], 2)

    def test_static_entries_must_say_what_a_real_page_looks_like(self):
        self.assertEqual(fetch.page_watch_problems(sources(LISTING, PAGE)), [])
        bad = {
            "listing without min_links": dict(LISTING, min_links=None),
            "min_links 0": dict(LISTING, min_links=0), "min_links 1001": dict(LISTING, min_links=1001),
            "min_links true": dict(LISTING, min_links=True), "min_links text": dict(LISTING, min_links="2"),
            "page without marker": dict(PAGE, expect_text=None), "short marker": dict(PAGE, expect_text="ab"),
            "long marker": dict(PAGE, expect_text="x" * 201), "marker number": dict(PAGE, expect_text=12345),
            "page without min text": dict(PAGE, min_text_chars=None), "min text 99": dict(PAGE, min_text_chars=99),
            "min text huge": dict(PAGE, min_text_chars=100_001), "min text bool": dict(PAGE, min_text_chars=True),
            "allow_link_drop text": dict(LISTING, allow_link_drop="yes"),
        }
        for name, entry in bad.items():
            with self.subTest(name):
                self.assertTrue(fetch.page_watch_problems(sources(entry)), name)
                with FakeFirecrawl() as fc:
                    result = self.watch(sources(entry), endpoint=fc.url)
                self.assertEqual((result.attempted, result.observations), ([], []))        # an invalid entry is not run
        plain = {k: v for k, v in LISTING.items() if k not in ("fetcher", "min_links")}
        self.assertEqual(fetch.page_watch_problems(sources(plain)), [])                     # Firecrawl entries need none of it

    # --- one source cannot take the run down -----------------------------------------------------------------
    def test_an_unexpected_error_is_that_sources_internal_error(self):
        self.server.serve("/news", Reply(200, V1))
        self.server.serve("/standard", Reply(200, article()))
        real = fetch._watch_one

        def sometimes(entry, *args, **kwargs):
            if entry["id"] == "figure":
                raise RuntimeError("SECRET-INTERNAL-DETAIL https://internal.example/x")
            return real(entry, *args, **kwargs)

        result = self.watch(sources(LISTING, PAGE), patches=[patch.object(fetch, "_watch_one", sometimes)])
        self.assertEqual(result.sources["figure"], {"fetcher": "static", "outcome": "unavailable", "reason": "internal-error"})
        self.assertEqual(result.checked, ["watch-iso"])
        self.assertFalse(result.unavailable)
        self.assertNotIn("SECRET", self.out)
        self.assertNotIn("internal.example", self.out)

    def test_an_interrupt_is_not_swallowed_by_the_isolation(self):
        self.server.serve("/news", Reply(200, V1))

        def interrupt(*args, **kwargs):
            raise KeyboardInterrupt()

        with self.assertRaises(KeyboardInterrupt):
            self.watch(sources(LISTING), patches=[patch.object(fetch, "_watch_one", interrupt)])

    def test_a_parse_that_outlasts_the_budget_is_budget(self):
        self.server.serve("/news", Reply(200, V1))

        def slow(*args, **kwargs):
            raise fetch.static_extract.ExtractTimeout()

        result = self.watch(sources(LISTING), patches=[patch.object(fetch.static_extract, "html_to_markdown", slow)])
        self.assertEqual(result.sources["figure"]["reason"], "budget")

    def test_a_static_failure_is_not_a_firecrawl_outage(self):
        self.server.serve("/news", Reply(500, "boom"))
        result = self.watch(sources(LISTING))
        self.assertFalse(result.unavailable)
        self.assertEqual(status_of(result), "all-failed")

    def test_page_text_cannot_forge_a_link_in_a_run(self):
        page = ("<html><head><title>News</title></head><body><main><h1>News</h1>"
                "<p>See [Forged post](https://good.test/news/forged) for details</p>"
                "<a href='/news/real'>A real post title here</a><p>Oct 20, 2026 · real</p></main></body></html>")
        self.server.serve("/news", Reply(200, page))
        result = self.watch(sources(LISTING))
        self.assertEqual(self.urls(result), [f"https://{HOST}/news/real"])
        self.assertNotIn("forged", json.dumps(result.state))

    # --- evidence, observation file and logs ---------------------------------------------------------------
    def test_the_observation_file_is_private_and_has_no_page_content(self):
        self.server.serve("/news", Reply(200, V1))
        result = self.watch(sources(LISTING))
        observations = result.observations + [{"id": "x", "body": "SECRET-BODY", "reason": "y" * 500, "bytes": True}]
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "observe"
            previous_umask = os.umask(0o022)                    # the mode must come from the code, not from the caller's umask
            try:
                path = cli.write_page_watch_observation(observations, "2026-10-21", directory)
            finally:
                os.umask(previous_umask)
            text = path.read_text(encoding="utf-8")
            self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")
            self.assertEqual(oct(directory.stat().st_mode & 0o777), "0o700")
            self.assertIsNone(cli.write_page_watch_observation([], "2026-10-22", directory))
            self.assertIsNone(cli.write_page_watch_observation(None, "2026-10-22", directory))
            self.assertEqual([p.name for p in directory.iterdir()], ["2026-10-21.json"])
        record = json.loads(text)
        self.assertEqual(record["schema"], "pairadar-page-watch-observe-v1")
        self.assertEqual(record["sources"][0]["id"], "figure")
        self.assertEqual(record["sources"][1]["reason"], "y" * 80)                           # cut, and only known keys survive
        self.assertNotIn("body", record["sources"][1])
        for leak in ("SECRET-BODY", "https://", HOST, "Helix", "127.0.0.1", "Introducing"):
            self.assertNotIn(leak, text)
        summary = json.dumps(result.summary())
        self.assertNotIn("digest", summary)                                                  # the published report carries none of it
        self.assertNotIn("observation", summary)

    def test_the_observation_reaches_the_state_directory_through_main(self):
        ctx = {"date": "2026-10-21", "fetch": {"page_watch": {"enabled": True, "attempted": [], "checked": [], "failed": [],
                                                               "robots_skipped": [], "unavailable": False, "items": 0}},
               "page_watch_observations": [{"id": "figure", "mode": "observe", "outcome": "checked", "reason": "", "detail": "",
                                            "robots": "allowed", "http_status": 200, "bytes": 5, "links": 2, "text_chars": 40,
                                            "digest": "0123456789abcdef"}]}
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"PAIRADAR_STATE_DIR": tmp}), \
                patch.object(cli, "run", lambda **kwargs: ctx), patch.object(cli, "degraded", lambda *a: False), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["--date", "2026-10-21"]), 0)
            self.assertTrue((Path(tmp) / "page-watch" / "2026-10-21.json").exists())
            observed = json.loads((Path(tmp) / "page-watch-observe" / "2026-10-21.json").read_text(encoding="utf-8"))
        self.assertEqual(observed["sources"][0]["digest"], "0123456789abcdef")

    def test_evidence_says_when_a_run_is_degraded_and_what_was_only_observed(self):
        self.server.serve("/news", Reply(200, V1))
        self.server.serve("/standard", Reply(500, "no"))
        with tempfile.TemporaryDirectory() as tmp:
            live = self.watch(sources(LISTING, PAGE))
            record = json.loads(cli.write_page_watch_evidence({"page_watch": live.summary()}, "2026-10-21", Path(tmp),
                                                              detail=live.detail()).read_text())
            self.assertTrue(record["degraded"])
            self.assertEqual(record["status"], "ok")
            observed = self.watch(sources(LISTING, static_mode="observe"))
            record = json.loads(cli.write_page_watch_evidence({"page_watch": observed.summary()}, "2026-10-22", Path(tmp),
                                                              detail=observed.detail()).read_text())
        self.assertEqual(record["sources_observed"], ["figure"])
        self.assertEqual((record["status"], record["sources_checked"], record["degraded"]), ("skipped", 0, True))

    def test_a_source_blocked_or_unavailable_three_records_in_a_row_is_escalated(self):
        def block(figure, iso):
            sources_ = {"figure": {"fetcher": "static", "outcome": figure, "reason": "http-403" if figure != "checked" else ""},
                        "watch-iso": {"fetcher": "static", "outcome": iso, "reason": "challenge" if iso != "checked" else ""}}
            return {"page_watch": {"enabled": True, "attempted": [], "checked": [], "failed": [], "robots_skipped": [],
                                   "unavailable": False, "items": 0, "planned": ["figure", "watch-iso"], "sources": sources_,
                                   "static_checked": []}}

        def day(directory, name, figure, iso):
            return json.loads(cli.write_page_watch_evidence(block(figure, iso), name, directory).read_text())

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            self.assertEqual(day(directory, "2026-10-19", "unavailable", "checked")["sources_escalated"], [])
            self.assertEqual(day(directory, "2026-10-20", "blocked", "unavailable")["sources_escalated"], [])
            with contextlib.redirect_stdout(io.StringIO()) as out:
                third = day(directory, "2026-10-21", "unavailable", "unavailable")
            self.assertEqual(third["sources_escalated"], ["figure"])            # iso was fine on the 19th
            self.assertIn("ESCALATE: figure", out.getvalue())
            (directory / "2026-10-20.json").write_text("not json")            # an unreadable record ends the run of failures
            self.assertEqual(day(directory, "2026-10-22", "unavailable", "unavailable")["sources_escalated"], [])
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(day(Path(tmp), "2026-10-21", "unavailable", "unavailable")["sources_escalated"], [])      # no history

    def test_fetch_all_carries_the_observations_but_not_into_the_published_report(self):
        import inspect
        from pairadar import heat
        self.server.serve("/news", Reply(403, "no"))
        self.server.serve("/standard", Reply(200, article()))
        for mode in ("observe", "live"):
            with self.subTest(mode):
                watched = self.watch(sources(LISTING, PAGE, static_mode=mode))
                config = Config(sources={"feeds": [], "page_watch": []}, taxonomy={}, glossary={}, baseline={})
                with patch.object(fetch, "fetch_page_watch", lambda *a, **k: watched), \
                        patch.object(heat, "fetch_signals", lambda *a, **k: heat.Signals()), contextlib.redirect_stdout(io.StringIO()):
                    report = fetch.fetch_all(config, offline=False, reference=DAY, page_state={})
                published = json.dumps(report.to_dict())
                for private in ("digest", "observation", "planned", "static_checked", "detail", "sources\": {", "http-403", "observed",
                                "blocked", "robots-unreadable", "static"):
                    self.assertNotIn(private, published, private)               # radar/latest.json carries none of the per-source result
                self.assertEqual(sorted(report.to_dict()["page_watch"]),
                                 ["attempted", "checked", "enabled", "failed", "items", "robots_skipped", "unavailable"])
                self.assertEqual(report.page_watch_observations, watched.observations)       # the private channels have it
                self.assertEqual(report.page_watch_detail, watched.detail())
                self.assertEqual(report.page_watch_detail["sources"]["figure"]["reason"], "http-403")
        self.assertIn("report.page_watch_observations", inspect.getsource(cli.build_context))     # the CLI hands them on
        self.assertIn("report.page_watch_detail", inspect.getsource(cli.build_context))

    def test_a_url_the_fetcher_would_not_resolve_as_robots_judged_it_is_refused_before_any_request(self):
        for url in (f"https://{HOST}/news/../private", f"https://{HOST}//news", f"https://{HOST}/%2e%2e/news", f"https://{HOST}/news/%4%31"):
            with self.subTest(url):
                self.server.requests.clear()
                result = self.watch(sources(dict(LISTING, url=url)))
                self.assertEqual(result.sources["figure"], {"fetcher": "static", "outcome": "blocked", "reason": "url-invalid"})
                self.assertEqual(self.server.requests, [])                                  # not even robots.txt was requested

    def test_encoded_separators_hide_nothing_from_robots_end_to_end(self):
        """01's R-D-b7b7-01: the page must not be requested for any spelling that a server could resolve to /private."""
        self.server.serve("/robots.txt", Reply(200, "User-agent: *\nAllow: /public\nDisallow: /private\n", ctype="text/plain"))
        self.server.serve("/public", Reply(200, V1))
        for path in ("/public%2F..%2Fprivate", "/public%2f..%2fprivate", "/public%2F%2e%2e%2Fprivate", "/public%2F%2E%2E%2Fprivate",
                     "/public/..%2Fprivate", "/public%2F../private", "/public%5C..%5Cprivate", "/public%5c..%5cprivate",
                     "/public%252F..%252Fprivate", "/public%252f..%252fprivate", "/public%25252F..%25252Fprivate",
                     "/%252e%252e/private", "/public/%2e%2e/private", "/public/%2E%2e/private", "/public/./../private"):
            with self.subTest(path):
                self.server.requests.clear()
                result = self.watch(sources(dict(LISTING, url=f"https://{HOST}{path}", link_prefix="/public/")))
                self.assertEqual(result.sources["figure"], {"fetcher": "static", "outcome": "blocked", "reason": "url-invalid"})
                self.assertEqual(self.server.requests, [])                           # not even robots.txt
        # an ordinary path on the same rules is judged and fetched as before
        self.server.requests.clear()
        result = self.watch(sources(dict(LISTING, url=f"https://{HOST}/public", link_prefix="/public/")))
        self.assertEqual(self.server.paths(), ["/robots.txt", "/public"])

    def test_decoding_depth_and_path_parameters_cannot_change_the_robots_decision_end_to_end(self):
        """01's R-D-155c-01 and -02: refused before any request, for each rule shape the review used."""
        paths = ("/%2570rivate", "/%2570rivate/", "/%2550rivate", "/%252570rivate", "/%25%37%30rivate", "/pr%2569vate",
                 "/private;v=1", "/private%3Bv=1", "/private%3bv=1", "/private;", "/private/;v=1", "/priv%61te;v=1")
        for robots in ("User-agent: *\nDisallow: /private\n", "User-agent: *\nDisallow: /private$\n", "User-agent: *\nAllow: /\nDisallow: /priv\n"):
            self.server.serve("/robots.txt", Reply(200, robots, ctype="text/plain"))
            for path in paths:
                with self.subTest(robots=robots.splitlines()[-1], path=path):
                    self.server.requests.clear()
                    result = self.watch(sources(dict(LISTING, url=f"https://{HOST}{path}", link_prefix="/p/")))
                    self.assertEqual(result.sources["figure"], {"fetcher": "static", "outcome": "blocked", "reason": "url-invalid"})
                    self.assertEqual(self.server.requests, [])

    def test_a_robots_rule_with_an_invalid_percent_sign_means_no_page_request(self):
        for rule in ("/%4%31", "/100%", "/%zz"):
            with self.subTest(rule):
                self.server.requests.clear()
                self.server.serve("/robots.txt", Reply(200, f"User-agent: *\nDisallow: {rule}\nAllow: /\n", ctype="text/plain"))
                self.server.serve("/news", Reply(200, V1))
                result = self.watch(sources(LISTING))
                self.assertEqual(result.sources["figure"], {"fetcher": "static", "outcome": "unavailable", "reason": "robots-unreadable",
                                                            "detail": "robots-unsupported"})
                self.assertEqual(self.server.paths(), ["/robots.txt"])

    def test_static_sources_are_never_fetched_on_a_hosted_runner(self):
        """The 09:20 fallback (.github/workflows/daily.yml) runs `python -m pairadar` on GitHub's machines."""
        self.server.serve("/news", Reply(200, V1))
        for value in ("true", "TRUE", " true "):
            for mode in ("observe", "live"):
                with self.subTest(value=value, mode=mode):
                    self.server.requests.clear()
                    accepted = self.server.accepted
                    result = self.watch(sources(LISTING, PAGE, static_mode=mode), patches=[patch.dict(os.environ, {"GITHUB_ACTIONS": value})])
                    self.assertEqual(self.server.accepted, accepted)                  # not one connection
                    self.assertEqual(result.sources["figure"], {"fetcher": "static", "outcome": "not-attempted", "reason": "hosted-runner"})
                    self.assertEqual((result.attempted, result.checked, result.failed, result.observations), ([], [], [], []))
        for value in ("false", "", "1", "yes"):
            with self.subTest(value=value):
                result = self.watch(sources(LISTING), patches=[patch.dict(os.environ, {"GITHUB_ACTIONS": value})])
                self.assertEqual(result.checked, ["figure"])                              # only the exact "true" excludes it

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
