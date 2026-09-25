# Changelog

Versions cover the pipeline in `pairadar/` and the published formats. The daily
radar content itself is dated, not versioned.

## Unreleased

The nightly jobs:

- The three cron jobs share one lock, so the repair job can no longer reset the
  clone under a draft still waiting for its checks. `scripts/radar-run` is now
  tracked; cron calls an installed copy.
- Every model call has a time limit, and a failed or stopped call says so in the
  log instead of ending the script silently.
- A day is published once. A second run on the same UTC date stops instead of
  committing a new update that differs only in timestamps; `--force` republishes.
- An open notes or repair pull request is not drafted again the next night.
- `daily.yml` checks at 03:20 UTC whether the day was published. If the machine
  missed it, the workflow publishes the day, requests a Pages build and opens an
  issue; if that fails too, the scheduled run fails and GitHub mails the maintainer.
- CI runs on every push to main, including the daily commits, lints the scripts and
  workflows, and validates the committed feeds, snapshot and landing pages.
- Every model call names its model and falls back down `RADAR_MODELS` when one is
  unavailable, which cost the nights of 09-24 and 09-25.
- `python3 -m pairadar.notes check` reports what is wrong with a draft item by item,
  and the drafter gets those findings to fix, at most twice, before the suite runs.
- The reviewer is a model that wrote no part of the draft. A rejection is handed back
  for one revision instead of ending the night.
- The notes record the models that actually ran (`pairadar.notes stamp`), not the
  agent's account of itself, and the agent files are validated before any model call.
- A drafted pull request merges only when every check has passed on the commit the
  script pushed. "No checks reported yet", right after the push, used to count as
  green; cancelled checks and checks still running after ten minutes now leave it open.
- The draft's re-render may rewrite the landing pages. With the site in place, every
  draft would otherwise have been discarded for touching them.

The site:

- The feeds link each item's day and the feed's home to the Pages site, not to
  GitHub's source view of the Markdown. Entry ids are unchanged, so readers see no
  duplicates.
- The Pages site opens on a landing page in each language (`/`, `/en/`, `/ja/`): the
  day's picks as cards, the eight lanes as a radar, the recent cadence and source mix,
  and share buttons that link the day's page. The daily, weekly and archive pages use
  the same dark layout and state their language in `<html lang>`.
- Each language has a social card (`assets/og.<lang>.png`), so a shared link shows a
  large preview. `python3 -m pairadar.site --og` redraws them.
- A sitemap and `robots.txt` are published, and CI builds the site the way Pages does.

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

What a reader gets out of each item:

- "Why it matters" is composed per item from its evidence tag, the checks its text
  gives or leaves out, and what to watch in its lane. It was one sentence per lane:
  48 lines in the week of 09-19, 8 of them distinct. `why_templates` in
  `data/glossary.json` is replaced by `why_parts`.
- Each figure is quoted with the words around it in the source, so `49.11%` reads
  as “on DynaForge data achieve 49.11% mean success”.
- Excerpts no longer quote feed furniture ("The post … appeared first on …", the
  Video Friday preamble).
- One story takes one slot: a vendor post and a retelling of it by a trade outlet
  no longer both make the page, on the same day or within the repeat window.
- The lane and evidence-mix charts count the last seven days of picks and no longer
  add the curated baseline. Daily pages state their counts as text instead of
  embedding charts that the next run overwrites.
- `python3 -m pairadar.feeds --backfill` fills the feed store from the daily
  snapshots in git history, so the weekly page counts every published day (W39
  showed 8 picks of 32).
- The English safety lane is "Safety, permissions & compliance"; Japanese pages say
  生産ライン for "production line".

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
