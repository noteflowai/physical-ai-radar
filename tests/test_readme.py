"""The repository's front page: the banner each README opens on and the day's block
between the RADAR markers. Both are drawn from fetched text, so both are checked
for escaping as well as for what they show.
"""
from __future__ import annotations

import re
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT/"tests"))

from pairadar import banner  # noqa: E402
from pairadar.charts import text_width  # noqa: E402
from pairadar.config import LANGS, load_config  # noqa: E402
from pairadar.render import MARKER_END, MARKER_START, README_FILES, readme_block  # noqa: E402
from test_site import context, item  # noqa: E402

SVG = "{http://www.w3.org/2000/svg}"


class BannerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.picked = [item("RoboRecover: Benchmarking <Robot> Policy & Recovery", numbers=["4.5x"]),
                       item("Soft gripper", lane="hardware", evidence="O")]

    def test_every_language_is_valid_svg_with_the_day_and_its_counts(self) -> None:
        for lang in LANGS:
            with self.subTest(lang=lang):
                svg = banner.banner_svg(self.config, lang, context(self.picked))
                root = ET.fromstring(svg)
                self.assertEqual(root.get("viewBox"), f"0 0 {banner.WIDTH} {banner.HEIGHT}")
                text = " ".join("".join(node.itertext()) for node in root.iter(f"{SVG}text"))
                self.assertIn("2026-09-25", text)
                for value in ("2", "924", "8/8", "7"):
                    self.assertIn(value, text.split())
                headline = self.config.ui(lang)["site"]["headline"]
                self.assertIn(headline, root.get("aria-label"))

    def test_fetched_text_stays_text(self) -> None:
        svg = banner.banner_svg(self.config, "en", context(self.picked))
        self.assertNotIn("<Robot>", svg)
        ET.fromstring(svg)

    def test_motion_stops_under_reduced_motion(self) -> None:
        svg = banner.banner_svg(self.config, "zh", context(self.picked))
        self.assertRegex(svg, r"@media \(prefers-reduced-motion:reduce\)\{[^}]*animation:none\}\.sweep\{display:none\}")

    def test_wrapping_fits_the_width_and_keeps_closing_punctuation(self) -> None:
        text = "每天一页物理 AI 雷达：八条赛道，每条都有证据标签，数字可以回到原文核对。"
        lines = banner.wrap(text, 300, 20)
        self.assertEqual("".join(lines).replace(" ", ""), text.replace(" ", ""))
        for line in lines:
            self.assertLessEqual(text_width(line, 20), 300 + text_width("。", 20))
            self.assertFalse(line[0] in banner.CLOSING, line)
        short = banner.wrap(text, 300, 20, lines=2)
        self.assertEqual(len(short), 2)
        self.assertTrue(short[-1].endswith("…"))

    def test_the_banners_are_written_per_language(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = banner.write_banners(self.config, context(self.picked), Path(temporary), LANGS)
            self.assertEqual([path.name for path in paths], [f"banner.{lang}.svg" for lang in LANGS])


class ReadmeBlockTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.picked = [item("A | B: pipes in a title", numbers=["4.30 ms", "12 Hz", "3x"]),
                       item("Soft gripper", lane="hardware", evidence="O"),
                       item("Odd evidence", evidence="?")]

    def block(self, lang: str = "en") -> str:
        return readme_block(self.config, lang, context(self.picked, window="w"))

    def rows(self, block: str) -> list[str]:
        return [line for line in block.splitlines() if re.match(r"\| \d\d \|", line)]

    def test_every_pick_is_one_row_of_four_cells(self) -> None:
        for lang in LANGS:
            with self.subTest(lang=lang):
                rows = self.rows(self.block(lang))
                self.assertEqual(len(rows), len(self.picked))
                for row in rows:
                    cells = re.split(r"(?<!\\)\|", row)[1:-1]
                    self.assertEqual(len(cells), 4, row)

    def test_pipes_are_escaped_and_figures_do_not_break(self) -> None:
        row = self.rows(self.block())[0]
        self.assertIn(r"A \| B", row)
        self.assertIn("`4.30 ms`<br>`12 Hz`", row)
        self.assertNotIn("3x", row, "at most two figures per row")

    def test_unknown_evidence_reads_as_media(self) -> None:
        self.assertIn("🟡&nbsp;`M`", self.rows(self.block())[2])

    def test_the_reasons_fold_under_the_table(self) -> None:
        block = self.block()
        self.assertIn("<details><summary>", block)
        self.assertLess(block.index("| 03 |"), block.index("<details>"))
        self.assertLess(block.index("<details>"), block.index("</details>"))
        self.assertIn("1. **A | B** — ", block, "the reasons name each pick by its short title")

    def test_the_committed_readmes_carry_the_block_and_the_banner(self) -> None:
        for lang, name in README_FILES.items():
            with self.subTest(lang=lang):
                text = (ROOT/name).read_text(encoding="utf-8")
                self.assertEqual(text.count(MARKER_START), 1)
                self.assertEqual(text.count(MARKER_END), 1)
                self.assertIn(f'src="assets/banner.{lang}.svg"', text)
                self.assertTrue((ROOT/"assets"/f"banner.{lang}.svg").exists())


if __name__ == "__main__":
    unittest.main()
