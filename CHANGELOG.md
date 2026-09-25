# Changelog

Versions cover the pipeline in `pairadar/` and the published formats. The daily
radar content itself is dated, not versioned.

## Unreleased

Fixes that change what gets published:

- Figures keep their thousands separators and multipliers: `1,000 Hz` was
  published as `000 Hz`, `1,200 ms` as `200 ms`, and `3×` was not found. A
  version number such as `v2.5` is no longer read as a figure, and the
  quantitative-results signal now fires on a percentage.
- A feed date that cannot be read no longer becomes today. Named zones (`PST`),
  dates without seconds or a weekday are parsed, every date is folded to its UTC
  day, and an entry with no readable date is left out instead of being shown as
  fresh news.
- The repeat guard covers the whole 30-day pool. At seven days, a post still in
  the pool was picked again on the eighth day, and the feeds then dropped it.
- The arXiv lookback is four days, so Monday's run reaches the papers submitted
  on Thursday evening, and it counts back from the run date rather than the clock.
- Titles and excerpts are escaped where they enter Markdown, so fetched text
  cannot open an HTML element, break a link, or stop the Pages build with Liquid.
- Entities are decoded once and completely (`&#8217;`, `&#215;`, `&hellip;`).

Fetching and source health:

- A garbled status line or a truncated body skips that source instead of ending
  the run. A 4xx refusal is not retried; 429 honours `Retry-After`, within limits.
- An arXiv reply that is not a list of papers (an HTML page, an empty feed, an
  error entry) counts as no answer, so the category feeds take over.
- A day served by the arXiv category feeds is recorded under `fallback`, and
  `pairadar.health` reports it. A source is struggling only while it is still
  failing, so a recovered source no longer wakes the repair job every night, and
  the fourteen-day window is fourteen days.
- The publish retry loop pushes after every catch-up; it used to rebase a third
  time, never push, and report failure.

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
