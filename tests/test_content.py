"""What a reader gets out of each item: its line, its figures, its excerpt, its count.

A review of the pages published 2026-09-19 → 09-25 found:

- "why it matters" was one sentence per lane: 48 lines, 8 distinct, and the README
  opened with two identical ones;
- figures were bare (`48.1% · 68.9%`), with nothing saying what they measure;
- excerpts quoted feed furniture ("The post … appeared first on The Robot Report",
  the Video Friday preamble);
- NVIDIA's Isaac ROS 5.0 post and The Robot Report's retelling both took a slot;
- the W39 weekly page said 8 picks where 32 had been published, because the store
  began on 09-25;
- the lane chart counted the 29 curated baseline entries with the day's picks, and
  every daily page embedded the shared charts, so an old page showed today's bars.

No test here touches the network.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT/"tests"))

from pairadar import feeds  # noqa: E402
from pairadar.config import LANGS, Item, load_config  # noqa: E402
from pairadar.distill import (  # noqa: E402
    number_context,
    one_liner,
    same_story,
    select,
    story_terms,
    strip_boilerplate,
)
from pairadar.render import chart_title, composed_why, readme_block, render_daily  # noqa: E402
from test_quality import recorded_day, shortlist  # noqa: E402

DYNAFORGE = ("With 800 demonstrations per task, DP3 policies trained on DynaForge data achieve "
             "49.11% mean success, compared with 7.07% for DOMINO data.")


def item(title: str, evidence: str = "O", lane: str = "systems", score: float = 3.0,
         source: str = "blog", signals: list[str] | None = None, numbers: list[str] | None = None,
         url: str | None = None) -> Item:
    entry = Item(id=f"{source}:{title}", title=title, url=url or f"https://example.org/{abs(hash(title))}",
                 publisher=source, source_id=source, evidence=evidence, published="2026-09-25")
    entry.lane, entry.lane_hits, entry.anchor_hits, entry.score = lane, 2, 1, score
    entry.signals, entry.numbers = signals or [], numbers or []
    return entry


class BoilerplateTest(unittest.TestCase):
    def test_the_wordpress_footer_is_not_quoted(self) -> None:
        summary = ("Epson Robots has expanded its six-axis robot line with the AX6. The post Epson "
                   "introduces AX6 cobot with compact design, no-code programming appeared first on "
                   "The Robot Report .")
        self.assertEqual(strip_boilerplate(summary),
                         "Epson Robots has expanded its six-axis robot line with the AX6.")
        self.assertNotIn("appeared first", one_liner(summary))

    def test_the_video_friday_preamble_is_not_quoted(self) -> None:
        summary = ("Video Friday is your weekly selection of awesome robotics videos, collected by your "
                   "friends at IEEE Spectrum robotics. We also post a weekly calendar. Enjoy today’s "
                   "videos! The couch drag in this one is impressive.")
        self.assertEqual(one_liner(summary), "The couch drag in this one is impressive.")

    def test_the_drupal_byline_is_not_quoted(self) -> None:
        summary = ("SIRE: SE(3) Intrinsic Rigidity Embeddings robyn.cherinka… Wed, 09/09/2026 - 12:51 "
                   "Motion serves as a strong cue for segmenting rigid parts.")
        self.assertEqual(one_liner(summary), "Motion serves as a strong cue for segmenting rigid parts.")

    def test_the_chinese_byline_is_not_quoted(self) -> None:
        self.assertEqual(strip_boilerplate("机器人缺的是经验。 作者：李秋悦 编辑：吴彤 那是最荒芜的时候。"),
                         "机器人缺的是经验。那是最荒芜的时候。")
        self.assertEqual(strip_boilerplate("作者丨高允毅 编辑丨岑 峰 当 RSI 的风吹到了 DeepSeek。"),
                         "当 RSI 的风吹到了 DeepSeek。")

    def test_chinese_and_japanese_excerpts_end_on_a_sentence(self) -> None:
        zh = "具身智能最难的问题，是怎么落地。" + "机器人得有足够稳定的量产能力" * 20 + "。"
        line = one_liner(zh)
        self.assertTrue(line.startswith("具身智能最难的问题，是怎么落地。机器人"), line)
        self.assertLessEqual(len(line), 240)
        # No space to break at: the cut keeps the text instead of falling back to it.
        self.assertGreater(len(one_liner("2026 年，" + "协作机器人出货量继续增长" * 30)), 200)
        self.assertEqual(one_liner("協働ロボットの新型を発表した。価格は未定。"), "協働ロボットの新型を発表した。価格は未定。")

    def test_text_without_furniture_is_unchanged(self) -> None:
        self.assertEqual(strip_boilerplate("The post-training recipe appeared in 2025."),
                         "The post-training recipe appeared in 2025.")


class NumberContextTest(unittest.TestCase):
    def test_a_figure_is_quoted_with_what_it_measures(self) -> None:
        self.assertEqual(number_context("49.11%", DYNAFORGE), "on DynaForge data achieve 49.11% mean success")
        self.assertEqual(number_context("800 demonstrations", DYNAFORGE), "With 800 demonstrations per task")

    def test_the_quote_stops_at_the_clause(self) -> None:
        self.assertEqual(number_context("7.07%", DYNAFORGE), "compared with 7.07% for DOMINO data")
        self.assertEqual(number_context("78.37%", "It rises from 41.30% to 78.37%. Then it stops."),
                         "rises from 41.30% to 78.37%")

    def test_an_aside_in_brackets_stays_in_the_quote(self) -> None:
        text = "It cuts energy by 17.6% (Thor) and 14.4% (Orin) and latency by 24.9%."
        self.assertEqual(number_context("14.4%", text), "by 17.6% (Thor) and 14.4% (Orin) and latency")

    def test_a_figure_is_quoted_where_it_stands_alone(self) -> None:
        text = "Energy falls by 12.1-17.6% overall. On Thor it falls by 17.6% at the same accuracy."
        self.assertEqual(number_context("17.6%", text), "Thor it falls by 17.6% at the same")
        self.assertEqual(number_context("17.6%", "Energy falls by 12.1-17.6% overall."),
                         "Energy falls by 12.1-17.6% overall")

    def test_a_figure_not_in_the_text_gets_no_quote(self) -> None:
        self.assertEqual(number_context("3x", DYNAFORGE), "")

    def test_the_page_shows_the_quote_next_to_the_figure(self) -> None:
        config = load_config()
        paper = item("DynaForge", evidence="R", lane="data", numbers=["49.11%"])
        paper.summary = DYNAFORGE
        page = render_daily(config, "en", {
            "date": "2026-09-25", "generated": "2026-09-25 01:40 UTC", "window": "", "picked": [paper],
            "baseline": [], "curated": {}, "lane_rows": [], "mix": {}, "cadence": []})
        self.assertIn("`49.11%` (“on DynaForge data achieve 49.11% mean success”)", page)


class StoryTest(unittest.TestCase):
    OFFICIAL = "NVIDIA Isaac ROS 5.0 Advances Agentic, Open Source Robotics Development"
    RETOLD = "Isaac ROS 5.0 brings AI agents to robotics development, says NVIDIA"

    def test_a_retelling_is_the_same_story(self) -> None:
        self.assertTrue(same_story(story_terms(self.OFFICIAL), story_terms(self.RETOLD)))
        self.assertTrue(same_story(story_terms("Accelerating a ROS 2 Node with an AI Agent and NVIDIA Isaac ROS"),
                                   story_terms(self.RETOLD)))

    def test_posts_that_share_a_product_name_are_not(self) -> None:
        for first, second in (
            ("NVIDIA Isaac Sim 5.0 adds new sensors", "Train humanoids in Isaac Sim with fewer demos"),
            ("Video Friday: Digit Redecorates", "Video Friday: Meet Microduck"),
            ("Gemini Robotics 2 brings whole body intelligence to robots",
             "Introducing agentic video understanding with Gemini"),
        ):
            with self.subTest(first=first):
                self.assertFalse(same_story(story_terms(first), story_terms(second)))

    def test_the_higher_ranked_telling_keeps_the_slot(self) -> None:
        official = item(self.OFFICIAL, "O", "systems", 4.0, "nvidia-blog")
        retold = item(self.RETOLD, "M", "hardware", 3.0, "the-robot-report")
        other = item("ANYbotics opens the door for inspections with ANYmal robots", "M", "hardware", 2.0,
                     "the-robot-report")
        self.assertEqual(select([official, retold, other], limit=8), [official, other])

    def test_a_story_told_on_an_earlier_day_is_not_told_again(self) -> None:
        retold = item(self.RETOLD, "M", "hardware", 3.0, "the-robot-report")
        self.assertEqual(select([retold], limit=8, told=[self.OFFICIAL]), [])
        self.assertEqual(select([retold], limit=8, told=["Epson introduces AX6 cobot"]), [retold])

    def test_papers_are_never_folded_together(self) -> None:
        first = item("Dexterous Manipulation with Vision-Language-Action Models", "R", "foundation", 4.0, "arxiv")
        second = item("Vision-Language-Action Models for Dexterous Manipulation at Scale", "R", "data", 3.0,
                      "arxiv")
        self.assertEqual(select([first, second], limit=8, told=[second.title]), [first, second])


class ComposedWhyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()

    def test_a_paper_says_what_its_text_leaves_out(self) -> None:
        paper = item("P", "R", "simeval", signals=["closed_loop"], numbers=["41.30%"])
        line = composed_why(paper, self.config, "en")
        self.assertTrue(line.startswith("Preprint, not yet peer-reviewed; the text gives no real-robot"))
        self.assertNotIn("closed-loop", line)
        self.assertIn("predicts real-robot performance", line)

    def test_a_paper_with_everything_says_so(self) -> None:
        paper = item("P", "R", "training", signals=["real_robot", "closed_loop"], numbers=["24%"])
        self.assertIn("the text gives real-robot results, closed-loop results and figures",
                      composed_why(paper, self.config, "en"))

    def test_hardware_is_not_asked_for_a_closed_loop(self) -> None:
        for lang in LANGS:
            paper = item("P", "R", "hardware", signals=["real_robot"], numbers=["3 kg"])
            closed_loop = self.config.glossary["why_parts"]["checks"]["closed_loop"][lang]
            self.assertNotIn(closed_loop, composed_why(paper, self.config, lang))

    def test_every_language_composes_a_full_line(self) -> None:
        news = item("N", "M", "hardware")
        for lang in LANGS:
            with self.subTest(lang=lang):
                line = composed_why(news, self.config, lang)
                parts = self.config.glossary["why_parts"]
                self.assertIn(parts["evidence"]["M"][lang], line)
                self.assertIn(parts["watch"]["hardware"][lang], line)
                self.assertNotIn("{", line)

    def test_the_recorded_day_reads_as_different_lines(self) -> None:
        picked = shortlist(self.config, recorded_day(self.config))
        for lang in LANGS:
            lines = [composed_why(entry, self.config, lang) for entry in picked]
            with self.subTest(lang=lang):
                self.assertGreaterEqual(len(set(lines)), len(lines) - 1)
                block = readme_block(self.config, lang, {"date": "2026-09-25", "generated": "x", "window": "w",
                                                         "picked": picked, "baseline": [], "curated": {}})
                whys = [row for row in block.splitlines() if row.startswith("  - ")]
                self.assertNotEqual(whys[0], whys[1], "the README must not open with two identical lines")


class BackfillTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()

    @staticmethod
    def snapshot(day: str, *urls: str) -> dict:
        return {"date": day, "generated": f"{day} 01:40 UTC",
                "picked": [{"id": url, "title": url, "url": url, "evidence": "O", "lane": "hardware",
                            "published": day, "summary": "The post x appeared first on y."} for url in urls]}

    def test_missing_days_are_filled_and_a_repeat_stays_on_its_first_day(self) -> None:
        store = [feeds.record(Item.from_dict(self.snapshot("2026-09-25", "https://e.org/c")["picked"][0]),
                              "2026-09-25", "2026-09-25T01:40:00Z")]
        history = {"2026-09-22": self.snapshot("2026-09-22", "https://e.org/a", "https://e.org/b"),
                   "2026-09-24": self.snapshot("2026-09-24", "https://e.org/b", "https://e.org/d"),
                   "2026-09-25": self.snapshot("2026-09-25", "https://e.org/ignored")}
        filled = feeds.backfill(store, history, "2026-09-25")
        self.assertEqual([(entry["date"], entry["url"]) for entry in filled], [
            ("2026-09-25", "https://e.org/c"),
            ("2026-09-24", "https://e.org/d"),
            ("2026-09-22", "https://e.org/a"),
            ("2026-09-22", "https://e.org/b"),
        ])
        self.assertEqual(filled[1]["radar_published"], "2026-09-24T01:40:00Z")
        self.assertEqual(filled[1]["excerpt"], "")

    def test_the_week_counts_every_published_day(self) -> None:
        history = {f"2026-09-{day}": self.snapshot(f"2026-09-{day}", *(f"https://e.org/{day}/{n}" for n in range(8)))
                   for day in (21, 22, 23, 25)}
        filled = feeds.backfill([], history, "2026-09-25")
        page = feeds.render_weekly(self.config, "en", "2026-09-25", filled)
        self.assertIn("32 picks published 2026-09-21 → 2026-09-27", page)

    def test_snapshots_are_read_from_the_git_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            run = lambda *args: subprocess.run(["git", "-C", tmp, *args], check=True, capture_output=True)  # noqa: E731
            run("init", "-q")
            (repo/"radar").mkdir()
            for snapshot in (self.snapshot("2026-09-24", "https://e.org/a"),
                             self.snapshot("2026-09-24", "https://e.org/b"),
                             self.snapshot("2026-09-25", "https://e.org/c")):
                (repo/"radar"/"latest.json").write_text(json.dumps(snapshot), encoding="utf-8")
                run("add", "-A")
                run("-c", "user.name=t", "-c", "user.email=t@e.org", "commit", "-qm", snapshot["date"])
            days = feeds.snapshots(repo)
        self.assertEqual(sorted(days), ["2026-09-24", "2026-09-25"])
        self.assertEqual(days["2026-09-24"]["picked"][0]["url"], "https://e.org/b", "the re-run of a day wins")

    def test_outside_git_there_is_nothing_to_backfill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(feeds.snapshots(Path(tmp)), {})


class ChartWindowTest(unittest.TestCase):
    def test_within_counts_the_window_inclusively(self) -> None:
        store = [{"date": day} for day in ("2026-09-18", "2026-09-19", "2026-09-25", "2026-09-26")]
        self.assertEqual([entry["date"] for entry in feeds.within(store, "2026-09-25", 6)],
                         ["2026-09-19", "2026-09-25"])

    def test_a_chart_names_its_window_only_when_the_day_recorded_one(self) -> None:
        ui = load_config().ui("en")
        self.assertEqual(chart_title(ui, "Lane distribution", {"chart_days": 7}),
                         "Lane distribution (picks, last 7 days)")
        self.assertEqual(chart_title(ui, "Lane distribution", {}), "Lane distribution")

    def test_the_lane_chart_counts_published_picks_not_the_baseline(self) -> None:
        from pairadar import cli
        from pairadar.fetch import FetchReport

        store = [{"date": "2026-09-24", "title": "t", "url": "https://e.org/1", "evidence": "M",
                  "lane": "safety", "id": "1"},
                 {"date": "2026-09-10", "title": "t", "url": "https://e.org/2", "evidence": "M",
                  "lane": "safety", "id": "2"}]
        with mock.patch.object(cli, "fetch_all", return_value=FetchReport()), \
                mock.patch.object(cli, "load_store", return_value=store):
            ctx = cli.build_context(load_config(), offline=True, limit=8, day="2026-09-25")
        self.assertEqual(dict(ctx["lane_rows"])["safety"], 1)
        self.assertEqual(sum(count for _, count in ctx["lane_rows"]), 1)
        self.assertEqual(ctx["mix"], {"O": 0, "R": 0, "M": 1})
        self.assertEqual(ctx["chart_days"], 7)


if __name__ == "__main__":
    unittest.main()
