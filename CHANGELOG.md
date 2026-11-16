# Changelog

Versions cover the pipeline in `pairadar/` and the published formats. The daily
radar content itself is dated, not versioned.

## 1.4.1 — 2026-10-04

- fix: label README baseline fallback on days with no live picks. On a day with no live picks, the README radar block (ZH/EN/JA) now matches the daily page. Under the day's heading and generated/window line it shows the localized no-items sentence as a quote, followed by a bold baseline label, and then the curated baseline table, reasons and links as before. Previously these baseline entries appeared under today's heading with ranks, with no explanation. When there are no picks and no baseline, the block shows the no-items line and an empty table header without raising. Days with picks render exactly as before. docs/METHODOLOGY.md section 3 describes the behavior. New tests in tests/test_feature_b41f745004f1.py cover all three cases in every language.

## 1.4.0 — 2026-09-30

- claim-audit worksheet for recently published numeric claims (RA-01). Adds python3 -m pairadar.claims, an offline, read-only command for the RA-01 review. It reads the published radar/feed.json store and prints a deterministic Markdown review sheet with one row per published figure. Each row has the day, evidence tag, figure, status, the quoted clause, a source link and an empty review cell. Statuses are quoted (found in the stored excerpt), title (found only in the title), no-excerpt (a baseline entry with no stored excerpt) and unlocated (not found in the stored title or truncated excerpt, so check the original source first). The reference day is the newest entry date, not the clock. The header gives the whole-window figure total, the rows shown and the skipped malformed entries. A whole-window status-count line always appears. When rows are capped, a truncation line says how many unlocated and no-excerpt figures are hidden and gives a --limit value that works. Text in cells is escaped for Markdown and table pipes. Store problems exit 2 with one 'claims: ' line and no traceback.

## 1.3.0 — 2026-09-29

- score keyword lane classification against a labeled shortlist. python3 -m pairadar.lanes now has a --labels PATH option that measures the keyword lane classifier against a hand-labeled file. For each entry it scores title + '. ' + summary with the same ranked_lanes/classify logic the pipeline uses. It applies the existing thin/ambiguous rules, which now live in a shared flag_reasons helper that inspect() also calls, so inspect() output is unchanged. It prints a JSON report with labels (the file's basename only), labeled, margin, agreement for all, confident and flagged entries (n, agree, rate; rate is null when n is 0) and the disagreements in input order. Each disagreement has id (null when absent), title, label, predicted, reasons, best and runner_up. A disagreement with reasons [] is a confident misfile that the flags missed. Invalid input exits 2 with one 'lanes: ' line on stderr and empty stdout. This covers an unreadable file, invalid JSON, no items list, a malformed entry (the index is named), a non-string id or summary, an unknown lane (the valid lanes are listed), and combining --labels with --latest or --fail-on-suspicious. Without --labels, the command behaves exactly as before. docs/METHODOLOGY.md section 9 documents the file format, the command and its limits.

## 1.2.0 — 2026-09-27

- report consecutive-failure streak and failing_since date for struggling sources in pairadar.health. Add a `streak(runs, reference, window, source)` helper returning `(streak_days, failing_since)`: the number of consecutive runs, counted backwards from the latest run with health inside the window, in which the source appears in `failed`, and the `date` of the oldest run in that streak. A run that lists the source under `fallback` or does not list it at all ends the streak. `report()` keeps every existing key and adds `streak_days` and `failing_since` to each entry of `struggling`. Because `struggling` already requires the source to have failed on the latest run, `streak_days` is always >= 1 there. The CLI (`python3 -m pairadar.health`) prints the enriched JSON; `--fail-on-struggling` semantics are unchanged. The module docstring and docs/automation.md gain one sentence explaining the two fields.

## Unreleased

- **No page_watch entry uses Firecrawl now** (it was unavailable on every one of the 26 days on record, 2026-10-22 to 11-16, and
  was not redeployed). The two standards pages that went through it, ISO 25785-1 and Regulation (EU) 2023/1230, are removed from
  `page_watch` and marked `covered: false` with a `not_covered_reason`, like the Council's timeline: ISO's robots.txt answered 403
  when read on 2026-11-14; no verifiable robots.txt content was obtained for EUR-Lex. Neither was ever read, so no day's content
  changes. The run no longer reports `unavailable: ["page_watch"]` (a run on a copy of the tree shows it gone). A `page_watch` entry
  can be switched off with `"enabled": false`, as a feed can: anything but `true` switches it off, it must carry a
  `disabled_reason`, and it is not read, not asked about in robots.txt, not planned and not counted as failed; its baseline stays.
- **Physical Intelligence is switched off** (`enabled: false`) from the change of 2026-11-16, which is when the radar stops
  asking its host for anything. What the records show: its page was last read in the 11-11 run; in each run from 11-12 to
  11-15 the page was not read because its robots.txt answered 429 (the private freshness record of each day says so); in
  the 11-16 run the remembered back-off was used and no request was sent. Until the switch-off the host was still asked
  (its robots.txt) in each run that was not backing off. By the robots rule its pages are not read, and the host is not
  asked again until a person switches it back on. Its published entries stay readable (the publisher name still resolves).
  **Known effect, not changed here:** the freshness record's `status` is `ok` only when the Firecrawl channel answered
  (`firecrawl_answered`, ADR 027); with no entry on Firecrawl that never happens, so the record is `skipped` or `all-failed`
  instead of `firecrawl-unavailable`. Both were non-success before and are now. Whether the objective should count a static read
  is a decision for its owner (noteflow-auto `config/delivery-freshness.json`).
- Industry events get reserved highlight slots. A new `industry_event` signal (funding, M&A, a listing, a production start,
  policy, a counted deployment milestone; English and Chinese terms in `data/taxonomy.json`) adds 0.50 to a post's score
  (never a paper's), `filters.highlights.reserve` gains `event` twice (two slots, recorded as `event` and `event#2`), and
  `filters.highlights.event_min_score` (0.6) lowers the score bar for those posts only; the lane keyword and topic anchor
  gates still apply. Before, a post about a funding round or a production start scored 0.4 to 0.9 against 2.0 to 3.8 for
  papers and never reached the page. `select(..., event_min_score=None)` keeps its old behaviour when the argument is
  absent. The signal label is added in zh, en and ja (`ui.signals.industry_event`); the Japanese and Chinese wording needs
  a native review. docs/METHODOLOGY.md section 3 (Highlights quota) describes it; tests are in tests/test_industry_events.py.

- The feeds (and the attention APIs) and the Firecrawl page path read robots.txt with `pairadar.robots_rules`
  (RFC 9309: `*` and a final `$` in a rule, the longest match wins, every group that names the radar is merged)
  instead of `urllib.robotparser`, which has no wildcards and takes the first match. A robots.txt that cannot be judged
  completely (not UTF-8, over 500,000 bytes, too many or too long rules, an invalid percent escape) now makes the
  host `unreachable` for the run instead of being cut or read as far as it goes. Feeds a wildcard rule disallows are
  `robots_skipped` with the reason `disallowed`, never `failed`.
- The robots.txt that a production run read (feeds, Firecrawl pages and static pages) is kept in a second file,
  `robots-texts.json`, in the state directory, next to `robots-state.json` (whose format and waits it does not touch):
  one entry per host, the latest, with the time, the sha256 and size of the bytes read and the text. A file over
  64 KiB, or not UTF-8, keeps only its sha256 and size and says why. It is never used to allow or refuse a request, so a
  rule change can be replayed offline against what the hosts really said without asking them again.
- The run record and `FetchReport.to_dict()` gain `robots_newly_denied`: the feeds, attention APIs (`signal:<name>`) and
  Firecrawl pages that the new reader refuses and `urllib.robotparser` (the reader before) would have allowed, each with
  its reason. Static pages already used `robots_rules`, so they are not in it. The old reader is kept only to compute
  this and decides nothing; remove it once the first runs have been read back.
- A feed, signal or Firecrawl page whose own URL has a `%` that is not a percent escape cannot be judged against any
  rules. That one URL is refused (`unsupported-url`, `robots_skipped`, never `failed`) and the rest of the run is read
  as before; before, the exception ended the whole feed or page loop.
- A feed can be switched off with `"enabled": false` in `data/sources.json` (anything but `true`
- A 2xx answer to the request for robots.txt is read as a robots.txt only if it is one. For the feeds and the
  attention APIs, the URL it came from after any redirect must be the same origin's `/robots.txt`, and its media type
  must be `text/plain` or absent; for Firecrawl pages, `metadata.sourceURL` and `metadata.url` (the final address; a reply
  without it is "final source unknown") must both be that `/robots.txt` and `contentType`, when present, must be
  `text/plain`. Whatever the Content-Type says or does not say, a body that is an HTML page (after a byte order mark and
  white space it starts with `<`, or it contains `<html`, `<!doctype`, `<head` or `<body` in any case) is not a robots.txt.
  An address that cannot be parsed (`https://[invalid/robots.txt`) is "not confirmed" for that one source and raises
  nothing. A login page, a challenge or the page a redirect
  led to makes the host `unreachable` for the run (the pages are refused) instead of reading as "no rules", and its
  text is not kept in `robots-texts.json`. A real empty robots.txt still allows everything. Firecrawl text that cannot be
  written as UTF-8 (an unpaired surrogate) is refused the same way instead of raising. Hosts that answer /robots.txt with
  a 200 HTML page (a soft 404) are now `unreachable`, not allowed. The cost: RFC 9309 lets a robots.txt redirect to another
  path or host, and that is refused too, on purpose.
  switches it off; absent means on) and keeps a `disabled_reason`. A disabled feed is not
  requested, not asked about in robots.txt and not reported as attempted. The three GitHub
  release feeds (LeRobot, Isaac Lab, MuJoCo) are switched off: github.com's robots.txt disallows
  `/*.atom$` for every crawler.
- robots.txt is read as RFC 9309 asks: 401/403 refuse the host (`forbidden`), other 4xx
  mean no rules, and a 5xx, a 429 after its retry or no answer at all leave the host
  `unreachable` and unrequested for the run. Refused feeds and attention APIs are
  `robots_skipped` with a reason in the run log and in `pairadar.health`
  (`robots_reasons`), never `failed`. Before, any robots.txt that could not be read
  allowed everything.
- The page path (`page_watch`, through Firecrawl) reads robots.txt the same way: a 401, 403 or
  429 refuses the page for the run (before, any 4xx was read as "no rules"); only another 4xx
  means no rules, and so does an empty 2xx body. A status that is not a whole number from 100 to
  599, a 2xx with no body, a 5xx and no answer refuse the page too. The policy is stricter than RFC 9309
  (which lets a crawler read after a 4xx) and is kept equal to the feeds'. A test compares the two paths for every status.

- More professional and mainstream sources, attention ("heat") and a highlights quota.
  Twelve feeds join after a read-only check (200, parses, robots.txt allows): OpenAI News,
  Google Research, MIT News Robotics, BAIR, Nature Machine Intelligence (RSS 1.0, now
  parsed), TechCrunch Robotics, Robohub, Import AI, 量子位, and the LeRobot, Isaac Lab and
  MuJoCo GitHub release feeds. General feeds keep only entries naming their own
  `keywords`. Science Robotics is not added: its feed path is disallowed by robots.txt.
  `pairadar.heat` scores cross-source coverage, Hugging Face Daily Papers upvotes, Hacker
  News points and GitHub star velocity, each capped and scaled, for ranking only; it never
  changes an evidence class. Items covered by two or more sources are labelled `trending`,
  press releases `press_release` and ranked lower. The shortlist now holds at most four
  arXiv papers of eight and reserves a slot for an official item and one for a trending
  one when any qualifies. robots.txt is honoured for every feed and API, requests to one
  host are spaced a second apart; robots skips and unanswered APIs are logged and reported
  by `pairadar.health`, never as struggling. docs/METHODOLOGY.md sections 1–3.
- A misspelt `filters.highlights.reserve` entry is ignored and reported as
  `unknown:<name>` in the run log (`quota.unmet`) instead of stopping the run.

- The README's picks table has two columns instead of four: the rank and its
  evidence dot, then the title with up to three figures and a small line for the
  lane, source and date. On a phone the four columns left each title about 110px
  wide and pushed the figures off the screen.
- The comparison table has three columns instead of five, the other sources
  sharing one, and the lanes table two instead of three, what a lane tracks and
  why it has its own lane in one cell. On a phone the five columns set
  "newsletters" one character per line and ran off the side of the screen.
- **A switched-off entry's host is no longer requested through a redirect** (`pairadar/host_policy.py`). Switching a `page_watch`
  entry off only removed its page from the plan; `fetch.http_get`, which follows redirects, would still have sent a request to
  that host if a feed, a robots.txt or an attention API redirected there. `http_get` now asks the policy before the first request
  and before every redirect hop (urllib's own redirect handler, one hop at a time), and a hop to a blocked host is not sent: the
  answer is the same as for no answer, and it is not retried. `fetch_page_watch` checks an entry's URL before its page or its
  robots.txt is requested. Blocked: the host of the entry's `url` and the names in its `hosts` with their subdomains
  (`www.pi.website`, `pi.website`, `physicalintelligence.company`), unless something switched on uses them; a switched-off feed
  is not blocked by host. A policy that cannot be read refuses every request. DNS is not resolved: an address literal, or another
  name for the same server, is not recognised. Tests run the real redirect handler and the real consumers (feeds, robots.txt,
  the attention APIs) against an in-memory transport.
- **A run of static sources only no longer depends on the Firecrawl endpoint.** The loopback check on `RADAR_FIRECRAWL_URL` ran even
  when no planned entry used Firecrawl, so a bad endpoint made a successful static run report `unavailable: ["page_watch"]`. It
  now runs only when an entry that goes through Firecrawl is planned; a run that mixes both, and a run of Firecrawl entries only,
  are refused as before.
- The nightly development planner and its two planning reviews (the feature-plan review and the
  no-change review) now see the same controller rules and the same account of what the reviewer can see.
  The two rules that were written only into the planner prompt ("A stale roadmap forbids new features."
  and "Pure documentation may be identified for maintainer follow-up; this code delivery lane requires
  behavioral acceptance.") are single constants in `scripts/feature_prompts.py` and are carried into both
  reviews, marked as controller-supplied, so a review no longer calls a plan that quotes them an
  invention. `recent_commits` is now the last background field dropped from an over-budget prompt (it was
  third, ahead of the much larger `research` and `issues`). The planner prompt now says which fields the
  reviewer's copy may lack and in what order (the same tuple that drives the dropping), tells the planner
  to label claims that rest on them unverified, and states that a file over 24,000 bytes reaches the reviewer
  as a signature index only and that at most five `inspect_ranges` are read. A requested line range that
  the excerpt limit would cut (240 lines, or the remaining byte budget) is no longer shortened silently:
  the plan attempt fails with the cut line named and every cut range listed, and the cut excerpt is not kept
  as inspected. The planner prompt text is otherwise unchanged. New tests in
  `tests/test_nightly_review_evidence.py`.
- **The host policy is asked again before every attempt to send, not once before the retry loop.** `fetch.http_get` asked
  `host_policy` once and then retried on the strength of that answer, so a host switched off while the first attempt or the wait
  before a retry was under way was still asked a second time; the same held for a policy that became unreadable in between. It
  now asks before each attempt. The policy is read from `data/sources.json` on every question instead of being cached by its
  modification time and size (a rewrite that kept both would not have been seen). Tests switch the host off, or break the file,
  during the wait and between two attempts, and rewrite the file with the same size and time.

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
