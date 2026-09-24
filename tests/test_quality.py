"""Relevance, balance and repeat-guard checks against recorded feed payloads.

The fixtures under tests/fixtures/ are trimmed copies of what the configured feeds
returned on 2026-09-25 (descriptions shortened, unused elements dropped). They
reproduce the problems a review of the published pages found:

- a GeForce NOW launch, "AI Day Singapore" and an oMLX hiring post were filed under
  RL training because "ppo" sits inside "support" and "opportunity";
- a coding-agent memory post and an LLM benchmark post were filed under systems and
  evaluation because those words are not specific to robots;
- on a day when the arXiv API refused every request and the category feeds took
  over, the shortlist came back all `[R]`;
- the API names a paper `http://arxiv.org/abs/IDvN`, the feeds `https://.../abs/ID`,
  and the seven-day repeat guard compared the strings.

No test here touches the network.
"""
from __future__ import annotations

import contextlib
import io
import json
import re
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT/"tests"/"fixtures"
sys.path.insert(0, str(ROOT))

from pairadar import fetch  # noqa: E402
from pairadar.config import LANGS, Item, load_config  # noqa: E402
from pairadar.distill import (  # noqa: E402
    count_terms,
    deduplicate,
    detect_signals,
    enrich,
    evidence_mix,
    lane_distribution,
    prefilter,
    qualifies,
    select,
    url_key,
)
from pairadar.render import item_block, readme_block, render_daily  # noqa: E402

DAY = date(2026, 9, 25)
RECORDED = {
    "https://blogs.nvidia.com/feed/": "nvidia-blog.rss",
    "https://huggingface.co/blog/feed.xml": "huggingface-blog.rss",
    "https://spectrum.ieee.org/feeds/topic/robotics.rss": "ieee-spectrum-robotics.rss",
    "https://aws.amazon.com/blogs/physical-ai/feed/": "aws-physical-ai.rss",
    "https://rss.arxiv.org/rss/cs.RO": "arxiv-cs.RO.rss",
    "https://rss.arxiv.org/rss/cs.LG": "arxiv-cs.LG.rss",
}
OFF_TOPIC = (
    "CONTROL Resonant",          # a cloud-gaming launch
    "AI Day Singapore",          # a regional event round-up
    "oMLX creator",              # a hiring announcement
    "Coding Agents a Memory",    # software agents, not robots
    "Vera Rubin NVL72",          # data-centre inference benchmark
    "RL Starts before RL",       # an LLM post-training paper from cs.LG
    "ChipMEM",                   # EDA agents, cs.LG
)


def recorded(url: str, *args, **kwargs) -> bytes | None:
    """Serve the recorded payload for a URL; the arXiv API answers nothing, as it did."""
    name = RECORDED.get(url)
    return (FIXTURES/name).read_bytes() if name else None


@contextlib.contextmanager
def recorded_clock():
    """Pin "now" to the recording day, so the arXiv lookback keeps the fixtures in range."""
    with patch.object(fetch, "datetime", wraps=fetch.datetime) as clock:
        clock.now.return_value = fetch.datetime(2026, 9, 25, 1, 40, tzinfo=fetch.timezone.utc)
        yield


def recorded_day(config) -> list[Item]:
    with patch.object(fetch, "http_get", recorded), patch.object(fetch.time, "sleep"), \
            recorded_clock(), contextlib.redirect_stdout(io.StringIO()):
        report = fetch.fetch_all(config)
    return deduplicate(enrich(prefilter(report.items, config, DAY), config, DAY))


def shortlist(config, items: list[Item], seen=()) -> list[Item]:
    filters = config.sources["filters"]
    return select(items, limit=8, seen=seen, per_source=filters["per_source"],
                  per_evidence=filters["per_evidence"])


class WholeTermMatchingTest(unittest.TestCase):
    """A keyword is a term, not a substring."""

    def test_short_keywords_do_not_fire_inside_other_words(self) -> None:
        cases = {
            "ppo": "We support new opportunities for partners.",
            "thor": "The author thanks the authority.",
            "droid": "Runs on Android phones.",
            "orin": "Exploring the market.",
            "bom": "A bombastic claim.",
            "vla": "The Vlasov equation.",
        }
        for keyword, text in cases.items():
            with self.subTest(keyword=keyword):
                self.assertEqual(count_terms(text, [keyword]), 0)

    def test_whole_terms_and_plurals_still_count(self) -> None:
        self.assertEqual(count_terms("Two humanoids and three benchmarks.", ["humanoid", "benchmark"]), 2)
        self.assertEqual(count_terms("PPO fine-tuning on Jetson Thor, DROID data.", ["ppo", "thor", "droid"]), 3)
        self.assertEqual(count_terms("GR00T N1.7 and π0.5 policies", ["gr00t", "π0.5"]), 2)

    def test_the_latency_signal_needs_a_latency_claim(self) -> None:
        taxonomy = load_config().taxonomy
        self.assertNotIn("latency", detect_signals("Systems and platforms for teams.", taxonomy),
                         '"ms" inside "systems" used to count as a latency figure')
        for text in ("Runs at 12 ms per step.", "Control at 50Hz.", "Low latency inference."):
            with self.subTest(text=text):
                self.assertIn("latency", detect_signals(text, taxonomy))


class RecordedDayTest(unittest.TestCase):
    """The 2026-09-25 feeds, with the arXiv API refusing and the category feeds answering."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.config = load_config()
        cls.items = recorded_day(cls.config)
        cls.picked = shortlist(cls.config, cls.items)

    def titles(self, items) -> list[str]:
        return [item.title for item in items]

    def test_off_topic_posts_do_not_qualify(self) -> None:
        for fragment in OFF_TOPIC:
            matches = [item for item in self.items if fragment in item.title]
            with self.subTest(fragment=fragment):
                self.assertEqual(len(matches), 1, "the fixture should carry this item")
                self.assertFalse(qualifies(matches[0], 0.0), f"{matches[0].title} qualified")

    def test_physical_ai_posts_from_general_blogs_still_qualify(self) -> None:
        for fragment in ("Isaac ROS 5.0", "Why Deploying Physical AI", "Warp and MjWarp"):
            matches = [item for item in self.items if fragment in item.title]
            with self.subTest(fragment=fragment):
                self.assertEqual(len(matches), 1)
                self.assertTrue(qualifies(matches[0], 0.9), f"{matches[0].title} was dropped")

    def test_a_fallback_day_keeps_official_and_media_items_visible(self) -> None:
        mix = evidence_mix(self.picked)
        self.assertEqual(len(self.picked), 8)
        self.assertGreaterEqual(mix["O"], 1, self.titles(self.picked))
        self.assertGreaterEqual(mix["M"], 1, self.titles(self.picked))
        per_source: dict[str, int] = {}
        for item in self.picked:
            per_source[item.source_id] = per_source.get(item.source_id, 0) + 1
        self.assertLessEqual(max(per_source.values()), self.config.sources["filters"]["per_source"])

    def test_without_the_caps_the_same_day_is_all_papers(self) -> None:
        # The regression this guards: rank alone hands the page to same-day preprints.
        uncapped = select(self.items, limit=8)
        self.assertGreater(evidence_mix(uncapped)["R"], evidence_mix(self.picked)["R"])

    def test_the_shortlist_is_in_rank_order(self) -> None:
        scores = [item.score for item in self.picked]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_every_pick_is_on_topic(self) -> None:
        for item in self.picked:
            with self.subTest(title=item.title):
                self.assertTrue(item.topical or item.anchor_hits >= 1)
                self.assertGreaterEqual(item.lane_hits, 1)

    def test_papers_only_in_general_listings_need_an_anchor(self) -> None:
        by_title = {item.title: item for item in self.items}
        robo = next(item for title, item in by_title.items() if title.startswith("DreamStream"))
        llm = next(item for title, item in by_title.items() if title.startswith("RL Starts before RL"))
        self.assertTrue(robo.topical, "a cs.RO listing is on-topic by construction")
        self.assertFalse(llm.topical, "a cs.LG listing is not")


class ArxivIdentityTest(unittest.TestCase):
    """One paper, one key, whichever arXiv endpoint answered."""

    def test_url_key_folds_scheme_version_and_view(self) -> None:
        variants = [
            "http://arxiv.org/abs/2609.25031v1",
            "https://arxiv.org/abs/2609.25031",
            "https://arxiv.org/abs/2609.25031v3/",
            "https://arxiv.org/pdf/2609.25031v2",
            "https://arxiv.org/html/2609.25031v1",
            "https://export.arxiv.org/abs/2609.25031",
        ]
        self.assertEqual({url_key(url) for url in variants}, {"https://arxiv.org/abs/2609.25031"})

    def test_url_key_is_idempotent_and_leaves_other_hosts_alone(self) -> None:
        for url in ("http://arxiv.org/abs/2609.25031v1", "https://Blogs.nvidia.com/blog/x/?utm=1#top"):
            self.assertEqual(url_key(url_key(url)), url_key(url))
        self.assertEqual(url_key("https://Blogs.nvidia.com/blog/x/?utm=1#top"), "https://blogs.nvidia.com/blog/x")
        self.assertEqual(url_key("http://spectrum.ieee.org/a"), "https://spectrum.ieee.org/a")

    def test_api_and_feed_produce_the_same_item(self) -> None:
        config = load_config()
        payload = (FIXTURES/"arxiv-api.atom").read_bytes()
        with patch.object(fetch, "http_get", lambda url, *a, **k: payload), \
                patch.object(fetch.time, "sleep"), recorded_clock():
            api_items, answered = fetch.fetch_arxiv(config)
        self.assertTrue(answered)
        with patch.object(fetch, "http_get", recorded), patch.object(fetch.time, "sleep"):
            feed_items, _ = fetch.fetch_arxiv_rss({"rss_categories": ["cs.RO"]}, date(2026, 9, 20))
        api = api_items[0]
        feed = next(item for item in feed_items if item.id == api.id)
        self.assertEqual(api.id, "arxiv:2609.25031")
        self.assertEqual(api.url, feed.url)
        self.assertEqual(api.url, "https://arxiv.org/abs/2609.25031")

    def test_the_repeat_guard_recognises_a_paper_first_seen_through_the_api(self) -> None:
        config = load_config()
        items = recorded_day(config)
        paper = next(item for item in items if item.id == "arxiv:2609.25031")
        # What radar/history.json stored for API-sourced picks before this change.
        seen = ["http://arxiv.org/abs/2609.25031v1"]
        self.assertIn(paper, select(items, limit=50, per_lane=50, min_score=0.0))
        self.assertNotIn(paper, select(items, limit=50, per_lane=50, min_score=0.0, seen=seen))

    def test_a_cross_listed_paper_is_one_item_and_on_topic(self) -> None:
        feed = (FIXTURES/"arxiv-cs.RO.rss").read_bytes()
        with patch.object(fetch, "http_get", lambda url, *a, **k: feed), patch.object(fetch.time, "sleep"):
            items, _ = fetch.fetch_arxiv_rss({"rss_categories": ["cs.LG", "cs.RO"]}, date(2026, 9, 20))
        ids = [item.id for item in items]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(item.topical for item in items),
                        "listed in cs.RO anywhere makes the paper on-topic")


class SoftCapTest(unittest.TestCase):
    """Caps reserve room for other sources; they never leave a slot empty."""

    def make(self, index: int, source: str, evidence: str, lane: str, score: float) -> Item:
        item = Item(id=f"{source}:{index}", title=f"item {source} {index}",
                    url=f"https://example.org/{source}/{index}", publisher=source,
                    source_id=source, evidence=evidence, published="2026-09-25")
        item.lane, item.lane_hits, item.anchor_hits, item.score = lane, 2, 1, score
        return item

    def test_capped_sources_yield_to_others_then_fill_what_is_left(self) -> None:
        lanes = ["foundation", "data", "training", "simeval", "edge", "safety", "systems", "hardware"]
        papers = [self.make(n, "arxiv", "R", lanes[n], 5.0 - n * 0.1) for n in range(8)]
        post = self.make(0, "blog", "O", "hardware", 1.0)
        picked = select(papers + [post], limit=5, per_source=3, per_evidence=None)
        self.assertIn(post, picked, "the cap should make room for a weaker item from another source")
        self.assertEqual(len(picked), 5, "and slots nobody else wanted are filled from the capped source")
        self.assertEqual([item.score for item in picked], sorted((item.score for item in picked), reverse=True))

    def test_the_lane_cap_stays_hard(self) -> None:
        papers = [self.make(n, "arxiv", "R", "foundation", 5.0 - n) for n in range(5)]
        self.assertEqual(len(select(papers, limit=5, per_lane=2, per_source=1)), 2)

    def test_the_evidence_cap(self) -> None:
        lanes = ["foundation", "data", "training", "simeval", "edge", "safety"]
        items = [self.make(n, f"src{n}", "R", lanes[n], 5.0 - n) for n in range(4)]
        items.append(self.make(9, "press", "M", "hardware", 0.95))
        picked = select(items, limit=3, per_evidence=2)
        self.assertEqual(evidence_mix(picked), {"O": 0, "R": 2, "M": 1})


class ReadabilityTest(unittest.TestCase):
    """What a reader sees: an honest window and labelled signal tags."""

    def setUp(self) -> None:
        self.config = load_config()
        self.item = Item(id="arxiv:1", title="A closed-loop VLA on a real robot",
                         url="https://arxiv.org/abs/2609.00001", publisher="arXiv 2609.00001",
                         source_id="arxiv", evidence="R", published="2026-09-25",
                         summary="We report 87.5% success rate at 23 Hz on a real robot.")
        enrich([self.item], self.config, DAY)
        detail = {"day": "2026-09-25", "papers_from": "2026-09-22", "posts_from": "2026-08-26",
                  "repeat_days": 7}
        self.ctx = {
            "date": "2026-09-25", "generated": "2026-09-25 01:40 UTC",
            "window": "papers 2026-09-22 → 2026-09-25 · posts 2026-08-26 → 2026-09-25 (UTC)",
            "window_detail": detail, "picked": [self.item], "baseline": [], "baseline_all": [],
            "curated": {}, "lane_rows": lane_distribution([self.item], self.config),
            "mix": evidence_mix([self.item]), "cadence": [("2026-09-25", 1)],
        }

    def test_the_window_names_both_horizons_in_every_language(self) -> None:
        for lang in LANGS:
            for text in (render_daily(self.config, lang, self.ctx), readme_block(self.config, lang, self.ctx)):
                with self.subTest(lang=lang):
                    self.assertIn("2026-09-22 → 2026-09-25", text)
                    self.assertIn("2026-08-26 → 2026-09-25", text,
                                  "feed posts up to max_age_days old are eligible and must be stated")

    def test_an_older_snapshot_keeps_its_stored_window(self) -> None:
        ctx = dict(self.ctx, window="2026-09-18 → 2026-09-21 (UTC)", window_detail=None)
        self.assertIn("2026-09-18 → 2026-09-21 (UTC)", render_daily(self.config, "en", ctx))

    def test_signal_tags_carry_a_label(self) -> None:
        for lang in LANGS:
            ui = self.config.ui(lang)
            block = "\n".join(item_block(self.item, self.config, lang, {}, 1))
            with self.subTest(lang=lang):
                self.assertIn(f"**{ui['signals_label']}**: ", block)
                self.assertIn(f"`{ui['signals']['closed_loop']}`", block)
                self.assertNotRegex(block, r"(?m)^- (?!\*\*)", "every bullet starts with its label")

    def test_a_note_keyed_by_the_legacy_api_link_still_applies(self) -> None:
        notes = {"notes": {"http://arxiv.org/abs/2609.00001v1": {"en": "Drafted line."}}}
        block = "\n".join(item_block(self.item, self.config, "en", {}, 1, notes))
        self.assertIn("Drafted line.", block)


class FeedHygieneTest(unittest.TestCase):
    def feed(self, entries: int, summary: str = "robot") -> bytes:
        items = "".join(
            f"<item><title>Post {n}</title><link>https://example.org/{n}</link>"
            f"<description>{summary}</description>"
            f"<pubDate>Thu, {1 + n % 28:02d} Sep 2026 00:00:00 +0000</pubDate></item>"
            for n in range(entries)
        )
        return f"<rss version='2.0'><channel><title>t</title>{items}</channel></rss>".encode()

    def config_with(self, payload: bytes):
        config = load_config()
        sources = dict(config.sources, feeds=[{"id": "big", "url": "https://example.org/feed",
                                                "evidence": "O", "weight": 1.0}])
        return type(config)(sources=sources, taxonomy=config.taxonomy,
                            glossary=config.glossary, baseline=config.baseline)

    def test_an_archive_feed_is_cut_to_its_newest_entries(self) -> None:
        payload = self.feed(300)
        config = self.config_with(payload)
        with patch.object(fetch, "http_get", lambda url, *a, **k: payload):
            items, failed = fetch.fetch_feeds(config)
        cap = config.sources["filters"]["max_entries_per_feed"]
        self.assertEqual(failed, [])
        self.assertEqual(len(items), cap)
        self.assertEqual(max(item.published for item in items), "2026-09-28")

    def test_full_text_descriptions_are_cut_to_abstract_length(self) -> None:
        payload = self.feed(1, summary="robot arm " * 2000)
        with patch.object(fetch, "http_get", lambda url, *a, **k: payload):
            items, _ = fetch.fetch_feeds(self.config_with(payload))
        self.assertLessEqual(len(items[0].summary), fetch.MAX_SUMMARY_CHARS + 2)

    def test_general_feeds_are_not_marked_topical(self) -> None:
        feeds = {feed["id"]: feed for feed in load_config().sources["feeds"]}
        for general in ("nvidia-blog", "huggingface-blog", "deepmind"):
            self.assertFalse(feeds[general].get("topical", False), general)


class BadgeHonestyTest(unittest.TestCase):
    """The READMEs' badges must describe how the radar is actually published."""

    def publish_time_utc(self) -> str:
        text = (ROOT/"docs"/"automation.md").read_text(encoding="utf-8")
        row = next(line for line in text.splitlines() if line.startswith("| `scripts/publish_daily.sh`"))
        hour, minute = re.search(r"(\d{2}):(\d{2}) Asia/Singapore", row).groups()
        return f"{(int(hour) - 8) % 24:02d}:{minute}"

    def test_no_badge_reports_a_manual_only_workflow_as_the_daily_run(self) -> None:
        workflows = ROOT/".github"/"workflows"
        manual = {path.name for path in workflows.glob("*.yml")
                  if "schedule:" not in path.read_text(encoding="utf-8")
                  and "pull_request" not in path.read_text(encoding="utf-8")}
        for name in ("README.md", "README.en.md", "README.ja.md"):
            text = (ROOT/name).read_text(encoding="utf-8")
            for workflow in manual:
                with self.subTest(readme=name, workflow=workflow):
                    self.assertNotIn(f"workflows/{workflow}/badge.svg", text)

    def test_the_schedule_badge_matches_the_publisher(self) -> None:
        expected = self.publish_time_utc().replace(":", "%3A")
        for name in ("README.md", "README.en.md", "README.ja.md"):
            text = (ROOT/name).read_text(encoding="utf-8")
            with self.subTest(readme=name):
                self.assertIn(f"daily%20{expected}%20UTC", text)


class DataFreshnessTest(unittest.TestCase):
    def test_anchor_terms_are_declared_and_lowercase(self) -> None:
        taxonomy = json.loads((ROOT/"data"/"taxonomy.json").read_text(encoding="utf-8"))
        anchors = taxonomy["anchors"]
        self.assertGreater(len(anchors), 20)
        self.assertEqual(anchors, [anchor.lower() for anchor in anchors])
        for lane in taxonomy["lanes"]:
            self.assertEqual(len(lane["keywords"]), len(set(lane["keywords"])), lane["id"])


if __name__ == "__main__":
    unittest.main()
