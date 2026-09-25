# Changelog

Versions cover the pipeline in `pairadar/` and the published formats. The daily
radar content itself is dated, not versioned.

## Unreleased

## 1.0.0 — 2026-09-25

First tagged release. The radar has published daily since 2026-09-19.

- Daily trilingual radar (Chinese, English, Japanese) in eight lanes, each item
  tagged `[O]` official, `[R]` paper or `[M]` media, with the checkable numbers
  (success rates, latency, Hz, parameters, data hours) extracted from the source.
- Relevance and balance: a whole-term relevance gate keeps off-topic posts out,
  per-source and per-evidence caps keep one feed or a day of preprints from
  filling the page, and revised old papers and announcement boilerplate are
  dropped from the arXiv fallback.
- Stable item ids and a 7-day repeat guard on normalized URLs, so an item is
  published once.
- Feeds: Atom per language and a JSON Feed 1.1, plus a weekly roundup in each
  language under `radar/weekly/`. The canonical feed addresses are on GitHub
  Pages (`https://noteflowai.github.io/physical-ai-radar/radar/feed.xml`,
  `feed.zh.xml`, `feed.ja.xml`, `feed.json`), which serves them with XML and
  JSON content types; the site pages advertise them for feed autodiscovery.
- Resilience: the arXiv category feeds are read when the API refuses requests,
  a fetch outage is reported instead of published, sources that quietly stop
  answering are named, and a published day can be re-rendered from its own
  snapshot.
- Drafted per-item analysis is labelled as drafted, and a drafted line with a
  multi-digit figure that the day's pages do not contain fails the test suite
  instead of being published.
- Charts (lane distribution, cadence, evidence mix) are hand-written SVG,
  labelled in each language and readable in dark themes.
- Standard library only; `python -m pairadar --offline` reproduces a page with no
  network access.
