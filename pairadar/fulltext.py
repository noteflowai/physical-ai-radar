"""Full text of an arXiv paper from its HTML rendering (arxiv.org/html/<id>), for deep reads.

The daily radar only ever saw an abstract, so every note it wrote was a guess about what
the paper measured. A deep read needs the sections, the tables and the limitations the
authors state. arXiv renders most new submissions to HTML (LaTeXML); that page is fetched
once per paper with the radar's own user agent and parsed with the standard library into
headed sections and tables. There is no PDF path: a paper without an HTML rendering is
not a deep-read candidate (`None`), and it stays a one-line index entry.

Nothing here runs in the daily build or in tests over the network; tests use fixtures.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
from html.parser import HTMLParser
import re

from . import fetch

ARXIV_ID = re.compile(r"^\d{4}\.\d{4,5}(?:v\d+)?$")
HTML_URL = "https://arxiv.org/html/{id}"
MAX_HTML_BYTES = 15 * 1024 * 1024
MAX_TEXT_CHARS = 150_000
MAX_TABLE_ROWS = 60
_WS = re.compile(r"\s+")
_SKIP = {"script", "style", "nav", "header", "footer", "button", "noscript", "svg", "form", "head"}
_SKIP_CLASSES = frozenset({"ltx_bibliography", "ltx_page_header", "ltx_page_footer", "ltx_authors",
                           "ltx_note_outer", "ltx_TOC", "ltx_dates"})   # exact class tokens
_VOID = {"br", "img", "meta", "link", "input", "hr", "col", "source", "wbr", "area", "base"}
_HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
_BLOCKS = {"p", "div", "li", "blockquote", "dd", "dt"}


def html_url(arxiv_id: str) -> str:
    if not ARXIV_ID.match(arxiv_id or ""):
        raise ValueError(f"not an arXiv identifier: {arxiv_id!r}")
    return HTML_URL.format(id=arxiv_id)


@dataclass
class Section:
    heading: str
    text: str = ""


@dataclass
class Table:
    caption: str
    rows: list[list[str]] = field(default_factory=list)


@dataclass
class Document:
    arxiv_id: str
    url: str
    sha256: str
    title: str = ""
    sections: list[Section] = field(default_factory=list)
    tables: list[Table] = field(default_factory=list)
    figures: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)

    def as_text(self) -> str:
        """Plain text with stable locators ("§ heading", "[Table I: …]") a writer can cite."""
        out = [f"# {self.title}", f"Source: {self.url} (sha256 {self.sha256[:16]})", ""]
        for section in self.sections:
            if section.text:
                out += [f"## § {section.heading}" if section.heading else "## §", section.text, ""]
        for table in self.tables:
            out.append(f"[{table.caption}]")
            out += ["| " + " | ".join(row) + " |" for row in table.rows]
            out.append("")
        for caption in self.figures:
            out.append(f"[{caption}]")
        text = "\n".join(out).strip() + "\n"
        return text if len(text) <= MAX_TEXT_CHARS else text[:MAX_TEXT_CHARS] + "\n[truncated]\n"


class _Parser(HTMLParser):
    """LaTeXML article -> sections, tables and figure captions. Math becomes its alttext."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, bool]] = []   # (tag, skipped); skipping is inherited
        self.math = 0
        self.title = ""
        self.sections: list[Section] = [Section("")]
        self.tables: list[Table] = []
        self.figures: list[str] = []
        self.heading: list[str] | None = None
        self.heading_tag = ""
        self.figure: list[dict] = []              # open <figure>s: kind, caption, rows
        self.caption: list[str] | None = None
        self.table_depth = 0
        self.rows: list[list[str]] = []
        self.row: list[str] | None = None
        self.cell: list[str] | None = None

    @property
    def skipped(self) -> bool:
        return bool(self.stack) and self.stack[-1][1]

    @property
    def in_article(self) -> bool:
        return any(tag == "article" for tag, _ in self.stack)

    def _emit(self, text: str) -> None:
        if self.cell is not None:
            self.cell.append(text)
        elif self.caption is not None:
            self.caption.append(text)
        elif self.heading is not None:
            self.heading.append(text)
        elif not self.table_depth:
            self.sections[-1].text += text

    def handle_starttag(self, tag: str, attrs) -> None:
        attributes = dict(attrs)
        classes = set((attributes.get("class") or "").split())
        if tag in _VOID:
            if tag == "br" and not self.skipped and self.in_article and not self.math:
                self._emit("\n")
            return
        skipped = self.skipped or tag in _SKIP or bool(classes & _SKIP_CLASSES)
        self.stack.append((tag, skipped))
        if skipped or not self.in_article:
            return
        if tag == "math":
            if self.math == 0:
                self._emit(" " + (attributes.get("alttext") or "").strip() + " ")
            self.math += 1
            return
        if self.math:
            return
        if tag in _HEADINGS:
            self.heading, self.heading_tag = [], tag
        elif tag == "figure":
            self.figure.append({"kind": "table" if "ltx_table" in classes else "figure", "caption": "", "rows": []})
        elif tag == "figcaption":
            self.caption = []
        elif tag == "table":
            self.table_depth += 1
        elif tag == "tr" and self.table_depth:
            self.row = []
        elif tag in ("td", "th") and self.row is not None:
            self.cell = []
        elif tag in _BLOCKS:
            self._emit("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _VOID or not any(open_tag == tag for open_tag, _ in self.stack):
            return
        while self.stack:   # tolerate unclosed inner tags: pop to the matching start
            open_tag, skipped = self.stack.pop()
            if open_tag == tag:
                break
        if skipped or not (self.in_article or tag == "article"):
            return
        if tag == "math":
            self.math = max(0, self.math - 1)
            return
        if self.math:
            return
        if tag in _HEADINGS and self.heading is not None:
            text = _clean("".join(self.heading))
            if self.heading_tag == "h1" and not self.title:
                self.title = text
            elif text:
                self.sections.append(Section(text))
            self.heading = None
        elif tag in ("td", "th") and self.cell is not None and self.row is not None:
            self.row.append(_clean("".join(self.cell)))
            self.cell = None
        elif tag == "tr" and self.row is not None:
            if any(self.row) and len(self.rows) < MAX_TABLE_ROWS:
                self.rows.append(self.row)
            self.row = None
        elif tag == "table" and self.table_depth:
            self.table_depth -= 1
            if not self.table_depth:
                if self.figure and self.figure[-1]["kind"] == "table":
                    self.figure[-1]["rows"].extend(self.rows)
                self.rows = []   # a layout table outside a table figure is dropped
        elif tag == "figcaption" and self.caption is not None:
            if self.figure:
                self.figure[-1]["caption"] = _clean("".join(self.caption))
            self.caption = None
        elif tag == "figure" and self.figure:
            figure = self.figure.pop()
            if figure["kind"] == "table":
                self.tables.append(Table(figure["caption"] or f"Table {len(self.tables) + 1}", figure["rows"]))
            elif figure["caption"]:
                self.figures.append(figure["caption"])
        elif tag in _BLOCKS:
            self._emit("\n")

    def handle_data(self, data: str) -> None:
        if self.skipped or self.math or not self.in_article:
            return
        self._emit(data)


def _clean(text: str) -> str:
    return _WS.sub(" ", text).strip()


def parse(raw: bytes, arxiv_id: str, url: str | None = None) -> Document:
    parser = _Parser()
    parser.feed(raw.decode("utf-8", errors="replace"))
    parser.close()
    sections = []
    for section in parser.sections:
        lines = (_clean(line) for line in section.text.split("\n"))
        text = "\n".join(line for line in lines if line)
        if section.heading or text:
            sections.append(Section(section.heading, text))
    return Document(arxiv_id, url or html_url(arxiv_id), hashlib.sha256(raw).hexdigest(), parser.title,
                    sections, parser.tables, parser.figures)


def fetch_document(arxiv_id: str, get=fetch.http_get) -> Document | None:
    """The paper's HTML rendering as a Document, or None if arXiv has none (or it is too big)."""
    url = html_url(arxiv_id)
    raw = get(url)
    if not raw or len(raw) > MAX_HTML_BYTES or b"ltx_document" not in raw:
        return None
    document = parse(raw, arxiv_id, url)
    return document if any(section.text for section in document.sections) else None
