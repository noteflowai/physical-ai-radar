"""The pages the page_watch tests watch, fixed here, not read from data/sources.json.

Several tests exercise the page_watch channel itself (Firecrawl unavailable, one Firecrawl page read, one disallowed by robots.txt,
all pages failing, a hosted runner) and the four static sources. They need a Firecrawl page to exist. The shipped configuration has
none any more (2026-11-16: `watch-iso-25785-1` and `watch-eu-machinery-regulation` are marked not covered, `physical-intelligence`
is switched off), and it changes by decision, so a test that reads it as its input fails whenever the decision changes. These are the
entries as bb1d64de shipped them, so the outcomes those tests state are the channel's, whatever the configuration later is.
`tests/test_page_watch.py::ShippedPageWatchTest` pins what the shipped configuration actually is.
"""
from __future__ import annotations

import copy
from typing import Any

STANDARDS: list[dict[str, Any]] = [
    {"id": "iso-25785-1", "title": "ISO 25785-1 — Safety requirements for dynamically stable industrial mobile robots",
     "url": "https://www.iso.org/standard/91469.html", "status_note": "Under development; committee-stage as of 2026-09."},
    {"id": "eu-machinery-regulation", "title": "Regulation (EU) 2023/1230 (Machinery Regulation)",
     "url": "https://eur-lex.europa.eu/eli/reg/2023/1230/oj/eng", "status_note": "Fully applicable 2027-01-20."},
]

PAGE_WATCH: list[dict[str, Any]] = [
    {"id": "figure", "url": "https://www.figure.ai/news", "mode": "listing", "link_prefix": "/news/", "hosts": ["figure.ai"],
     "fetcher": "static", "min_links": 5, "evidence": "O", "weight": 1.1, "publisher": "Figure AI News", "topical": True},
    {"id": "1x", "url": "https://www.1x.tech/discover", "mode": "listing", "link_prefix": "/discover/", "hosts": ["1x.tech"],
     "fetcher": "static", "min_links": 5, "evidence": "O", "weight": 1.05, "publisher": "1X Discover", "topical": True},
    {"id": "physical-intelligence", "url": "https://www.pi.website/blog", "mode": "listing", "link_prefix": "/blog/",
     "hosts": ["pi.website", "physicalintelligence.company"], "fetcher": "static", "min_links": 2, "evidence": "O",
     "weight": 1.15, "publisher": "Physical Intelligence (π) Blog", "topical": True},
    {"id": "unitree", "url": "https://www.unitree.com/news/", "mode": "listing", "link_prefix": "/news", "hosts": ["unitree.com"],
     "fetcher": "static", "min_links": 2, "evidence": "O", "weight": 1.0, "publisher": "Unitree Robotics News", "topical": True},
    {"id": "watch-iso-25785-1", "standards_watch": "iso-25785-1", "mode": "page", "evidence": "O", "weight": 1.0,
     "publisher": "ISO", "topical": True},
    {"id": "watch-eu-machinery-regulation", "standards_watch": "eu-machinery-regulation", "mode": "page", "evidence": "O",
     "weight": 1.0, "publisher": "EUR-Lex", "topical": True},
]

STATIC_IDS = ["1x", "figure", "physical-intelligence", "unitree"]


def watched(sources: dict[str, Any]) -> dict[str, Any]:
    """`sources` with the fixed pages in place of its own: page_watch is the fixed list, and the two standards pages it names are
    the fixed ones (not marked not covered); other standards_watch entries (the Council's) are kept."""
    out = copy.deepcopy(sources)
    out["page_watch"] = copy.deepcopy(PAGE_WATCH)
    fixed = {watch["id"] for watch in STANDARDS}
    out["standards_watch"] = copy.deepcopy(STANDARDS) + [w for w in out.get("standards_watch", []) if w.get("id") not in fixed]
    return out
