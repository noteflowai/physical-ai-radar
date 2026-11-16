"""Mainstream sources, attention ("heat") and the highlights quota (2026-10-25).

The 10-24 shortlist held four arXiv papers out of eight and nothing any other site was
talking about. These tests pin what changed: the new feeds and their filters, robots.txt
and request spacing, the heat score (ranking only, never evidence), and the quota on
the highlights. No test here touches the network.
"""
from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
import urllib.parse
from datetime import date
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pairadar import cli, fetch, heat, health  # noqa: E402
from pairadar.config import LANGS, Item, load_config  # noqa: E402
from pairadar.distill import enrich, press_release, select, strip_boilerplate  # noqa: E402

DAY = date(2026, 10, 24)
CONFIG = load_config()


def item(title: str, *, source: str = "blog", evidence: str = "M", score: float = 3.0,
         lane: str = "systems", url: str | None = None, signals: list[str] | None = None) -> Item:
    entry = Item(id=f"{source}:{title}", title=title, url=url or f"https://{source}.example/{abs(hash(title))}",
                 publisher=source, source_id=source, evidence=evidence, published=DAY.isoformat())
    entry.lane, entry.lane_hits, entry.anchor_hits, entry.score = lane, 2, 1, score
    entry.signals = list(signals or [])
    return entry


def config_with(**sources):
    return type(CONFIG)(sources={**CONFIG.sources, **sources}, taxonomy=CONFIG.taxonomy,
                        glossary=CONFIG.glossary, baseline=CONFIG.baseline)


def rss(*entries: tuple[str, str, str]) -> bytes:
    body = "".join(f"<item><title>{t}</title><link>{u}</link><description>{d}</description>"
                   f"<pubDate>Fri, 23 Oct 2026 10:00:00 GMT</pubDate></item>" for t, u, d in entries)
    return f"<rss version='2.0'><channel><title>x</title>{body}</channel></rss>".encode()


class SourcesConfigTest(unittest.TestCase):
    NEW = {"openai", "google-research", "bair", "mit-news-robotics", "nature-machine-intelligence",
           "techcrunch-robotics", "robohub", "qbitai", "import-ai", "github-lerobot", "github-isaaclab",
           "github-mujoco"}

    def feeds(self):
        return {feed["id"]: feed for feed in CONFIG.sources["feeds"]}

    def test_the_checked_sources_are_declared_and_general_ones_are_filtered(self) -> None:
        feeds = self.feeds()
        self.assertLessEqual(self.NEW, set(feeds))
        for feed_id in self.NEW:
            feed = feeds[feed_id]
            self.assertIn(feed["evidence"], {"O", "R", "M"})
            self.assertTrue(feed["url"].startswith("https://"))
            # Every new source either only publishes Physical AI, or filters by its own terms.
            self.assertTrue(feed.get("topical") or feed.get("keywords"), feed_id)
        for general in ("openai", "google-research", "techcrunch-robotics", "qbitai"):
            self.assertTrue(feeds[general].get("keywords"), general)
            self.assertFalse(feeds[general].get("topical"), general)
        self.assertNotIn("cosmos", feeds["openai"]["keywords"])
        self.assertIn("机器人", feeds["qbitai"]["keywords"])

    def test_robots_disallowed_and_feedless_sources_are_not_declared(self) -> None:
        urls = [feed["url"] for feed in CONFIG.sources["feeds"]]
        self.assertFalse([url for url in urls if "science.org" in url])     # /action/ disallowed
        self.assertFalse([url for url in urls if "openpi" in url])          # no releases, no tags
        self.assertFalse([url for url in urls if "36kr" in url or "jiqizhixin" in url])

    def test_release_feeds_name_their_project_and_repository(self) -> None:
        feeds = self.feeds()
        tracked = heat.settings(CONFIG)["github_repos"]
        for feed_id in ("github-lerobot", "github-isaaclab", "github-mujoco"):
            self.assertTrue(feeds[feed_id]["title_prefix"])
            self.assertEqual(feeds[feed_id]["max_entries"], 1)
            self.assertIn(feeds[feed_id]["repo"], tracked)

    def test_new_signals_have_labels_in_every_language(self) -> None:
        for lang in LANGS:
            labels = CONFIG.ui(lang)["signals"]
            self.assertTrue(labels["trending"] and labels["press_release"], lang)


class FeedReadingTest(unittest.TestCase):
    NATURE = (b'<?xml version="1.0"?><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" '
              b'xmlns="http://purl.org/rss/1.0/" xmlns:dc="http://purl.org/dc/elements/1.1/" '
              b'xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel rdf:about="x"><title>N</title>'
              b'</channel><item rdf:about="https://www.nature.com/articles/s42256-026-00001-1">'
              b'<title>A humanoid robot learns dexterous manipulation</title>'
              b'<link>https://www.nature.com/articles/s42256-026-00001-1</link>'
              b'<content:encoded>&lt;p&gt;Nature Machine Intelligence, Published online: 23 October 2026; '
              b'&lt;a href="x"&gt;doi:10.1038/s42256-026-00001-1&lt;/a&gt;&lt;/p&gt;The authors train a '
              b'policy on 40 hours of teleoperation.</content:encoded><dc:date>2026-10-23</dc:date></item>'
              b'</rdf:RDF>')

    def read(self, payload: bytes, **feed):
        config = config_with(feeds=[{"id": "f", "url": "https://f.example/feed", "evidence": "R", **feed}])
        with mock.patch.object(fetch, "http_get", lambda url, *a, **k: payload), \
                mock.patch.object(fetch.time, "sleep"):
            return fetch.fetch_feeds(config)

    def test_rss_1_0_journal_feeds_are_read_and_their_doi_line_is_dropped(self) -> None:
        items, failed = self.read(self.NATURE)
        self.assertEqual((failed, len(items)), ([], 1))
        self.assertEqual(items[0].published, "2026-10-23")
        self.assertEqual(items[0].url, "https://www.nature.com/articles/s42256-026-00001-1")
        self.assertTrue(strip_boilerplate(items[0].summary).startswith("The authors train"))

    def test_keywords_filter_before_the_per_feed_cap(self) -> None:
        offtopic = [(f"Scaling storage part {n}", f"https://o.example/{n}", "databases") for n in range(150)]
        cosmos = ("Rapidly scaling Azure Cosmos DB storage", "https://o.example/cosmos", "Cosmos DB")
        payload = rss(*offtopic, cosmos, ("A humanoid robot demo", "https://o.example/robot", "robot arms"))
        kept, _ = self.read(payload, keywords=["robot", "humanoid"])
        self.assertEqual([entry.url for entry in kept], ["https://o.example/robot"])
        # The taxonomy anchors are looser: "cosmos" (the NVIDIA world model) lets the DB post in.
        anchored, _ = self.read(payload, require_anchor=True)
        self.assertEqual(sorted(entry.url for entry in anchored), ["https://o.example/cosmos", "https://o.example/robot"])
        everything, _ = self.read(payload)
        self.assertEqual(len(everything), CONFIG.sources["filters"]["max_entries_per_feed"])

    def test_max_entries_keeps_a_release_feed_to_its_newest_release(self) -> None:
        payload = (b"<feed xmlns='http://www.w3.org/2005/Atom'>"
                   b"<entry><title>3.16.0</title><link href='https://g.example/3.16.0'/><updated>2026-10-14T00:00:00Z</updated></entry>"
                   b"<entry><title>3.17.0</title><link href='https://g.example/3.17.0'/><updated>2026-10-22T00:00:00Z</updated></entry>"
                   b"</feed>")
        items, _ = self.read(payload, max_entries=1, title_prefix="MuJoCo")
        self.assertEqual([entry.title for entry in items], ["MuJoCo 3.17.0"])

    def test_a_bare_release_title_gets_its_project_name(self) -> None:
        items, _ = self.read(rss(("3.17.0", "https://github.com/x/y/releases/tag/3.17.0", "Physics"),
                                 ("MuJoCo 3.16.0", "https://github.com/x/y/releases/tag/3.16.0", "Physics")),
                             title_prefix="MuJoCo")
        self.assertEqual(sorted(entry.title for entry in items), ["MuJoCo 3.16.0", "MuJoCo 3.17.0"])

    def test_one_source_failing_leaves_the_others(self) -> None:
        config = config_with(feeds=[{"id": "dead", "url": "https://dead.example/feed", "evidence": "M"},
                                    {"id": "live", "url": "https://live.example/feed", "evidence": "M"}])
        payload = rss(("A humanoid robot", "https://live.example/a", "robot"))
        with mock.patch.object(fetch, "http_get", lambda url, *a, **k: None if "dead" in url else payload), \
                mock.patch.object(fetch.time, "sleep"):
            items, failed = fetch.fetch_feeds(config)
        self.assertEqual((failed, [entry.source_id for entry in items]), (["dead"], ["live"]))


class DisabledFeedsTest(unittest.TestCase):
    """A feed switched off in data/sources.json is not read: no robots.txt question, no GET, not "attempted", not "failed"."""

    ON = {"id": "on", "url": "https://on.example/feed", "evidence": "M", "topical": True}

    def off(self, value=False):
        return {"id": "off", "url": "https://off.example/feed", "evidence": "M", "topical": True, "enabled": value,
                "disabled_reason": "test"}

    def read(self, feeds, **extra):
        requested, asked = [], []

        def get(url, *a, **k):
            requested.append(url)
            return rss(("A robot", "https://on.example/a", "robot"))
        cache = fetch.RobotsCache(getter=lambda url: asked.append(url) or (404, None))
        skipped: list[str] = []
        with mock.patch.object(fetch, "http_get", get), mock.patch.object(fetch.time, "sleep"), \
                contextlib.redirect_stdout(io.StringIO()):
            items, failed = fetch.fetch_feeds(config_with(feeds=feeds, **extra), robots=cache, skipped=skipped)
        return requested, asked, skipped, failed, items

    def test_a_disabled_feed_is_not_requested_and_not_asked_about(self) -> None:
        requested, asked, skipped, failed, items = self.read([self.off(), self.ON])
        self.assertEqual(requested, ["https://on.example/feed"])
        self.assertEqual(asked, ["https://on.example/robots.txt"])
        self.assertEqual((skipped, failed), ([], []))
        self.assertEqual([entry.source_id for entry in items], ["on"])

    def test_only_true_or_nothing_switches_a_feed_on(self) -> None:
        for value in (False, "false", "False", "no", "true", "", 0, 1, None, []):
            with self.subTest(enabled=value):
                requested, asked, skipped, failed, _ = self.read([self.off(value)])
                self.assertEqual((requested, asked, skipped, failed), ([], [], [], []))
        for feed in (self.ON, {**self.ON, "enabled": True}):
            requested, _, _, _, _ = self.read([feed])
            self.assertEqual(requested, ["https://on.example/feed"])

    def test_a_disabled_feed_is_neither_attempted_nor_counted(self) -> None:
        config = config_with(feeds=[self.off(), self.ON], arxiv={**CONFIG.sources["arxiv"], "enabled": False},
                             page_watch_settings={**CONFIG.sources["page_watch_settings"], "enabled": False})
        get = lambda url, *a, **k: (b"User-agent: *\nAllow: /\n" if url.endswith("robots.txt")
                                    else rss(("A robot", "https://on.example/a", "robot")))
        with mock.patch.object(fetch, "http_get", get), mock.patch.object(fetch.time, "sleep"), \
                contextlib.redirect_stdout(io.StringIO()):
            report = fetch.fetch_all(config, reference=DAY)
        self.assertEqual(report.to_dict()["attempted"], 1)
        self.assertNotIn("off", report.failed)
        self.assertEqual(fetch.metadata(config)["feed_count"], 1)

    def test_the_committed_sources_keep_a_reason_for_every_disabled_feed(self) -> None:
        feeds = CONFIG.sources["feeds"]
        for feed in feeds:
            if feed.get("enabled", True) is not True:
                self.assertTrue(isinstance(feed.get("disabled_reason"), str) and feed["disabled_reason"].strip(), feed["id"])
        # github.com's robots.txt disallows /*.atom$ for every crawler (RFC 9309, read 2026-11-15): none may be read.
        for feed in fetch.enabled_feeds(CONFIG.sources):
            parts = urllib.parse.urlsplit(feed["url"])
            self.assertFalse(parts.netloc.lower() == "github.com" and parts.path.endswith(".atom"), feed["id"])
        # The three GitHub release feeds read the API since github.com's atom files are disallowed (tests/test_github_releases.py),
        # so none of them is disabled now.
        enabled = {feed["id"] for feed in fetch.enabled_feeds(CONFIG.sources)}
        self.assertLessEqual({"github-lerobot", "github-isaaclab", "github-mujoco"}, enabled)


class RobotsRulesTest(unittest.TestCase):
    """The feeds read robots.txt as RFC 9309 says (`robots_rules`), not with urllib.robotparser, which has no `*` or `$`."""

    def verdicts(self, body: bytes, *urls: str) -> list[str]:
        cache = fetch.RobotsCache(getter=lambda url: (200, body))
        with mock.patch.object(fetch.time, "sleep"), contextlib.redirect_stdout(io.StringIO()):
            return [cache.verdict(url) for url in urls]

    def test_github_atom_is_disallowed_by_its_wildcard_rule_and_the_page_is_not(self) -> None:
        body = (b"User-agent: *\nAllow: /*?tab=achievements&achievement=*\nDisallow: /gist/\nDisallow: /*/*/tree/\n"
                b"Disallow: /*.atom$\nDisallow: /*/*/tags\n")
        got = self.verdicts(body, "https://github.com/huggingface/lerobot/releases.atom",
                            "https://github.com/huggingface/lerobot/releases",
                            "https://github.com/huggingface/lerobot/releases.atom?page=2",     # `$` anchors the end of the path
                            "https://github.com/huggingface/lerobot/tags", "https://github.com/gist/x")
        self.assertEqual(got, [fetch.DISALLOW, fetch.ALLOW, fetch.ALLOW, fetch.DISALLOW, fetch.DISALLOW])

    def test_the_longest_match_wins_and_allow_wins_a_tie_not_the_first_rule(self) -> None:
        body = b"User-agent: *\nDisallow: /a/\nAllow: /a/b\nDisallow: /t\nAllow: /t\n"
        self.assertEqual(self.verdicts(body, "https://x.example/a/b", "https://x.example/a/c", "https://x.example/t"),
                         [fetch.ALLOW, fetch.DISALLOW, fetch.ALLOW])

    def test_the_group_that_names_the_radar_wins_over_star_and_groups_with_the_same_name_are_merged(self) -> None:
        body = (b"User-agent: *\nDisallow: /\n\nUser-agent: physical-ai-radar\nDisallow: /private\n\n"
                b"User-agent: Physical-AI-Radar\nDisallow: /second\n")
        self.assertEqual(self.verdicts(body, "https://x.example/feed", "https://x.example/private/a",
                                       "https://x.example/second"),
                         [fetch.ALLOW, fetch.DISALLOW, fetch.DISALLOW])
        self.assertEqual(self.verdicts(b"User-agent: otherbot\nDisallow: /\n", "https://x.example/feed"), [fetch.ALLOW])

    def test_a_file_that_cannot_be_judged_completely_is_unreachable_and_never_cut(self) -> None:
        cases = {"an invalid percent escape in a rule": b"User-agent: *\nDisallow: /a%zz\n",
                 "bytes that are not UTF-8": b"User-agent: *\nDisallow: /\xff\xfe\n",
                 "a rule longer than the limit": b"User-agent: *\nDisallow: /" + b"a" * 2001 + b"\n",
                 "too many rules": b"User-agent: *\n" + b"".join(b"Disallow: /p%d\n" % n for n in range(5001)),
                 "a file larger than the limit": b"# " + b"x" * fetch.MAX_ROBOTS_CHARS + b"\nUser-agent: *\nDisallow: /\n"}
        for name, body in cases.items():
            with self.subTest(name):
                self.assertEqual(self.verdicts(body, "https://x.example/feed"), [fetch.UNREACHABLE])

    def test_an_empty_file_and_a_file_with_no_group_for_us_allow_everything_and_an_html_page_is_not_a_robots_txt(self) -> None:
        for body in (b"", b"Sitemap: https://x.example/s.xml\n"):
            with self.subTest(body=body[:12]):
                self.assertEqual(self.verdicts(body, "https://x.example/feed"), [fetch.ALLOW])
        # An HTML page (a login page, the page a site answers in place of a missing robots.txt) says nothing about the rules:
        # the host is unreachable for the run (01's D1, 2026-11-16), where it used to read as "no rules"
        self.assertEqual(self.verdicts(b"<html><body>Not found</body></html>", "https://x.example/feed"), [fetch.UNREACHABLE])

    def test_a_feed_that_a_wildcard_rule_disallows_is_skipped_with_its_reason_and_not_requested(self) -> None:
        requested = []

        def get(url, *a, **k):
            requested.append(url)
            if url.endswith("/robots.txt"):
                return b"User-agent: *\nDisallow: /*.atom$\n"
            return rss(("A robot", "https://k.example/a", "robot"))
        config = config_with(feeds=[{"id": "gh", "url": "https://k.example/o/r/releases.atom", "evidence": "O", "topical": True},
                                    {"id": "ok", "url": "https://k.example/feed", "evidence": "M", "topical": True}])
        skipped: list[str] = []
        reasons: dict[str, str] = {}
        with mock.patch.object(fetch, "http_get", get), mock.patch.object(fetch.time, "sleep"), \
                contextlib.redirect_stdout(io.StringIO()):
            items, failed = fetch.fetch_feeds(config, robots=fetch.RobotsCache(), skipped=skipped, reasons=reasons)
        self.assertEqual((skipped, reasons, failed), (["gh"], {"gh": fetch.DISALLOW}, []))
        self.assertNotIn("https://k.example/o/r/releases.atom", requested)
        self.assertEqual([entry.source_id for entry in items], ["ok"])


class PoliteFetchingTest(unittest.TestCase):
    def test_robots_disallow_skips_the_feed_without_failing_it(self) -> None:
        robots = b"User-agent: *\nDisallow: /action/\n"
        requested = []

        def get(url, *a, **k):
            requested.append(url)
            return robots if url.endswith("/robots.txt") else rss(("A robot", "https://j.example/a", "robot"))
        config = config_with(feeds=[{"id": "journal", "url": "https://j.example/action/showFeed?x=1", "evidence": "R"},
                                    {"id": "ok", "url": "https://k.example/feed", "evidence": "M"}])
        skipped: list[str] = []
        with mock.patch.object(fetch, "http_get", get), mock.patch.object(fetch.time, "sleep"), \
                contextlib.redirect_stdout(io.StringIO()):
            items, failed = fetch.fetch_feeds(config, robots=fetch.RobotsCache(), skipped=skipped)
        self.assertEqual((skipped, failed), (["journal"], []))
        self.assertNotIn("https://j.example/action/showFeed?x=1", requested)
        self.assertEqual([entry.source_id for entry in items], ["ok"])

    def test_robots_outcomes_follow_rfc_9309_and_are_read_once_per_host(self) -> None:
        # 4xx: no rules (allow); 401/403: disallow; 5xx, 429 or no answer: unreachable.
        cases = {404: fetch.ALLOW, 410: fetch.ALLOW, 401: fetch.FORBIDDEN, 403: fetch.FORBIDDEN,
                 429: fetch.UNREACHABLE, 500: fetch.UNREACHABLE, 503: fetch.UNREACHABLE, None: fetch.UNREACHABLE}
        for status, expected in cases.items():
            calls = []
            cache = fetch.RobotsCache(getter=lambda url, s=status: calls.append(url) or (s, None))
            with mock.patch.object(fetch.time, "sleep"), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cache.verdict("https://a.example/feed"), expected, status)
                self.assertEqual(cache.verdict("https://a.example/other"), expected, status)
            self.assertEqual(calls, ["https://a.example/robots.txt"], status)
            self.assertEqual(cache.allows("https://a.example/feed"), expected == fetch.ALLOW)

    def test_the_default_getter_reports_the_http_status(self) -> None:
        import urllib.error

        def refuse(request, timeout=None):
            raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {}, None)

        def offline(request, timeout=None):
            raise urllib.error.URLError("unreachable")
        with mock.patch.object(fetch.time, "sleep"), contextlib.redirect_stdout(io.StringIO()):
            with mock.patch.object(fetch, "_urlopen", refuse):
                self.assertEqual(fetch._robots_get("https://a.example/robots.txt")[:2], (403, None))
            with mock.patch.object(fetch, "_urlopen", offline):
                self.assertEqual(fetch._robots_get("https://a.example/robots.txt")[:2], (None, None))

    def test_a_feed_on_an_unreachable_host_is_skipped_with_its_reason_and_not_requested(self) -> None:
        requested = []

        def get(url, *a, **k):
            requested.append(url)
            return None                                           # robots.txt does not answer
        config = config_with(feeds=[{"id": "down", "url": "https://down.example/feed", "evidence": "M"}])
        skipped: list[str] = []
        reasons: dict[str, str] = {}
        with mock.patch.object(fetch, "http_get", get), mock.patch.object(fetch.time, "sleep"), \
                contextlib.redirect_stdout(io.StringIO()):
            items, failed = fetch.fetch_feeds(config, robots=fetch.RobotsCache(), skipped=skipped, reasons=reasons)
        # 01's acceptance 4: refused, not requested, robots_skipped with a reason, never failed.
        self.assertEqual((items, failed, skipped, reasons), ([], [], ["down"], {"down": "unreachable"}))
        self.assertEqual(requested, ["https://down.example/robots.txt"])

    def test_every_refusal_reason_reaches_the_run_log_and_health_but_never_struggling(self) -> None:
        statuses = {"https://a.example": (403, None), "https://b.example": (503, None),
                    "https://c.example": (200, b"User-agent: *\nDisallow: /\n")}
        cache = fetch.RobotsCache(getter=lambda url: statuses[url.rsplit("/", 1)[0]])
        config = config_with(feeds=[{"id": i, "url": f"https://{h}.example/feed", "evidence": "M"}
                                    for i, h in (("fa", "a"), ("fb", "b"), ("fc", "c"))])
        skipped: list[str] = []
        reasons: dict[str, str] = {}
        with mock.patch.object(fetch, "http_get", lambda url, *a, **k: self.fail("requested " + url)), \
                mock.patch.object(fetch.time, "sleep"), contextlib.redirect_stdout(io.StringIO()):
            items, failed = fetch.fetch_feeds(config, robots=cache, skipped=skipped, reasons=reasons)
        self.assertEqual((items, failed, sorted(skipped)), ([], [], ["fa", "fb", "fc"]))
        self.assertEqual(reasons, {"fa": "forbidden", "fb": "unreachable", "fc": "disallowed"})
        runs = [{"date": f"2026-10-{day}", "failed": [], "robots_skipped": sorted(skipped),
                 "robots_reasons": reasons} for day in (22, 23, 24)]
        verdict = health.report(runs, DAY)
        self.assertEqual(verdict["robots_reasons"], {"fa:forbidden": 3, "fb:unreachable": 3, "fc:disallowed": 3})
        self.assertEqual((verdict["struggling"], verdict["failures"]), ([], {}))

    def test_our_agent_token_is_matched(self) -> None:
        cache = fetch.RobotsCache(getter=lambda url: (200, b"User-agent: physical-ai-radar\nDisallow: /\n"))
        with mock.patch.object(fetch.time, "sleep"):
            self.assertFalse(cache.allows("https://a.example/feed"))

    def test_two_requests_to_one_host_are_spaced(self) -> None:
        clock = [100.0]
        slept = []
        fetch._LAST_REQUEST.clear()
        with mock.patch.object(fetch.time, "monotonic", lambda: clock[0]), \
                mock.patch.object(fetch.time, "sleep", lambda s: slept.append(s)):
            fetch.polite("https://github.com/a/releases.atom")
            fetch.polite("https://other.example/feed")
            fetch.polite("https://github.com/b/releases.atom")
        self.assertEqual(slept, [fetch.HOST_SPACING])

    def test_fetch_all_reports_robots_skips_apart_from_failures(self) -> None:
        config = config_with(feeds=[{"id": "journal", "url": "https://j.example/action/feed", "evidence": "R"}],
                             arxiv={**CONFIG.sources["arxiv"], "enabled": False},
                             page_watch_settings={**CONFIG.sources["page_watch_settings"], "enabled": False})
        get = lambda url, *a, **k: b"User-agent: *\nDisallow: /action/\n" if url.endswith("robots.txt") else None
        with mock.patch.object(fetch, "http_get", get), mock.patch.object(fetch.time, "sleep"), \
                contextlib.redirect_stdout(io.StringIO()):
            report = fetch.fetch_all(config, reference=DAY)
        summary = report.to_dict()
        self.assertEqual((summary["robots_skipped"], summary["failed"], summary["attempted"]), (["journal"], [], 0))
        # The attention APIs did not answer: recorded, never a failing source.
        self.assertIn("hf_daily_papers", summary["signals"]["failed"])


class HeatTest(unittest.TestCase):
    def test_two_independent_sources_telling_one_story_are_trending_and_keep_their_evidence(self) -> None:
        a = item("Tesla starts Optimus humanoid production in Fremont", source="techcrunch-robotics")
        b = item("Tesla begins Optimus humanoid production at Fremont factory", source="the-robot-report")
        c = item("An unrelated gripper paper", source="arxiv", evidence="R")
        before = {entry.id: (entry.evidence, entry.score) for entry in (a, b, c)}
        ranked = heat.apply([c, a, b], CONFIG)
        for entry in (a, b):
            self.assertIn("trending", entry.signals)
            self.assertGreater(entry.heat, 0)
            self.assertEqual(entry.evidence, before[entry.id][0])
            self.assertLessEqual(entry.score - before[entry.id][1], heat.settings(CONFIG)["weight"] + 1e-9)
        self.assertEqual((c.heat, c.signals), (0.0, []))
        self.assertEqual(ranked[-1], c)

    def test_one_source_twice_is_not_cross_source(self) -> None:
        a = item("Tesla starts Optimus humanoid production in Fremont", source="techcrunch-robotics")
        b = item("Tesla begins Optimus humanoid production at Fremont factory", source="techcrunch-robotics")
        heat.apply([a, b], CONFIG)
        self.assertEqual((a.heat, a.signals), (0.0, []))

    def test_papers_are_joined_by_link_never_by_a_similar_title(self) -> None:
        a = item("Diffusion policy for dexterous humanoid manipulation", source="arxiv", evidence="R")
        b = item("Diffusion policy for dexterous humanoid manipulation tasks", source="bair", evidence="R")
        heat.apply([a, b], CONFIG)
        self.assertEqual(a.heat, 0.0)
        c = item("Same paper", source="hf", evidence="R", url="https://arxiv.org/abs/2610.30043v2")
        d = item("Same paper", source="arxiv", evidence="R", url="http://arxiv.org/abs/2610.30043")
        heat.apply([c, d], CONFIG)
        self.assertGreater(c.heat, 0.0)

    def test_every_component_is_capped(self) -> None:
        paper = item("A VLA paper", source="arxiv", evidence="R", url="https://arxiv.org/abs/2610.00001")
        post = item("Humanoid robot walks a marathon", source="ieee", url="https://ieee.example/marathon")
        signals = heat.Signals(upvotes={"2610.00001": 10**9},
                               stories=[{"key": "https://ieee.example/marathon", "terms": frozenset(), "points": 10**9}])
        heat.apply([paper, post], CONFIG, signals)
        cfg = heat.settings(CONFIG)
        total = sum(cfg["components"].values())
        self.assertAlmostEqual(paper.heat, cfg["components"]["hf"] / total, places=3)
        self.assertAlmostEqual(post.heat, cfg["components"]["hn"] / total, places=3)
        self.assertLessEqual(max(paper.heat, post.heat), 1.0)

    def test_hn_matches_by_link_or_same_story_title_and_hf_by_arxiv_id(self) -> None:
        by_title = item("Figure humanoid robot folds laundry in a home", source="the-robot-report")
        signals = heat.Signals(stories=[{"key": "https://news.example/x", "points": 120,
                                         "terms": heat.story_terms("Figure humanoid robot folds laundry at home")}],
                               upvotes={"2610.30043": 50})
        paper = item("TARSphere", source="arxiv", evidence="R", url="https://arxiv.org/abs/2610.30043v1")
        heat.apply([by_title, paper], CONFIG, signals)
        self.assertGreater(by_title.heat, 0)
        self.assertGreater(paper.heat, 0)

    def test_star_velocity_uses_the_previous_count_and_survives_a_second_run(self) -> None:
        state = heat.next_state({"schema": heat.STATE_SCHEMA, "stars": {}}, {"huggingface/lerobot": 28000}, "2026-10-23")
        self.assertEqual(heat.velocity(state["stars"]["huggingface/lerobot"]), 0.0)   # first sight: no rate
        state = heat.next_state(state, {"huggingface/lerobot": 28400}, "2026-10-25")
        self.assertEqual(heat.velocity(state["stars"]["huggingface/lerobot"]), 200.0)
        again = heat.next_state(state, {"huggingface/lerobot": 28500}, "2026-10-25")
        self.assertEqual(heat.velocity(again["stars"]["huggingface/lerobot"]), 250.0)  # still against 10-23
        kept = heat.next_state(again, {}, "2026-10-26")                                 # API down: kept
        self.assertEqual(kept["stars"], again["stars"])
        release = item("LeRobot v0.7.0", source="github-lerobot", evidence="O")
        heat.apply([release], CONFIG, heat.Signals(), again)
        self.assertGreater(release.heat, 0)

    def test_signals_fetch_never_raises_and_records_what_did_not_answer(self) -> None:
        requested = []

        def get(url):
            requested.append(url)
            if "huggingface" in url:
                return json.dumps([{"paper": {"id": "2610.1", "upvotes": 7}}]).encode()
            if "algolia" in url:
                return b"not json"
            return None
        config = config_with(heat={**CONFIG.sources["heat"], "hn_queries": ["robot"] * 9})
        with mock.patch.object(fetch.time, "sleep"):
            signals = heat.fetch_signals(config, DAY, getter=get)
        self.assertEqual(signals.upvotes, {"2610.1": 7})
        self.assertIn("hn:robot", signals.failed)
        self.assertTrue(all(name.startswith(("hn:", "github:")) for name in signals.failed))
        hn = [urllib.parse.parse_qs(urllib.parse.urlsplit(url).query) for url in requested if "algolia" in url]
        self.assertEqual(len(hn), heat.MAX_QUERIES)                        # bounded
        self.assertEqual(hn[0]["typoTolerance"], ["false"])
        self.assertTrue(hn[0]["numericFilters"][0].startswith("created_at_i>"))

    def test_signals_respect_robots(self) -> None:
        cache = fetch.RobotsCache(getter=lambda url: (200, b"User-agent: *\nDisallow: /api/\n"))
        with mock.patch.object(fetch.time, "sleep"):
            signals = heat.fetch_signals(config_with(heat={**CONFIG.sources["heat"], "hn_queries": [],
                                                            "github_repos": []}),
                                         DAY, robots=cache, getter=lambda url: self.fail("requested " + url))
        self.assertEqual((signals.robots_skipped, signals.failed), (["hf_daily_papers"], []))
        down = fetch.RobotsCache(getter=lambda url: (503, None))
        with mock.patch.object(fetch.time, "sleep"), contextlib.redirect_stdout(io.StringIO()):
            signals = heat.fetch_signals(config_with(heat={**CONFIG.sources["heat"], "hn_queries": [],
                                                            "github_repos": []}),
                                         DAY, robots=down, getter=lambda url: self.fail("requested " + url))
        self.assertEqual((signals.robots_skipped, signals.failed), (["hf_daily_papers"], []))
        self.assertEqual(signals.robots_reasons, {"hf_daily_papers": "unreachable"})


class PressReleaseTest(unittest.TestCase):
    def test_announcements_are_labelled_and_ranked_lower_with_their_evidence(self) -> None:
        wire = Item(id="w", title="Acme Robotics announces a humanoid robot for warehouses", url="https://w.example/1",
                    publisher="w", source_id="the-robot-report", evidence="M", published=DAY.isoformat(),
                    summary="BOSTON (BUSINESS WIRE) -- the humanoid robot ships in 2027.")
        report = Item(id="r", title="A humanoid robot for warehouses, tested", url="https://w.example/2",
                      publisher="w", source_id="the-robot-report", evidence="M", published=DAY.isoformat(),
                      summary="We spent a week with the humanoid robot in a warehouse.")
        enrich([wire, report], CONFIG, DAY)
        self.assertIn("press_release", wire.signals)
        self.assertNotIn("press_release", report.signals)
        for title in ("Acme announces X", "Acme announced X", "Acme is announcing X", "Acme 宣布推出人形机器人"):
            entry = item(title, source="the-robot-report")
            self.assertTrue(press_release(entry, CONFIG), title)
        self.assertEqual(wire.evidence, "M")

    def test_a_press_release_is_ranked_lower_by_the_configured_penalty(self) -> None:
        def scored(config):
            entry = Item(id="w", title="Acme announces a humanoid robot", url="https://w.example/1", publisher="w",
                         source_id="the-robot-report", evidence="M", published=DAY.isoformat(), summary="robot")
            return enrich([entry], config, DAY)[0].score
        rules = CONFIG.sources["filters"]["press_release"]
        free = config_with(filters={**CONFIG.sources["filters"], "press_release": {**rules, "penalty": 0}})
        self.assertAlmostEqual(scored(free) - scored(CONFIG), rules["penalty"])

    def test_papers_are_never_press_releases(self) -> None:
        paper = item("We announce a new benchmark for robot manipulation", source="arxiv", evidence="R")
        self.assertFalse(press_release(paper, CONFIG))


class QuotaTest(unittest.TestCase):
    def pool(self):
        papers = [item(f"Paper {n} robot policy", source="arxiv", evidence="R", score=9 - n * 0.1,
                       lane=f"lane{n}") for n in range(8)]
        media = item("Robot Report story", source="the-robot-report", score=2.0, lane="m1")
        official = item("NVIDIA Isaac release", source="nvidia-blog", evidence="O", score=1.5, lane="o1")
        hot = item("Tesla Optimus production", source="techcrunch-robotics", score=1.2, lane="h1",
                   signals=["trending"])
        return papers, media, official, hot

    def test_without_quota_parameters_selection_is_unchanged(self) -> None:
        papers, media, official, hot = self.pool()
        self.assertEqual(select(papers + [media, official, hot], limit=8), papers)

    def test_arxiv_takes_at_most_half_and_official_and_hot_are_reserved(self) -> None:
        papers, media, official, hot = self.pool()
        report: dict = {}
        picked = select(papers + [media, official, hot], limit=8, max_arxiv=4,
                        reserve=["official", "hot"], report=report)
        self.assertEqual(sum(entry.source_id == "arxiv" for entry in picked), 4)
        self.assertIn(official, picked)
        self.assertIn(hot, picked)
        self.assertEqual((report["unmet"], report["arxiv"], report["max_arxiv"]), ([], 4, 4))
        self.assertEqual(report["reserved"], {"official": official.id, "hot": hot.id})

    def test_the_arxiv_cap_holds_in_the_fill_pass(self) -> None:
        papers, *_ = self.pool()
        report: dict = {}
        picked = select(papers, limit=8, max_arxiv=4, reserve=["official", "hot"], report=report)
        self.assertEqual(len(picked), 4)
        self.assertEqual(report["unmet"], ["hot", "official"])

    def test_a_reservation_obeys_the_gates(self) -> None:
        papers, media, official, hot = self.pool()
        official.lane_hits = 0                                   # does not qualify
        picked = select(papers + [official], limit=8, max_arxiv=4, reserve=["official"], report={})
        self.assertNotIn(official, picked)

    def test_a_reservation_respects_the_lane_cap_and_the_repeat_guard(self) -> None:
        papers, media, official, hot = self.pool()
        hot.lane = official.lane                                  # the official slot fills that lane
        report: dict = {}
        picked = select(papers + [official, hot], limit=8, per_lane=1, max_arxiv=4,
                        reserve=["official", "hot"], report=report)
        self.assertNotIn(hot, picked)
        self.assertEqual(report["unmet"], ["hot"])
        hot.lane = "h1"
        report = {}
        picked = select(papers + [hot], limit=8, max_arxiv=4, reserve=["hot"], report=report,
                        told=["Tesla Optimus production line"])    # an earlier day told this story
        self.assertNotIn(hot, picked)
        self.assertEqual(report["unmet"], ["hot"])

    def test_an_unknown_reservation_is_ignored_and_reported(self) -> None:
        papers, media, official, hot = self.pool()
        report: dict = {}
        picked = select(papers + [official], limit=8, max_arxiv=4, reserve=["oficial", "official"], report=report)
        self.assertIn(official, picked)
        self.assertEqual(report["unmet"], ["unknown:oficial"])

    def test_the_report_counts_the_pool_by_lane_not_just_the_picks(self) -> None:
        papers, media, official, hot = self.pool()
        crowded = [item(f"Crowded lane story {n} robot", source="blog", score=5 - n * 0.1, lane="crowd",
                        url=f"https://crowd.example/{n}") for n in range(3)]
        weak = item("Does not qualify", lane="weak")
        weak.lane_hits = 0                                       # fails the lane-keyword gate
        pool = papers + [media, official, hot] + crowded + [weak]
        report: dict = {}
        picked = select(pool, limit=8, report=report)
        counts = report["pool"]
        self.assertEqual(counts["per_lane"], 2)
        self.assertEqual(counts["eligible"], len(pool) - 1)      # the one that does not qualify is not counted
        self.assertEqual(counts["by_lane"]["crowd"], 3)           # three qualified, at most two can be picked
        self.assertLessEqual(sum(entry.lane == "crowd" for entry in picked), 2)
        self.assertNotIn("weak", counts["by_lane"])
        self.assertEqual(sum(counts["by_lane"].values()), counts["eligible"])
        self.assertEqual(list(counts["by_lane"]), sorted(counts["by_lane"]))
        # an item already published is not part of the pool, and the cap in force is reported
        report = {}
        select(pool, limit=8, per_lane=1, seen=[papers[0].url, crowded[0].url], report=report)
        self.assertEqual(report["pool"]["per_lane"], 1)
        self.assertEqual(report["pool"]["eligible"], len(pool) - 3)
        self.assertNotIn("lane0", report["pool"]["by_lane"])
        self.assertEqual(report["pool"]["by_lane"]["crowd"], 2)
        # the pool is the same whatever the limit cuts
        again: dict = {}
        select(pool, limit=2, per_lane=1, seen=[papers[0].url, crowded[0].url], report=again)
        self.assertEqual(again["pool"]["by_lane"], report["pool"]["by_lane"])

    def test_a_pool_report_does_not_change_the_selection(self) -> None:
        papers, media, official, hot = self.pool()
        pool = papers + [media, official, hot]
        with_report = select(pool, limit=8, max_arxiv=4, reserve=["official", "hot"], report={})
        without = select(pool, limit=8, max_arxiv=4, reserve=["official", "hot"])
        self.assertEqual(with_report, without)

    def test_the_configured_quota_is_half_of_the_daily_limit(self) -> None:
        highlights = CONFIG.sources["filters"]["highlights"]
        self.assertEqual(int(8 * highlights["max_arxiv_share"]), 4)
        self.assertEqual(highlights["reserve"], ["official", "hot", "event", "event"])


class RunLogTest(unittest.TestCase):
    def test_the_run_log_records_quota_robots_and_signals_and_health_counts_them(self) -> None:
        report = fetch.FetchReport(robots_skipped=["journal"], robots_reasons={"journal": "forbidden"},
                                   signals=heat.Signals(failed=["hf_daily_papers"], attempted=["hf_daily_papers", "hn:robot"],
                                                        robots_skipped=["hn:robot"],
                                                        robots_reasons={"hn:robot": "unreachable"}))
        with mock.patch.object(cli, "fetch_all", return_value=report), \
                mock.patch.object(cli, "load_history", return_value=[]), \
                mock.patch.object(heat, "load_state", return_value={"schema": heat.STATE_SCHEMA, "stars": {}}):
            ctx = cli.build_context(CONFIG, offline=False, limit=8, day="2026-10-24")
        entry = ctx["history"][-1]
        self.assertEqual((entry["robots_skipped"], entry["signal_failed"]), (["journal"], ["hf_daily_papers"]))
        self.assertEqual(entry["robots_reasons"], {"journal": "forbidden", "signal:hn:robot": "unreachable"})
        self.assertEqual(entry["quota"]["max_arxiv"], 4)
        self.assertEqual(entry["quota"]["unmet"], ["event", "event#2", "hot", "official"])
        lanes = [lane["id"] for lane in CONFIG.lanes]
        self.assertEqual(entry["pool"], {"live": 0, "eligible": 0, "per_lane_cap": 2,
                                         "by_lane": {lane: 0 for lane in lanes},
                                         "picked_by_lane": {lane: 0 for lane in lanes}})
        self.assertNotIn("pool", entry["quota"])                  # the quota keeps only what it had
        self.assertIsNone(ctx["heat_state"])                     # nothing read: the file is left alone
        verdict = health.report([dict(entry, failed=[])] * 3, DAY)
        self.assertEqual(verdict["signals_unavailable"], {"hf_daily_papers": 3})
        self.assertEqual(verdict["robots_skipped"], {"journal": 3})
        self.assertEqual(verdict["struggling"], [])

    def test_the_run_log_keeps_the_pool_and_the_picks_by_lane_with_every_lane_present(self) -> None:
        def story(title: str, lane: str, n: int) -> Item:
            entry = Item(id=f"nvidia-blog:{n}", title=title, url=f"https://nvidia.example/post/{n}",
                         publisher="NVIDIA", source_id="nvidia-blog", evidence="O", published=DAY.isoformat(),
                         summary="A robot release.")
            entry.lane, entry.lane_locked = lane, True
            return entry
        items = [story("Alpha manipulation dataset release", "foundation", 1),
                 story("Beta quadruped locomotion controller", "foundation", 2),
                 story("Gamma humanoid whole body planner", "foundation", 3),
                 story("Delta collision safety monitor", "safety", 4)]
        general = story("Epsilon cloud gaming update", "hardware", 5)
        general.lane_locked = False                              # no lane keyword, not about Physical AI: filtered
        items.append(general)
        report = fetch.FetchReport(items=items)
        with mock.patch.object(cli, "fetch_all", return_value=report), \
                mock.patch.object(cli, "load_history", return_value=[]), \
                mock.patch.object(heat, "load_state", return_value={"schema": heat.STATE_SCHEMA, "stars": {}}):
            ctx = cli.build_context(CONFIG, offline=False, limit=8, day="2026-10-24")
        pool = ctx["history"][-1]["pool"]
        lanes = [lane["id"] for lane in CONFIG.lanes]
        self.assertEqual(list(pool["by_lane"]), lanes)
        self.assertEqual(list(pool["picked_by_lane"]), lanes)
        self.assertEqual((pool["live"], pool["per_lane_cap"]), (5, 2))
        self.assertEqual(pool["eligible"], 4)                      # live is what was fetched; eligible is what qualified
        self.assertEqual(pool["eligible"], sum(pool["by_lane"].values()))
        self.assertEqual(sum(pool["picked_by_lane"].values()), len(ctx["picked"]))
        for lane in lanes:
            self.assertLessEqual(pool["picked_by_lane"][lane], pool["by_lane"][lane])
            self.assertLessEqual(pool["picked_by_lane"][lane], pool["per_lane_cap"])
        self.assertEqual(pool["by_lane"]["foundation"], 3)         # three qualified; the lane gives at most two
        self.assertEqual(pool["picked_by_lane"]["foundation"], 2)
        self.assertEqual(pool["by_lane"]["safety"], 1)
        self.assertEqual(ctx["history"][-1]["count"], len(ctx["picked"]))

    def test_the_run_log_reports_the_lane_cap_that_select_used(self) -> None:
        entry = Item(id="nvidia-blog:1", title="Alpha manipulation dataset release", url="https://nvidia.example/post/1",
                     publisher="NVIDIA", source_id="nvidia-blog", evidence="O", published=DAY.isoformat(),
                     summary="A robot release.")
        entry.lane, entry.lane_locked = "foundation", True
        other = Item(id="nvidia-blog:2", title="Beta quadruped locomotion controller", url="https://nvidia.example/post/2",
                     publisher="NVIDIA", source_id="nvidia-blog", evidence="O", published=DAY.isoformat(),
                     summary="A robot release.")
        other.lane, other.lane_locked = "foundation", True
        report = fetch.FetchReport(items=[entry, other])
        with mock.patch.object(cli, "fetch_all", return_value=report), \
                mock.patch.object(cli, "load_history", return_value=[]), \
                mock.patch.object(cli, "select", side_effect=lambda *a, **k: select(*a, per_lane=1, **k)), \
                mock.patch.object(heat, "load_state", return_value={"schema": heat.STATE_SCHEMA, "stars": {}}):
            ctx = cli.build_context(CONFIG, offline=False, limit=8, day="2026-10-24")
        pool = ctx["history"][-1]["pool"]
        self.assertEqual((pool["per_lane_cap"], pool["by_lane"]["foundation"], pool["picked_by_lane"]["foundation"]),
                         (1, 2, 1))

    def test_heat_state_is_written_only_when_a_count_changed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"radar"/"heat-state.json"
            state = heat.next_state({"schema": heat.STATE_SCHEMA, "stars": {}}, {"a/b": 3}, "2026-10-24")
            heat.save_state(path, state)
            self.assertEqual(heat.load_state(path), state)
            path.write_text("{broken")
            self.assertEqual(heat.load_state(path), {"schema": heat.STATE_SCHEMA, "stars": {}})


if __name__ == "__main__":
    unittest.main()
