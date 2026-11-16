"""Industry events get reserved slots. A post that reports funding, an acquisition, a listing, a production
milestone or a policy action used to score 0.7 to 1.0 against 2.0 to 2.7 for papers, so a day's eight
picks were papers and posts about the tool chain. `event` in `filters.highlights.reserve` reserves slots for
the best such posts; a paper never takes one. No test here touches the network.
"""
from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pairadar import distill  # noqa: E402
from pairadar.config import Item, load_config  # noqa: E402
from pairadar.distill import detect_signals, enrich, select  # noqa: E402

CONFIG = load_config()
DAY = date(2026, 11, 16)


def make(source: str, evidence: str, title: str, summary: str) -> Item:
    return Item(id=f"{source}:{title}", title=title, url=f"https://example.org/{abs(hash(title))}",
                publisher=source, source_id=source, evidence=evidence, published="2026-11-15", summary=summary,
                topical=True)


class IndustryEventTest(unittest.TestCase):
    # Headlines as the feeds and trade press printed them (2026-11-16 pool; the radar's own gates, not these
    # strings, decide the topic). What is an event: funding, M&A, a listing, a production start, policy, a counted
    # deployment milestone. What is not: a tool, a paper, a podcast, a product feature.
    EVENTS = (
        "Tesla starts Optimus robot production in California as profit falls 43%",
        "Axon invests $1.2B into Skydio and becomes its U.S. public safety distributor",
        "Physicl raises $10M to make the digital world real enough for robots",
        "Agility is the first pure humanoid robot company on the stock market. Are you buying?",
        "Six AGIBOT G2 Robots Complete 1,730 Tasks in 24-Hour Shanghai Convenience Store Trial",
        "Agility Robotics lists on Nasdaq after a SPAC merger",
        "Zipline closes a funding round at a $21.5 billion valuation",
        "White House issues a Section 232 finding on foreign-made robots",
        "SoftBank to acquire a robotics company",
        "启元机器人累计下线10000台，进入量产爬坡", "舍弗勒收购一家机器人公司", "智元机器人完成新一轮融资",
        "中国否决软银54亿美元收购ABB Robotics",
    )
    NOT_EVENTS = (
        "Gemini Robotics 2.5 accelerates real-world general dexterity",
        "Run Native DLA Inference in Python on NVIDIA Jetson AGX Orin Using cuDLA",
        "Adapt Human Motion for Robots with NVIDIA SOMA Retargeter",
        "Video Friday: NASA PROMISEs a New Moon Rover",
        "Robot Talk Episode 170 - Robots in the real world, with Daniela Shchegelskaya",
        "Korea Advances Physical AI Across Industries With NVIDIA",
        "Can Agentic Robotics Solve Robotics' Data-Scaling Problem?",
        "A diffusion policy for dexterous manipulation with 40 ms latency",
        "We evaluate closed-loop success on a real robot",
        "Isaac Lab 3.0 adds a new contact solver for humanoid training",
        "Laplace Intelligence red-teams robots and self-driving cars before they hit the real world",
        "Boston Dynamics adds wheels to Atlas humanoid to test whole-body control",
        "40 亿参数模型拿下 6 个评测第一，西湖大学团队押注概念世界模型",
        "Robots raises concerns about safety in the workplace",
    )

    def test_headlines_that_report_events_are_flagged(self) -> None:
        for text in self.EVENTS:
            with self.subTest(text=text):
                self.assertIn("industry_event", detect_signals(text, CONFIG.taxonomy))

    def test_tools_papers_and_features_are_not_flagged(self) -> None:
        for text in self.NOT_EVENTS:
            with self.subTest(text=text):
                self.assertNotIn("industry_event", detect_signals(text, CONFIG.taxonomy))

    def test_a_low_scoring_event_post_clears_only_the_event_score_bar(self) -> None:
        post = make("techcrunch-robotics", "M", "Humanoid robot maker closes Series B funding round", "A humanoid robot company announced a Series B funding round")
        enrich([post], CONFIG, DAY)
        post.score = 0.7      # above event_min_score (0.6), below min_score (0.9)
        plain = make("robohub", "M", "Robot Talk episode about humanoid robot design", "A conversation about humanoid robot design")
        enrich([plain], CONFIG, DAY)
        plain.score = 0.7
        self.assertEqual(select([post, plain], limit=8, reserve=["event"]), [])
        picked = select([post, plain], limit=8, reserve=["event"], event_min_score=0.6)
        self.assertEqual([item.title for item in picked], [post.title], "only the event post gets the lower bar")

    def test_the_shipped_event_score_bar_is_below_the_general_one(self) -> None:
        filters = CONFIG.sources["filters"]
        self.assertLess(filters["highlights"]["event_min_score"], 0.9)

    def test_a_paper_never_earns_the_event_bonus(self) -> None:
        def scores() -> tuple[float, float]:
            paper = make("arxiv", "R", "Robot policy evaluation with valuation", "A robot manipulation policy with valuation and merger terms")
            post = make("robohub", "M", "Humanoid robot maker closes funding round", "A robot humanoid company announced a funding round and a valuation")
            enrich([paper, post], CONFIG, DAY)
            self.assertIn("industry_event", paper.signals)
            self.assertIn("industry_event", post.signals)
            return paper.score, post.score

        with_bonus = scores()
        saved = dict(distill.SIGNAL_BONUS)
        try:
            distill.SIGNAL_BONUS["industry_event"] = 0.0
            without_bonus = scores()
        finally:
            distill.SIGNAL_BONUS.clear()
            distill.SIGNAL_BONUS.update(saved)
        self.assertAlmostEqual(with_bonus[0], without_bonus[0], places=6)       # the paper is unchanged
        self.assertAlmostEqual(with_bonus[1] - without_bonus[1], 0.5, places=6)  # the post gains the bonus

    def test_reserved_event_slots_go_to_posts_and_never_to_papers(self) -> None:
        pool = [make("arxiv", "R", f"Robot manipulation policy {n}", "A robot manipulation policy with closed-loop success rate 91% on a real robot") for n in range(8)]
        pool += [make("robohub", "M", "Humanoid robot maker closes Series B funding round", "A humanoid robot company announced a Series B funding round at a valuation"),
                 make("therobotreport", "M", "Robot maker starts mass production of a humanoid", "A humanoid robot manufacturer began mass production this week")]
        enrich(pool, CONFIG, DAY)
        report: dict = {}
        picked = select(pool, limit=8, per_lane=2, per_source=3, per_evidence=4, max_arxiv=4,
                        reserve=["official", "hot", "event", "event"], report=report)
        titles = [item.title for item in picked]
        self.assertTrue(any("Series B" in title for title in titles), titles)
        self.assertTrue(any("mass production" in title for title in titles), titles)
        self.assertEqual(report["reserved"]["event"] is not None and report["reserved"]["event#2"] is not None, True, report["reserved"])
        self.assertLessEqual(report["arxiv"], 4)

    def test_an_unfilled_second_slot_stays_empty_and_is_reported(self) -> None:
        pool = [make("robohub", "M", "Humanoid robot maker closes Series B funding round", "A humanoid robot company announced a Series B funding round")]
        enrich(pool, CONFIG, DAY)
        report: dict = {}
        select(pool, limit=8, reserve=["event", "event"], report=report)
        self.assertIsNotNone(report["reserved"]["event"])
        self.assertIsNone(report["reserved"]["event#2"])
        self.assertIn("event#2", report["unmet"])

    def test_the_shipped_configuration_reserves_two_event_slots(self) -> None:
        reserve = CONFIG.sources["filters"]["highlights"]["reserve"]
        self.assertEqual(reserve.count("event"), 2)

    def test_the_new_signal_has_a_label_in_every_language(self) -> None:
        for lang in ("zh", "en", "ja"):
            with self.subTest(lang=lang):
                self.assertTrue(CONFIG.ui(lang)["signals"]["industry_event"])


if __name__ == "__main__":
    unittest.main()
