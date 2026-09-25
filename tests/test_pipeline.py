"""Tests for the Physical AI Radar pipeline (standard library unittest only)."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
import urllib.error
from datetime import date
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pairadar import fetch, charts, cli, health, lanes  # noqa: E402
from pairadar.config import LANGS, Config, Item, load_config, load_notes  # noqa: E402
from pairadar.distill import (  # noqa: E402
    classify,
    url_key,
    deduplicate,
    detect_signals,
    enrich,
    evidence_mix,
    extract_numbers,
    lane_distribution,
    one_liner,
    prefilter,
    recency_bonus,
    select,
)
from pairadar.cli import degraded, recent_urls, save_history  # noqa: E402
from pairadar import fetch as fetch_module  # noqa: E402
from pairadar.fetch import (  # noqa: E402
    ARXIV_ENDPOINT,
    FetchReport,
    baseline_items,
    clean_text,
    fetch_all,
    fetch_arxiv,
    http_get,
    link_digest,
    parse_date,
)
from pairadar import render  # noqa: E402
from pairadar.render import composed_why, readme_block, render_daily  # noqa: E402
from pairadar.notes import unsupported_numbers  # noqa: E402


def make_item(**kwargs) -> Item:
    defaults = dict(
        id="t1",
        title="A VLA policy with closed-loop success on a real robot",
        url="https://example.org/a",
        publisher="Example",
        source_id="arxiv",
        evidence="R",
        published=date.today().isoformat(),
        summary="We report 87.5% success rate at 23 Hz on real robot hardware.",
    )
    defaults.update(kwargs)
    return Item(**defaults)


class DataFilesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()

    def test_config_loads(self) -> None:
        self.assertIsInstance(self.config, Config)
        self.assertGreaterEqual(len(self.config.lanes), 6)

    def test_every_lane_has_three_languages(self) -> None:
        for lane in self.config.lanes:
            for lang in LANGS:
                self.assertTrue(lane["name"].get(lang), f"{lane['id']} missing {lang}")
                self.assertTrue(
                    self.config.glossary["why_parts"]["watch"][lane["id"]].get(lang),
                    f"why_parts.watch {lane['id']} missing {lang}",
                )

    def test_ui_strings_cover_all_languages(self) -> None:
        keys = set(self.config.ui("en"))
        for lang in LANGS:
            self.assertEqual(set(self.config.ui(lang)), keys, f"UI key drift in {lang}")

    def test_baseline_entries_are_well_formed(self) -> None:
        lane_ids = {lane["id"] for lane in self.config.lanes}
        seen: set[str] = set()
        for raw in self.config.baseline["items"]:
            self.assertNotIn(raw["id"], seen, "duplicate baseline id")
            seen.add(raw["id"])
            self.assertIn(raw["lane"], lane_ids)
            self.assertIn(raw["evidence"], {"O", "R", "M"})
            self.assertTrue(raw["url"].startswith("http"))
            for lang in LANGS:
                self.assertTrue(raw["why"].get(lang), f"{raw['id']} missing why.{lang}")

    def test_the_curated_baseline_never_cites_our_own_work(self) -> None:
        # The READMEs disclose this, so it has to be enforced rather than promised:
        # a radar that grades evidence cannot quietly rank its authors' own project.
        ours = [raw["id"] for raw in self.config.baseline["items"]
                if any(host in raw["url"] for host in ("noteflowai", "robot-reel", "glayguo"))]
        self.assertEqual(ours, [], "a self-citation would make the README disclosure false")

    def test_sources_declare_weight_and_evidence(self) -> None:
        for feed in self.config.sources["feeds"]:
            self.assertIn(feed["evidence"], {"O", "R", "M"})
            self.assertGreater(float(feed["weight"]), 0)


class DistillTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()

    def test_extract_numbers(self) -> None:
        text = "We reach 89.7% MSE reduction, 197 ms per chunk, 23 Hz, 3B parameters, 44,000 hours."
        found = extract_numbers(text, limit=6)
        self.assertEqual(found, ["89.7%", "197 ms", "23 Hz", "3B parameters", "44,000 hours"])

    def test_extract_numbers_keeps_whole_figures(self) -> None:
        # "\b\d+" read these from the middle and published "000 Hz" and "200 ms".
        self.assertEqual(extract_numbers("control at 1,000 Hz"), ["1,000 Hz"])
        self.assertEqual(extract_numbers("latency of 1,200 ms"), ["1,200 ms"])
        self.assertEqual(extract_numbers("a 3× speedup, 2x cheaper"), ["3×", "2x"])
        self.assertEqual(extract_numbers("since v2.5 % of runs"), [])

    def test_the_numbers_signal_sees_a_percentage(self) -> None:
        self.assertIn("numbers", detect_signals("improves by 89.7% over baseline", self.config.taxonomy))
        self.assertNotIn("numbers", detect_signals("no figures here", self.config.taxonomy))

    def test_classification_routes_to_expected_lane(self) -> None:
        lane, hits = classify("on-device inference latency on Jetson Thor at 23 Hz", self.config)
        self.assertEqual(lane, "edge")
        self.assertGreater(hits, 0)
        lane, _ = classify("control barrier function safety filter with runtime monitor", self.config)
        self.assertEqual(lane, "safety")
        lane, _ = classify("egocentric human video data scaling for pretraining", self.config)
        self.assertEqual(lane, "data")

    def test_signals_detected(self) -> None:
        signals = detect_signals(
            "Closed-loop success rate on a real robot, code open-source on github.com, 40 ms latency",
            self.config.taxonomy,
        )
        for expected in ("closed_loop", "real_robot", "open_release", "latency"):
            self.assertIn(expected, signals)

    def test_recency_bonus_monotonic(self) -> None:
        today = date(2026, 9, 19)
        self.assertGreater(recency_bonus("2026-09-19", today), recency_bonus("2026-09-18", today))
        self.assertGreater(recency_bonus("2026-09-18", today), recency_bonus("2026-09-10", today))
        self.assertEqual(recency_bonus("2026-01-01", today), 0.0)

    def test_enrich_sorts_by_score_desc(self) -> None:
        strong = make_item(id="s", evidence="O", source_id="aws-physical-ai")
        weak = make_item(
            id="w",
            evidence="M",
            title="A blog post about robots",
            summary="No numbers here.",
            published="2026-01-01",
        )
        ranked = enrich([weak, strong], self.config, date(2026, 9, 19))
        self.assertEqual(ranked[0].id, "s")
        self.assertGreater(ranked[0].score, ranked[1].score)

    def test_deduplicate_by_url_and_title(self) -> None:
        a = make_item(id="a", url="https://example.org/x?utm=1")
        b = make_item(id="b", url="https://example.org/x/")
        self.assertEqual(len(deduplicate([a, b])), 1)

    def test_select_respects_per_lane_cap(self) -> None:
        items = enrich([make_item(id=f"i{n}", url=f"https://example.org/{n}") for n in range(6)], self.config, date.today())
        picked = select(items, limit=10, per_lane=2, min_score=0.0)
        self.assertLessEqual(len(picked), 2)

    def test_curated_baseline_lanes_survive_enrichment(self) -> None:
        curated = {f"baseline:{raw['id']}": raw["lane"] for raw in self.config.baseline["items"]}
        for item in enrich(baseline_items(self.config), self.config, date(2026, 9, 19)):
            self.assertEqual(item.lane, curated[item.id], f"{item.id} was reclassified")

    def test_locked_lane_is_scored_on_its_own_keywords(self) -> None:
        item = make_item(
            id="locked",
            title="A quarterly note with no lane vocabulary",
            summary="Nothing in this text matches any lane keyword list.",
            lane="safety",
            lane_locked=True,
        )
        ranked = enrich([item], self.config, date(2026, 9, 19))
        self.assertEqual(ranked[0].lane, "safety")
        self.assertEqual(ranked[0].lane_hits, 0)
        # The unlocked classifier would have filed it under the fallback lane.
        self.assertEqual(classify(f"{item.title}. {item.summary}", self.config), ("foundation", 0))

    def test_off_topic_item_clears_the_score_but_is_not_selected(self) -> None:
        # Evidence plus same-day recency alone reach min_score, so without a lane
        # gate an unrelated paper would be published under the fallback lane.
        item = make_item(
            id="off",
            title="A recipe for sourdough bread",
            summary="No robotics content whatsoever.",
            published=date(2026, 9, 19).isoformat(),
        )
        ranked = enrich([item], self.config, date(2026, 9, 19))
        self.assertEqual(ranked[0].lane_hits, 0)
        self.assertGreater(ranked[0].score, 0.9)
        self.assertEqual(select(ranked), [])

    def test_curated_items_stay_selectable_without_keywords(self) -> None:
        item = make_item(id="curated", title="Regulation timeline", summary="No keywords.",
                         lane="safety", lane_locked=True, evidence="O",
                         published=date(2026, 9, 19).isoformat())
        ranked = enrich([item], self.config, date(2026, 9, 19))
        self.assertEqual([picked.id for picked in select(ranked)], ["curated"])

    def test_one_liner_truncates(self) -> None:
        long_text = "First sentence. " + ("padding words " * 60)
        self.assertLessEqual(len(one_liner(long_text)), 241)

    def test_prefilter_drops_an_undated_entry(self) -> None:
        kept = prefilter([make_item(published=""), make_item(id="t2", published="2026-09-19")],
                         self.config, date(2026, 9, 19))
        self.assertEqual([item.id for item in kept], ["t2"])

    def test_prefilter_drops_sponsored_and_stale(self) -> None:
        today = date(2026, 9, 19)
        sponsored = make_item(id="ad", summary="This article is brought to you by SomeVendor.")
        stale = make_item(id="old", published="2026-01-01")
        fresh = make_item(id="fresh", published="2026-09-18")
        kept = {item.id for item in prefilter([sponsored, stale, fresh], self.config, today)}
        self.assertEqual(kept, {"fresh"})

    def test_lane_distribution_and_mix(self) -> None:
        items = enrich(baseline_items(self.config), self.config, date.today())
        rows = dict(lane_distribution(items, self.config))
        self.assertEqual(sum(rows.values()), len(items))
        mix = evidence_mix(items)
        self.assertEqual(sum(mix.values()), len(items))


class FetchHelpersTest(unittest.TestCase):
    def test_clean_text_strips_markup(self) -> None:
        self.assertEqual(clean_text("<p>Hello &amp;  world</p>"), "Hello & world")

    def test_link_digest_is_stable_across_processes(self) -> None:
        # str.hash is randomised per process, so a hash()-derived id changed on
        # every run and no item could be recognised across days.
        link = "https://example.org/a-post"
        expected = link_digest(link)
        script = (
            "import sys; sys.path.insert(0, %r);"
            "from pairadar.fetch import link_digest;"
            "print(link_digest(%r))" % (str(ROOT), link)
        )
        for seed in ("0", "1", "12345"):
            environment = {**os.environ, "PYTHONHASHSEED": seed}
            result = subprocess.run([sys.executable, "-c", script], check=True,
                                    capture_output=True, text=True, env=environment)
            self.assertEqual(result.stdout.strip(), expected, f"unstable under seed {seed}")
        self.assertEqual(len(expected), 12)

    def test_parse_date_formats(self) -> None:
        self.assertEqual(parse_date("2026-09-19T04:00:00Z"), "2026-09-19")
        self.assertEqual(parse_date("Fri, 19 Sep 2026 04:00:00 +0000"), "2026-09-19")
        self.assertEqual(parse_date("2026-09-19"), "2026-09-19")
        # Named zones, no seconds, no weekday: all RFC 822, all once read as today.
        self.assertEqual(parse_date("Mon, 3 Aug 2026 10:00:00 PST"), "2026-08-03")
        self.assertEqual(parse_date("Thu, 24 Sep 2026 20:00 +0000"), "2026-09-24")
        self.assertEqual(parse_date("24 Sep 2026 10:00:00 GMT"), "2026-09-24")

    def test_parse_date_reports_the_utc_day(self) -> None:
        self.assertEqual(parse_date("Thu, 24 Sep 2026 20:00:00 -0700"), "2026-09-25")
        self.assertEqual(parse_date("2026-09-19T04:00:00+09:00"), "2026-09-18")

    def test_an_unreadable_date_is_empty_not_today(self) -> None:
        for raw in ("", None, "sometime last week", "2026-13-45"):
            self.assertEqual(parse_date(raw), "", raw)

    def test_clean_text_decodes_every_entity_once(self) -> None:
        self.assertEqual(clean_text("it&#8217;s 3&#215; &hellip;"), "it’s 3× …")
        # Escaped markup is text about a tag, and stays text.
        self.assertEqual(clean_text("<p>use &lt;details&gt;</p>"), "use <details>")
        self.assertEqual(clean_text("&amp;lt;b&amp;gt;"), "&lt;b&gt;")

    def test_baseline_items_carry_numbers(self) -> None:
        items = baseline_items(load_config())
        self.assertGreaterEqual(len(items), 20)
        self.assertTrue(any(item.numbers for item in items))


class RepeatMemoryTest(unittest.TestCase):
    """An item published yesterday must not headline again today."""

    def setUp(self) -> None:
        self.config = load_config()

    def test_seen_urls_are_skipped_while_new_ones_survive(self) -> None:
        today = date(2026, 9, 19)
        old = make_item(id="old", url="https://example.org/paper-a")
        fresh = make_item(id="fresh", url="https://example.org/paper-b")
        ranked = enrich([old, fresh], self.config, today)
        self.assertEqual({item.id for item in select(ranked, per_lane=4)}, {"old", "fresh"})
        remaining = select(ranked, per_lane=4, seen=["https://example.org/paper-a?utm=x"])
        self.assertEqual({item.id for item in remaining}, {"fresh"})

    def test_recent_urls_respects_the_window(self) -> None:
        runs = [
            {"date": "2026-09-18", "urls": ["https://example.org/yesterday"]},
            {"date": "2026-09-01", "urls": ["https://example.org/ancient"]},
            {"date": "2026-09-30", "urls": ["https://example.org/future"]},
            {"date": "broken"},
        ]
        urls = recent_urls(runs, date(2026, 9, 19), 7)
        self.assertEqual(urls, {"https://example.org/yesterday"})

    def test_saved_history_keeps_urls_only_inside_the_window(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)/"history.json"
            runs = [
                {"date": "2026-09-01", "count": 3, "urls": ["https://example.org/ancient"]},
                {"date": "2026-09-18", "count": 4, "urls": ["https://example.org/yesterday"]},
            ]
            save_history(runs, keep_urls_days=7, path=path)
            stored = {entry["date"]: entry for entry in json.loads(path.read_text())["runs"]}
        self.assertNotIn("urls", stored["2026-09-01"], "old URLs should not grow the log forever")
        self.assertEqual(stored["2026-09-18"]["urls"], ["https://example.org/yesterday"])
        self.assertEqual(stored["2026-09-01"]["count"], 3, "counts must survive pruning")

    def test_url_key_normalises_tracking_and_slashes(self) -> None:
        self.assertEqual(url_key("https://Example.org/Post/?utm_source=x"),
                         url_key("https://Example.org/Post"))


class FetchReliabilityTest(unittest.TestCase):
    """A dead source must not silently become a quiet day."""

    def setUp(self) -> None:
        self.config = load_config()

    def test_http_get_retries_a_transient_failure(self) -> None:
        class Response:
            def read(self) -> bytes:
                return b"<feed/>"

            def __enter__(self):
                return self

            def __exit__(self, *exc) -> None:
                return None

        attempts = []

        def flaky(request, timeout=None):
            attempts.append(request.full_url)
            if len(attempts) == 1:
                raise urllib.error.URLError("connection reset")
            return Response()

        with patch.object(fetch_module.urllib.request, "urlopen", flaky), \
                patch.object(fetch_module.time, "sleep") as sleep:
            self.assertEqual(http_get("https://example.org/feed"), b"<feed/>")
        self.assertEqual(len(attempts), 2)
        self.assertEqual(sleep.call_count, 1)

    def test_http_get_gives_up_and_returns_none(self) -> None:
        def always_fail(request, timeout=None):
            raise urllib.error.HTTPError(request.full_url, 406, "Not Acceptable", {}, None)

        with patch.object(fetch_module.urllib.request, "urlopen", always_fail), \
                patch.object(fetch_module.time, "sleep"):
            self.assertIsNone(http_get("https://example.org/feed", retries=2))

    ARXIV_REPLY = (
        '<feed xmlns="http://www.w3.org/2005/Atom"><entry>'
        '<id>http://arxiv.org/abs/2609.01234v1</id><title>A VLA policy</title>'
        '<summary>We train a policy.</summary><published>2026-09-24T18:00:00Z</published>'
        '</entry></feed>'
    ).encode()

    def test_arxiv_uses_https_and_spaces_its_queries(self) -> None:
        # arXiv asks for roughly three seconds between API calls.
        urls: list[str] = []
        with patch.object(fetch_module, "http_get", lambda url: urls.append(url) or self.ARXIV_REPLY), \
                patch.object(fetch_module.time, "sleep") as sleep:
            items, answered = fetch_arxiv(self.config, date(2026, 9, 25))
        queries = len(self.config.sources["arxiv"]["queries"])
        self.assertEqual({item.id for item in items}, {"arxiv:2609.01234"})
        self.assertTrue(answered)
        self.assertEqual(len(urls), queries)
        self.assertTrue(all(url.startswith("https://") for url in urls), urls)
        self.assertTrue(ARXIV_ENDPOINT.startswith("https://"))
        self.assertEqual(sleep.call_count, queries - 1)

    def test_an_unusable_arxiv_reply_is_no_answer_and_the_feeds_take_over(self) -> None:
        # An HTML rate-limit page, an empty feed and an error entry all used to count
        # as an answer, so the category feeds never stood in and the day had no papers.
        error = (b'<feed xmlns="http://www.w3.org/2005/Atom"><entry>'
                 b'<id>http://arxiv.org/api/errors#incorrect_id_format</id>'
                 b'<title>Error</title></entry></feed>')
        for reply in (b"<html><body>Rate exceeded.</body></html>", b"<feed/>", error, b"<html>"):
            report = FetchReport()
            with patch.object(fetch_module, "http_get", lambda url, *a, **k: reply), \
                    patch.object(fetch_module, "fetch_arxiv_rss", return_value=([], True)) as rss, \
                    patch.object(fetch_module.time, "sleep"), \
                    contextlib.redirect_stdout(io.StringIO()):
                items, answered = fetch_arxiv(self.config, date(2026, 9, 25), report)
            self.assertEqual(items, [], reply)
            self.assertTrue(rss.called, reply)
            self.assertTrue(answered)
            self.assertEqual(report.fallback, ["arxiv"], "served, but the API is still down")

    def test_the_arxiv_cutoff_counts_back_from_the_run_date(self) -> None:
        with patch.object(fetch_module, "http_get", lambda url: self.ARXIV_REPLY), \
                patch.object(fetch_module.time, "sleep"):
            self.assertEqual({item.id for item in fetch_arxiv(self.config, date(2026, 9, 28))[0]},
                             {"arxiv:2609.01234"},
                             "Monday's run must still reach Thursday evening's papers")
            self.assertEqual(fetch_arxiv(self.config, date(2026, 10, 5))[0], [])

    def test_http_get_does_not_retry_a_refusal(self) -> None:
        calls = []

        def refuse(request, timeout=None):
            calls.append(1)
            raise urllib.error.HTTPError(request.full_url, 406, "Not Acceptable", {}, None)

        with patch.object(fetch_module.urllib.request, "urlopen", refuse), \
                patch.object(fetch_module.time, "sleep") as sleep, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertIsNone(http_get("https://example.org/feed", retries=3))
        self.assertEqual(len(calls), 1)
        self.assertEqual(sleep.call_count, 0)

    def test_http_get_honours_retry_after_within_bounds(self) -> None:
        calls = []

        def limited(request, timeout=None):
            calls.append(1)
            raise urllib.error.HTTPError(request.full_url, 429, "Too Many Requests",
                                         {"Retry-After": "120"}, None)

        with patch.object(fetch_module.urllib.request, "urlopen", limited), \
                patch.object(fetch_module.time, "sleep") as sleep, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertIsNone(http_get("https://example.org/feed", retries=2))
        self.assertEqual(len(calls), 2)
        sleep.assert_called_once_with(fetch_module.MAX_RETRY_AFTER)

    def test_http_get_survives_a_protocol_error(self) -> None:
        # A garbled status line or a truncated body raise http.client errors, which
        # are not OSErrors; one of them used to end the whole daily run.
        import http.client
        for error in (http.client.BadStatusLine("garbage"), http.client.IncompleteRead(b"12345", 995)):
            def broken(request, timeout=None, error=error):
                raise error

            with patch.object(fetch_module.urllib.request, "urlopen", broken), \
                    patch.object(fetch_module.time, "sleep"), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertIsNone(http_get("https://example.org/feed"), type(error).__name__)

    def test_total_outage_is_reported_by_the_fetch_report(self) -> None:
        with patch.object(fetch_module, "http_get", lambda url, *a, **k: None), \
                patch.object(fetch_module.time, "sleep"), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            report = fetch_all(self.config)
        # Captured: a simulated total outage in the log would read as a real one.
        self.assertIn("no answer from", out.getvalue())
        expected = {"arxiv", *(feed["id"] for feed in self.config.sources["feeds"])}
        self.assertEqual(set(report.failed), expected)
        self.assertEqual(report.answered, [])
        summary = report.to_dict()
        self.assertEqual(summary["items"], 0)
        self.assertEqual(summary["answered"], 0)
        self.assertEqual(summary["attempted"], len(expected))

    def test_degraded_only_fires_on_a_real_outage(self) -> None:
        outage = FetchReport(attempted=["arxiv"], failed=["arxiv"]).to_dict()
        partial = FetchReport(items=[make_item()], attempted=["arxiv", "deepmind"],
                              failed=["deepmind"]).to_dict()
        self.assertTrue(degraded(outage, offline=False))
        self.assertFalse(degraded(outage, offline=True), "offline runs fetch nothing by design")
        self.assertFalse(degraded(partial, offline=False), "one dead source must not fail the run")
        self.assertFalse(degraded(FetchReport().to_dict(), offline=False))

    def test_published_payload_carries_fetch_health(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            # Pass the root in; patching the module attribute no longer reaches a
            # default argument, and this test used to write into the repository.
            path = render.write_latest(
                {
                    "date": "2026-09-19",
                    "generated": "2026-09-19 01:30 UTC",
                    "window": "w",
                    "picked": [],
                    "lane_rows": [("edge", 1)],
                    "mix": {"O": 0, "R": 1, "M": 0},
                    "baseline_all": [],
                    "fetch": FetchReport(items=[make_item()], attempted=["arxiv"]).to_dict(),
                },
                root=Path(temporary),
            )
            payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["fetch"]["answered"], 1)
        self.assertEqual(payload["fetch"]["per_source"], {"arxiv": 1})


class OutputRootTest(unittest.TestCase):
    """`--out` must let a reader see real output without rewriting their clone."""

    WATCHED = ("README.md", "README.en.md", "README.ja.md", "radar/latest.json",
               "radar/history.json", "radar/INDEX.md", "assets/lane-distribution.svg")

    def digests(self) -> dict[str, str]:
        return {
            name: hashlib.sha256((ROOT/name).read_bytes()).hexdigest()
            for name in self.WATCHED if (ROOT/name).exists()
        }

    def test_out_redirects_every_generated_file_and_leaves_the_checkout_alone(self) -> None:
        before = self.digests()
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary)/"preview"
            cli.run(offline=True, day="2026-02-03", out=out)
            produced = {path.relative_to(out).as_posix() for path in out.rglob("*") if path.is_file()}
        self.assertEqual(self.digests(), before, "a --out run must not touch the repository")
        for expected in (
            "radar/daily/2026-02-03.zh.md",
            "radar/daily/2026-02-03.en.md",
            "radar/daily/2026-02-03.ja.md",
            "radar/latest.json",
            "radar/history.json",
            "radar/INDEX.md",
            "assets/lane-distribution.svg",
            "assets/cadence.svg",
            "assets/evidence-mix.svg",
        ):
            self.assertIn(expected, produced)

    def test_out_still_reads_the_repository_run_log(self) -> None:
        # The repeat window only works if previous days are still visible.
        repository_dates = {entry["date"] for entry in cli.load_history()}
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary)
            ctx = cli.run(offline=True, day="2026-02-04", out=out)
            stored = {entry["date"] for entry in json.loads(
                (out/"radar"/"history.json").read_text(encoding="utf-8"))["runs"]}
        self.assertTrue(repository_dates <= stored, "previous runs disappeared from the log")
        self.assertIn("2026-02-04", {entry["date"] for entry in ctx["history"]})


class SourceHealthTest(unittest.TestCase):
    """A source that quietly stops answering must become visible."""

    # deepmind fails on four of the last five days; nvidia-blog once; the August
    # entry is outside the window; the last two entries are malformed on purpose.
    RUNS = [
        {"date": "2026-09-19", "count": 8, "failed": ["deepmind"]},
        {"date": "2026-09-18", "count": 8, "failed": ["deepmind", "nvidia-blog"]},
        {"date": "2026-09-17", "count": 7, "failed": ["deepmind"]},
        {"date": "2026-09-16", "count": 8, "failed": []},
        {"date": "2026-09-15", "count": 8, "failed": ["deepmind"]},
        {"date": "2026-08-01", "count": 8, "failed": ["aws-physical-ai"]},
        {"date": "2026-09-30", "count": 8, "failed": ["huggingface-blog"]},
        {"count": 8, "failed": ["broken"]},
    ]
    TODAY = date(2026, 9, 19)

    def test_failures_are_counted_inside_the_window_only(self) -> None:
        counts = health.failures_by_source(self.RUNS, self.TODAY)
        self.assertEqual(counts, {"deepmind": 4, "nvidia-blog": 1})

    def test_struggling_applies_the_threshold_and_ranks_worst_first(self) -> None:
        self.assertEqual(health.struggling(self.RUNS, self.TODAY), [("deepmind", 4)])
        self.assertEqual(health.struggling(self.RUNS, self.TODAY, threshold=5), [])
        runs = [{"date": "2026-09-19", "failed": ["deepmind", "nvidia-blog"]}, *self.RUNS[1:]]
        self.assertEqual(health.struggling(runs, self.TODAY, threshold=1),
                         [("deepmind", 4), ("nvidia-blog", 2)])

    def test_a_source_that_has_recovered_is_not_struggling(self) -> None:
        # nvidia-blog missed a day but answered the latest run; so did arXiv after
        # five straight failures, and the repair job woke a model for a fortnight.
        self.assertEqual(health.struggling(self.RUNS, self.TODAY, threshold=1), [("deepmind", 4)])
        recovered = [{"date": "2026-09-20", "failed": []}, *self.RUNS]
        self.assertEqual(health.struggling(recovered, date(2026, 9, 20)), [])
        self.assertEqual(health.failures_by_source(recovered, date(2026, 9, 20))["deepmind"], 4,
                         "the history is still reported")

    def test_the_window_holds_exactly_its_days(self) -> None:
        runs = [{"date": "2026-09-06", "failed": ["old"]}, {"date": "2026-09-05", "failed": ["older"]}]
        self.assertEqual(health.failures_by_source(runs, self.TODAY, window=14), {"old": 1})

    def test_a_fallback_is_reported_but_is_not_a_failure(self) -> None:
        runs = [{"date": f"2026-09-{day}", "failed": [], "fallback": ["arxiv"]} for day in (17, 18, 19)]
        verdict = health.report(runs, self.TODAY)
        self.assertEqual(verdict["fallbacks"], {"arxiv": 3})
        self.assertEqual(verdict["failures"], {})
        self.assertEqual(verdict["struggling"], [])

    def test_only_runs_that_recorded_health_are_counted(self) -> None:
        # Entries written before health was recorded must not read as healthy days.
        self.assertEqual(health.days_observed(self.RUNS, self.TODAY), 5)
        self.assertEqual(health.days_observed([{"date": "2026-09-19", "count": 8}], self.TODAY), 0)

    def test_cli_exits_non_zero_only_when_asked_and_only_when_struggling(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)/"history.json"
            path.write_text(json.dumps({"runs": self.RUNS}), encoding="utf-8")
            common = ["--history", str(path), "--date", "2026-09-19"]
            # health.main prints its verdict; captured so a synthetic fixture cannot
            # end up in the scheduled job's log looking like a real outage.
            def verdict(argv):
                with contextlib.redirect_stdout(io.StringIO()) as out:
                    code = health.main(argv)
                self.assertIn("struggling", out.getvalue())
                return code

            self.assertEqual(verdict(common), 0, "the daily run must stay green")
            self.assertEqual(verdict([*common, "--fail-on-struggling"]), 1)
            self.assertEqual(verdict([*common, "--fail-on-struggling", "--threshold", "9"]), 0)

    def test_a_run_records_which_sources_did_not_answer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary)
            cli.run(offline=True, day="2026-03-05", out=out)
            runs = json.loads((out/"radar"/"history.json").read_text(encoding="utf-8"))["runs"]
        # The log is date-sorted, so look the run up rather than taking the last entry.
        entry = next(run for run in runs if run["date"] == "2026-03-05")
        self.assertIn("failed", entry, "health has to be recorded to be readable later")
        self.assertEqual(entry["failed"], [], "an offline run attempts no source")


class LaneEvidenceTest(unittest.TestCase):
    """Name the items whose lane rests on almost nothing, without changing any lane."""

    def setUp(self) -> None:
        self.config = load_config()

    def test_ranked_lanes_covers_every_lane_best_first(self) -> None:
        ranking = lanes.ranked_lanes("on-device inference latency at 23 Hz on Jetson", self.config)
        self.assertEqual(len(ranking), len(self.config.lanes))
        self.assertEqual(ranking[0][0], "edge")
        self.assertGreaterEqual(ranking[0][2], ranking[1][2])

    def test_a_well_matched_item_is_not_flagged(self) -> None:
        item = make_item(title="On-device inference latency and control frequency",
                         summary="Quantization for real-time control at 50 Hz on Jetson Thor, measured on-device.")
        enrich([item], self.config, date(2026, 9, 19))
        self.assertIsNone(lanes.inspect(item, self.config))

    def test_a_single_keyword_is_reported_as_thin(self) -> None:
        item = make_item(title="A note mentioning one humanoid", summary="Nothing else matches a lane.")
        enrich([item], self.config, date(2026, 9, 19))
        finding = lanes.inspect(item, self.config)
        self.assertIsNotNone(finding)
        self.assertIn("thin", finding["reasons"])
        self.assertEqual(finding["lane"], item.lane)

    def test_two_close_lanes_are_reported_as_ambiguous(self) -> None:
        item = make_item(
            title="Closed-loop evaluation of on-device inference",
            summary="sim2real success rate measured on-device with quantization and real-time control.")
        enrich([item], self.config, date(2026, 9, 19))
        finding = lanes.inspect(item, self.config, margin=10.0)
        self.assertIsNotNone(finding)
        self.assertIn("ambiguous", finding["reasons"])
        self.assertNotEqual(finding["best"]["lane"], finding["runner_up"]["lane"])

    def test_the_report_says_how_much_text_it_had(self) -> None:
        # A snapshot written before excerpts were stored classifies on titles alone,
        # which inflates "thin"; the report has to admit that rather than hide it.
        items = [make_item(id="a", summary=""), make_item(id="b", url="https://example.org/b")]
        verdict = lanes.report(items, self.config)
        self.assertEqual(verdict["checked"], 2)
        self.assertEqual(verdict["with_summaries"], 1)

    def test_cli_exit_codes_and_a_missing_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)/"latest.json"
            self.assertEqual(lanes.published_picks(path), [], "a missing snapshot is not an error")
            item = make_item(title="A note mentioning one humanoid", summary="Nothing else.")
            enrich([item], self.config, date(2026, 9, 19))
            path.write_text(json.dumps({"picked": [item.to_dict()]}), encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(lanes.main(["--latest", str(path)]), 0)
                self.assertEqual(lanes.main(["--latest", str(path), "--fail-on-suspicious"]), 1)
            self.assertIn("suspicious", out.getvalue())


class DraftedNumbersTest(unittest.TestCase):
    """A drafted line may not introduce a number the page does not have."""

    def test_the_check_catches_an_invented_figure(self) -> None:
        page = "PASSAGE: humanoid traversal `50 Hz` `48.1%` reported by the authors."
        self.assertEqual(unsupported_numbers("50 Hz control, 48.1% success", page), [])
        self.assertEqual(unsupported_numbers("a 3.2x speedup", page), ["3.2"])
        self.assertEqual(unsupported_numbers("one embodiment only", page), [])

    def test_every_drafted_note_is_supported_by_its_page(self) -> None:
        directory = ROOT/"data"/"notes"
        files = sorted(directory.glob("*.json")) if directory.exists() else []
        if not files:
            self.skipTest("no drafted notes in the tree yet")
        for path in files:
            document = json.loads(path.read_text(encoding="utf-8"))
            day = document["date"]
            pages = [ROOT/"radar"/"daily"/f"{day}.{lang}.md" for lang in LANGS]
            source = "\n".join(page.read_text(encoding="utf-8") for page in pages if page.exists())
            if not source:
                continue
            for url, note in document["notes"].items():
                for lang, line in note.items():
                    with self.subTest(day=day, url=url, lang=lang):
                        self.assertEqual(unsupported_numbers(line, source), [],
                                         "a drafted line cites a number the page does not contain")


class DraftedNotesTest(unittest.TestCase):
    """Drafted analysis is optional, ranked below human curation, and always labelled."""

    def setUp(self) -> None:
        self.config = load_config()
        self.item = make_item(id="live", url="https://example.org/paper", source_id="arxiv")
        enrich([self.item], self.config, date(2026, 9, 19))
        self.notes = {
            "schema": "pairadar-notes-1",
            "date": "2026-09-19",
            "author": {"kind": "llm-draft", "agent": "radar-analyst", "model": "test-model"},
            "notes": {url_key(self.item.url): {"zh": "中文草稿", "en": "English draft", "ja": "日本語の下書き"}},
        }

    def context(self, notes=None, curated=None):
        return {
            "date": "2026-09-19", "generated": "g", "window": "w",
            "picked": [self.item], "baseline": [], "baseline_all": [],
            "curated": curated or {},
            "lane_rows": lane_distribution([self.item], self.config),
            "mix": evidence_mix([self.item]), "cadence": [("2026-09-19", 1)],
            "notes": notes or {},
        }

    def test_drafted_note_is_used_and_labelled(self) -> None:
        for lang, expected in (("zh", "中文草稿"), ("en", "English draft"), ("ja", "日本語の下書き")):
            page = render_daily(self.config, lang, self.context(self.notes))
            label = self.config.ui(lang)["llm_draft"]
            self.assertIn(expected, page)
            self.assertIn(f"`{label}`", page, f"{lang}: drafted line must be labelled")

    def test_without_notes_nothing_is_labelled(self) -> None:
        page = render_daily(self.config, "zh", self.context())
        self.assertNotIn(self.config.ui("zh")["llm_draft"], page)
        self.assertNotIn("radar-analyst", page)

    def test_human_curation_outranks_a_draft(self) -> None:
        curated = {"live": {"zh": "人工撰写", "en": "Human written", "ja": "人間が執筆"}}
        page = render_daily(self.config, "zh", self.context(self.notes, curated))
        self.assertIn("人工撰写", page)
        self.assertNotIn("中文草稿", page)
        self.assertNotIn(f"`{self.config.ui('zh')['llm_draft']}`", page)

    def test_a_missing_language_falls_back_to_the_composed_line(self) -> None:
        partial = {**self.notes, "notes": {url_key(self.item.url): {"en": "English only"}}}
        page = render_daily(self.config, "zh", self.context(partial))
        self.assertIn(composed_why(self.item, self.config, "zh"), page)
        self.assertNotIn(f"`{self.config.ui('zh')['llm_draft']}`", page)

    def test_the_page_names_who_drafted_it(self) -> None:
        page = render_daily(self.config, "en", self.context(self.notes))
        self.assertIn("radar-analyst", page)
        self.assertIn("test-model", page)

    def test_the_readme_block_labels_drafts_too(self) -> None:
        block = readme_block(self.config, "zh", self.context(self.notes))
        self.assertIn("中文草稿", block)
        self.assertIn(self.config.ui("zh")["llm_draft"], block)

    def test_a_malformed_or_foreign_notes_file_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory/"2026-09-19.json").write_text("{ not json", encoding="utf-8")
            self.assertEqual(load_notes("2026-09-19", directory), {})
            (directory/"2026-09-20.json").write_text(json.dumps({"schema": "other", "notes": {}}), encoding="utf-8")
            self.assertEqual(load_notes("2026-09-20", directory), {})
            (directory/"2026-09-21.json").write_text(json.dumps({"schema": "pairadar-notes-1"}), encoding="utf-8")
            self.assertEqual(load_notes("2026-09-21", directory), {}, "notes must be a mapping")
            self.assertEqual(load_notes("2026-09-22", directory), {}, "a missing day is not an error")


class RerenderTest(unittest.TestCase):
    """A published day must be re-renderable from its own snapshot."""

    def setUp(self) -> None:
        self.config = load_config()
        self.item = make_item(id="arxiv:1", url="https://example.org/paper",
                              title="A closed-loop VLA result", source_id="arxiv",
                              summary="We report 87.5% success at 23 Hz on real hardware. A second sentence.")
        enrich([self.item], self.config, date(2026, 9, 19))

    def test_a_round_tripped_item_renders_identically(self) -> None:
        # The property that makes re-rendering safe: the snapshot keeps everything the
        # page shows, the source excerpt included.
        rebuilt = Item.from_dict(self.item.to_dict())
        for field in ("id", "title", "url", "publisher", "evidence", "published", "lane",
                      "numbers", "signals", "summary"):
            self.assertEqual(getattr(rebuilt, field), getattr(self.item, field), field)

    def write_snapshot(self, root: Path, notes: dict | None = None) -> None:
        (root/"radar").mkdir(parents=True, exist_ok=True)
        (root/"radar"/"latest.json").write_text(json.dumps({
            "date": "2026-09-19", "generated": "2026-09-19 01:30 UTC",
            "window": "2026-09-16 → 2026-09-19 (UTC)",
            "picked": [self.item.to_dict()],
            "lane_counts": {lane["id"]: 0 for lane in self.config.lanes},
            "evidence_mix": {"O": 0, "R": 1, "M": 0}, "baseline_count": 29,
        }, ensure_ascii=False), encoding="utf-8")
        (root/"radar"/"history.json").write_text(
            json.dumps({"runs": [{"date": "2026-09-19", "count": 1, "lanes": 1}]}), encoding="utf-8")

    def test_rerender_rebuilds_the_day_without_fetching(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.write_snapshot(root)
            with patch.object(cli, "fetch_all", side_effect=AssertionError("must not fetch")):
                ctx = cli.rerender(source=root)
            pages = {lang: (root/"radar"/"daily"/f"2026-09-19.{lang}.md").read_text(encoding="utf-8")
                     for lang in LANGS}
            for name in ("lane-distribution.svg", "cadence.svg", "evidence-mix.svg"):
                self.assertTrue((root/"assets"/name).exists(),
                                f"{name}: the pages embed the charts, so a re-render must refresh them")
        self.assertEqual([item.id for item in ctx["picked"]], ["arxiv:1"])
        for lang, page in pages.items():
            self.assertIn("A closed-loop VLA result", page)
            self.assertIn("87.5% success at 23 Hz", page, f"{lang}: the source excerpt must survive")

    def test_rerender_picks_up_drafted_notes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.write_snapshot(root)
            notes_dir = root/"notes"
            notes_dir.mkdir()
            (notes_dir/"2026-09-19.json").write_text(json.dumps({
                "schema": "pairadar-notes-1", "date": "2026-09-19",
                "author": {"agent": "radar-analyst", "model": "test-model"},
                "notes": {url_key(self.item.url): {"zh": "草稿中文", "en": "Draft EN", "ja": "下書き"}},
            }, ensure_ascii=False), encoding="utf-8")
            with patch.object(cli, "load_notes", lambda day: load_notes(day, notes_dir)):
                cli.rerender(source=root)
            page = (root/"radar"/"daily"/"2026-09-19.zh.md").read_text(encoding="utf-8")
        self.assertIn("草稿中文", page)
        self.assertIn(f"`{self.config.ui('zh')['llm_draft']}`", page)
        self.assertIn("radar-analyst", page)

    def test_rerender_refuses_a_day_the_snapshot_does_not_hold(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.write_snapshot(root)
            with self.assertRaises(SystemExit):
                cli.rerender(day="2026-09-18", source=root)


class RenderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        items = enrich(baseline_items(self.config), self.config, date(2026, 9, 19))
        self.ctx = {
            "date": "2026-09-19",
            "generated": "2026-09-19 01:30 UTC",
            "window": "2026-09-16 → 2026-09-19 (UTC)",
            "picked": items[:3],
            "baseline": items[:4],
            "baseline_all": items,
            "curated": {
                f"baseline:{raw['id']}": raw["why"] for raw in self.config.baseline["items"]
            },
            "lane_rows": lane_distribution(items, self.config),
            "mix": evidence_mix(items),
            "cadence": [("2026-09-18", 4), ("2026-09-19", 6)],
        }

    def test_daily_page_renders_in_three_languages(self) -> None:
        for lang in LANGS:
            page = render_daily(self.config, lang, self.ctx)
            self.assertIn(self.config.ui(lang)["title"], page)
            # A day's page is frozen text: the shared charts are redrawn every run.
            self.assertNotIn(".svg", page)
            self.assertIn(f"| {self.config.lane_name('edge', lang)} |", page)
            self.assertIn("http", page)
            self.assertGreater(len(page), 1200)

    def test_evidence_tags_present(self) -> None:
        page = render_daily(self.config, "zh", self.ctx)
        self.assertTrue(any(tag in page for tag in ("`[O]`", "`[R]`", "`[M]`")))

    def test_fetched_text_cannot_break_the_page(self) -> None:
        # A feed title or excerpt is someone else's text: an unclosed <details> folded
        # the page on GitHub, "]" broke the link, and "{{" fails the Pages build.
        hostile = make_item(title="Robots [v2] use <details> {{ site.url }}",
                            url="https://example.org/a (b)", source_id="nvidia-blog",
                            summary="An excerpt with <details> and {% raw %} in it.")
        ctx = {**self.ctx, "picked": [hostile]}
        for lang in LANGS:
            page = render_daily(self.config, lang, ctx)
            self.assertNotIn("<details>", page)
            self.assertNotIn("{{", page)
            self.assertNotIn("{%", page)
            self.assertIn(r"Robots \[v2\] use &lt;details>", page)
            self.assertIn("(https://example.org/a%20%28b%29)", page)


class ChartsTest(unittest.TestCase):
    def test_svg_outputs_are_valid_xml(self) -> None:
        import xml.etree.ElementTree as ET

        svgs = [
            charts.horizontal_bars("Lane distribution", [("Edge & real-time", 3), ("Data", 5)]),
            charts.cadence_bars("Cadence", [("2026-09-18", 3), ("2026-09-19", 7)]),
            charts.evidence_strip("Evidence mix", {"O": 4, "R": 9, "M": 2}),
        ]
        for svg in svgs:
            root = ET.fromstring(svg)
            self.assertTrue(root.tag.endswith("svg"))

    def test_cadence_marks_the_latest_run_and_its_mean(self) -> None:
        series = [("2026-09-1%d" % day, value) for day, value in enumerate([4, 6, 5, 8, 7, 8], 1)]
        svg = charts.cadence_bars("Daily cadence", series)
        self.assertIn("mean 6.3", svg, "the bars need a reference to be read against")
        self.assertIn('fill-opacity="0.95"', svg, "the latest run has to stand out")
        self.assertIn('fill-opacity="0.5"', svg, "earlier runs recede")
        # A single point is still a valid chart, and is its own mean.
        self.assertIn("mean 3.0", charts.cadence_bars("T", [("2026-09-19", 3)]))

    def test_evidence_strip_shows_shares_when_a_segment_fits_them(self) -> None:
        svg = charts.evidence_strip("Evidence mix", {"O": 14, "R": 20, "M": 3})
        for share in ("38%", "54%"):
            self.assertIn(share, svg, "counts alone are hard to compare between days")
        self.assertIn("M 3", svg, "a narrow segment keeps its count")
        self.assertNotIn("M 3 ·", svg, "a narrow segment must not carry text wider than itself")

    def test_charts_adapt_to_a_dark_theme(self) -> None:
        # GitHub renders READMEs dark for many readers; a hard white plate glares.
        for svg in (charts.horizontal_bars("T", [("Edge", 3)]),
                    charts.cadence_bars("T", [("2026-09-19", 3)]),
                    charts.evidence_strip("T", {"O": 1, "R": 2, "M": 0})):
            self.assertIn("prefers-color-scheme: dark", svg)
            self.assertIn('class="plate"', svg)
            self.assertNotIn('fill="#ffffff"/>', svg, "the plate must be themed, not hardcoded")

    def test_every_lane_gets_its_own_colour(self) -> None:
        # Two lanes sharing a colour in one chart is worse than any single colour
        # choice, and hashing labels cannot guarantee they differ.
        import re
        config = load_config()
        rows = [(config.chart_label(lane["id"]), 3) for lane in config.lanes]
        svg = charts.horizontal_bars("T", rows, colors=charts.PALETTE)
        used = re.findall(r'rx="3" fill="(#[0-9a-f]{6})"', svg)
        self.assertEqual(len(used), len(rows))
        self.assertEqual(len(set(used)), len(rows), "two lanes drew the same colour")
        self.assertNotIn(charts.ACCENT_3, used,
                         "the alarm red belongs to the media segment, not to a lane")

    def test_a_label_keeps_its_colour_across_processes(self) -> None:
        # A lane changing colour from day to day would read as a change in the data.
        expected = charts.color_for("Edge & real-time")
        self.assertIn(expected, charts.PALETTE)
        script = ("import sys; sys.path.insert(0, %r);"
                  "from pairadar.charts import color_for; print(color_for('Edge & real-time'))" % str(ROOT))
        for seed in ("0", "7"):
            result = subprocess.run([sys.executable, "-c", script], check=True, capture_output=True,
                                    text=True, env={**os.environ, "PYTHONHASHSEED": seed})
            self.assertEqual(result.stdout.strip(), expected)
        self.assertNotEqual(charts.color_for("Edge & real-time"), charts.color_for("Data engine"))

    def test_charts_handle_empty_input(self) -> None:
        self.assertIn("<svg", charts.horizontal_bars("Empty", []))
        self.assertIn("<svg", charts.cadence_bars("Empty", []))
        self.assertIn("<svg", charts.evidence_strip("Empty", {}))

    def test_lane_chart_labels_fit_the_gutter(self) -> None:
        # The full lane names are 43-53 characters; drawn at 12px from x=20 they
        # ran over the bars, which start at x=250.
        config = load_config()
        for lane in config.lanes:
            label = config.chart_label(lane["id"])
            self.assertLessEqual(charts.text_width(label), 220, f"{lane['id']}: {label}")
            self.assertNotIn("…", label, "a declared chart label should not need trimming")

    def test_over_long_labels_are_trimmed_not_overlapped(self) -> None:
        long_label = "Edge & real-time (on-device inference / control rate)"
        svg = charts.horizontal_bars("T", [(long_label, 3)])
        self.assertNotIn(long_label, svg)
        drawn = svg.split('font-size="12"')[1].split(">")[1].split("<")[0]
        self.assertTrue(drawn.endswith("…"), drawn)
        # The SVG carries escaped text; measure what the reader actually sees.
        self.assertLessEqual(charts.text_width(drawn.replace("&amp;", "&")), 220, drawn)

    def test_fit_keeps_short_labels_untouched(self) -> None:
        self.assertEqual(charts.fit("Data engine", 220), "Data engine")

    def test_labels_are_escaped(self) -> None:
        svg = charts.horizontal_bars("T", [("A & B <x>", 1)])
        self.assertIn("A &amp; B &lt;x&gt;", svg)


class OutputArtefactsTest(unittest.TestCase):
    def test_latest_json_is_valid_when_present(self) -> None:
        path = ROOT / "radar" / "latest.json"
        if not path.exists():
            self.skipTest("latest.json not generated yet")
        payload = json.loads(path.read_text(encoding="utf-8"))
        for key in ("date", "generated", "picked", "lane_counts", "evidence_mix"):
            self.assertIn(key, payload)

    def test_readmes_contain_markers(self) -> None:
        for name in ("README.md", "README.en.md", "README.ja.md"):
            text = (ROOT / name).read_text(encoding="utf-8")
            self.assertIn("<!-- RADAR:START -->", text)
            self.assertIn("<!-- RADAR:END -->", text)


if __name__ == "__main__":
    unittest.main()


class LabelWidthTests(unittest.TestCase):
    """A CJK label is about twice as wide as its character count suggests."""

    def test_a_cjk_label_measures_wider_than_a_latin_one_of_the_same_length(self):
        latin = charts.text_width("abcdefgh")
        cjk = charts.text_width("具身智能基础模型")
        self.assertGreater(cjk, latin * 1.6, "full-width glyphs must not be counted as narrow")

    def test_fit_respects_the_budget_for_every_script(self):
        for label in ("foundation models and world models", "具身智能基础模型与世界模型",
                      "ロボット基盤モデルと世界モデル", "mixed 混合 label"):
            trimmed = charts.fit(label, 120.0)
            self.assertLessEqual(charts.text_width(trimmed), 120.0, label)
            self.assertTrue(trimmed == label or trimmed.endswith("…"), label)


class LocalizedChartTests(unittest.TestCase):
    """Each language gets its own chart set, with labels in that language."""

    def test_a_run_writes_one_set_per_language_and_the_english_alias(self):
        with tempfile.TemporaryDirectory() as raw:
            out = Path(raw)
            cli.run(offline=True, day="2026-03-05", write_readme=False, out=out)
            for name in ("lane-distribution", "cadence", "evidence-mix"):
                self.assertTrue((out/"assets"/f"{name}.svg").exists(), f"{name}: published pages link here")
                for lang in LANGS:
                    self.assertTrue((out/"assets"/f"{name}.{lang}.svg").exists(), f"{name}.{lang}")
            zh = (out/"assets"/"lane-distribution.zh.svg").read_text(encoding="utf-8")
            en = (out/"assets"/"lane-distribution.en.svg").read_text(encoding="utf-8")
            self.assertTrue(any("\u4e00" <= ch <= "\u9fff" for ch in zh), "the zh chart must carry Han glyphs")
            self.assertNotIn("Foundation models", zh, "the zh chart must not fall back to English")
            self.assertIn("Foundation models", en)


class LabelFitTests(unittest.TestCase):
    """A truncated lane label is a content problem, not a rendering detail."""

    def test_no_lane_label_needs_trimming_in_any_language(self):
        config = load_config()
        gutter = charts.LABEL_GUTTER if hasattr(charts, "LABEL_GUTTER") else 230.0
        for lane in config.taxonomy["lanes"]:
            for lang in LANGS:
                label = config.chart_label(lane["id"], lang)
                self.assertEqual(charts.fit(label, gutter - 10), label,
                                 f"{lane['id']} ({lang}) does not fit: give it a short form "
                                 f"in data/taxonomy.json rather than letting the chart cut it")


class ArxivFallbackTests(unittest.TestCase):
    """The Atom API answered 406 to everything from this host; the feeds took over."""

    FEED = """<?xml version='1.0' encoding='UTF-8'?>
<rss version="2.0"><channel>
  <title>cs.RO updates on arXiv.org</title>
  <pubDate>Mon, 21 Sep 2026 00:00:00 -0400</pubDate>
  <item>
    <title>A policy that reports its own latency</title>
    <link>https://arxiv.org/abs/2609.01234</link>
    <description>We measure 12 ms per control step.</description>
  </item>
</channel></rss>"""
    REVISED = """<?xml version='1.0' encoding='UTF-8'?>
<rss xmlns:arxiv="http://arxiv.org/schemas/atom" version="2.0"><channel>
  <title>cs.RO updates on arXiv.org</title>
  <pubDate>Thu, 24 Sep 2026 00:00:00 -0400</pubDate>
  <item>
    <title>A new policy</title>
    <link>https://arxiv.org/abs/2609.25376</link>
    <description>arXiv:2609.25376v1 Announce Type: new
Abstract: We quantize a VLA.</description>
    <pubDate>Thu, 24 Sep 2026 00:00:00 -0400</pubDate>
    <arxiv:announce_type>new</arxiv:announce_type>
  </item>
  <item>
    <title>An old paper, revised</title>
    <link>https://arxiv.org/abs/2512.24310</link>
    <description>arXiv:2512.24310v4 Announce Type: replace
Abstract: We introduce an ecosystem.</description>
    <pubDate>Thu, 24 Sep 2026 00:00:00 -0400</pubDate>
    <arxiv:announce_type>replace</arxiv:announce_type>
  </item>
  <item>
    <title>An old cross-list, revised</title>
    <link>https://arxiv.org/abs/2511.00001</link>
    <description>arXiv:2511.00001v2 Announce Type: replace-cross
Abstract: Older still.</description>
    <pubDate>Thu, 24 Sep 2026 00:00:00 -0400</pubDate>
  </item>
</channel></rss>"""
    EMPTY = """<?xml version='1.0' encoding='UTF-8'?>
<rss version="2.0"><channel><title>cs.RO</title>
  <pubDate>Sun, 20 Sep 2026 00:00:00 -0400</pubDate>
  <skipDays><day>Sunday</day><day>Saturday</day></skipDays>
</channel></rss>"""

    def test_an_item_without_its_own_date_takes_the_announcement_day(self):
        cfg = {"rss_categories": ["cs.RO"], "evidence": "R"}
        with unittest.mock.patch.object(fetch, "http_get", return_value=self.FEED.encode()):
            items, answered = fetch.fetch_arxiv_rss(cfg, date(2026, 9, 18))
        self.assertTrue(answered)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].id, "arxiv:2609.01234")
        self.assertEqual(items[0].published, "2026-09-21")
        self.assertEqual(items[0].evidence, "R")

    def test_a_weekend_feed_answers_with_nothing_and_is_not_a_failure(self):
        cfg = {"rss_categories": ["cs.RO"], "evidence": "R"}
        with unittest.mock.patch.object(fetch, "http_get", return_value=self.EMPTY.encode()):
            items, answered = fetch.fetch_arxiv_rss(cfg, date(2026, 9, 18))
        self.assertEqual(items, [])
        self.assertTrue(answered, "arXiv declares skipDays; a closed archive is not an outage")

    def test_a_revised_old_paper_is_not_todays_news(self):
        cfg = {"rss_categories": ["cs.RO"], "evidence": "R"}
        with unittest.mock.patch.object(fetch, "http_get", return_value=self.REVISED.encode()):
            items, answered = fetch.fetch_arxiv_rss(cfg, date(2026, 9, 21))
        self.assertTrue(answered)
        self.assertEqual([item.id for item in items], ["arxiv:2609.25376"],
                         "replace and replace-cross re-announce old papers with today's date")
        self.assertEqual(items[0].summary, "We quantize a VLA.",
                         "the announce boilerplate would otherwise lead every excerpt")
