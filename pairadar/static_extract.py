"""HTML to the markdown shape page_watch reads, and the checks that decide whether a 200 is the page.

Standard library only. `html_to_markdown` produces what `fetch.listing_links` and `fetch._page_lines` already
consume: links as `[text](href)`, one block per line, headings with `# `. It never executes anything and drops
script, style, noscript, template, svg, iframe, object and canvas content.

`page_problem` is deliberately conservative and heuristic: a 200 that looks like a challenge, an interstitial or an
empty JavaScript shell is not the target page. It cannot recognise every disguised page; that residual risk is stated
in docs/METHODOLOGY.md, and a page it cannot vouch for stays unread with its baseline untouched.
"""
from __future__ import annotations

import html.parser
import re

MAX_MARKDOWN_CHARS = 200_000
MAX_HREF_CHARS = 500
# visible text a page-mode source must have before it counts as the page
MIN_PAGE_TEXT_CHARS = 300
MIN_PAGE_LINES = 5

_SKIP = frozenset({"script", "style", "noscript", "template", "svg", "iframe", "object", "canvas", "head"})
_BLOCK = frozenset({
    "p", "div", "li", "ul", "ol", "tr", "table", "br", "hr", "section", "article", "header", "footer", "nav", "main",
    "blockquote", "pre", "dt", "dd", "dl", "figure", "figcaption", "aside", "form", "h1", "h2", "h3", "h4", "h5", "h6",
    "td", "th", "details", "summary", "address"})
_CHROME = frozenset({"nav", "header", "footer", "aside", "form"})
_MAIN = frozenset({"main", "article"})
_HEADING = re.compile(r"^h[1-6]$")
_WS = re.compile(r"\s+")
_BAD_HREF = re.compile(r"^\s*(#|javascript:|mailto:|tel:|data:|vbscript:)", re.I)

# a page whose <title> says this is a gate, not the page
_GATE_TITLE = re.compile(
    r"^\W*(just a moment|attention required|access denied|forbidden|403\b|429\b|robot or human|are you a (robot|human)|"
    r"verify you are (a )?human|security check|checking (your|the) browser|please wait|pardon our interruption|"
    r"request (blocked|denied|rejected)|one more step|human verification|sign in|log in|login|"
    r"before you continue|cookie (consent|settings)|consent)", re.I)
# markers that name a bot-management product; only decisive on a page with little visible text
_GATE_MARKERS = ("cf-chl-", "cdn-cgi/challenge-platform", "challenge-form", "awswaf", "aws-waf", "_incapsula_",
                 "px-captcha", "captcha-delivery.com", "perimeterx", "datadome", "akamai bot manager")


def _href(raw: str | None) -> str:
    if not raw or _BAD_HREF.match(raw):
        return ""
    href = raw.strip()
    if not href or len(href) > MAX_HREF_CHARS or any(ord(ch) < 0x21 and ch != " " for ch in href):
        return ""
    return href.replace(" ", "%20").replace("(", "%28").replace(")", "%29").replace("<", "%3C").replace(">", "%3E")


class _Extractor(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lines: list[tuple[str, bool, bool]] = []     # text, inside main/article, inside chrome
        self.title = ""
        self._buf: list[str] = []
        self._skip = 0
        self._main = 0
        self._chrome = 0
        self._href = ""
        self._anchor: list[str] = []
        self._in_title = False
        self._heading = False
        self._size = 0

    # --- line handling -------------------------------------------------------------------------------
    def _flush(self) -> None:
        text = _WS.sub(" ", "".join(self._buf)).strip()
        self._buf = []
        if text and self._size < MAX_MARKDOWN_CHARS:
            line = ("# " + text) if self._heading else text
            self.lines.append((line, self._main > 0, self._chrome > 0))
            self._size += len(line) + 1
        self._heading = False

    def _add(self, text: str) -> None:
        (self._anchor if self._href else self._buf).append(text)

    # --- parser callbacks ----------------------------------------------------------------------------
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self._in_title = True
            return
        if tag in _SKIP:
            if tag != "head":
                self._skip += 1
            return
        if self._skip:
            return
        if tag in _BLOCK:
            self._flush()
            if _HEADING.match(tag):
                self._heading = True
        if tag in _MAIN:
            self._main += 1
        if tag in _CHROME:
            self._chrome += 1
        if tag == "a" and not self._href:
            href = _href(dict(attrs).get("href"))
            if href:
                self._href = href
                self._anchor = []
        elif tag == "br":
            self._flush()

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("br", "hr"):
            if not self._skip:
                self._flush()

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
            return
        if tag in _SKIP:
            if tag != "head" and self._skip:
                self._skip -= 1
            return
        if self._skip:
            return
        if tag == "a" and self._href:
            text = _WS.sub(" ", "".join(self._anchor).replace("[", " ").replace("]", " ")).strip()
            if text:
                self._buf.append(f"[{text}]({self._href})")
            self._href, self._anchor = "", []
        if tag in _BLOCK:
            self._flush()
        if tag in _MAIN and self._main:
            self._main -= 1
        if tag in _CHROME and self._chrome:
            self._chrome -= 1

    def handle_data(self, data: str) -> None:
        if self._in_title:
            if len(self.title) < 300:
                self.title += data
            return
        if not self._skip and data:
            self._add(data)

    def close(self) -> None:
        super().close()
        self._flush()


def html_to_markdown(text: str, mode: str = "listing") -> tuple[str, str]:
    """(markdown, title). `listing` keeps every line; `page` keeps main/article text when the page has any, else
    everything outside nav, header, footer, aside and form."""
    parser = _Extractor()
    try:
        parser.feed(text)
        parser.close()
    except Exception:  # noqa: BLE001 -- malformed markup is an unreadable page, not a crash
        return "", ""
    lines = parser.lines
    if mode == "page":
        in_main = [line for line in lines if line[1]]
        lines = in_main or [line for line in lines if not line[2]]
    markdown = "\n".join(line for line, _, _ in lines)[:MAX_MARKDOWN_CHARS]
    return markdown, _WS.sub(" ", parser.title).strip()[:300]


def _visible(markdown: str) -> str:
    return re.sub(r"\]\([^)]*\)", "", markdown).replace("[", "")


def page_problem(raw_html: str, markdown: str, title: str, mode: str) -> str:
    """"" when the extracted page may be read, else a fixed reason: `challenge`, `empty-page`."""
    head = raw_html[:200_000].lower()
    text = _visible(markdown)
    if _GATE_TITLE.match(title or "") and len(text) < 2_000:
        return "challenge"
    if any(marker in head for marker in _GATE_MARKERS) and len(text) < 1_500:
        return "challenge"
    if mode == "page":
        lines = [line for line in markdown.splitlines() if len(line.strip()) >= 3]
        if len(text) < MIN_PAGE_TEXT_CHARS or len(lines) < MIN_PAGE_LINES:
            return "empty-page"
    elif not markdown.strip():
        return "empty-page"
    return ""
