"""The deep-read pilot runner (scripts/editorial_pilot.py): offline, private output outside the repo."""
import importlib.util
import json
from pathlib import Path
import stat
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("editorial_pilot", ROOT / "scripts" / "editorial_pilot.py")
pilot = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pilot)
FIXTURE = (ROOT / "tests" / "fixtures" / "arxiv-html-sample.html").read_bytes()


def item(n, lane, score, evidence="R", kind="arxiv"):
    ident = f"arxiv:0000.0000{n}" if kind == "arxiv" else f"web:{n}"
    return {"id": ident, "title": f"Paper {n}", "url": f"https://arxiv.org/abs/0000.0000{n}", "lane": lane,
            "score": score, "evidence": evidence, "summary": f"Abstract {n}."}


class PilotTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.out = Path(tmp.name) / "pilot"

    def test_three_deep_reads_in_distinct_lanes_with_full_text_then_five_index_items(self):
        items = [item(1, "foundation", 3.0), item(2, "foundation", 2.9), item(3, "simeval", 2.8),
                 item(4, "systems", 2.5), item(5, "edge", 2.4), item(6, "edge", 2.0, evidence="M", kind="web"),
                 item(7, "hardware", 1.9), item(8, "training", 1.0, evidence="O", kind="web")]
        asked = []

        def get(url):
            asked.append(url)
            return None if url.endswith("0000.00003") else FIXTURE   # paper 3 has no HTML rendering

        deep, index = pilot.choose(items, [], get)
        self.assertEqual([i["id"] for i, _ in deep], ["arxiv:0000.00001", "arxiv:0000.00004", "arxiv:0000.00005"])
        self.assertNotIn("https://arxiv.org/html/0000.00002", asked)   # same lane as paper 1: not fetched
        self.assertEqual([i["id"] for i in index],
                         ["arxiv:0000.00002", "arxiv:0000.00003", "web:6", "arxiv:0000.00007", "web:8"])
        named, _ = pilot.choose(items, ["0000.00002", "0000.00001"], lambda url: FIXTURE)
        self.assertEqual([i["id"] for i, _ in named], ["arxiv:0000.00002", "arxiv:0000.00001"])

    def test_brief_carries_style_full_text_and_index(self):
        deep, index = pilot.choose([item(1, "foundation", 3.0), item(2, "edge", 1.0, evidence="M", kind="web")],
                                   [], lambda url: FIXTURE)
        text = pilot.brief("2026-10-25", deep, index)
        self.assertIn("## Deep-read evidence", text)
        self.assertIn("| Ours | 72.6 | 88.3 |", text)
        self.assertIn("**Bottom line**", text)            # the style document is embedded
        self.assertIn("- Paper 2 | https://arxiv.org/abs/0000.00002 | [M] |", text)

    def test_output_must_be_outside_the_repository_and_private(self):
        with self.assertRaises(SystemExit):
            pilot.day_dir(ROOT / "scratch", "2026-10-25")
        with self.assertRaises(SystemExit):
            pilot.day_dir(self.out, "25-10-2026")
        path = pilot.day_dir(self.out, "2026-10-25") / "brief.md"
        pilot.write_private(path, "x")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)

    def test_old_page_metrics_use_the_why_lines(self):
        page = ("# Day\n## Today's picks\n"
                "### 1. A\n- **Why it matters** `AI draft`: The visible excerpt gives no figures, read the paper first.\n"
                "### 2. B\n- **Why it matters** `AI draft`: The visible excerpt gives no figures, read the paper first.\n"
                "## Frontier baseline (curated)\n### 1. C\n- **Why it matters**: A hand-written baseline note.\n")
        metrics = pilot.old_metrics(page, "en", pilot.editorial.load_style())
        self.assertEqual(metrics["items"], 2)                  # the curated baseline is not compared
        self.assertGreaterEqual(metrics["banned_total"], 2)
        self.assertEqual(metrics["template_rate"], 1.0)
        ja = "# 日\n## 本日の注目\n### 1. A\n- **重要な理由**: 査読前のプレプリント。\n"
        self.assertEqual(pilot.old_metrics(ja, "ja", pilot.editorial.load_style())["items"], 1)

    def test_blind_pair_hides_which_is_new(self):
        day = "2026-10-25"
        base = pilot.day_dir(self.out, day)
        for lang in pilot.LANGS:
            pilot.write_private(base / f"new.{lang}.md", f"NEW {lang}")
        original_root = pilot.ROOT
        fake_root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(fake_root, ignore_errors=True))
        for lang in pilot.LANGS:
            (fake_root / "radar" / "daily").mkdir(parents=True, exist_ok=True)
            (fake_root / "radar" / "daily" / f"{day}.{lang}.md").write_text(f"OLD {lang}")
        pilot.ROOT = fake_root
        try:
            pilot.cmd_blind(type("A", (), {"date": day, "out": self.out})())
        finally:
            pilot.ROOT = original_root
        key = json.loads((base / "blind-key.json").read_text())
        for lang in pilot.LANGS:
            a = (base / "blind" / f"A.{lang}.md").read_text()
            self.assertEqual(a.split()[0].lower(), key[lang]["A"])
            self.assertEqual({key[lang]["A"], key[lang]["B"]}, {"new", "old"})
        self.assertEqual(stat.S_IMODE((base / "blind-key.json").stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
