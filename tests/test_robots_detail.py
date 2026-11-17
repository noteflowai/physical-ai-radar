"""Why a host's robots.txt is `unreachable`: `robots_detail`, beside `robots_reasons`.

On 2026-11-17 MONOist was refused as `unreachable` and the only way to tell a limit from a robots.txt that our own checks
did not take for one was to read the run log. `robots_reasons` keeps its words (`disallowed`, `forbidden`, `unreachable`);
`robots_detail` adds, for an unreachable host only, one of `fetch.ROBOTS_DETAILS`. Pages read by Firecrawl or the static
reader have none. No test here touches the network.
"""
from __future__ import annotations

import contextlib
import io
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from pairadar import cli, fetch, health, heat  # noqa: E402
from test_sources_mainstream import CONFIG, config_with, rss  # noqa: E402

ORIGIN = "https://h.example"
GOOD = b"User-agent: *\nDisallow: /private/\n"
HTML = b"<!DOCTYPE html><html><body>not found, but 200</body></html>"


REAL_ROBOTS_CACHE = fetch.RobotsCache      # some tests below replace `fetch.RobotsCache` for the length of a call


def silent():
    return contextlib.redirect_stdout(io.StringIO())


def cache_for(answer):
    return REAL_ROBOTS_CACHE(getter=lambda url: answer)


def verdict_and_detail(answer, url=ORIGIN + "/feed"):
    cache = cache_for(answer)
    with mock.patch.object(fetch.time, "sleep"), silent():
        return cache.verdict(url), cache.detail(url)


class EveryPathToUnreachableHasADetailTest(unittest.TestCase):
    def test_a_status_that_is_not_a_file(self) -> None:
        for answer, detail in (((None, None), "no-answer"), ((429, b""), "http-429"), ((500, b""), "http-5xx"),
                               ((503, b"busy"), "http-5xx"), ((599, b""), "http-5xx"),
                               ((302, b""), "other"), ((200, None), "other"), ((100, b""), "other")):
            with self.subTest(answer=answer):
                self.assertEqual(verdict_and_detail(answer), (fetch.UNREACHABLE, detail))

    def test_a_2xx_answer_that_is_not_the_hosts_robots_txt(self) -> None:
        # an HTML page; the file of another address; a content type that is not text/plain
        self.assertEqual(verdict_and_detail((200, HTML)), (fetch.UNREACHABLE, "not-robots-txt"))
        elsewhere = (200, GOOD, {"url": "https://login.example/robots.txt", "content_type": None})
        self.assertEqual(verdict_and_detail(elsewhere), (fetch.UNREACHABLE, "not-robots-txt"))
        moved_path = (200, GOOD, {"url": ORIGIN + "/en/robots.txt", "content_type": None})
        self.assertEqual(verdict_and_detail(moved_path), (fetch.UNREACHABLE, "not-robots-txt"))
        wrong_type = (200, GOOD, {"url": ORIGIN + "/robots.txt", "content_type": "text/html"})
        self.assertEqual(verdict_and_detail(wrong_type), (fetch.UNREACHABLE, "not-robots-txt"))

    def test_a_2xx_robots_txt_that_cannot_be_judged_completely(self) -> None:
        too_long = b"User-agent: *\n" + b"#" * (fetch.MAX_ROBOTS_CHARS + 1)
        for body in (b"User-agent: *\nDisallow: /\xff\xfe\n", b"User-agent: *\nDisallow: /a%zz\n", too_long):
            with self.subTest(body=body[:40]):
                self.assertEqual(verdict_and_detail((200, body)), (fetch.UNREACHABLE, "unjudgeable"))

    def test_the_detail_is_always_one_of_the_fixed_words(self) -> None:
        for answer in ((None, None), (429, b""), (500, b""), (200, HTML), (200, b"\xff"), (302, b"")):
            self.assertIn(verdict_and_detail(answer)[1], fetch.ROBOTS_DETAILS)

    def test_the_verdict_is_what_it_was(self) -> None:
        # the detail decides nothing: every one of these was `unreachable` before and still is
        for answer in ((None, None), (429, b""), (503, b""), (200, HTML), (200, b"\xff"), (302, b"")):
            self.assertEqual(verdict_and_detail(answer)[0], fetch.UNREACHABLE)


class OnlyAnUnreachableHostHasOneTest(unittest.TestCase):
    def test_every_other_verdict_has_none(self) -> None:
        for answer, verdict in (((200, GOOD), fetch.ALLOW), ((404, b"gone"), fetch.ALLOW), ((403, b"no"), fetch.FORBIDDEN),
                                ((401, b"no"), fetch.FORBIDDEN)):
            with self.subTest(answer=answer):
                self.assertEqual(verdict_and_detail(answer), (verdict, None))
        self.assertEqual(verdict_and_detail((200, GOOD), ORIGIN + "/private/x"), (fetch.DISALLOW, None))

    def test_a_host_that_was_never_asked_has_none(self) -> None:
        self.assertIsNone(fetch.RobotsCache(getter=lambda url: (200, GOOD)).detail(ORIGIN + "/feed"))

    def test_each_host_keeps_its_own(self) -> None:
        answers = {"https://a.example": (429, b""), "https://b.example": (200, HTML), "https://c.example": (200, GOOD)}
        cache = fetch.RobotsCache(getter=lambda url: answers[url.split("/robots.txt")[0]])
        with mock.patch.object(fetch.time, "sleep"), silent():
            for origin in answers:
                cache.verdict(origin + "/feed")
        self.assertEqual([cache.detail(origin + "/feed") for origin in answers], ["http-429", "not-robots-txt", None])


class TheFeedsAndTheReportTest(unittest.TestCase):
    def test_a_feed_with_an_unreachable_host_gets_its_detail_and_the_reason_keeps_its_word(self) -> None:
        answers = {"https://a.example": (200, HTML), "https://b.example": (403, b"no"), "https://c.example": (200, GOOD),
                   "https://d.example": (None, None)}
        config = config_with(feeds=[{"id": name, "url": f"https://{name}.example/feed", "evidence": "O"} for name in "abcd"])
        cache = fetch.RobotsCache(getter=lambda url: answers[url.split("/robots.txt")[0]])
        skipped, reasons, details = [], {}, {}
        with mock.patch.object(fetch, "http_get", return_value=rss(("A robot", "https://c.example/a", "robot"))), \
                mock.patch.object(fetch.time, "sleep"), silent():
            fetch.fetch_feeds(config, robots=cache, skipped=skipped, reasons=reasons, details=details)
        self.assertEqual(sorted(skipped), ["a", "b", "d"])
        self.assertEqual(reasons, {"a": "unreachable", "b": "forbidden", "d": "unreachable"})
        self.assertEqual(details, {"a": "not-robots-txt", "d": "no-answer"})

    def test_a_caller_that_passes_no_details_still_works(self) -> None:
        config = config_with(feeds=[{"id": "a", "url": "https://a.example/feed", "evidence": "O"}])
        skipped = []
        with mock.patch.object(fetch.time, "sleep"), silent():
            fetch.fetch_feeds(config, robots=cache_for((200, HTML)), skipped=skipped)
        self.assertEqual(skipped, ["a"])

    def test_the_report_dictionary_carries_it_sorted_and_empty_by_default(self) -> None:
        report = fetch.FetchReport(robots_detail={"b": "http-429", "a": "not-robots-txt"})
        self.assertEqual(list(report.to_dict()["robots_detail"].items()), [("a", "not-robots-txt"), ("b", "http-429")])
        self.assertEqual(fetch.FetchReport().to_dict()["robots_detail"], {})

    def test_fetch_all_hands_the_details_to_the_report_and_the_log_line_says_them(self) -> None:
        config = config_with(feeds=[{"id": "a", "url": "https://a.example/feed", "evidence": "O"}])
        config.sources["arxiv"] = {"enabled": False}
        out = io.StringIO()
        with mock.patch.object(fetch, "RobotsCache", lambda store=None: cache_for((200, HTML))), \
                mock.patch.object(fetch, "fetch_page_watch", lambda *args, **kwargs: fetch.PageWatchResult()), \
                mock.patch.object(heat, "fetch_signals", lambda *args, **kwargs: heat.Signals()), \
                mock.patch.object(fetch.time, "sleep"), contextlib.redirect_stdout(out):
            report = fetch.fetch_all(config, offline=False, reference=date(2026, 11, 17))
        self.assertEqual((report.robots_reasons, report.robots_detail), ({"a": "unreachable"}, {"a": "not-robots-txt"}))
        self.assertIn("robots.txt refuses: a (unreachable: not-robots-txt)", out.getvalue())

    def test_a_refusal_without_a_detail_reads_as_before(self) -> None:
        config = config_with(feeds=[{"id": "a", "url": "https://a.example/feed", "evidence": "O"}])
        config.sources["arxiv"] = {"enabled": False}
        out = io.StringIO()
        with mock.patch.object(fetch, "RobotsCache", lambda store=None: cache_for((403, b"no"))), \
                mock.patch.object(fetch, "fetch_page_watch", lambda *args, **kwargs: fetch.PageWatchResult()), \
                mock.patch.object(heat, "fetch_signals", lambda *args, **kwargs: heat.Signals()), \
                mock.patch.object(fetch.time, "sleep"), contextlib.redirect_stdout(out):
            report = fetch.fetch_all(config, offline=False, reference=date(2026, 11, 17))
        self.assertEqual(report.robots_detail, {})
        self.assertIn("robots.txt refuses: a (forbidden)", out.getvalue())


class TheSignalsTheRunLogAndHealthTest(unittest.TestCase):
    def test_a_signal_on_an_unreachable_host_gets_its_detail(self) -> None:
        for answer, detail in (((429, b""), "http-429"), ((200, HTML), "not-robots-txt")):
            with self.subTest(detail=detail):
                signals = heat.Signals()
                with mock.patch.object(fetch.time, "sleep"), silent():
                    heat._json("https://api.example/v1/x", "x", signals, cache_for(answer), lambda url: b"[]")
                self.assertEqual((signals.robots_reasons, signals.robots_detail), ({"x": "unreachable"}, {"x": detail}))
                self.assertEqual(signals.summary()["robots_detail"], {"x": detail})

    def test_a_signal_the_rules_refuse_has_none(self) -> None:
        signals = heat.Signals()
        with mock.patch.object(fetch.time, "sleep"), silent():
            heat._json("https://api.example/private/x", "x", signals, cache_for((200, GOOD.replace(b"/private/", b"/"))),
                       lambda url: b"[]")
        self.assertEqual((signals.robots_reasons, signals.robots_detail), ({"x": "disallowed"}, {}))

    def test_the_run_log_prefixes_signals_and_health_counts_the_details(self) -> None:
        report = fetch.FetchReport(robots_skipped=["monoist"], robots_reasons={"monoist": "unreachable"},
                                   robots_detail={"monoist": "not-robots-txt"},
                                   signals=heat.Signals(attempted=["hn:robot"], robots_skipped=["hn:robot"],
                                                        robots_reasons={"hn:robot": "unreachable"},
                                                        robots_detail={"hn:robot": "http-429"}))
        with mock.patch.object(cli, "fetch_all", return_value=report), \
                mock.patch.object(cli, "load_history", return_value=[]), \
                mock.patch.object(heat, "load_state", return_value={"schema": heat.STATE_SCHEMA, "stars": {}}):
            ctx = cli.build_context(CONFIG, offline=False, limit=8, day="2026-11-17")
        entry = ctx["history"][-1]
        self.assertEqual(entry["robots_reasons"], {"monoist": "unreachable", "signal:hn:robot": "unreachable"})
        self.assertEqual(entry["robots_detail"], {"monoist": "not-robots-txt", "signal:hn:robot": "http-429"})
        verdict = health.report([dict(entry, failed=[])] * 3, date(2026, 11, 17))
        self.assertEqual(verdict["robots_detail"], {"monoist:not-robots-txt": 3, "signal:hn:robot:http-429": 3})
        self.assertEqual(verdict["robots_reasons"], {"monoist:unreachable": 3, "signal:hn:robot:unreachable": 3})

    def test_health_reads_a_run_log_from_before_the_field(self) -> None:
        old = {"date": "2026-11-16", "failed": [], "robots_reasons": {"monoist": "unreachable"}}
        verdict = health.report([old], date(2026, 11, 17))
        self.assertEqual((verdict["robots_detail"], verdict["robots_reasons"]), ({}, {"monoist:unreachable": 1}))
        self.assertEqual(health._reasons([{"date": "2026-11-16", "robots_detail": "not a map"}], "robots_detail"), {})


if __name__ == "__main__":
    unittest.main()
