# Changelog

Versions cover the pipeline in `pairadar/` and the published formats. The daily
radar content itself is dated, not versioned.

## Unreleased

- The README's picks table has two columns instead of four: the rank and its
  evidence dot, then the title with up to three figures and a small line for the
  lane, source and date. On a phone the four columns left each title about 110px
  wide and pushed the figures off the screen.

## 1.1.0 — 2026-09-26

The radar gets a front door: a Pages site in each language with a shareable poster,
a README that opens on the day, three more sources, and nightly jobs that no
longer lose a night to one unavailable model.

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
- Every workflow action is pinned to a commit, and Dependabot proposes the bumps.
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
- The landing page draws the day as a 1080×1440 poster in the reader's browser: the
  picks with their lanes, evidence and lead figures, and a QR code to the day's page,
  for the places a link does not survive (an image forwarded in WeChat, Xiaohongshu or
  LINE). On a phone it can be long-pressed to save or sent through the share sheet.
  The QR encoder (`pairadar/qr.py`) is standard library only and is checked against
  known answers from Project Nayuki's qrcodegen.
- "Copy today's digest" puts the picks on the clipboard as text, one title and link
  each, with the day's page at the end.
- The cards can be filtered by lane, and the page carries a QR code to the day for
  a reader moving from desktop to phone.
- The daily, weekly and archive pages speak their own language in the navigation,
  end with a share bar (copy link, X, Weibo or LINE, LinkedIn) and show a reading
  progress bar where the browser supports scroll-driven animation.
- English titles on Chinese and Japanese pages keep their own punctuation (Latin
  faces come first in the font stack), keyboard focus is visible, and the pages print
  without the chrome. The sharing and motion live in one static `assets/site.js`;
  every page reads without it, and motion stays off under reduced-motion settings.

- The READMEs, the repository's front page, open on the day as a banner
  (`assets/banner.<lang>.svg`, redrawn with the charts): the headline, the day's four
  counts and the lane radar with its picks, animated except under reduced motion. The
  day's picks are one table (evidence, title, lane, source, figures) with the reasons
  folded beneath it, and the cadence and source-mix charts read at full width. The
  lane chart is gone from the README; the banner's radar counts the lanes.
- The READMEs show the site and its share poster side by side
  (`assets/showcase.<lang>.webp`, a screenshot taken by hand), compare the radar
  with newsletters, awesome lists and arXiv daily lists, and have a "take it with
  you" section: the feeds, `latest.json`, and `curl | jq` one-liners that run
  against the live site.
- The cadence chart's mean line has its key in the title row, where the latest bar
  can no longer cover it.

Fixes that change what gets published:

- Three more sources: Toyota Research Institute's paper pages (`R`), 雷峰网 Leiphone
  in Chinese and MONOist in Japanese (`M`). Lane keywords and anchors now include
  Chinese and Japanese terms, so those items can be read at all; 机器人 alone is not
  an anchor, because 聊天机器人 is a chatbot. Their descriptions are cut at a third of
  the English length: 2,000 Chinese characters hold about three times the text, and
  a promotional piece outscored the Qualcomm–PickNik acquisition on keyword count. Chinese and Japanese excerpts end on 。, and a
  long excerpt without spaces is cut at its length instead of at its last space.
  TRI's feed repeats the title, author handle and timestamp in every description;
  that prefix, and the 作者：… 编辑：… credit in Chinese articles, is no longer quoted.
- Fewer items in the wrong lane. Six keywords that meant something else as often
  as not are now phrases: `encoder` (a text encoder was filed as hardware),
  `demonstration` (a "proof-of-concept demonstration" as teleoperation data),
  `standard`, `compliance`, `memory` and `advantage`. Hardware gained soft,
  micro-, cyborg and medical robots; edge gained model-inference phrases. On a
  tie, a lane that names what a paper does beats `foundation`, the VLA it builds
  on: "Dexterous Manipulation through VLA Post-Training" is a training paper.
  `python3 -m pairadar.lanes` breaks ties the same way; it used to break them
  alphabetically and could name a lane the classifier had not chosen.
- Two links that differ only in their query (`news.php?id=12`, `?id=13`) are two
  items again; only tracking parameters (`utm_*`, `fbclid`, `oc`, ...) are ignored.
  Every article of such a site used to count as one, so all but the first were dropped.
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
