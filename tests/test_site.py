"""The Pages landing site: one page per language, safe with any fetched text, and
shareable -- each page names its language, links its translations and carries a
social card.

No test here touches the network or a browser.
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pairadar import qr, site  # noqa: E402
from pairadar.config import LANGS, Item, load_config  # noqa: E402


def item(title: str, lane: str = "simeval", evidence: str = "R", score: float = 3.0,
         numbers: list[str] | None = None, summary: str = "") -> Item:
    entry = Item(id=f"arxiv:{abs(hash(title))}", title=title, url=f"https://arxiv.org/abs/{abs(hash(title))}",
                 publisher="arXiv", source_id="arxiv", evidence=evidence, published="2026-09-25",
                 summary=summary)
    entry.lane, entry.score, entry.numbers = lane, score, numbers or []
    return entry


def context(picked: list[Item], **extra) -> dict:
    ctx = {"date": "2026-09-25", "generated": "2026-09-25 01:40 UTC", "picked": picked, "baseline": [],
           "curated": {}, "notes": {}, "lane_rows": [("simeval", 5), ("hardware", 2)],
           "mix": {"O": 1, "R": 5, "M": 1}, "cadence": [("2026-09-24", 2), ("2026-09-25", 8)],
           "fetch": {"items": 924, "attempted": 8, "answered": 8}, "chart_days": 7}
    ctx.update(extra)
    return ctx


class Tags(HTMLParser):
    """Collects start tags and fails on a tag closed out of order."""

    VOID = {"meta", "link", "img", "br", "input", "polygon", "line", "circle", "path", "rect", "stop",
            "animatetransform"}

    def __init__(self) -> None:
        super().__init__()
        self.stack: list[str] = []
        self.starts: list[tuple[str, dict]] = []

    def handle_starttag(self, tag, attrs):
        self.starts.append((tag, dict(attrs)))
        if tag not in self.VOID:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.starts.append((tag, dict(attrs)))

    def handle_endtag(self, tag):
        if tag in self.VOID:
            return
        assert self.stack and self.stack[-1] == tag, f"</{tag}> closes <{self.stack[-1] if self.stack else None}>"
        self.stack.pop()


def parse(page: str) -> Tags:
    tags = Tags()
    tags.feed(page)
    tags.close()
    assert tags.stack == ["html"] or tags.stack == [], tags.stack
    return tags


class LandingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.picked = [item("RoboRecover: Benchmarking Robot Policy Recovery", numbers=["4.5x"],
                            summary="It delivers a 4.5x steady-state replanning speedup."),
                       item("Agility explores wheeled robots", lane="hardware", evidence="M", score=2.0)]

    def test_each_language_names_itself_and_links_the_others(self) -> None:
        for lang in LANGS:
            with self.subTest(lang=lang):
                page = site.render_landing(self.config, lang, context(self.picked))
                self.assertIn(f'<html lang="{site.HTML_LANG[lang]}">', page)
                for other in LANGS:
                    self.assertIn(f'hreflang="{site.HTML_LANG[other]}" href="{site.page_url(other)}"', page)
                self.assertIn(f'<link rel="canonical" href="{site.page_url(lang)}">', page)
                self.assertIn(f'content="{site.SITE_URL}/assets/og.{lang}.png"', page)
                self.assertIn('name="twitter:card" content="summary_large_image"', page)
                self.assertIn(self.config.ui(lang)["site"]["headline"], page)

    def test_the_page_is_well_formed(self) -> None:
        for lang in LANGS:
            parse(site.render_landing(self.config, lang, context(self.picked)))

    def test_fetched_text_cannot_become_markup(self) -> None:
        hostile = item('<script>alert(1)</script> "quoted" {{ site.title }}', numbers=["3x"],
                       summary='Up to 3x faster. <img src=x onerror=alert(1)>')
        page = site.render_landing(self.config, "en", context([hostile]))
        self.assertNotIn("<script>alert", page)
        self.assertNotIn("<img src=x", page)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt; &quot;quoted&quot;", page)
        self.assertFalse(page.startswith("---"), "front matter would hand the page to Liquid")
        ld = re.search(r'<script type="application/ld\+json">(.*?)</script>', page, re.S).group(1)
        self.assertNotIn("</", ld)
        parse(page)

    def test_every_pick_has_a_card_its_blip_links_to(self) -> None:
        page = site.render_landing(self.config, "en", context(self.picked))
        tags = parse(page)
        cards = [attrs["id"] for tag, attrs in tags.starts if tag == "article"]
        blips = [attrs["href"] for tag, attrs in tags.starts if tag == "a" and attrs.get("class") == "blip"]
        self.assertEqual(cards, ["p1", "p2"])
        self.assertEqual(blips, ["#p1", "#p2"])

    def test_a_card_quotes_its_lead_figure(self) -> None:
        page = site.render_landing(self.config, "en", context(self.picked))
        self.assertIn("<strong>4.5x</strong><span class=\"context\">“It delivers a 4.5x steady-state "
                      "replanning speedup”</span>", page)

    def test_a_share_links_the_frozen_day_page(self) -> None:
        page = site.render_landing(self.config, "ja", context(self.picked))
        self.assertIn(f'data-url="{site.SITE_URL}/radar/daily/2026-09-25.ja.html"', page)
        self.assertIn("https://twitter.com/intent/tweet?text=", page)

    def test_links_are_relative_to_each_page(self) -> None:
        self.assertEqual(site.prefix("zh"), "")
        self.assertEqual(site.prefix("en"), "../")
        page = site.render_landing(self.config, "en", context(self.picked))
        self.assertIn('href="../radar/daily/2026-09-25.en.html"', page)
        self.assertIn('href="../assets/site.css"', page)

    def test_a_day_without_picks_shows_the_baseline(self) -> None:
        baseline = item("A curated entry", lane="foundation", evidence="O")
        baseline.source_id = "baseline"
        page = site.render_landing(self.config, "zh", context([], baseline=[baseline]))
        self.assertIn(self.config.ui("zh")["site"]["baseline_heading"], page)
        self.assertIn("A curated entry", page)
        parse(page)

    def test_a_snapshot_from_before_the_chart_window_still_renders(self) -> None:
        page = site.render_landing(self.config, "en", context(self.picked, chart_days=None, fetch={}))
        self.assertIn("picks, last 7 days", page)
        self.assertIn("<dd>—</dd>", page)

    def share_data(self, page: str) -> dict:
        raw = re.search(r'<script type="application/json" id="share-data">(.*?)</script>', page, re.S).group(1)
        self.assertNotIn("</", raw)
        return json.loads(raw)

    def test_the_poster_has_every_pick_and_a_code_back_to_the_day(self) -> None:
        for lang in LANGS:
            with self.subTest(lang=lang):
                data = self.share_data(site.render_landing(self.config, lang, context(self.picked)))
                url = f"{site.SITE_URL}/radar/daily/2026-09-25.{lang}.html"
                self.assertEqual(data["url"], url)
                self.assertEqual(data["qr"], qr.rows(url))
                self.assertEqual([pick["title"] for pick in data["picks"]], [entry.title for entry in self.picked])
                self.assertEqual(data["picks"][0]["figure"], "4.5x")
                self.assertEqual(data["picks"][1]["color"], site.lane_color(self.config, "hardware"))
                self.assertEqual(set(data["evidence"]), {"O", "R", "M"})
                self.assertNotIn("（", data["picks"][0]["lane"])
                self.assertNotIn("(", data["picks"][0]["lane"])

    def test_the_digest_reads_as_text_with_every_link(self) -> None:
        text = site.digest(self.config, "zh", "2026-09-25", self.picked)
        lines = text.splitlines()
        self.assertEqual(lines[0], f"{self.config.ui('zh')['title']} · 2026-09-25")
        self.assertIn("01 [仿真与评测 · R] RoboRecover: Benchmarking Robot Policy Recovery", lines)
        for entry in self.picked:
            self.assertIn(f"   {entry.url}", lines)
        self.assertTrue(lines[-1].endswith(f"{site.SITE_URL}/radar/daily/2026-09-25.zh.html"))

    def test_fetched_text_cannot_escape_the_share_data(self) -> None:
        hostile = item('</script><script>alert(1)</script> & "q"', numbers=["3x"])
        page = site.render_landing(self.config, "en", context([hostile]))
        self.assertNotIn("<script>alert", page)
        self.assertEqual(self.share_data(page)["picks"][0]["title"], hostile.title)
        parse(page)

    def test_lane_filters_count_the_cards_they_show(self) -> None:
        page = site.render_landing(self.config, "en", context(self.picked))
        tags = parse(page)
        chips = [attrs for tag, attrs in tags.starts if tag == "button" and attrs.get("class") == "chip-filter"]
        self.assertEqual([chip["data-lane"] for chip in chips], ["", "simeval", "hardware"])
        self.assertEqual([chip["aria-pressed"] for chip in chips], ["true", "false", "false"])
        cards = [attrs["data-lane"] for tag, attrs in tags.starts if tag == "article"]
        self.assertEqual(cards, ["simeval", "hardware"])
        # one lane has nothing to filter
        single = site.render_landing(self.config, "en", context(self.picked[:1]))
        self.assertNotIn('class="chip-filter"', single)

    def test_the_page_loads_the_shared_script_and_scans_to_the_day(self) -> None:
        page = site.render_landing(self.config, "en", context(self.picked))
        self.assertIn('<script src="../assets/site.js" defer></script>', page)
        self.assertTrue((ROOT/"assets"/"site.js").exists())
        self.assertIn(qr.svg(f"{site.SITE_URL}/radar/daily/2026-09-25.en.html",
                             self.config.ui("en")["site"]["scan_heading"]), page)
        self.assertIn('<dialog class="poster-dialog"', page)
        self.assertIn('download="physical-ai-radar-2026-09-25.en.png"', page)

    def test_write_site_writes_one_page_per_language(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            written = site.write_site(self.config, context(self.picked), Path(tmp))
            self.assertEqual(sorted(path.relative_to(tmp).as_posix() for path in written),
                             ["en/index.html", "index.html", "ja/index.html"])


class RadarTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()

    def test_one_spoke_and_label_per_lane(self) -> None:
        svg = site.radar_svg(self.config, "en", [("simeval", 3)], [])
        self.assertEqual(svg.count('class="spoke"'), len(self.config.lanes))
        self.assertEqual(svg.count('class="label"'), len(self.config.lanes))

    def test_picks_sharing_a_lane_do_not_overlap(self) -> None:
        picks = [item("First", score=3.0), item("Second", score=3.0)]
        svg = site.radar_svg(self.config, "en", [("simeval", 2)], picks)
        centres = re.findall(r'<circle cx="([-\d.]+)" cy="([-\d.]+)" r="5"/>', svg)
        self.assertEqual(len(set(centres)), 2)

    def test_without_labels_the_chart_fills_its_box(self) -> None:
        svg = site.radar_svg(self.config, "en", [], [], radius=150, labels=False)
        self.assertIn('viewBox="-162 -162 324 324"', svg)
        self.assertNotIn('class="label"', svg)


class LayoutTest(unittest.TestCase):
    """The Jekyll layout for the daily pages speaks each page's language and shares it."""

    def test_the_layout_uses_the_glossary_words_in_all_three_languages(self) -> None:
        layout = (ROOT/"_layouts"/"default.html").read_text(encoding="utf-8")
        config = load_config()
        # one block of assigns per language, in the layout's if / elsif / else order
        blocks = dict(zip(("zh", "ja", "en"), ("{%- assign t_brand" + part.split("{%- endif -%}", 1)[0]
                                                for part in layout.split("{%- assign t_brand")[1:])))
        for lang in LANGS:
            ui, words, block = config.ui(lang), config.ui(lang)["site"], blocks[lang]
            with self.subTest(lang=lang):
                for name, value in (("brand", ui["title"]), ("archive", ui["history"]), ("feed", words["nav_feeds"]),
                                    ("share", words["share"]), ("copied", words["copied"]),
                                    ("post_x", words["post_x"]), ("home", words["nav_home"]),
                                    ("method", ui["methodology"])):
                    self.assertIn(f'assign t_{name} = "{value}"', block)
        self.assertIn('class="sharebar"', layout)
        self.assertIn("{{ '/assets/site.js' | relative_url }}", layout)
        self.assertIn("share_url | cgi_escape", layout)


class GlossaryTest(unittest.TestCase):
    def test_every_language_has_every_site_string(self) -> None:
        config = load_config()
        keys = {lang: set(config.ui(lang)["site"]) for lang in LANGS}
        self.assertEqual(keys["zh"], keys["en"])
        self.assertEqual(keys["ja"], keys["en"])


if __name__ == "__main__":
    unittest.main()
