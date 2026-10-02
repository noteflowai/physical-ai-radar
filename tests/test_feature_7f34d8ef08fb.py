"""RA-02: a short daily issue says why it is short, and keeps saying so on re-render.

All items here are synthetic fixtures built in the test; they are not real radar picks.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pairadar import cli, render
from pairadar.config import Item, load_config
from pairadar.distill import select
from pairadar.render import render_daily, sparse_note

LANGS = ("zh", "en", "ja")


def synthetic_item(index: int, lane: str = "edge") -> Item:
    return Item(
        id=f"synthetic:{index}", title=f"Synthetic fixture paper number {index} about {lane}",
        url=f"https://example.org/synthetic/{index}", publisher="Example",
        source_id=f"source-{index}", evidence="R", published="2026-09-19",
        summary="A synthetic excerpt.", lane=lane, lane_locked=True, score=3.0 - index * 0.1,
    )


def make_ctx(config, picked, **extra):
    ctx = {
        "date": "2026-09-19", "generated": "2026-09-19 01:30 UTC",
        "window": "2026-09-16 → 2026-09-19 (UTC)", "picked": picked,
        "baseline": [], "baseline_all": [], "curated": {},
        "lane_rows": [(lane["id"], 0) for lane in config.lanes],
        "mix": {"O": 0, "R": len(picked), "M": 0},
        "cadence": [("2026-09-19", len(picked))],
    }
    ctx.update(extra)
    return ctx


class SparseDayPageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.picked = [synthetic_item(i) for i in range(1, 4)]

    def test_short_day_states_n_of_up_to_m_under_the_heading(self) -> None:
        for lang in LANGS:
            note = sparse_note(lang, 3, 8)
            self.assertIsNotNone(note, lang)
            self.assertIn("3", note)
            self.assertIn("8", note)
            with_limit = render_daily(self.config, lang, make_ctx(self.config, self.picked, limit=8))
            without = render_daily(self.config, lang, make_ctx(self.config, self.picked))
            line = f"> {note}"
            self.assertEqual(with_limit.count(line), 1, lang)
            heading = with_limit.index(f"## {self.config.ui(lang)['top_items']}")
            first_item = with_limit.index(self.picked[0].title, heading)
            self.assertLess(heading, with_limit.index(line))
            self.assertLess(with_limit.index(line), first_item)
            self.assertEqual(with_limit.replace(line + "\n\n", "", 1), without,
                             f"{lang}: only the note may differ")
            self.assertNotIn(note, without)

    def test_lane_cap_short_day_is_not_blamed_on_quality(self) -> None:
        pool = [synthetic_item(i, lane="edge") for i in range(1, 5)]
        picked = select(pool, limit=8, per_lane=2, min_score=0.0)
        self.assertEqual(len(picked), 2, "the lane cap holds back qualifying items")
        page = render_daily(self.config, "en", make_ctx(self.config, list(picked), limit=8))
        note = sparse_note("en", 2, 8)
        self.assertIn(f"> {note}", page)
        for check in ("quality", "topic-diversity", "repeat"):
            self.assertIn(check, note)
        self.assertNotIn("quality threshold", note)

    def test_full_empty_and_unlimited_days_render_unchanged(self) -> None:
        full = [synthetic_item(i) for i in range(1, 4)]
        for lang in LANGS:
            base = render_daily(self.config, lang, make_ctx(self.config, full))
            self.assertEqual(render_daily(self.config, lang, make_ctx(self.config, full, limit=3)), base)
            empty = render_daily(self.config, lang, make_ctx(self.config, []))
            self.assertEqual(render_daily(self.config, lang, make_ctx(self.config, [], limit=8)), empty)
            self.assertIn(self.config.ui(lang)["no_items"], empty)

    def test_invalid_limits_give_no_note_and_never_raise(self) -> None:
        for bad in (None, "8", True, -1, 2.5):
            self.assertIsNone(sparse_note("en", 3, bad), repr(bad))
            base = render_daily(self.config, "en", make_ctx(self.config, self.picked))
            page = render_daily(self.config, "en", make_ctx(self.config, self.picked, limit=bad))
            self.assertEqual(page, base, repr(bad))
        self.assertIsNone(sparse_note("en", 0, 8))
        self.assertIsNone(sparse_note("en", 8, 8))


class SparseDayPersistenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.item = synthetic_item(1)

    def latest_ctx(self, **extra):
        ctx = {"date": "2026-09-19", "generated": "g", "window": "w", "picked": [],
               "lane_rows": [("edge", 1)], "mix": {"O": 0, "R": 1, "M": 0}, "baseline_all": []}
        ctx.update(extra)
        return ctx

    def test_write_latest_adds_only_a_valid_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plain = json.loads(render.write_latest(self.latest_ctx(), root=root).read_text(encoding="utf-8"))
            limited = json.loads(render.write_latest(self.latest_ctx(limit=8), root=root).read_text(encoding="utf-8"))
            bogus = json.loads(render.write_latest(self.latest_ctx(limit=True), root=root).read_text(encoding="utf-8"))
        self.assertNotIn("limit", plain)
        self.assertEqual(limited.pop("limit"), 8)
        self.assertEqual(limited, plain, "every other key is unchanged")
        self.assertNotIn("limit", bogus)

    def write_snapshot(self, root: Path, limit=None) -> None:
        (root/"radar").mkdir(parents=True, exist_ok=True)
        payload = {
            "date": "2026-09-19", "generated": "2026-09-19 01:30 UTC",
            "window": "2026-09-16 → 2026-09-19 (UTC)", "picked": [self.item.to_dict()],
            "lane_counts": {lane["id"]: 0 for lane in self.config.lanes},
            "evidence_mix": {"O": 0, "R": 1, "M": 0}, "baseline_count": 29,
        }
        if limit is not None:
            payload["limit"] = limit
        (root/"radar"/"latest.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        (root/"radar"/"history.json").write_text(
            json.dumps({"runs": [{"date": "2026-09-19", "count": 1, "lanes": 1}]}), encoding="utf-8")

    def rerendered(self, limit) -> dict:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.write_snapshot(root, limit)
            with patch.object(cli, "fetch_all", side_effect=AssertionError("must not fetch")):
                cli.rerender(source=root)
            return {lang: (root/"radar"/"daily"/f"2026-09-19.{lang}.md").read_text(encoding="utf-8")
                    for lang in LANGS}

    def test_rerender_keeps_the_note_when_the_snapshot_has_a_limit(self) -> None:
        for lang, page in self.rerendered(8).items():
            self.assertIn(f"> {sparse_note(lang, 1, 8)}", page, lang)

    def test_rerender_of_an_old_snapshot_shows_no_note(self) -> None:
        for lang, page in self.rerendered(None).items():
            self.assertNotIn(sparse_note(lang, 1, 8), page, lang)

    def test_a_run_records_its_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary)
            ctx = cli.run(offline=True, day="2026-03-06", out=out)
            stored = json.loads((out/"radar"/"latest.json").read_text(encoding="utf-8"))
        self.assertIsInstance(ctx["limit"], int)
        self.assertEqual(stored["limit"], ctx["limit"])
        self.assertLessEqual(len(ctx["picked"]), ctx["limit"])


if __name__ == "__main__":
    unittest.main()
