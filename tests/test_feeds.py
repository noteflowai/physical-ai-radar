"""JSON Feed, Atom and weekly roundup output, built from the recorded 2026-09-25 day.

No test here touches the network.
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT/"tests"))

from pairadar import feeds  # noqa: E402
from pairadar.config import LANGS, README_FILES, Item, load_config  # noqa: E402
from pairadar.render import update_index  # noqa: E402
from test_quality import recorded_day, shortlist  # noqa: E402

ATOM = "{http://www.w3.org/2005/Atom}"
RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
GENERATED = "2026-09-25 01:40 UTC"


def make(index: int, day_url: str | None = None, lane: str = "hardware", title: str | None = None) -> Item:
    item = Item(id=f"blog:{index}", title=title or f"Humanoid post {index}",
                url=day_url or f"https://example.org/post/{index}", publisher="Example",
                source_id="blog", evidence="O", published="2026-09-24",
                summary="A humanoid robot walked 3 km at 1.2 m/s. More text follows here.")
    item.lane = lane
    return item


class RecordedFeedTest(unittest.TestCase):
    """The feeds a publish of the recorded day would write."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.config = load_config()
        cls.picked = shortlist(cls.config, recorded_day(cls.config))
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        ctx = {"date": "2026-09-25", "generated": GENERATED, "picked": cls.picked}
        cls.written = feeds.write_feeds(cls.config, ctx, root=cls.root, source=cls.root)
        cls.feed = json.loads((cls.root/"radar"/"feed.json").read_text(encoding="utf-8"))

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def test_the_json_feed_meets_version_1_1(self) -> None:
        feed = self.feed
        self.assertEqual(feed["version"], "https://jsonfeed.org/version/1.1")
        self.assertTrue(feed["title"])
        self.assertTrue(feed["feed_url"].endswith("/radar/feed.json"))
        self.assertEqual(len(feed["items"]), len(self.picked))
        ids = [entry["id"] for entry in feed["items"]]
        self.assertEqual(len(ids), len(set(ids)))
        for entry, item in zip(feed["items"], self.picked):
            with self.subTest(title=item.title):
                self.assertEqual(entry["title"], item.title, "shortlist order and titles are kept")
                self.assertEqual(entry["url"], item.url)
                self.assertRegex(entry["date_published"], RFC3339)
                self.assertTrue(entry["content_html"] and entry["content_text"])
                self.assertEqual(entry["authors"], [{"name": item.publisher}])
                self.assertTrue(all(key.startswith("_") for key in entry if key not in {
                    "id", "url", "title", "content_html", "content_text", "summary",
                    "date_published", "authors", "tags", "language"}), "extensions start with _")

    def test_every_atom_feed_is_well_formed_rfc_4287(self) -> None:
        for lang in LANGS:
            path = self.root/"radar"/feeds.atom_name(lang)
            with self.subTest(lang=lang):
                feed = ET.parse(path).getroot()
                self.assertEqual(feed.tag, f"{ATOM}feed")
                self.assertEqual(feed.get("{http://www.w3.org/XML/1998/namespace}lang"), lang)
                for required in ("id", "title", "updated", "author"):
                    self.assertIsNotNone(feed.find(f"{ATOM}{required}"), required)
                self_link = [link for link in feed.findall(f"{ATOM}link") if link.get("rel") == "self"]
                self.assertEqual(len(self_link), 1)
                self.assertTrue(self_link[0].get("href").endswith(f"/radar/{feeds.atom_name(lang)}"))
                entries = feed.findall(f"{ATOM}entry")
                self.assertEqual(len(entries), len(self.picked))
                for entry, item in zip(entries, self.picked):
                    self.assertEqual(entry.findtext(f"{ATOM}title"), item.title, "titles are never translated")
                    self.assertRegex(entry.findtext(f"{ATOM}updated"), RFC3339)
                    self.assertTrue(entry.findtext(f"{ATOM}id").startswith("tag:"))
                    links = {link.get("rel"): link.get("href") for link in entry.findall(f"{ATOM}link")}
                    self.assertEqual(links["alternate"], item.url)
                    self.assertTrue(links["related"].endswith(f"/radar/daily/2026-09-25.{lang}.md"))

    def test_the_feeds_are_labelled_in_their_language(self) -> None:
        for lang in LANGS:
            feed = ET.parse(self.root/"radar"/feeds.atom_name(lang)).getroot()
            first = feed.find(f"{ATOM}entry")
            with self.subTest(lang=lang):
                self.assertEqual(feed.findtext(f"{ATOM}title"), self.config.ui(lang)["title"])
                labels = [node.get("label") for node in first.findall(f"{ATOM}category")]
                self.assertIn(self.config.lane_name(self.picked[0].lane, lang), labels)
                self.assertIn(self.config.ui(lang)["lane"], first.findtext(f"{ATOM}content"))

    def test_the_atom_and_json_ids_agree_and_stay_stable(self) -> None:
        atom = ET.parse(self.root/"radar"/"feed.xml").getroot()
        atom_ids = [entry.findtext(f"{ATOM}id") for entry in atom.findall(f"{ATOM}entry")]
        self.assertEqual(atom_ids, [entry["id"] for entry in self.feed["items"]])
        paper = next(item for item in self.picked if item.id.startswith("arxiv:"))
        self.assertIn(feeds.entry_id(paper.id), atom_ids)
        self.assertEqual(feeds.entry_id(paper.id),
                         f"tag:noteflowai.github.io,2026:physical-ai-radar:{paper.id}")

    def test_the_weekly_pages_list_the_week_by_lane(self) -> None:
        for lang in LANGS:
            page = (self.root/"radar"/"weekly"/f"2026-W39.{lang}.md").read_text(encoding="utf-8")
            with self.subTest(lang=lang):
                self.assertIn("2026-09-21 → 2026-09-27", page)
                for item in self.picked:
                    self.assertIn(f"[{item.title}]({item.url})", page)
                self.assertIn(f"(../daily/2026-09-25.{lang}.md)", page)
                lanes = {item.lane for item in self.picked}
                self.assertEqual(page.count("\n## "), len(lanes), "one section per lane present")

    def test_the_index_links_the_feeds_and_the_week(self) -> None:
        index = update_index(self.config, {}, [], self.root).read_text(encoding="utf-8")
        for lang in LANGS:
            self.assertIn(f"]({feeds.atom_name(lang)})", index)
            self.assertIn(f"(weekly/2026-W39.{lang}.md)", index)
        self.assertIn("(feed.json)", index)


class StoreTest(unittest.TestCase):
    """feed.json is also the store the next run reads back."""

    def setUp(self) -> None:
        self.config = load_config()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def publish(self, day: str, picked: list[Item]) -> list[dict]:
        feeds.write_feeds(self.config, {"date": day, "generated": f"{day} 01:40 UTC", "picked": picked},
                          root=self.root, source=self.root)
        return json.loads((self.root/"radar"/"feed.json").read_text(encoding="utf-8"))["items"]

    def test_rerunning_a_day_replaces_it(self) -> None:
        self.publish("2026-09-24", [make(1), make(2)])
        self.publish("2026-09-25", [make(3)])
        items = self.publish("2026-09-25", [make(4), make(5)])
        self.assertEqual([entry["_radar"]["id"] for entry in items], ["blog:4", "blog:5", "blog:1", "blog:2"])

    def test_a_link_delivered_before_is_not_delivered_again(self) -> None:
        self.publish("2026-09-10", [make(1)])
        items = self.publish("2026-09-25", [make(9, day_url="https://example.org/post/1/?utm=x"), make(2)])
        self.assertEqual([entry["_radar"]["id"] for entry in items], ["blog:2", "blog:1"])

    def test_the_store_keeps_thirty_days_and_a_hundred_items(self) -> None:
        start = date(2026, 8, 1)
        for offset in range(40):
            day = (start + timedelta(days=offset)).isoformat()
            items = self.publish(day, [make(offset * 10 + n) for n in range(4)])
        days = sorted({entry["_radar"]["date"] for entry in items})
        self.assertEqual(len(items), 100)
        self.assertEqual(days[-1], "2026-09-09")
        self.assertGreater(days[0], (date(2026, 9, 9) - timedelta(days=30)).isoformat())

    def test_markup_in_titles_is_escaped(self) -> None:
        self.publish("2026-09-25", [make(1, title='R&D <b>"fast"</b> humanoid')])
        for lang in LANGS:
            root = ET.parse(self.root/"radar"/feeds.atom_name(lang)).getroot()
            self.assertEqual(root.find(f"{ATOM}entry").findtext(f"{ATOM}title"), 'R&D <b>"fast"</b> humanoid')

    def test_a_foreign_or_broken_store_starts_fresh(self) -> None:
        path = self.root/"radar"/"feed.json"
        path.parent.mkdir(parents=True)
        for payload in ("{not json", json.dumps({"version": "https://jsonfeed.org/version/1", "items": [{}]})):
            path.write_text(payload, encoding="utf-8")
            self.assertEqual(feeds.load_store(path), [])

    def test_an_empty_day_still_writes_valid_feeds(self) -> None:
        self.publish("2026-01-01", [])
        for lang in LANGS:
            root = ET.parse(self.root/"radar"/feeds.atom_name(lang)).getroot()
            self.assertEqual(root.findall(f"{ATOM}entry"), [])
            self.assertRegex(root.findtext(f"{ATOM}updated"), RFC3339)


class BoundaryTest(unittest.TestCase):
    def test_iso_weeks_cross_the_year_correctly(self) -> None:
        self.assertEqual(feeds.iso_week("2026-09-25"), ("2026-W39", date(2026, 9, 21), date(2026, 9, 27)))
        self.assertEqual(feeds.iso_week("2027-01-01")[0], "2026-W53")
        self.assertEqual(feeds.iso_week("2025-12-29")[0], "2026-W01")

    def test_generated_stamps_become_rfc_3339(self) -> None:
        self.assertEqual(feeds.rfc3339(GENERATED), "2026-09-25T01:40:00Z")


class DraftSeparationTest(unittest.TestCase):
    """Drafted analysis must always carry its label, so it stays off the feeds."""

    def test_no_why_line_reaches_a_feed(self) -> None:
        config = load_config()
        item = make(1)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            feeds.write_feeds(config, {"date": "2026-09-25", "generated": GENERATED, "picked": [item],
                                       "notes": {"notes": {item.url: {"en": "Drafted line."}}}},
                              root=root, source=root)
            for path in (root/"radar").rglob("*"):
                if path.is_file():
                    text = path.read_text(encoding="utf-8")
                    with self.subTest(path=path.name):
                        self.assertNotIn("Drafted line.", text)
                        for lang in LANGS:
                            template = config.glossary["why_templates"][item.lane][lang]
                            self.assertNotIn(template, text)

    def test_rerender_leaves_the_feeds_alone(self) -> None:
        # scripts/draft_daily_notes.sh reverts a re-render that touches anything but
        # the notes file, the READMEs and the day's pages.
        source = (ROOT/"pairadar"/"cli.py").read_text(encoding="utf-8")
        body = source.split("def rerender", 1)[1].split("\ndef ", 1)[0]
        self.assertNotIn("write_feeds", body)


class ReadmeLinkTest(unittest.TestCase):
    def test_each_readme_links_its_own_atom_feed(self) -> None:
        for lang in LANGS:
            text = (ROOT/README_FILES[lang]).read_text(encoding="utf-8")
            with self.subTest(lang=lang):
                self.assertIn(f"{feeds.RAW_URL}/radar/{feeds.atom_name(lang)}", text)
                self.assertIn(f"{feeds.RAW_URL}/radar/feed.json", text)


if __name__ == "__main__":
    unittest.main()
