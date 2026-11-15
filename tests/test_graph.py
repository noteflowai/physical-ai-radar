"""The explore-page exports (`pairadar.graph`): what is stored, how a published day is read back, and the nightly step.

No network. The round trip renders a day with the real renderer and reads it back, so a change to the page that the
reader cannot follow fails here, not in a night's run.
"""
from __future__ import annotations

import contextlib
import inspect
import io
import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pairadar import cli, graph  # noqa: E402
from pairadar.config import Item, load_config  # noqa: E402
from pairadar.fetch import baseline_items, link_digest  # noqa: E402
from pairadar.render import render_daily  # noqa: E402

CONFIG = load_config()
DAY = "2026-11-12"
TRICKY = "Hand_over [x] *y* `z` <b> {{ a }} {% b %} back\\slash"


def pick(title: str, *, source: str, publisher: str, url: str, lane: str = "simeval", evidence: str = "R",
         numbers: list[str] | None = None, signals: list[str] | None = None, summary: str = "A robot paper.",
         published: str = "2026-11-10") -> Item:
    ident = "arxiv:" + url.rsplit("/", 1)[-1] if source == "arxiv" else f"{source}:{link_digest(url)}"
    item = Item(id=ident, title=title, url=url, publisher=publisher, source_id=source, evidence=evidence,
                published=published, summary=summary)
    item.lane, item.numbers, item.signals = lane, list(numbers or []), list(signals or [])
    return item


def sample() -> list[Item]:
    return [
        pick("ManiLadder: a task ladder", source="arxiv", publisher="arXiv 2611.09490",
             url="https://arxiv.org/abs/2611.09490", numbers=["95%", "2.57x"],
             signals=["closed_loop", "real_robot"],
             summary="We report 95% success with `fast` `mode` and a 2.57x speed-up on a real robot in closed loop."),
        pick(TRICKY, source="nvidia-blog", publisher="NVIDIA Blog", url="https://blogs.nvidia.com/blog/a(b)c d/",
             lane="foundation", evidence="O", summary="Summary with _under_ [brackets] and <tags> {{ braces }}. " * 8),
        pick("Weekly roundup", source="the-robot-report", publisher="The Robot Report",
             url="https://www.therobotreport.com/roundup/", lane="hardware", evidence="M", summary=""),
    ]


def page(items: list[Item], day: str = DAY) -> str:
    baseline = baseline_items(CONFIG)[:3]
    return render_daily(CONFIG, "en", {"date": day, "generated": f"{day} 01:40 UTC", "window": "", "picked": items,
                                       "baseline": baseline, "curated": {}, "lane_rows": [], "mix": {},
                                       "cadence": []})


class ReadBackTest(unittest.TestCase):
    def test_a_rendered_day_reads_back_to_exactly_what_was_stored(self) -> None:
        items = sample()
        parsed = graph.parse_picks(page(items), DAY, CONFIG)
        self.assertEqual(parsed, [graph.item_entry(item, DAY) for item in items])

    def test_the_curated_baseline_is_never_read(self) -> None:
        items = sample()
        text = page(items)
        self.assertIn("Frontier baseline", text)                   # the page does carry baseline entries
        self.assertEqual(len(graph.parse_picks(text, DAY, CONFIG)), len(items))
        self.assertEqual(graph.parse_picks(page([]), DAY, CONFIG), [])

    def test_only_the_first_section_counts_even_if_a_later_one_looks_like_a_pick(self) -> None:
        text = page(sample())
        extra = "\n## Later\n\n### 9. Impostor\n\n- **Lane**: Data engine (collection / curation / scaling) ｜ " \
                "**Evidence**: `[R]` ｜ **Source**: [arXiv 2611.00001](https://arxiv.org/abs/2611.00001) ｜ `2026-11-01`\n"
        self.assertEqual(len(graph.parse_picks(text + extra, DAY, CONFIG)), 3)

    def test_what_cannot_be_placed_is_refused_not_guessed(self) -> None:
        good = page(sample())
        cases = {
            "unknown lane name": good.replace("Simulation & evaluation (sim2real / closed-loop)", "Made-up lane"),
            "unknown or ambiguous source label": good.replace("[NVIDIA Blog]", "[Nobody Blog]"),
            "unknown signal name": good.replace("`closed-loop success reported`", "`something else`"),
            "older format": good.replace("https://arxiv.org/abs/2611.09490", "http://arxiv.org/abs/2611.09490v1"),
            "no lane line": good.replace("**Lane**", "**Zone**", 1).replace(" ｜ ", " | ", 1),
        }
        for needle, text in cases.items():
            with self.subTest(needle):
                with self.assertRaises(graph.GraphError) as raised:
                    graph.parse_picks(text, DAY, CONFIG)
                self.assertIn(needle, str(raised.exception))
        with self.assertRaises(graph.GraphError):
            graph.parse_picks("no sections here\n", DAY, CONFIG)

    def test_an_ambiguous_publisher_label_is_refused(self) -> None:
        feeds = [dict(feed) for feed in CONFIG.sources["feeds"]]
        twin = dict(next(feed for feed in feeds if feed["id"] == "nvidia-blog"), id="nvidia-twin")
        config = type(CONFIG)(sources={**CONFIG.sources, "feeds": feeds + [twin]}, taxonomy=CONFIG.taxonomy,
                              glossary=CONFIG.glossary, baseline=CONFIG.baseline)
        with self.assertRaisesRegex(graph.GraphError, "ambiguous"):
            graph.parse_picks(page(sample()), DAY, config)

    def test_a_lane_that_was_renamed_is_still_the_same_lane(self) -> None:
        text = page(sample()).replace(CONFIG.lane_name("foundation", "en"), "Safety, authority & compliance")
        self.assertEqual(graph.parse_picks(text, DAY, CONFIG)[1]["lane"], "safety")

    def test_ids_are_the_ones_the_pipeline_makes(self) -> None:
        parsed = graph.parse_picks(page(sample()), DAY, CONFIG)
        self.assertEqual(parsed[0]["id"], "arxiv:2611.09490")
        self.assertEqual(parsed[1]["id"], "nvidia-blog:" + link_digest("https://blogs.nvidia.com/blog/a(b)c d/"))
        self.assertRegex(parsed[2]["id"], r"^the-robot-report:[0-9a-f]{12}$")


class StoreTest(unittest.TestCase):
    def test_a_day_is_replaced_not_duplicated_and_days_stay_in_order(self) -> None:
        doc = graph.empty_items()
        first = [graph.item_entry(item, "2026-11-12") for item in sample()]
        doc = graph.append_day(doc, first, "2026-11-12")
        doc = graph.append_day(doc, [dict(first[0], date="2026-11-10")], "2026-11-10")
        self.assertEqual([entry["date"] for entry in doc["items"]], ["2026-11-10", "2026-11-12", "2026-11-12", "2026-11-12"])
        doc = graph.append_day(doc, first[:1], "2026-11-12")
        self.assertEqual([entry["date"] for entry in doc["items"]], ["2026-11-10", "2026-11-12"])
        self.assertEqual(doc["schema"], "radar-graph-1")

    def test_a_missing_file_is_none_and_a_wrong_one_is_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "items.json"
            self.assertIsNone(graph.load_items(path))
            for text in ("{broken", "[]", json.dumps({"schema": "other", "items": []}),
                         json.dumps({"schema": graph.SCHEMA, "items": {}})):
                path.write_text(text)
                with self.assertRaises(graph.GraphError, msg=text):
                    graph.load_items(path)
            path.write_text(json.dumps(graph.empty_items()))
            self.assertEqual(graph.load_items(path)["items"], [])

    def test_the_stored_fields_are_the_public_fields_of_the_feed_record(self) -> None:
        entry = graph.item_entry(sample()[0], DAY)
        self.assertEqual(list(entry), ["id", "date", "published", "lane", "evidence", "source_id", "publisher",
                                       "title", "url", "signals", "numbers", "excerpt"])
        self.assertEqual(entry["date"], DAY)
        self.assertEqual(entry["published"], "2026-11-10")


class FillTest(unittest.TestCase):
    def folder(self, pages: dict[str, str]) -> Path:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        for day, text in pages.items():
            (Path(temp.name) / f"{day}.en.md").write_text(text, encoding="utf-8")
        return Path(temp.name)

    def test_days_without_entries_are_read_from_their_pages_and_gaps_say_why(self) -> None:
        folder = self.folder({"2026-11-10": page(sample(), "2026-11-10"), "2026-11-11": page(sample()[:1], "2026-11-11"),
                              "2026-11-13": "## Today's picks\n\n### 1. Broken\n"})
        history = [{"date": "2026-11-09", "count": 2}, {"date": "2026-11-10", "count": 3},
                   {"date": "2026-11-11", "count": 2},      # the page has 1 pick, the run recorded 2
                   {"date": "2026-11-12", "count": 0},      # nothing was published: nothing to read, no gap
                   {"date": "2026-11-13", "count": 1}]
        doc = graph.fill_missing(graph.empty_items(), history, folder, CONFIG)
        self.assertEqual({entry["date"] for entry in doc["items"]}, {"2026-11-10"})
        reasons = {gap["date"]: gap["reason"] for gap in doc["backfill"]["gaps"]}
        self.assertEqual(sorted(reasons), ["2026-11-09", "2026-11-11", "2026-11-13"])
        self.assertIn("the page has 1 picks, the run recorded 2", reasons["2026-11-11"])
        self.assertIn("2026-11-09.en.md", reasons["2026-11-09"])                  # a page that is not there
        self.assertIn("no lane line", reasons["2026-11-13"])

    def test_a_day_that_has_entries_is_left_alone_and_the_day_being_written_is_skipped(self) -> None:
        folder = self.folder({"2026-11-10": page(sample(), "2026-11-10")})
        existing = graph.append_day(graph.empty_items(), [dict(graph.item_entry(sample()[0], "2026-11-10"), title="kept")],
                                    "2026-11-10")
        history = [{"date": "2026-11-10", "count": 3}, {"date": "2026-11-12", "count": 1}]
        doc = graph.fill_missing(existing, history, folder, CONFIG, skip_day="2026-11-12")
        self.assertEqual([entry["title"] for entry in doc["items"]], ["kept"])
        self.assertEqual(doc["backfill"]["gaps"], [])

    def test_a_gap_is_cleared_when_the_day_is_read_later(self) -> None:
        history = [{"date": "2026-11-10", "count": 3}]
        first = graph.fill_missing(graph.empty_items(), history, self.folder({}), CONFIG)
        self.assertEqual(len(first["backfill"]["gaps"]), 1)
        second = graph.fill_missing(first, history, self.folder({"2026-11-10": page(sample(), "2026-11-10")}), CONFIG)
        self.assertEqual((len(second["items"]), second["backfill"]["gaps"]), (3, []))


class LaneTimeTest(unittest.TestCase):
    LANES = [lane["id"] for lane in CONFIG.lanes]

    def table(self, history, items, as_of="2026-11-12", rules=None):
        doc = graph.empty_items()
        doc["items"] = items
        return graph.lane_time(CONFIG, history, doc, rules or [], as_of)

    def test_every_day_and_every_lane_is_present_and_zero_is_a_count(self) -> None:
        entries = [graph.item_entry(item, "2026-11-11") for item in sample()]
        history = [{"date": "2026-11-10", "count": 0}, {"date": "2026-11-12", "count": 0, "failed": ["arxiv"],
                                                          "unavailable": ["page_watch"], "fallback": []},
                   {"date": "2026-11-11", "count": 3}]
        table = self.table(history, entries)
        self.assertEqual([day["date"] for day in table["days"]], ["2026-11-10", "2026-11-11", "2026-11-12"])
        for day in table["days"]:
            self.assertEqual(list(day["lanes"]), self.LANES)
        middle = table["days"][1]["lanes"]
        self.assertEqual((middle["simeval"]["R"], middle["simeval"]["picked"]), (1, 1))
        self.assertEqual((middle["foundation"]["O"], middle["hardware"]["M"]), (1, 1))
        self.assertEqual(middle["data"], {"O": 0, "R": 0, "M": 0, "picked": 0, "pool": None})
        self.assertEqual(table["days"][2]["failed_sources"], ["arxiv"])
        self.assertEqual(table["days"][2]["unavailable"], ["page_watch"])
        self.assertEqual(table["mismatch_days"], [])

    def test_a_day_with_no_run_is_not_a_run_and_a_mismatch_is_named(self) -> None:
        history = [{"date": "2026-11-10", "count": 5}, {"date": "2026-11-12", "count": 0}]
        table = self.table(history, [])
        self.assertEqual([(day["date"], day["ran"], day["count"]) for day in table["days"]],
                         [("2026-11-10", True, 5), ("2026-11-11", False, None), ("2026-11-12", True, 0)])
        self.assertEqual(table["mismatch_days"], ["2026-11-10"])            # the run recorded 5, no entries
        gap = table["days"][0]
        self.assertFalse(gap["picks_known"])                                 # the day's picks cannot be placed
        for cell in gap["lanes"].values():
            self.assertEqual((cell["O"], cell["R"], cell["M"], cell["picked"]), (None, None, None, None))
        self.assertTrue(table["days"][1]["picks_known"] and table["days"][2]["picks_known"])
        self.assertEqual(table["days"][2]["lanes"]["data"]["picked"], 0)    # a day that really had none is a zero

    def test_a_day_that_cannot_be_placed_keeps_its_pool(self) -> None:
        pool = {"live": 3, "eligible": 2, "per_lane_cap": 2, "by_lane": {lane: 1 for lane in self.LANES},
                "picked_by_lane": {lane: 0 for lane in self.LANES}}
        table = self.table([{"date": "2026-11-12", "count": 4, "pool": pool}], [], "2026-11-12")
        self.assertEqual(table["mismatch_days"], ["2026-11-12"])
        self.assertTrue(all(cell["pool"] == 1 and cell["picked"] is None for cell in table["days"][0]["lanes"].values()))

    def test_the_pool_is_null_before_the_run_log_recorded_one_and_never_zero(self) -> None:
        pool = {"live": 9, "eligible": 4, "per_lane_cap": 2,
                "by_lane": {lane: (3 if lane == "simeval" else 0) for lane in self.LANES},
                "picked_by_lane": {lane: 0 for lane in self.LANES}}
        history = [{"date": "2026-11-10", "count": 0}, {"date": "2026-11-11", "count": 0, "pool": pool},
                   {"date": "2026-11-12", "count": 0, "pool": pool}]
        table = self.table(history, [])
        self.assertEqual(table["pool_since"], "2026-11-11")
        self.assertEqual(table["per_lane_cap"], 2)
        self.assertIsNone(table["days"][0]["lanes"]["simeval"]["pool"])
        self.assertEqual(table["days"][1]["lanes"]["simeval"]["pool"], 3)
        self.assertEqual(table["days"][2]["lanes"]["data"]["pool"], 0)
        none = self.table([{"date": "2026-11-11", "count": 0}], [], "2026-11-11")
        self.assertEqual((none["pool_since"], none["per_lane_cap"]), (None, None))

    def test_rules_are_loaded_in_date_order_and_the_committed_file_is_sound(self) -> None:
        rules = graph.load_rules(ROOT / "data" / graph.RULES_FILE)
        self.assertEqual((rules[0]["from"], rules[0]["per_lane"], rules[0]["min_score"]), ("2026-09-19", 2, 0.9))
        # per_day is the daily shortlist size, the `limit` of `run()` and of `--limit`: 8 since the first commit.
        self.assertEqual(rules[0]["per_day"], 8)
        self.assertEqual(inspect.signature(cli.run).parameters["limit"].default, rules[0]["per_day"])
        self.assertIn("2026-09-25", [rule["from"] for rule in rules])
        self.assertIn("2026-10-25", [rule["from"] for rule in rules])
        self.assertTrue(all(rule["effect_measured"] is False for rule in rules))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "rules.json"
            for text in ("{broken", "{}", json.dumps([{"title": "no date"}]),
                         json.dumps([{"from": "2026-10-01"}, {"from": "2026-09-01"}])):
                path.write_text(text)
                with self.assertRaises(graph.GraphError, msg=text):
                    graph.load_rules(path)


class NightlyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "data").mkdir()
        (self.root / "data" / graph.RULES_FILE).write_text(json.dumps([{"from": "2026-09-19", "per_lane": 2}]))
        (self.root / "radar" / "daily").mkdir(parents=True)
        (self.root / "radar" / "daily" / "2026-11-11.en.md").write_text(page(sample(), "2026-11-11"), encoding="utf-8")
        self.ctx = {"date": DAY, "picked": sample()[:2],
                    "history": [{"date": "2026-11-11", "count": 3}, {"date": DAY, "count": 2}]}

    def run_it(self, ctx=None) -> list[Path]:
        return graph.write_graph(CONFIG, ctx or self.ctx, root=self.root, source=self.root)

    def test_the_day_is_appended_and_the_days_before_it_are_read_from_their_pages(self) -> None:
        paths = self.run_it()
        self.assertEqual([path.name for path in paths], ["items.json", "lane-time.json"])
        doc = json.loads(paths[0].read_text())
        self.assertEqual(sorted({entry["date"] for entry in doc["items"]}), ["2026-11-11", DAY])
        self.assertEqual((doc["as_of"], doc["first_run"], doc["schema"]), (DAY, "2026-11-11", "radar-graph-1"))
        table = json.loads(paths[1].read_text())
        self.assertEqual((table["as_of"], table["mismatch_days"], len(table["days"])), (DAY, [], 2))
        self.assertEqual(table["rules"], [{"from": "2026-09-19", "per_lane": 2}])

    def test_a_second_run_of_the_same_day_changes_nothing(self) -> None:
        first = [path.read_bytes() for path in self.run_it()]
        second = [path.read_bytes() for path in self.run_it()]
        self.assertEqual(first, second)

    def test_a_later_day_keeps_the_earlier_ones_as_stored(self) -> None:
        self.run_it()
        stored = json.loads((self.root / "radar/graph/items.json").read_text())
        stored["items"][0]["title"] = "edited by hand, must survive"
        (self.root / "radar/graph/items.json").write_text(json.dumps(stored))
        later = {"date": "2026-11-13", "picked": sample()[:1],
                 "history": [*self.ctx["history"], {"date": "2026-11-13", "count": 1}]}
        paths = self.run_it(later)
        doc = json.loads(paths[0].read_text())
        self.assertIn("edited by hand, must survive", [entry["title"] for entry in doc["items"]])
        self.assertEqual({entry["date"] for entry in doc["items"]}, {"2026-11-11", DAY, "2026-11-13"})

    def test_rerunning_an_older_day_does_not_move_the_date_back(self) -> None:
        later = {"date": "2026-11-13", "picked": sample()[:1],
                 "history": [*self.ctx["history"], {"date": "2026-11-13", "count": 1}]}
        self.run_it(later)
        paths = self.run_it()                                      # the 12th again, after the 13th was written
        self.assertEqual(json.loads(paths[0].read_text())["as_of"], "2026-11-13")
        table = json.loads(paths[1].read_text())
        self.assertEqual((table["as_of"], table["days"][-1]["date"]), ("2026-11-13", "2026-11-13"))

    def test_a_problem_is_reported_and_nothing_is_written(self) -> None:
        (self.root / "data" / graph.RULES_FILE).write_text("{broken")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            paths = self.run_it()
        self.assertEqual(paths, [])
        self.assertIn("graph export skipped", output.getvalue())
        self.assertFalse((self.root / "radar" / "graph").exists())
        (self.root / "data" / graph.RULES_FILE).write_text(json.dumps([{"from": "2026-09-19"}]))
        (self.root / "radar" / "graph").mkdir()
        (self.root / "radar" / "graph" / "items.json").write_text("{broken")
        with contextlib.redirect_stdout(io.StringIO()) as quiet:
            self.assertEqual(self.run_it(), [])
        self.assertEqual((self.root / "radar" / "graph" / "items.json").read_text(), "{broken")   # untouched
        self.assertFalse((self.root / "radar" / "graph" / "lane-time.json").exists())


class CommittedDataTest(unittest.TestCase):
    """The published pages and the feed in this repository, read back.

    Frozen at the day the exports were added: later days come from the run itself, not from their pages, so a page
    that a later change makes unreadable must not stop the night's publication."""

    FROZEN_THROUGH = "2026-11-15"

    def runs(self) -> list[dict]:
        history = json.loads((ROOT / "radar" / "history.json").read_text())["runs"]
        return [run for run in history if run["date"] <= self.FROZEN_THROUGH]

    def test_every_day_after_the_first_early_format_reads_back_and_matches_the_run_log(self) -> None:
        history = self.runs()
        doc = graph.fill_missing(graph.empty_items(), history, ROOT / "radar" / "daily", CONFIG)
        gaps = {gap["date"]: gap["reason"] for gap in doc["backfill"]["gaps"]}
        # Only the first run published links in an older arXiv format, whose ids cannot be recovered.
        self.assertEqual(sorted(gaps), ["2026-09-19"], gaps)
        counts = {run["date"]: run["count"] for run in history}
        per_day: dict[str, int] = {}
        for entry in doc["items"]:
            per_day[entry["date"]] = per_day.get(entry["date"], 0) + 1
        for day, number in per_day.items():
            self.assertEqual(number, counts[day], day)
        self.assertEqual(len(doc["items"]) + sum(counts[day] for day in gaps), sum(counts.values()))

    def test_what_is_read_back_equals_the_feed_record_for_every_item_the_feed_holds(self) -> None:
        doc = graph.fill_missing(graph.empty_items(), self.runs(), ROOT / "radar" / "daily", CONFIG)
        mine = {entry["id"]: entry for entry in doc["items"]}
        feed = [entry["_radar"] for entry in json.loads((ROOT / "radar" / "feed.json").read_text())["items"]
                if "_radar" in entry]
        compared = 0
        for record in feed:
            entry = mine.get(record["id"])
            if entry is None:
                continue
            compared += 1
            for key in ("date", "title", "url", "lane", "evidence", "published", "source_id", "publisher",
                        "signals", "numbers", "excerpt"):
                self.assertEqual(entry[key], record[key], (record["id"], key))
        self.assertGreater(compared, 20)


if __name__ == "__main__":
    unittest.main()
