"""The files readers actually receive, as committed.

Every other feed and page test builds its own output in a temporary directory. That
proves the code, not the day: until this file, nothing ever opened the committed
`radar/feed.json`, and CI skipped the daily commits altogether. The publish script
runs the suite after generating, so these checks now stand between a broken day and
the push, and CI repeats them on every commit to main.

No test here touches the network.
"""
from __future__ import annotations

import json
import re
import sys
import unittest
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pairadar import feeds  # noqa: E402
from pairadar.config import LANGS  # noqa: E402

ATOM = "{http://www.w3.org/2005/Atom}"
RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
RADAR = ROOT/"radar"


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


@unittest.skipUnless((RADAR/"latest.json").exists(), "no published day in this checkout")
class PublishedDayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.latest = load(RADAR/"latest.json")
        cls.day = cls.latest["date"]
        cls.feed = load(RADAR/"feed.json")

    def test_the_snapshot_names_a_real_day_with_its_pages(self) -> None:
        date.fromisoformat(self.day)
        self.assertTrue(self.latest.get("picked") or self.latest.get("baseline"), "an empty snapshot")
        for lang in LANGS:
            with self.subTest(lang=lang):
                self.assertTrue((RADAR/"daily"/f"{self.day}.{lang}.md").stat().st_size > 0)

    def test_the_json_feed_is_valid_and_bounded(self) -> None:
        self.assertEqual(self.feed["version"], "https://jsonfeed.org/version/1.1")
        self.assertTrue(self.feed["feed_url"].startswith(feeds.SITE_URL))
        items = self.feed["items"]
        self.assertLessEqual(len(items), feeds.KEEP_ITEMS)
        ids = [entry["id"] for entry in items]
        self.assertEqual(len(ids), len(set(ids)), "an item was delivered twice")
        newest = date.fromisoformat(self.day)
        for entry in items:
            with self.subTest(id=entry["id"]):
                self.assertTrue(entry["id"].startswith("tag:"))
                self.assertRegex(entry["url"], r"^https?://")
                self.assertTrue(entry["title"] and entry["content_text"])
                self.assertRegex(entry["date_published"], RFC3339)
                stored = date.fromisoformat(entry["_radar"]["date"])
                self.assertLessEqual(stored, newest, "an item from after the published day")
                self.assertLess((newest - stored).days, feeds.KEEP_DAYS, "an item past the store window")

    def test_the_feed_carries_the_published_day(self) -> None:
        if not self.latest.get("picked"):
            self.skipTest("a day without picks adds nothing to the feed")
        days = {entry["_radar"]["date"] for entry in self.feed["items"]}
        self.assertIn(self.day, days, "latest.json moved on but the feed did not")

    def test_every_atom_feed_parses_and_matches_the_json_feed(self) -> None:
        ids = [entry["id"] for entry in self.feed["items"]]
        for lang in LANGS:
            with self.subTest(lang=lang):
                root = ET.parse(RADAR/feeds.atom_name(lang)).getroot()
                self.assertEqual(root.tag, f"{ATOM}feed")
                self.assertRegex(root.findtext(f"{ATOM}updated"), RFC3339)
                self.assertEqual([entry.findtext(f"{ATOM}id") for entry in root.findall(f"{ATOM}entry")], ids)

    def test_the_landing_pages_show_the_published_day(self) -> None:
        for lang, page in (("zh", "index.html"), ("en", "en/index.html"), ("ja", "ja/index.html")):
            path = ROOT/page
            if not path.exists():
                self.skipTest("this checkout predates the site")
            with self.subTest(lang=lang):
                self.assertIn(f"radar/daily/{self.day}.{lang}.html", path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
