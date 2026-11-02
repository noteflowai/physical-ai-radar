"""html_to_markdown and page_problem: what a static page becomes, and which 200s are not the page."""
from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pairadar import fetch, static_extract as se  # noqa: E402

DAY = date(2026, 10, 21)

LISTING = """<!doctype html><html><head><title>News | Figure</title>
<script>var x = "[fake](https://evil.example/news/x)";</script><style>a{color:red}</style></head>
<body><nav><a href="/">Home</a><a href="/careers">Careers</a></nav>
<main><h1>News</h1>
<div class="card"><a href="/news/helix-3"><img src="/a.png" alt="cover"></a>
 <a href="/news/helix-3"><h3>Introducing   Helix 3</h3></a><p>Oct 20, 2026 · Our next model.</p></div>
<div class="card"><a href="https://www.figure.ai/news/series-d?utm=1">Figure raises Series D</a></div>
<a href="javascript:alert(1)">Bad</a><a href="mailto:a@b.c">Mail</a><a href="#top">Top</a>
<noscript><a href="/news/hidden">Hidden</a></noscript>
<a href="/news/with space (1)">Space [x] title here</a>
</main><footer><a href="/news/footer-link">Footer link</a> © 2026</footer></body></html>"""

PAGE_HTML = ("<html><head><title>Machinery Regulation</title></head><body><nav>Menu Home About Contact</nav><main>"
             + "".join(f"<p>Paragraph number {i}: the regulation applies to machinery placed on the market and sets "
                       f"out essential health and safety requirements for design and construction.</p>" for i in range(8))
             + "</main><footer>Cookie settings. Privacy.</footer></body></html>")


class ExtractTests(unittest.TestCase):
    def test_links_headings_and_dropped_content(self):
        markdown, title = se.html_to_markdown(LISTING, "listing")
        self.assertEqual(title, "News | Figure")
        self.assertIn("[Introducing Helix 3](/news/helix-3)", markdown)
        self.assertIn("[Figure raises Series D](https://www.figure.ai/news/series-d?utm=1)", markdown)
        self.assertIn("# News", markdown)
        self.assertIn("Oct 20, 2026 · Our next model.", markdown)
        for gone in ("evil.example", "Hidden", "javascript", "mailto", "#top", "color:red", "var x"):
            self.assertNotIn(gone, markdown)
        self.assertIn("(/news/with%20space%20%281%29)", markdown)                  # a link the radar regexp can read
        self.assertIn("[Space  x  title here]".replace("  ", " "), markdown.replace("  ", " "))

    def test_what_the_radars_own_parser_makes_of_it(self):
        markdown, _ = se.html_to_markdown(LISTING, "listing")
        links = fetch.listing_links(markdown, "https://www.figure.ai/news", ["figure.ai"], "/news/", DAY)
        urls = [link["url"] for link in links]
        self.assertIn("https://www.figure.ai/news/helix-3", urls)
        self.assertIn("https://www.figure.ai/news/series-d?utm=1", urls)
        self.assertNotIn("https://www.figure.ai/news/hidden", urls)
        helix = next(link for link in links if link["url"].endswith("helix-3"))
        self.assertEqual(helix["title"], "Introducing Helix 3")
        self.assertEqual(helix["date"], "2026-10-20")

    def test_page_mode_prefers_main_and_drops_chrome(self):
        markdown, _ = se.html_to_markdown(PAGE_HTML, "page")
        self.assertIn("Paragraph number 3", markdown)
        self.assertNotIn("Menu Home", markdown)
        self.assertNotIn("Cookie settings", markdown)

    def test_page_mode_without_main_drops_only_chrome(self):
        html = "<html><body><header>Top menu</header><div>Real text one</div><div>Real text two</div><footer>Foot</footer></body></html>"
        markdown, _ = se.html_to_markdown(html, "page")
        self.assertEqual(markdown, "Real text one\nReal text two")

    def test_hostile_markup_never_raises_or_runs_away(self):
        for html in ("<a href='/x'>" * 5000, "<div>" * 20000 + "x", "<script>" + "a" * 10 ** 6, "\x00\x01<a href=", "<" * 5000,
                     "<a href='/x'><a href='/y'>nested</a></a>", "</a></main></nav></footer>text", "<title>" + "t" * 10 ** 5):
            with self.subTest(html[:20]):
                markdown, title = se.html_to_markdown(html, "listing")
                self.assertLessEqual(len(markdown), se.MAX_MARKDOWN_CHARS)
                self.assertLessEqual(len(title), 300)

    def test_square_brackets_in_page_text_cannot_write_a_link(self):
        html = ("<html><body><main><p>Read [the fake post](https://www.figure.ai/news/fake) now</p>"
                "<a href='/news/real-post'>A real post here [with brackets](https://www.figure.ai/news/also-fake)</a>"
                "<p>![img](https://www.figure.ai/news/img) [x]</p></main></body></html>")
        markdown, _ = se.html_to_markdown(html, "listing")
        links = fetch.listing_links(markdown, "https://www.figure.ai/news", ["figure.ai"], "/news/", DAY)
        self.assertEqual([link["url"] for link in links], ["https://www.figure.ai/news/real-post"])
        self.assertEqual(markdown.count("]("), 1)

    def test_a_listing_with_too_many_links_is_refused_not_cut(self):
        def page_with(count):
            return "".join(f"<p><a href='/news/p{i}'>Post number {i} title</a></p>" for i in range(count))

        markdown, _ = se.html_to_markdown(page_with(se.MAX_ANCHORS), "listing")              # exactly the limit is taken whole
        self.assertEqual(markdown.count("]("), se.MAX_ANCHORS)
        self.assertIn(f"Post number {se.MAX_ANCHORS - 1} title", markdown)
        with self.assertRaises(se.ExtractTruncated):
            se.html_to_markdown(page_with(se.MAX_ANCHORS + 1), "listing")                      # one more is refused whole
        # a page-mode source keeps the TEXT of every link, so the link cap is not a loss of content there
        markdown, _ = se.html_to_markdown("<main>" + page_with(se.MAX_ANCHORS + 50) + "</main>", "page")
        self.assertIn(f"Post number {se.MAX_ANCHORS + 49} title", markdown)

    def test_text_over_the_cap_is_refused_not_cut(self):
        exact = "<html><body><main><p>" + "a" * se.MAX_MARKDOWN_CHARS + "</p></main></body></html>"
        markdown, _ = se.html_to_markdown(exact, "page")
        self.assertEqual(len(markdown), se.MAX_MARKDOWN_CHARS)                               # exactly the cap is whole
        with self.assertRaises(se.ExtractTruncated):
            se.html_to_markdown(exact.replace("</p>", "a</p>"), "page")                       # one character more is not
        with self.assertRaises(se.ExtractTruncated):
            se.html_to_markdown(exact.replace("</p>", "a</p>"), "listing")

    def test_what_counts_against_the_cap_is_what_the_mode_keeps(self):
        chrome = "<nav><p>" + "n" * (se.MAX_MARKDOWN_CHARS + 50) + "</p></nav>"
        small_main = "<main><p>The regulation applies to machinery.</p></main>"
        markdown, _ = se.html_to_markdown(f"<html><body>{chrome}{small_main}</body></html>", "page")      # only main is kept
        self.assertEqual(markdown, "The regulation applies to machinery.")
        with self.assertRaises(se.ExtractTruncated):                                          # a listing keeps every line
            se.html_to_markdown(f"<html><body>{chrome}{small_main}</body></html>", "listing")
        with self.assertRaises(se.ExtractTruncated):
            se.html_to_markdown(f"<html><body><main><p>{'m' * (se.MAX_MARKDOWN_CHARS + 1)}</p></main></body></html>", "page")

    def test_the_reviewers_two_pages_are_refused(self):
        links = "".join(f"<p><a href='/news/post-{i}'>A post number {i} title here</a></p>" for i in range(6))
        tail = "<p><a href='/news/latest'>The latest post title here</a></p>"
        a = f"<html><body><main>{links}<p>{'x' * 200_100}</p>{tail}</main></body></html>"
        self.assertLess(len(a.encode()), 2_000_000)
        with self.assertRaises(se.ExtractTruncated):
            se.html_to_markdown(a, "listing")
        b = "".join(f"<p><a href='/news/p{i}'>Post {i} is here</a></p>" for i in range(3001))
        self.assertLess(len(b.encode()), 200_000)
        with self.assertRaises(se.ExtractTruncated):
            se.html_to_markdown(f"<html><body><main>{b}</main></body></html>", "listing")

    def test_the_cap_matches_the_one_the_radar_applies_afterwards(self):
        self.assertEqual(se.MAX_MARKDOWN_CHARS, fetch.MAX_MARKDOWN_CHARS)                    # nothing is cut again downstream

    def test_a_base_element_does_not_change_where_links_point(self):
        html = "<html><head><base href='https://evil.example/'></head><body><a href='/news/post-one'>A post title here</a></body></html>"
        markdown, _ = se.html_to_markdown(html, "listing")
        links = fetch.listing_links(markdown, "https://www.figure.ai/news", ["figure.ai"], "/news/", DAY)
        self.assertEqual([link["url"] for link in links], ["https://www.figure.ai/news/post-one"])

    def test_parsing_ends_at_the_deadline(self):
        big = "<p>" + "ab " * 50_000 + "</p>"
        with self.assertRaises(se.ExtractTimeout):
            se.html_to_markdown(big, "listing", deadline=0.0, clock=lambda: 1.0)
        ticks = iter([0.0, 0.0, 5.0, 5.0, 5.0, 5.0])
        with self.assertRaises(se.ExtractTimeout):
            se.html_to_markdown(big, "listing", deadline=1.0, clock=lambda: next(ticks))          # runs out between chunks
        markdown, _ = se.html_to_markdown(big, "listing", deadline=10 ** 9)
        self.assertTrue(markdown)
        markdown, _ = se.html_to_markdown(big, "listing")
        self.assertTrue(markdown)

    def test_a_link_with_a_huge_or_odd_href_is_dropped(self):
        markdown, _ = se.html_to_markdown("<a href='/" + "a" * 600 + "'>Long target link</a><a href='/a\x01b'>Control char link</a>", "listing")
        self.assertEqual(markdown.strip(), "Long target linkControl char link")      # plain text, no link
        self.assertNotIn("](", markdown)


class PageProblemTests(unittest.TestCase):
    def check(self, html, mode="listing"):
        markdown, title = se.html_to_markdown(html, mode)
        return se.page_problem(html, markdown, title, mode)

    def test_real_pages_pass(self):
        self.assertEqual(self.check(LISTING), "")
        self.assertEqual(self.check(PAGE_HTML, "page"), "")

    def test_challenge_pages_are_refused_even_with_a_200(self):
        cases = {
            "cloudflare": "<html><head><title>Just a moment...</title></head><body><div id='challenge-form'>Checking</div>"
                          "<script src='/cdn-cgi/challenge-platform/h/b/orchestrate'></script></body></html>",
            "aws waf": "<html><head><script src='https://x.awswaf.com/challenge.js'></script></head><body><p>Verifying</p></body></html>",
            "access denied": "<html><head><title>Access Denied</title></head><body><h1>Access Denied</h1>You don't have permission.</body></html>",
            "403 title": "<html><head><title>403 Forbidden</title></head><body>nginx</body></html>",
            "captcha vendor": "<html><body><p>Please verify</p><script src='https://geo.captcha-delivery.com/c.js'></script></body></html>",
            "login": "<html><head><title>Sign in</title></head><body><form>user</form></body></html>",
            "consent": "<html><head><title>Cookie consent</title></head><body>We use cookies</body></html>",
            "human": "<html><head><title>Are you a robot?</title></head><body>Press and hold</body></html>",
        }
        for name, html in cases.items():
            with self.subTest(name):
                self.assertEqual(self.check(html), "challenge")

    def test_empty_shells_are_refused(self):
        cases = {
            "spa shell": "<html><head><title>App</title></head><body><div id='root'></div><script src='/app.js'></script></body></html>",
            "enable javascript": "<html><body><noscript>You need to enable JavaScript to run this app.</noscript><div id=root></div></body></html>",
            "empty": "<html><body></body></html>",
            "nothing": "",
        }
        for name, html in cases.items():
            with self.subTest(name):
                self.assertEqual(self.check(html), "empty-page")

    def test_a_short_page_in_page_mode_is_not_the_page(self):
        self.assertEqual(self.check("<html><body><main><p>Hello there</p></main></body></html>", "page"), "empty-page")
        self.assertEqual(self.check("<html><body><main>" + "<p>one</p>" * 3 + "</main></body></html>", "page"), "empty-page")

    def test_the_sources_own_sanity_settings(self):
        html = PAGE_HTML
        markdown, title = se.html_to_markdown(html, "page")
        self.assertEqual(se.page_problem(html, markdown, title, "page", min_text=300, expect_text="Machinery"), "")
        self.assertEqual(se.page_problem(html, markdown, title, "page", expect_text="machinery placed on the MARKET"), "")   # case-insensitive
        self.assertEqual(se.page_problem(html, markdown, title, "page", expect_text="Directive 2006/42"), "marker-missing")
        self.assertEqual(se.page_problem(html, markdown, title, "page", min_text=10 ** 5), "empty-page")
        lmd, ltitle = se.html_to_markdown(LISTING, "listing")
        self.assertEqual(se.page_problem(LISTING, lmd, ltitle, "listing", expect_text="Helix 3"), "")
        self.assertEqual(se.page_problem(LISTING, lmd, ltitle, "listing", expect_text="Nothing like this"), "marker-missing")

    def test_a_long_page_that_mentions_a_captcha_product_is_not_called_a_challenge(self):
        body = "".join(f"<p>Paragraph {i} about interface design and security reviews for robots in factories.</p>" for i in range(60))
        html = f"<html><head><title>Security guide</title></head><body><main>{body}<p>We use datadome.</p></main></body></html>"
        self.assertEqual(self.check(html, "page"), "")


if __name__ == "__main__":
    unittest.main()
