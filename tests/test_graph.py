"""The explore-page exports (`pairadar.graph`): what is stored, how a published day is read back, and the nightly step.

No network. The round trip renders a day with the real renderer and reads it back, so a change to the page that the
reader cannot follow fails here, not in a night's run.
"""
from __future__ import annotations

import contextlib
import hashlib
import inspect
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pairadar import cli, distill, graph  # noqa: E402
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

    def test_the_picks_are_found_by_their_title_and_a_later_section_that_looks_like_a_pick_is_not_read(self) -> None:
        text = page(sample())
        extra = "\n## Later\n\n### 9. Impostor\n\n- **Lane**: Data engine (collection / curation / scaling) ｜ " \
                "**Evidence**: `[R]` ｜ **Source**: [arXiv 2611.00001](https://arxiv.org/abs/2611.00001) ｜ `2026-11-01`\n"
        self.assertEqual(len(graph.parse_picks(text + extra, DAY, CONFIG)), 3)

    def test_a_section_before_the_picks_that_has_the_pick_format_is_not_read_as_the_picks(self) -> None:
        # 01 F3: the picks are not "the first section". A notice or baseline placed first, in the same format, is skipped.
        heading = f"## {CONFIG.ui('en')['top_items']}"
        text = page(sample())
        fake = ("## Notice\n\n### 1. Impostor\n\n- **Lane**: Data engine (collection / curation / scaling) ｜ "
                "**Evidence**: `[R]` ｜ **Source**: [arXiv 2611.00001](https://arxiv.org/abs/2611.00001) ｜ `2026-11-01`\n\n")
        before, _, after = text.partition(heading)
        moved = before + fake + heading + after
        self.assertEqual(graph.parse_picks(moved, DAY, CONFIG), [graph.item_entry(item, DAY) for item in sample()])

    def test_a_page_without_exactly_one_picks_section_is_refused(self) -> None:
        heading = f"## {CONFIG.ui('en')['top_items']}"
        text = page(sample())
        cases = {"the picks section renamed (the first section is now something else)":
                 text.replace(heading, "## Frontier baseline (curated)", 1),
                 "no picks heading at all": text.replace(heading, "### Picks", 1),
                 "two picks sections": text + "\n" + heading + "\n\n" + text.partition(heading)[2]}
        for name, bad in cases.items():
            with self.subTest(name), self.assertRaises(graph.GraphError):
                graph.parse_picks(bad, DAY, CONFIG)
        with self.assertRaisesRegex(graph.GraphError, "2 sections"):
            graph.parse_picks(cases["two picks sections"], DAY, CONFIG)

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

    def test_a_complete_day_is_left_alone_and_the_day_being_written_is_skipped(self) -> None:
        folder = self.folder({"2026-11-10": page(sample(), "2026-11-10")})
        entries = [dict(graph.item_entry(item, "2026-11-10"), title=f"kept {index}") for index, item in enumerate(sample())]
        existing = graph.append_day(graph.empty_items(), entries, "2026-11-10")
        history = [{"date": "2026-11-10", "count": 3}, {"date": "2026-11-12", "count": 1}]
        doc = graph.fill_missing(existing, history, folder, CONFIG, skip_day="2026-11-12")
        self.assertEqual([entry["title"] for entry in doc["items"]], ["kept 0", "kept 1", "kept 2"])
        self.assertEqual(doc["backfill"]["gaps"], [])

    def test_a_partial_day_is_read_again_and_replaced_only_by_a_full_page(self) -> None:
        # 01 F4: one of the run's three picks was stored. The whole page is read; the partial entry is replaced.
        partial = graph.append_day(graph.empty_items(), [dict(graph.item_entry(sample()[0], "2026-11-10"), title="kept")],
                                   "2026-11-10")
        history = [{"date": "2026-11-10", "count": 3}]
        doc = graph.fill_missing(partial, history, self.folder({"2026-11-10": page(sample(), "2026-11-10")}), CONFIG)
        self.assertEqual(len(doc["items"]), 3)
        self.assertNotIn("kept", [entry["title"] for entry in doc["items"]])
        self.assertEqual(doc["backfill"]["gaps"], [])
        # No page to read: the partial day stays as stored and is listed as a gap, which is never cleared.
        doc = graph.fill_missing(partial, history, self.folder({}), CONFIG)
        self.assertEqual([entry["title"] for entry in doc["items"]], ["kept"])
        self.assertEqual([gap["date"] for gap in doc["backfill"]["gaps"]], ["2026-11-10"])
        self.assertIn("1 of 3 stored", doc["backfill"]["gaps"][0]["reason"])
        again = graph.fill_missing(doc, history, self.folder({}), CONFIG)
        self.assertEqual(len(again["backfill"]["gaps"]), 1)

    def test_a_gap_on_the_day_being_written_is_cleared_when_its_picks_are_all_there(self) -> None:
        doc = graph.empty_items()
        doc["backfill"]["gaps"] = [{"date": "2026-11-12", "reason": "an earlier run could not place it"}]
        doc = graph.append_day(doc, [graph.item_entry(item, "2026-11-12") for item in sample()[:2]], "2026-11-12")
        out = graph.fill_missing(doc, [{"date": "2026-11-12", "count": 2}], self.folder({}), CONFIG, skip_day="2026-11-12")
        self.assertEqual(out["backfill"]["gaps"], [])

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

    def test_the_evidence_class_of_every_source_that_failed_is_named_from_the_current_configuration(self) -> None:
        # 06's weekly table marks a zero only in the evidence classes whose sources failed that week.
        history = [{"date": "2026-11-10", "count": 0, "failed": ["deepmind", "arxiv"]},
                   {"date": "2026-11-11", "count": 0, "failed": ["leiphone", "no-such-source"]},
                   {"date": "2026-11-12", "count": 0}]
        table = self.table(history, [], "2026-11-12")
        self.assertEqual(table["source_evidence"], {"arxiv": "R", "deepmind": "O", "leiphone": "M"})
        self.assertEqual(list(table["source_evidence"]), ["arxiv", "deepmind", "leiphone"])      # sorted, and no unknown id
        self.assertIn("not on the day it failed", table["source_evidence_note"])
        self.assertEqual(self.table([{"date": "2026-11-10", "count": 0}], [], "2026-11-10")["source_evidence"], {})
        # An id with no class in the configuration is left out, not guessed.
        config = type(CONFIG)(sources={**CONFIG.sources, "feeds": [{"id": "classless", "url": "https://x.example/f"}]},
                              taxonomy=CONFIG.taxonomy, glossary=CONFIG.glossary, baseline=CONFIG.baseline)
        odd = graph.lane_time(config, [{"date": "2026-11-10", "count": 0, "failed": ["classless", "arxiv"]}],
                              graph.empty_items(), [], "2026-11-10")
        self.assertEqual(odd["source_evidence"], {"arxiv": "R"})

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
        # per_day is the daily shortlist size, the `limit` of `run()` and of `--limit`; per_lane is the default of
        # `select()`. Both were checked in git at each rule's commit (all 10 read 8 and 2) and are pinned to the code now.
        select_default = inspect.signature(distill.select).parameters["per_lane"].default
        run_default = inspect.signature(cli.run).parameters["limit"].default
        self.assertEqual([(rule["per_day"], rule["per_lane"]) for rule in rules], [(run_default, select_default)] * len(rules))
        self.assertEqual((run_default, select_default), (8, 2))
        self.assertIn("2026-09-25", [rule["from"] for rule in rules])
        self.assertIn("2026-10-25", [rule["from"] for rule in rules])
        self.assertTrue(all(rule["effect_measured"] is False for rule in rules))
        # 07 decided which changes the page draws as lines (07-radar-rules-show-1116.md): the ones after which the two sides
        # are not measured with the same ruler. The others stay in the data. One line per date: 09-26 holds two commits.
        self.assertEqual(sorted({rule["from"] for rule in rules if rule["show"]}),
                         ["2026-09-25", "2026-09-26", "2026-10-25", "2026-11-06", "2026-11-14", "2026-11-16"])
        self.assertEqual(sorted({rule["from"] for rule in rules[1:] if not rule["show"]}),
                         ["2026-09-21", "2026-10-22", "2026-10-26"])
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "rules.json"
            ok = {"from": "2026-09-19", "per_lane": 2, "per_day": 8, "show": True}
            bad = [{**ok, "per_day": value} for value in (0, -1, "8", True, 8.0)]
            bad += [{"from": "2026-09-19", "per_lane": 2, "show": True}, {"from": "2026-09-19", "per_day": 8, "show": True}]
            bad += [{**ok, "per_lane": value} for value in (0, "2", False)]
            bad += [{key: value for key, value in ok.items() if key != "show"}]                  # show is required
            bad += [{**ok, "show": value} for value in ("true", 1, 0, None, "false")]            # and is a real boolean
            for text in ("{broken", "{}", json.dumps([{"title": "no date"}]),
                         json.dumps([{**ok, "from": "2026-10-01"}, {**ok, "from": "2026-09-01"}])) + tuple(
                             json.dumps([rule]) for rule in bad):
                path.write_text(text)
                with self.assertRaises(graph.GraphError, msg=text):
                    graph.load_rules(path)
            path.write_text(json.dumps([{**ok, "per_day": None, "per_lane": None}]))
            self.assertEqual(graph.load_rules(path)[0]["per_day"], None)                 # "not verified" is allowed


class PublishTest(unittest.TestCase):
    """01 F2 and F4: the pair is published as one version, and a stored file that is not what this module writes stops the
    export instead of crashing the run."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "data").mkdir()
        (self.root / "data" / graph.RULES_FILE).write_text(json.dumps([{"from": "2026-09-19", "per_lane": 2, "per_day": 8, "show": False}]))
        (self.root / "radar" / "daily").mkdir(parents=True)
        (self.root / "radar" / "daily" / "2026-11-11.en.md").write_text(page(sample(), "2026-11-11"), encoding="utf-8")
        self.ctx = {"date": DAY, "picked": sample()[:2],
                    "history": [{"date": "2026-11-11", "count": 3}, {"date": DAY, "count": 2}]}
        self.folder = self.root / "radar" / "graph"

    def run_it(self, ctx=None) -> list[Path]:
        with contextlib.redirect_stdout(io.StringIO()) as out:
            paths = graph.write_graph(CONFIG, ctx or self.ctx, root=self.root, source=self.root)
        self.out = out.getvalue()
        return paths

    def files(self) -> dict[str, bytes]:
        return {path.name: path.read_bytes() for path in sorted(self.folder.glob("*"))} if self.folder.exists() else {}

    @staticmethod
    def replace_failing_on(*calls: int):
        real, seen = os.replace, {"n": 0}

        def replace(source, target):
            seen["n"] += 1
            if seen["n"] in calls:
                raise OSError(f"injected failure on replacement {seen['n']}")
            return real(source, target)
        return mock.patch.object(graph.os, "replace", replace)

    def later(self) -> dict:
        return {"date": "2026-11-13", "picked": sample()[:1],
                "history": [*self.ctx["history"], {"date": "2026-11-13", "count": 1}]}

    def test_a_run_log_whose_count_cannot_be_trusted_stops_the_export_and_keeps_the_previous_pair(self) -> None:
        # D1 (00's ruling, 01's two counter-examples): `count=True` equals one stored pick and a missing count equals zero,
        # so without the check both would be exported as if they were fine.
        self.run_it()
        before = self.files()
        day13 = {"date": "2026-11-13", "picked": sample()[:1]}                       # one pick stored for the 13th
        cases = {
            "count is True where one pick is stored": [*self.ctx["history"], {"date": "2026-11-13", "count": True}],
            "count is False where none is stored": [*self.ctx["history"], {"date": "2026-11-13", "count": False}],
            "count is missing where none is stored": [{"date": "2026-11-10"}, *self.ctx["history"], {"date": "2026-11-13", "count": 1}],
            "count is null": [{"date": "2026-11-10", "count": None}, *self.ctx["history"], {"date": "2026-11-13", "count": 1}],
            "count is a string": [{"date": "2026-11-10", "count": "0"}, *self.ctx["history"], {"date": "2026-11-13", "count": 1}],
            "count is a float": [{"date": "2026-11-10", "count": 0.0}, *self.ctx["history"], {"date": "2026-11-13", "count": 1}],
            "count is negative": [{"date": "2026-11-10", "count": -1}, *self.ctx["history"], {"date": "2026-11-13", "count": 1}],
            "a run has no usable date": [{"date": "20261110", "count": 0}, *self.ctx["history"], {"date": "2026-11-13", "count": 1}],
            "a run is not an object": [None, *self.ctx["history"], {"date": "2026-11-13", "count": 1}],
        }
        for name, history in cases.items():
            with self.subTest(name):
                self.assertEqual(self.run_it({**day13, "history": history}), [])
                self.assertIn("no usable date or count", self.out)
                self.assertEqual(self.files(), before)
        # Zero is a count, and a run log of whole numbers is exported.
        paths = self.run_it({**day13, "history": [{"date": "2026-11-10", "count": 0}, *self.ctx["history"],
                                                  {"date": "2026-11-13", "count": 1}]})
        self.assertEqual([path.name for path in paths], ["items.json", "lane-time.json"])

    def test_a_written_pair_is_one_version_and_says_so(self) -> None:
        paths = self.run_it()
        self.assertEqual([path.name for path in paths], ["items.json", "lane-time.json"])
        self.assertEqual(graph.check_pair(self.folder, CONFIG), [])
        table = json.loads(paths[1].read_text())
        self.assertEqual(table["items_sha256"], hashlib.sha256(paths[0].read_bytes()).hexdigest())
        self.assertEqual(table["items_count"], len(json.loads(paths[0].read_text())["items"]))
        self.assertEqual(list(self.folder.glob(".*")), [])                       # no temporary file is left

    def test_a_failure_while_the_second_file_is_staged_leaves_the_first_untouched(self) -> None:
        # 04 T1: items.json must not be replaced before lane-time.json has been staged; the second staging fails here.
        self.run_it()
        before = self.files()
        real, seen = os.fsync, {"n": 0}

        def fsync(descriptor):
            seen["n"] += 1
            if seen["n"] == 2:                                 # the first staged file is flushed, the second is not
                raise OSError("disk full")
            return real(descriptor)
        with mock.patch.object(graph.os, "fsync", fsync):
            self.assertEqual(self.run_it(self.later()), [])
        self.assertEqual(seen["n"], 2)
        self.assertEqual(self.files(), before)                 # items.json is still the previous bytes, no temporary file
        self.assertIn("graph export skipped", self.out)
        self.assertEqual(graph.check_pair(self.folder, CONFIG), [])

    def test_the_directory_is_flushed_after_the_two_renames_and_a_failure_to_do_so_is_reported_not_fatal(self) -> None:
        with mock.patch.object(graph, "_sync_directory", wraps=graph._sync_directory) as spy:
            self.assertEqual(len(self.run_it()), 2)
        spy.assert_called_once_with(self.folder)
        with mock.patch.object(graph, "_sync_directory", side_effect=OSError("io error")):
            paths = self.run_it(self.later())
        self.assertEqual([path.name for path in paths], ["items.json", "lane-time.json"])      # the pair is in place
        self.assertIn("the directory was not synced", self.out)
        self.assertEqual(graph.check_pair(self.folder, CONFIG), [])

    def test_the_directory_is_not_flushed_when_nothing_was_replaced(self) -> None:
        with mock.patch.object(graph, "_sync_directory") as spy, self.replace_failing_on(1):
            self.assertEqual(self.run_it(), [])
        spy.assert_not_called()

    def test_a_pair_that_fails_the_contract_is_not_published(self) -> None:
        with mock.patch.object(graph, "contract_problems", return_value=["boom"]):
            self.assertEqual(self.run_it(), [])
        self.assertIn("the pair is not sound: boom", self.out)
        self.assertEqual(self.files(), {})

    def test_a_failure_of_the_first_replacement_leaves_the_previous_pair_and_the_run_goes_on(self) -> None:
        self.run_it()
        before = self.files()
        with self.replace_failing_on(1):
            paths = self.run_it(self.later())
        self.assertEqual(paths, [])
        self.assertIn("graph export skipped", self.out)
        self.assertEqual(self.files(), before)
        self.assertEqual(graph.check_pair(self.folder, CONFIG), [])

    def test_a_failure_of_the_second_replacement_puts_the_first_file_back(self) -> None:
        self.run_it()
        before = self.files()
        with self.replace_failing_on(2):                       # items.json is replaced, lane-time.json is not
            paths = self.run_it(self.later())
        self.assertEqual(paths, [])
        self.assertIn("graph export skipped", self.out)
        self.assertEqual(self.files(), before)                 # the previous pair, byte for byte, and no temporary file
        self.assertEqual(graph.check_pair(self.folder, CONFIG), [])

    def test_a_failure_of_the_second_replacement_with_no_previous_pair_leaves_neither_file(self) -> None:
        with self.replace_failing_on(2):
            self.assertEqual(self.run_it(), [])
        self.assertEqual(self.files(), {})
        self.assertIn("graph export skipped", self.out)

    def test_if_the_first_file_cannot_be_put_back_a_reader_can_still_tell_the_two_apart(self) -> None:
        self.run_it()
        with self.replace_failing_on(2, 3):                    # lane-time.json fails, and so does putting items.json back
            self.assertEqual(self.run_it(self.later()), [])
        self.assertIn("readers must compare items_sha256", self.out)
        self.assertEqual(list(self.folder.glob(".*")), [])
        problems = graph.check_pair(self.folder, CONFIG)       # new items.json, old lane-time.json
        self.assertEqual(len(problems), 1)
        self.assertIn("mixed generations", problems[0])

    def test_a_failure_while_the_files_are_staged_writes_nothing(self) -> None:
        self.run_it()
        before = self.files()
        with mock.patch.object(graph.os, "fsync", side_effect=OSError("disk full")):
            self.assertEqual(self.run_it(self.later()), [])
        self.assertEqual(self.files(), before)
        self.assertIn("graph export skipped", self.out)

    def test_a_stored_file_that_is_not_what_this_module_writes_stops_the_export_and_is_left_alone(self) -> None:
        self.run_it()
        stored = json.loads((self.folder / "items.json").read_text())
        broken = {
            "an entry that is null": [None],
            "an entry with an extra key": [{**stored["items"][0], "extra": 1}],
            "an entry with a missing key": [{k: v for k, v in stored["items"][0].items() if k != "url"}],
            "an unknown lane": [{**stored["items"][0], "lane": "made-up"}],
            "an unknown evidence class": [{**stored["items"][0], "evidence": "X"}],
            "a date that is not a date": [{**stored["items"][0], "date": "2026-13-45"}],
            "signals that are not text": [{**stored["items"][0], "signals": [1]}],
            "the same pick twice on a day": [stored["items"][0], stored["items"][0]],
        }
        for name, items in broken.items():
            with self.subTest(name):
                text = json.dumps({**stored, "items": items})
                (self.folder / "items.json").write_text(text)
                before = self.files()
                self.assertEqual(self.run_it(self.later()), [])
                self.assertIn("graph export skipped", self.out)
                self.assertEqual(self.files(), before)

    def test_a_stored_day_with_fewer_picks_than_the_run_recorded_is_read_again(self) -> None:
        self.run_it()
        stored = json.loads((self.folder / "items.json").read_text())
        eleventh = [entry for entry in stored["items"] if entry["date"] == "2026-11-11"]
        stored["items"] = eleventh[:1] + [entry for entry in stored["items"] if entry["date"] == DAY]
        (self.folder / "items.json").write_text(json.dumps(stored))
        paths = self.run_it()
        doc = json.loads(paths[0].read_text())
        self.assertEqual(sum(1 for entry in doc["items"] if entry["date"] == "2026-11-11"), 3)
        self.assertEqual(doc["backfill"]["gaps"], [])
        self.assertEqual(graph.check_pair(self.folder, CONFIG), [])


class PoolTest(unittest.TestCase):
    """01 F4: a lane the run log has no number for is unknown, not zero."""

    LANES = [lane["id"] for lane in CONFIG.lanes]

    def pool_cell(self, pool, lane="data"):
        doc = graph.empty_items()
        table = graph.lane_time(CONFIG, [{"date": "2026-11-11", "count": 0, "pool": pool}], doc, [], "2026-11-11")
        return table["days"][0]["lanes"][lane]["pool"]

    def test_a_lane_that_the_pool_does_not_name_is_null_and_a_named_zero_is_zero(self) -> None:
        pool = {"live": 9, "eligible": 4, "per_lane_cap": 2, "by_lane": {"simeval": 4, "data": 0}, "picked_by_lane": {}}
        self.assertEqual(self.pool_cell(pool, "simeval"), 4)
        self.assertEqual(self.pool_cell(pool, "data"), 0)
        self.assertIsNone(self.pool_cell(pool, "hardware"))                 # the log has no number for this lane
        self.assertIsNone(self.pool_cell(None))
        self.assertIsNone(self.pool_cell({}))
        for bad in ({"by_lane": None}, {"by_lane": []}, {"by_lane": {"data": -1}}, {"by_lane": {"data": "3"}},
                    {"by_lane": {"data": True}}, {"by_lane": {"data": 2.5}}):
            with self.subTest(bad=bad):
                self.assertIsNone(self.pool_cell(bad))


class ContractTest(unittest.TestCase):
    """What a consumer of `radar-graph-1` may rely on, checked before the pair is published."""

    def pair(self):
        doc = graph.empty_items()
        doc["items"] = [graph.item_entry(item, "2026-11-11") for item in sample()]
        doc["as_of"], doc["first_run"] = "2026-11-12", "2026-11-10"
        history = [{"date": "2026-11-10", "count": 4}, {"date": "2026-11-11", "count": 3}, {"date": "2026-11-12", "count": 0}]
        table = graph.lane_time(CONFIG, history, doc, [{"from": "2026-09-19", "per_lane": 2, "per_day": 8}], "2026-11-12")
        data = graph.render_json(doc)
        table["items_sha256"], table["items_count"] = hashlib.sha256(data).hexdigest(), len(doc["items"])
        doc["backfill"]["gaps"] = [{"date": "2026-11-10", "reason": "x"}]
        table["items_sha256"] = hashlib.sha256(graph.render_json(doc)).hexdigest()
        return doc, table

    def test_a_sound_pair_has_no_problems_and_names_what_its_rule_dates_mean(self) -> None:
        doc, table = self.pair()
        self.assertEqual(graph.contract_problems(doc, table, CONFIG), [])
        self.assertEqual(table["mismatch_days"], ["2026-11-10"])
        self.assertIn("not the first run", table["rules_dates"])           # 01 C1

    def test_every_way_the_two_files_can_disagree_is_a_problem(self) -> None:
        def damaged(change):
            doc, table = self.pair()
            change(doc, table)
            return graph.contract_problems(doc, table, CONFIG)
        cases = {
            "a zero where the picks are not known": lambda d, t: t["days"][0]["lanes"]["data"].update(picked=0),
            "a number in an evidence class where the picks are not known": lambda d, t: t["days"][0]["lanes"]["data"].update(O=1),
            "picks known but the cells do not add up": lambda d, t: t["days"][1]["lanes"]["data"].update(O=2, picked=2),
            "a lane whose evidence classes do not add up": lambda d, t: t["days"][1]["lanes"]["data"].update(picked=5),
            "evidence classes that do not add up": lambda d, t: t["days"][1]["lanes"]["foundation"].update(O=0),
            "mismatch_days that lists a known day": lambda d, t: t.update(mismatch_days=["2026-11-10", "2026-11-11"]),
            "mismatch_days that misses an unknown day": lambda d, t: t.update(mismatch_days=[]),
            "a day missing": lambda d, t: t["days"].pop(1),
            "a lane missing": lambda d, t: t["days"][2]["lanes"].pop("data"),
            "another items.json": lambda d, t: d["items"].pop(),
            "an items.json with the same count and other content": lambda d, t: d["items"][0].update(title="edited"),
            "a count that is not the items": lambda d, t: t.update(items_count=99),
            "as_of that differs": lambda d, t: t.update(as_of="2026-11-11"),
            "a pool that is neither null nor a count": lambda d, t: t["days"][1]["lanes"]["data"].update(pool="3"),
            "picks_known that is not a boolean": lambda d, t: t["days"][1].update(picks_known=None),
            "source_evidence missing": lambda d, t: t.pop("source_evidence"),
            "source_evidence that names a source that never failed": lambda d, t: t["source_evidence"].update(deepmind="O"),
            "source_evidence with a class that is not O, R or M": lambda d, t: (t["days"][1].update(failed_sources=["deepmind"]),
                                                                               t["source_evidence"].update(deepmind="Z")),
            "a gap on a day said to be known": lambda d, t: (d["backfill"]["gaps"].append({"date": "2026-11-11", "reason": "x"}),
                                                              t.update(items_sha256=hashlib.sha256(graph.render_json(d)).hexdigest())),
        }
        for name, change in cases.items():
            with self.subTest(name):
                self.assertTrue(damaged(change), name)


class NightlyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "data").mkdir()
        (self.root / "data" / graph.RULES_FILE).write_text(json.dumps([{"from": "2026-09-19", "per_lane": 2, "per_day": 8, "show": False}]))
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
        self.assertEqual(table["rules"], [{"from": "2026-09-19", "per_lane": 2, "per_day": 8, "show": False}])

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
    def test_the_real_history_makes_a_pair_that_passes_the_contract(self) -> None:
        history = self.runs()
        doc = graph.fill_missing(graph.empty_items(), history, ROOT / "radar" / "daily", CONFIG)
        doc["as_of"], doc["first_run"] = self.FROZEN_THROUGH, min(run["date"] for run in history)
        graph.validate_items(doc, CONFIG)                         # every entry read back is one this module accepts
        table = graph.lane_time(CONFIG, history, doc, graph.load_rules(ROOT / "data" / graph.RULES_FILE), self.FROZEN_THROUGH)
        table["items_sha256"] = hashlib.sha256(graph.render_json(doc)).hexdigest()
        table["items_count"] = len(doc["items"])
        self.assertEqual(graph.contract_problems(doc, table, CONFIG), [])
        self.assertEqual(table["mismatch_days"], ["2026-09-19"])
        # 06's weekly table: the four sources that failed in the 58 days, by the current configuration.
        self.assertEqual(table["source_evidence"], {"arxiv": "R", "deepmind": "O", "leiphone": "M", "tri": "R"})
        self.assertTrue(all(cell["picked"] is None for cell in table["days"][0]["lanes"].values()))
        # The links are published as they were: an old plain-http link is not turned into https.
        self.assertTrue(any(entry["url"].startswith("http://") for entry in doc["items"]))

    def test_a_real_generation_publishes_a_pair_that_passes_the_contract(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "data").mkdir()
            (root / "data" / graph.RULES_FILE).write_text((ROOT / "data" / graph.RULES_FILE).read_text())
            (root / "radar").mkdir()
            (root / "radar" / "daily").symlink_to(ROOT / "radar" / "daily")
            ctx = {"date": "2026-11-16", "picked": [], "history": [*self.runs(), {"date": "2026-11-16", "count": 0}]}
            with contextlib.redirect_stdout(io.StringIO()) as out:
                paths = graph.write_graph(CONFIG, ctx, root=root, source=root)
            self.assertEqual([path.name for path in paths], ["items.json", "lane-time.json"], out.getvalue())
            self.assertEqual(graph.check_pair(root / "radar" / "graph", CONFIG), [])


if __name__ == "__main__":
    unittest.main()
