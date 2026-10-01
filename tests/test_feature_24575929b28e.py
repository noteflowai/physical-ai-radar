"""The README block on a day with no qualifying picks.

The front page used to list the rotated curated baseline under the \"today\" heading
with nothing saying that no new item qualified. These cases are synthetic contexts
built with the shared test helpers; they check the rendered Markdown text only.
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT/"tests"))

from pairadar.config import LANGS, load_config  # noqa: E402
from pairadar.render import readme_block  # noqa: E402
from test_site import context, item  # noqa: E402

ROW = re.compile(r"^\| \d\d<br>", re.MULTILINE)


class EmptyDayReadmeBlockTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.picks = [item("Fresh pick about grasping", numbers=["12 Hz"]),
                      item("Soft gripper", lane="hardware", evidence="O")]
        self.baseline = [item("Curated baseline alpha", evidence="R"),
                         item("Curated baseline beta", lane="hardware", evidence="O")]

    def block(self, lang: str, picked, baseline) -> str:
        ctx = context(list(self.picks), window="w")
        ctx["picked"] = list(picked)
        ctx["baseline"] = list(baseline)
        return readme_block(self.config, lang, ctx)

    def test_an_empty_day_explains_itself_and_labels_the_baseline(self) -> None:
        for lang in LANGS:
            with self.subTest(lang=lang):
                ui = self.config.ui(lang)
                block = self.block(lang, [], self.baseline)
                self.assertIn(ui["no_items"], block)
                label = f"**{ui['baseline']}**"
                self.assertIn(label, block)
                first_row = ROW.search(block)
                self.assertIsNotNone(first_row, "the baseline is still listed")
                self.assertLess(block.index(ui["no_items"]), first_row.start())
                self.assertLess(block.index(label), first_row.start())
                self.assertLess(block.index(ui["no_items"]), block.index(label))

    def test_the_baseline_entries_and_their_reasons_still_render(self) -> None:
        for lang in LANGS:
            with self.subTest(lang=lang):
                block = self.block(lang, [], self.baseline)
                self.assertEqual(len(ROW.findall(block)), len(self.baseline))
                for entry in self.baseline:
                    self.assertIn(entry.title, block)
                self.assertIn("<details><summary>", block)

    def test_a_day_with_picks_gains_no_empty_day_text(self) -> None:
        for lang in LANGS:
            with self.subTest(lang=lang):
                ui = self.config.ui(lang)
                block = self.block(lang, self.picks, self.baseline)
                self.assertNotIn(ui["no_items"], block)
                self.assertNotIn(f"**{ui['baseline']}**", block)
                self.assertEqual(len(ROW.findall(block)), len(self.picks))
                for entry in self.baseline:
                    self.assertNotIn(entry.title, block)

    def test_a_day_with_picks_does_not_depend_on_the_baseline(self) -> None:
        other = [item("Some other curated entry", evidence="M")]
        for lang in LANGS:
            with self.subTest(lang=lang):
                self.assertEqual(self.block(lang, self.picks, self.baseline),
                                 self.block(lang, self.picks, other))
                self.assertEqual(self.block(lang, self.picks, self.baseline),
                                 self.block(lang, self.picks, []))

    def test_nothing_at_all_prints_the_notice_and_no_empty_table(self) -> None:
        for lang in LANGS:
            with self.subTest(lang=lang):
                ui = self.config.ui(lang)
                block = self.block(lang, [], [])
                self.assertIn(ui["no_items"], block)
                self.assertNotIn("| :-: | --- |", block)
                self.assertNotIn(f"| # | {ui['pick']}", block)
                self.assertNotIn(f"**{ui['baseline']}**", block)
                self.assertNotIn("<details>", block)
                self.assertIn(f"radar/daily/{context([])['date']}.{lang}.md", block)


if __name__ == "__main__":
    unittest.main()
