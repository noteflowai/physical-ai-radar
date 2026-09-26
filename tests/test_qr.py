"""The QR codes on the landing page and the day's poster: the fixed parts of the
symbol, its format word, and two known answers from Project Nayuki's qrcodegen.

A code that does not scan fails silently in somebody's WeChat, so this is checked
here rather than by eye.
"""
from __future__ import annotations

import hashlib
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pairadar import qr  # noqa: E402

LANDING = "https://noteflowai.github.io/physical-ai-radar/"
DAY_PAGE = "https://noteflowai.github.io/physical-ai-radar/radar/daily/2026-09-25.zh.html"


def digest(grid: list[list[bool]]) -> str:
    text = "\n".join("".join("1" if dark else "0" for dark in row) for row in grid)
    return hashlib.sha256(text.encode()).hexdigest()


def format_word(grid: list[list[bool]]) -> int:
    """The 15 format bits as drawn beside the top-left finder."""
    cells = [(8, i) for i in range(6)] + [(8, 7), (8, 8), (7, 8)] + [(14 - i, 8) for i in range(9, 15)]
    return sum(grid[y][x] << i for i, (x, y) in enumerate(cells))


class QrTest(unittest.TestCase):
    def test_known_answers(self) -> None:
        # qrcodegen 1.8, byte mode, level M, fixed mask, no error-correction boost
        self.assertEqual(digest(qr.matrix(LANDING, mask=3)),
                         "160e801757e46ced59604d54b4fd66d645c4a999cc035a3bcdc61eccbaf9a86a")
        self.assertEqual(digest(qr.matrix("雷达" * 33, mask=6)),
                         "856e518f3a94453370d4c7b7c708a8cad2bff95f443aed9c8d4052b931d65ea5")

    def test_the_smallest_version_that_fits(self) -> None:
        for text, version in (("a", 1), (LANDING, 4), (DAY_PAGE, 5), ("x" * 213, 10)):
            with self.subTest(length=len(text)):
                self.assertEqual(len(qr.matrix(text)), version * 4 + 17)
        with self.assertRaises(ValueError):
            qr.matrix("x" * 214)

    def test_finders_and_timing(self) -> None:
        grid = qr.matrix(DAY_PAGE)
        size = len(grid)
        finder = [[max(abs(x - 3), abs(y - 3)) not in (2,) for x in range(7)] for y in range(7)]
        for ox, oy in ((0, 0), (size - 7, 0), (0, size - 7)):
            with self.subTest(corner=(ox, oy)):
                self.assertEqual([row[ox:ox + 7] for row in grid[oy:oy + 7]], finder)
        self.assertEqual([grid[6][x] for x in range(8, size - 8)], [x % 2 == 0 for x in range(8, size - 8)])
        self.assertEqual([grid[y][6] for y in range(8, size - 8)], [y % 2 == 0 for y in range(8, size - 8)])
        self.assertTrue(grid[size - 8][8], "the dark module")

    def test_the_format_word_names_level_m_and_its_mask(self) -> None:
        for mask in range(8):
            with self.subTest(mask=mask):
                word = format_word(qr.matrix(LANDING, mask=mask)) ^ 0x5412
                self.assertEqual(word >> 13, qr.FORMAT_M)
                self.assertEqual(word >> 10 & 7, mask)
                rem = word >> 10
                for _ in range(10):
                    rem = (rem << 1) ^ ((rem >> 9) * 0x537)
                self.assertEqual(word & 0x3FF, rem & 0x3FF, "BCH check bits")

    def test_svg_has_a_quiet_zone_and_escapes_its_label(self) -> None:
        svg = qr.svg(LANDING, label='"<scan>"')
        size = len(qr.matrix(LANDING)) + 8
        self.assertIn(f'viewBox="0 0 {size} {size}"', svg)
        self.assertIn('aria-label="&quot;&lt;scan&gt;&quot;"', svg)
        self.assertNotIn("M0,", svg)


if __name__ == "__main__":
    unittest.main()
