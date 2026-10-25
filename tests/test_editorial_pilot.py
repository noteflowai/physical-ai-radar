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

    def blind_fixture(self, author_kind="llm-draft", notes_day="2026-10-25"):
        """Three deep reads per language and a model-notes file for the same three URLs."""
        day = "2026-10-25"
        base = pilot.day_dir(self.out, day)
        urls = [f"https://arxiv.org/abs/0000.0000{n}" for n in (1, 2, 3)]
        source = {"zh": "来源：", "en": "Source: ", "ja": "出典："}
        tail = {"zh": " · 全文（arXiv HTML）", "en": " · full text (arXiv HTML)", "ja": " · 全文（arXiv HTML）"}
        heading = {"zh": "深读", "en": "Deep reads", "ja": "深掘り"}
        for lang in pilot.LANGS:
            text = f"# Physical AI Radar · {day} (deep-read pilot)\n\n## {heading[lang]}\n\n"
            for n, url in enumerate(urls, 1):
                text += (f"### {n}. Paper {n}\n\n{source[lang]}[arXiv 0000.0000{n}]({url}){tail[lang]}\n\n"
                         f"**Bottom line** NEW-{lang}-{n} body.\n\n**Limits** more.\n\n")
            text += "## Also today\n\n- [R] [X](https://example.org/x): one.\n"
            pilot.write_private(base / f"new.{lang}.md", text)
        notes = {"schema": "pairadar-notes-1", "date": notes_day, "author": {"kind": author_kind},
                 "notes": {url: {lang: f"OLD-{lang}-{n} note." for lang in pilot.LANGS}
                           for n, url in enumerate(urls, 1)}}
        path = Path(tempfile.mkdtemp()) / "notes.json"
        self.addCleanup(lambda: __import__("shutil").rmtree(path.parent, ignore_errors=True))
        path.write_text(json.dumps(notes), encoding="utf-8")
        keys = Path(tempfile.mkdtemp()) / "keys"
        self.addCleanup(lambda: __import__("shutil").rmtree(keys.parent, ignore_errors=True))
        return day, base, path, keys

    def run_blind(self, day, notes, keys):
        return pilot.cmd_blind(type("A", (), {"date": day, "out": self.out, "notes": notes, "keys": keys})())

    def test_blind_pair_is_one_neutral_template_with_only_the_body_differing(self):
        day, base, notes, keys = self.blind_fixture()
        self.run_blind(day, notes, keys)
        key = json.loads((keys / f"{day}.json").read_text())
        names = sorted(p.name for p in (base / "blind").iterdir())
        self.assertEqual(names, sorted(f"{side}.{lang}.md" for side in "AB" for lang in pilot.LANGS))
        stamps = {p.stat().st_mtime_ns for p in (base / "blind").iterdir()}
        self.assertEqual(len(stamps), 1)                                  # no mtime to tell A from B
        for lang in pilot.LANGS:
            a = (base / "blind" / f"A.{lang}.md").read_text()
            b = (base / "blind" / f"B.{lang}.md").read_text()
            self.assertEqual({key[lang]["A"], key[lang]["B"]}, {"new", "old"})
            new, old = (a, b) if key[lang]["A"] == "new" else (b, a)
            self.assertIn(f"NEW-{lang}-1 body.", new)
            self.assertIn(f"OLD-{lang}-1 note.", old)
            self.assertNotIn("NEW-", old)
            self.assertNotIn("OLD-", new)
            for text in (a, b):
                self.assertNotIn("pilot", text)                           # no version marker
                self.assertNotIn("Also today", text)                      # no index, only the three items
            def skeleton(text):                                           # everything but the body paragraphs
                return [line for line in text.splitlines() if line.startswith(("#", "Source", "来源", "出典"))]
            self.assertEqual(skeleton(a), skeleton(b))
            self.assertEqual(len(skeleton(a)), 7)                         # header + 3 x (title + source)
        self.assertEqual(stat.S_IMODE((keys / f"{day}.json").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(keys.stat().st_mode), 0o700)
        self.assertFalse((base / "blind-key.json").exists())              # nothing identifying beside the pair

    def test_blind_refuses_a_day_without_model_drafted_notes(self):
        day, base, notes, keys = self.blind_fixture()
        with self.assertRaises(SystemExit):
            self.run_blind(day, notes.parent / "absent.json", keys)       # the nightly has not run yet
        for author_kind, notes_day in (("rules", day), ("llm-draft", "2026-10-24")):
            bad = self.blind_fixture(author_kind, notes_day)
            with self.subTest(author_kind=author_kind, notes_day=notes_day), self.assertRaises(SystemExit):
                self.run_blind(bad[0], bad[2], bad[3])
        data = json.loads(notes.read_text())
        del data["notes"]["https://arxiv.org/abs/0000.00002"]
        notes.write_text(json.dumps(data))
        with self.assertRaises(SystemExit):                               # one item has no note: not a fair pair
            self.run_blind(day, notes, keys)
        self.assertFalse((base / "blind").exists())

    def test_blind_key_must_not_sit_beside_the_pair(self):
        day, base, notes, _ = self.blind_fixture()
        with self.assertRaises(SystemExit):
            self.run_blind(day, notes, base / "keys")


if __name__ == "__main__":
    unittest.main()
