"""Tests for the Physical AI Radar pipeline (standard library unittest only)."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from datetime import date
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pairadar import charts, cli, health  # noqa: E402
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
from pairadar.render import readme_block, render_daily  # noqa: E402


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
                    self.config.glossary["why_templates"][lane["id"]].get(lang),
                    f"why_template {lane['id']} missing {lang}",
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
        self.assertIn("89.7%", found)
        self.assertTrue(any("ms" in value for value in found))
        self.assertTrue(any("Hz" in value for value in found))

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

    def test_arxiv_uses_https_and_spaces_its_queries(self) -> None:
        # arXiv asks for roughly three seconds between API calls.
        urls: list[str] = []
        with patch.object(fetch_module, "http_get", lambda url: urls.append(url) or b"<feed/>"), \
                patch.object(fetch_module.time, "sleep") as sleep:
            items, answered = fetch_arxiv(self.config)
        queries = len(self.config.sources["arxiv"]["queries"])
        self.assertEqual(items, [])
        self.assertTrue(answered)
        self.assertEqual(len(urls), queries)
        self.assertTrue(all(url.startswith("https://") for url in urls), urls)
        self.assertTrue(ARXIV_ENDPOINT.startswith("https://"))
        self.assertEqual(sleep.call_count, queries - 1)

    def test_total_outage_is_reported_by_the_fetch_report(self) -> None:
        with patch.object(fetch_module, "http_get", lambda url, *a, **k: None), \
                patch.object(fetch_module.time, "sleep"):
            report = fetch_all(self.config)
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
        self.assertEqual(health.struggling(self.RUNS, self.TODAY, threshold=1),
                         [("deepmind", 4), ("nvidia-blog", 1)])
        self.assertEqual(health.struggling(self.RUNS, self.TODAY, threshold=5), [])

    def test_only_runs_that_recorded_health_are_counted(self) -> None:
        # Entries written before health was recorded must not read as healthy days.
        self.assertEqual(health.days_observed(self.RUNS, self.TODAY), 5)
        self.assertEqual(health.days_observed([{"date": "2026-09-19", "count": 8}], self.TODAY), 0)

    def test_cli_exits_non_zero_only_when_asked_and_only_when_struggling(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)/"history.json"
            path.write_text(json.dumps({"runs": self.RUNS}), encoding="utf-8")
            common = ["--history", str(path), "--date", "2026-09-19"]
            self.assertEqual(health.main(common), 0, "the daily run must stay green")
            self.assertEqual(health.main([*common, "--fail-on-struggling"]), 1)
            self.assertEqual(health.main([*common, "--fail-on-struggling", "--threshold", "9"]), 0)

    def test_a_run_records_which_sources_did_not_answer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary)
            cli.run(offline=True, day="2026-03-05", out=out)
            runs = json.loads((out/"radar"/"history.json").read_text(encoding="utf-8"))["runs"]
        # The log is date-sorted, so look the run up rather than taking the last entry.
        entry = next(run for run in runs if run["date"] == "2026-03-05")
        self.assertIn("failed", entry, "health has to be recorded to be readable later")
        self.assertEqual(entry["failed"], [], "an offline run attempts no source")


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

    def test_a_missing_language_falls_back_to_the_template(self) -> None:
        partial = {**self.notes, "notes": {url_key(self.item.url): {"en": "English only"}}}
        page = render_daily(self.config, "zh", self.context(partial))
        self.assertIn(self.config.glossary["why_templates"][self.item.lane]["zh"], page)
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
            self.assertIn("assets/lane-distribution.svg", page)
            self.assertIn("http", page)
            self.assertGreater(len(page), 1200)

    def test_evidence_tags_present(self) -> None:
        page = render_daily(self.config, "zh", self.ctx)
        self.assertTrue(any(tag in page for tag in ("`[O]`", "`[R]`", "`[M]`")))


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
