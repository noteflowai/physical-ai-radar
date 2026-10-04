"""The README radar block on a day with no live picks (roadmap RA-02).

The curated baseline used to be shown under today's heading as if it were the day's
selection. These tests read the rendered block as a reader would: the empty day is
named, the fallback table is labelled as the baseline, and normal days are unchanged.
"""
from __future__ import annotations

import re
import sys
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pairadar import render  # noqa: E402
from pairadar.config import LANGS, load_config, md_url  # noqa: E402
from pairadar.distill import enrich  # noqa: E402
from pairadar.fetch import baseline_items  # noqa: E402
from pairadar.render import readme_block  # noqa: E402

ROW = re.compile(r"^\| \d{2}<br>")


class ReadmeEmptyDayTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.items = enrich(baseline_items(self.config), self.config, date(2026, 9, 19))
        self.assertGreaterEqual(len(self.items), 4)

    def lines(self, lang, picked, baseline):
        ctx = {
            "date": "2026-09-19",
            "generated": "2026-09-19 01:30 UTC",
            "window": "2026-09-16 → 2026-09-19 (UTC)",
            "picked": picked,
            "baseline": baseline,
            "curated": {},
            "notes": {},
        }
        return readme_block(self.config, lang, ctx).splitlines()

    def find(self, lines, predicate, what):
        for position, line in enumerate(lines):
            if predicate(line):
                return position
        self.fail(f"no {what} line in block")

    def link(self, item):
        return f"**[{render._cell(item.title)}]({md_url(item.url)})**"

    def test_empty_day_names_the_day_and_labels_the_baseline(self) -> None:
        baseline = self.items[:4]
        for lang in LANGS:
            ui = self.config.ui(lang)
            lines = self.lines(lang, [], baseline)
            generated = self.find(lines, lambda l: l.startswith(f"`{ui['generated']}:"), "generated")
            note = self.find(lines, lambda l: l == f"> {ui['no_items']}", "no_items")
            label = self.find(lines, lambda l: l == f"**{ui['baseline']}**", "baseline label")
            header = self.find(lines, lambda l: l.startswith("| # |"), "table header")
            first_row = self.find(lines, lambda l: bool(ROW.match(l)), "table row")
            self.assertLess(generated, note, lang)
            self.assertLess(note, label, lang)
            self.assertLess(label, header, lang)
            self.assertLess(header, first_row, lang)
            rows = [line for line in lines if ROW.match(line)]
            self.assertEqual(len(rows), len(baseline), lang)
            for row, item in zip(rows, baseline):
                self.assertIn(self.link(item), row, lang)

    def test_day_with_picks_is_not_labelled(self) -> None:
        picked = self.items[:3]
        for lang in LANGS:
            ui = self.config.ui(lang)
            lines = self.lines(lang, picked, self.items[:4])
            self.assertNotIn(f"> {ui['no_items']}", lines, lang)
            self.assertNotIn(f"**{ui['baseline']}**", lines, lang)
            generated = self.find(lines, lambda l: l.startswith(f"`{ui['generated']}:"), "generated")
            # the table still follows the window line directly, as before
            self.assertEqual(lines[generated + 1], "", lang)
            self.assertTrue(lines[generated + 2].startswith("| # |"), lang)
            rows = [line for line in lines if ROW.match(line)]
            self.assertEqual(len(rows), len(picked), lang)
            for row, item in zip(rows, picked):
                self.assertIn(self.link(item), row, lang)

    def test_nothing_at_all_still_renders(self) -> None:
        for lang in LANGS:
            ui = self.config.ui(lang)
            lines = self.lines(lang, [], [])
            self.assertIn(f"> {ui['no_items']}", lines, lang)
            self.assertNotIn(f"**{ui['baseline']}**", lines, lang)
            self.find(lines, lambda l: l.startswith("| # |"), "table header")
            self.assertFalse([line for line in lines if ROW.match(line)], lang)


if __name__ == "__main__":
    unittest.main()
