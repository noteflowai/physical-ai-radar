"""AW-0 (arXiv 2610.30136) was missed on 2026-10-22: fetched (the API answered; the abstract names "world
model" and "human video") and scored 2.90, it was held back by per_source in the capped pass while two
lower-ranked NVIDIA posts filled its lane, and the fill pass dropped it for a full lane. These tests pin
the fix in distill.select: an item the soft caps hold back keeps its lane slot for the rest of the
capped pass, and only there. No test here touches the network.
"""
from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pairadar.config import Item, load_config  # noqa: E402
from pairadar.distill import enrich, qualifies, select  # noqa: E402

CONFIG = load_config()
DAY = date(2026, 10, 22)
# The abstract as arXiv lists it (export.arxiv.org/abs/2610.30136, v1 2026-10-21 17:59:56 UTC).
AW0_ABSTRACT = (
    "Recent world action models provide visual foresight and future prediction for robot manipulation, but lack "
    "explicit mechanisms for compositional skill reuse. Agentic robotics combines code-based planning and reasoning "
    "with reusable skills, but often lacks predictive grounding in physical dynamics. We introduce AW-0, an agentic "
    "world action model framework that integrates code-based planning with joint video and action prediction. We "
    "jointly model code, future video, and robot actions as three coupled generative streams within a unified "
    "mixture-of-transformers architecture. Within this architecture a code expert generates parameterized skill "
    "routines and control logic while video and action experts generate future visual states and low-level motor "
    "commands. Code serves two complementary roles: it structures multi-step prediction and control within the world "
    "model and represents skills as reusable programs for subsequent agentic execution. We further introduce an "
    "automated curation pipeline that labels robot and human video trajectories with aligned programs and "
    "consolidates recurring routines into reusable skill libraries. Evaluations on RoboTwin 2.0 and physical bimanual "
    "manipulation tasks show improved task performance over VLA and WAM baselines, alongside gains in language "
    "following, semantic reasoning and robustness, demonstrating that agentic code enables connection between "
    "planning, prediction and execution within WAMs.")


def aw0() -> Item:
    paper = Item(id="arxiv:2610.30136", title="Agentic World Action Models", url="https://arxiv.org/abs/2610.30136",
                 publisher="arXiv 2610.30136", source_id="arxiv", evidence="R", published="2026-10-21",
                 summary=AW0_ABSTRACT, topical=True)
    return enrich([paper], CONFIG, DAY)[0]


def item(item_id: str, source: str, evidence: str, lane: str, score: float, title: str | None = None) -> Item:
    entry = Item(id=item_id, title=title or item_id, url=f"https://{source}.example/{item_id.split(':')[1]}",
                 publisher=source, source_id=source, evidence=evidence, published="2026-10-21")
    entry.lane, entry.lane_hits, entry.anchor_hits, entry.score = lane, 2, 1, score
    return entry


def picks_1022() -> list[Item]:
    """The eight items published on 2026-10-22 (radar/latest.json), with their lanes and scores."""
    return [
        item("arxiv:2610.29264", "arxiv", "R", "training", 3.900, "Paper 29264 on robot training"),
        item("arxiv:2610.27045", "arxiv", "R", "simeval", 3.875, "Paper 27045 on simulation evaluation"),
        item("arxiv:2610.30097", "arxiv", "R", "training", 3.600, "Paper 30097 on policy learning"),
        item("nvidia-blog:381130e24d54", "nvidia-blog", "O", "foundation", 2.600, "Cosmos reasoning for humanoids"),
        item("nvidia-blog:231615289514", "nvidia-blog", "O", "simeval", 2.335, "Isaac Sim evaluation workflow"),
        item("nvidia-blog:0c405e6fd045", "nvidia-blog", "O", "foundation", 1.800, "GR00T developer update"),
        item("the-robot-report:4b586e594e2b", "the-robot-report", "M", "hardware", 1.182, "Cobot arm shipping"),
        item("the-robot-report:a9044ca8f5b5", "the-robot-report", "M", "hardware", 1.182, "Gripper maker funding"),
    ]


def ranked(items: list[Item]) -> list[Item]:
    return sorted(items, key=lambda entry: (-entry.score, entry.published, entry.title))


class Aw0IsFetchedAndQualifies(unittest.TestCase):
    def test_the_abstract_matches_two_configured_queries(self) -> None:
        queries = CONFIG.sources["arxiv"]["queries"]
        abstract = AW0_ABSTRACT.lower()
        hits = [index for index, query in enumerate(queries)
                if any(phrase.lower() in abstract for phrase in query.split('"')[1::2])]
        self.assertEqual(hits, [0, 4])                       # "world model" and "human video"

    def test_it_is_a_qualifying_foundation_paper_that_outscores_most_of_the_day(self) -> None:
        paper = aw0()
        self.assertEqual((paper.lane, paper.lane_hits, paper.anchor_hits), ("foundation", 5, 7))
        self.assertAlmostEqual(paper.score, 2.9, places=3)
        self.assertTrue(qualifies(paper, 0.9))
        self.assertEqual(sum(other.score < paper.score for other in picks_1022()), 5)


class HeldItemsKeepTheirLane(unittest.TestCase):
    def test_the_2026_10_22_shortlist_now_includes_aw0(self) -> None:
        # The parameters installed that day: per_source 3, per_evidence 4, no quota, no reservations.
        paper, day = aw0(), picks_1022()
        picked = select(ranked([*day, paper]), limit=8, per_source=3, per_evidence=4)
        self.assertIn(paper, picked)
        self.assertEqual([entry.id for entry in day if entry not in picked], ["nvidia-blog:0c405e6fd045"])

    def test_and_with_the_current_quota_and_reservations(self) -> None:
        highlights = CONFIG.sources["filters"]["highlights"]
        paper = aw0()
        picked = select(ranked([*picks_1022(), paper]), limit=8, per_source=3, per_evidence=4,
                        max_arxiv=int(8 * highlights["max_arxiv_share"]), reserve=highlights["reserve"], report={})
        self.assertIn(paper, picked)
        self.assertEqual(sum(entry.source_id == "arxiv" for entry in picked), 4)
        self.assertEqual(len(picked), 8)

    def test_a_held_item_that_cannot_come_back_leaves_its_slot_to_the_next_one(self) -> None:
        # 09's condition 1: E is held by per_source and then refused by max_arxiv in the fill pass; the lane
        # slot it held goes to y2, and the list is still full.
        pool = [item("arxiv:a", "arxiv", "R", "la", 9.0), item("arxiv:b", "arxiv", "R", "lb", 8.9),
                item("arxiv:c", "arxiv", "R", "lc", 8.8), item("arxiv:d", "arxiv", "R", "lx", 8.7),
                item("arxiv:e", "arxiv", "R", "ly", 8.6),
                item("blog:x1", "bx", "M", "lx", 2.0), item("blog:x2", "bx2", "M", "lx", 1.9),
                item("blog:y1", "by", "M", "ly", 1.8), item("blog:y2", "by2", "M", "ly", 1.7),
                item("blog:z1", "bz", "M", "lz", 1.6)]
        picked = {entry.id for entry in select(pool, limit=8, per_source=3, max_arxiv=4)}
        self.assertEqual(picked, {"arxiv:a", "arxiv:b", "arxiv:c", "arxiv:d", "blog:x1", "blog:y1", "blog:y2",
                                  "blog:z1"})

    def test_the_fill_pass_keeps_its_old_rule(self) -> None:
        # 09's condition 2: a short day keeps every item the old rule kept. Two in one lane held by per_source,
        # nothing else to take: the fill pass takes both, as before.
        pool = [item("arxiv:a", "arxiv", "R", "la", 9.0), item("arxiv:b", "arxiv", "R", "lx", 8.9),
                item("arxiv:c", "arxiv", "R", "lx", 8.8)]
        self.assertEqual([entry.id for entry in select(pool, limit=8, per_source=1)],
                         ["arxiv:a", "arxiv:b", "arxiv:c"])

    def test_two_held_items_in_one_lane_hold_only_one_slot(self) -> None:
        # The recorded 2026-09-25 shape: two papers held in one lane must not keep a post out of it.
        pool = [item("arxiv:a", "arxiv", "R", "la", 9.0), item("arxiv:b", "arxiv", "R", "ls", 8.9),
                item("arxiv:c", "arxiv", "R", "ls", 8.8), item("blog:s", "bs", "O", "ls", 1.7),
                item("blog:t", "bt", "M", "lt", 1.6)]
        picked = [entry.id for entry in select(pool, limit=3, per_source=1)]
        self.assertEqual(picked, ["arxiv:a", "blog:s", "blog:t"])

    def test_no_caps_means_no_change(self) -> None:
        # Without per_source and per_evidence nothing is held, so the list is rank order under the lane cap.
        pool = ranked([*picks_1022(), aw0()])
        expected, lanes = [], {}
        for entry in pool:
            if lanes.get(entry.lane, 0) < 2 and len(expected) < 8:
                expected.append(entry)
                lanes[entry.lane] = lanes.get(entry.lane, 0) + 1
        self.assertEqual(select(pool, limit=8), [entry for entry in pool if entry in expected])


if __name__ == "__main__":
    unittest.main()
