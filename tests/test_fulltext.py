"""Full text from arXiv HTML for deep reads (pairadar/fulltext.py). Offline: fixtures only."""
from pathlib import Path
import unittest

from pairadar import fulltext

FIXTURE = Path(__file__).parent / "fixtures" / "arxiv-html-sample.html"


class FullTextParseTest(unittest.TestCase):
    def setUp(self):
        self.raw = FIXTURE.read_bytes()
        self.doc = fulltext.parse(self.raw, "0000.00001")

    def test_title_sections_and_math_alttext(self):
        self.assertEqual(self.doc.title, "Fixture: Memory for Humanoid Loco-Manipulation")
        headings = [s.heading for s in self.doc.sections]
        self.assertEqual(headings, ["Abstract", "I Introduction", "V Experiments", "VI Limitations and Conclusion"])
        intro = self.doc.sections[1].text
        self.assertIn("L=6", intro)
        self.assertEqual(intro.count("L=6"), 1)          # the annotation and MathML nodes are not repeated
        self.assertIn("Six real tasks only", self.doc.sections[3].text)

    def test_tables_keep_caption_and_cells(self):
        [table] = self.doc.tables
        self.assertEqual(table.caption, "Table II: Success rate (%) on memory tasks.")
        self.assertEqual(table.rows, [["Method", "Sim", "Real"], ["Base", "29.1", "36.7"], ["Ours", "72.6", "88.3"]])
        experiments = self.doc.sections[2].text
        self.assertNotIn("72.6", experiments)            # table cells live in the table, not the prose
        self.assertEqual(experiments.splitlines(), ["Table II reports 30 real trials per task.", "Second line."])
        self.assertEqual(self.doc.figures, ["Fig. 4: Real G1 rollout."])

    def test_page_chrome_authors_bibliography_and_scripts_are_dropped(self):
        text = self.doc.as_text()
        for canary in ("SCRIPT-CANARY", "NAV-CANARY", "HEADER-CANARY", "AUTHOR-CANARY", "BIB-CANARY",
                       "FOOTER-CANARY", "Refer to caption"):
            self.assertNotIn(canary, text)
        self.assertIn("## § V Experiments", text)
        self.assertIn("[Table II: Success rate (%) on memory tasks.]\n| Method | Sim | Real |", text)
        self.assertIn(self.doc.sha256[:16], text)

    def test_identifier_validation_and_fixed_host(self):
        self.assertEqual(fulltext.html_url("0000.00001v2"), "https://arxiv.org/html/0000.00001v2")
        for bad in ("", "2610.311", "../0000.00001", "0000.00001/../x", "https://evil/0000.00001", None):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                fulltext.html_url(bad)

    def test_fetch_uses_only_the_arxiv_html_url_and_refuses_non_latexml(self):
        asked = []

        def get(url):
            asked.append(url)
            return self.raw

        doc = fulltext.fetch_document("0000.00001", get=get)
        self.assertEqual(asked, ["https://arxiv.org/html/0000.00001"])
        self.assertEqual(doc.tables[0].rows[2], ["Ours", "72.6", "88.3"])
        for answer in (None, b"", b"<html>No HTML for this paper</html>",
                       b"ltx_document" + b" " * (fulltext.MAX_HTML_BYTES + 1)):
            with self.subTest(answer=answer[:20] if answer else answer):
                self.assertIsNone(fulltext.fetch_document("0000.00001", get=lambda url: answer))

    def test_text_is_bounded(self):
        doc = fulltext.parse(self.raw, "0000.00001")
        doc.sections[1].text = "x" * (fulltext.MAX_TEXT_CHARS + 10)
        text = doc.as_text()
        self.assertTrue(text.endswith("[truncated]\n"))
        self.assertLessEqual(len(text), fulltext.MAX_TEXT_CHARS + 20)


if __name__ == "__main__":
    unittest.main()
