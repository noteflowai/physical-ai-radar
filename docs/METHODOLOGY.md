# Methodology · 方法论 · 方法論

This document is the contract between the repository and its readers. If the pipeline
changes, this file changes in the same pull request.

## 1. Sources

| Kind | Endpoint | Evidence tag | Why it is trusted at this level |
| --- | --- | --- | --- |
| arXiv API | `https://export.arxiv.org/api/query`, six `cs.RO` queries | `R` | primary author text, but self-reported and often not peer reviewed |
| arXiv category feeds | `https://rss.arxiv.org/rss/<category>`, read only when the API returns nothing | `R` | the same papers, announced listings instead of search |
| Official vendor and lab blogs | AWS Physical AI, NVIDIA Technical Blog / NVIDIA Blog, Google DeepMind, Hugging Face, OpenAI News, Google Research, MIT News (Robotics) | `O` | first-party statements about their own products and work |
| Official open-source releases | GitHub release feeds of LeRobot, Isaac Lab, MuJoCo (newest release only) | `O` | the project's own release notes |
| Lab research pages and journals | Toyota Research Institute, BAIR blog, Nature Machine Intelligence | `R` | a lab's own page or a journal's listing of a paper: the same standing as the paper |
| Trade and mainstream press | IEEE Spectrum Robotics, The Robot Report, TechCrunch Robotics, Robohub, Import AI; 雷峰网 Leiphone and 量子位 QbitAI (Chinese), MONOist (Japanese) | `M` | professional reporting and analysis, still secondary |
| Attention signals | Hugging Face Daily Papers API, Hacker News (Algolia search API), GitHub REST (stars) | — | ranking only (section 3); never an item and never evidence |
| Standards watch | ISO, EUR-Lex, Council of the EU | `O` | authoritative status and dates |
| Page watch | Figure, 1X, Physical Intelligence, Unitree and the standards-watch pages, through a loopback Firecrawl | `O` | first-party pages of sources that publish no feed |

Rules:

- only declared machine-readable endpoints (Atom / RSS / documented API);
- no HTML scraping of article bodies, no paywall circumvention;
- the one exception is `page_watch`: an official source with no feed at all is read as a
  page, only through a self-hosted Firecrawl on 127.0.0.1, localhost or ::1 (any other
  endpoint, including `RADAR_FIRECRAWL_URL`, is refused; redirects and proxies are not
  followed). robots.txt is checked first, through the same Firecrawl; a robots.txt that
  cannot be read disallows the page for that run. Firecrawl's own change history needs
  its hosted store, so the radar keeps a baseline in `radar/page-watch.json` (digests,
  link URLs with the day each was first seen and last offered, line digests; no page
  text). It is written atomically, and only when something in it changed; an unreadable
  one is rebuilt, as on a first look, instead of failing the run.
  - A listing records every first-party link under its `link_prefix`. Each run offers
    the links still inside `filters.max_age_days` -- by the date the link states, or, for
    a link that appeared after the first look, the day it was first seen --
    `max_items_per_source` at most, least recently offered first; the rest are offered
    on the next run. As with feed entries, the repeat guard keeps a published link from
    being picked again. On the first look only links that state a date are offered.
  - A single page yields one item pointing at it when it has lines it did not have
    before, dated the run day (the page's own publishedTime is when the page went up,
    often years ago). Lines that only stamp an update time ("Last updated …", "©") are
    ignored; other numbers, such as a stage code or an application date, are the news.
  - A page that cannot be read (non-2xx, Firecrawl `success: false`, 408, or a 5xx with
    a Firecrawl error code such as `SCRAPE_SITE_ERROR`) is that page failing: recorded
    and the next page is tried. Firecrawl itself is unavailable when it refuses, times
    out, answers 502/503/504, a 5xx that is not JSON or carries no error code or
    `UNKNOWN_ERROR`, or after three page failures in a row answered with a 5xx; the
    channel then stops, keeps its baseline and is recorded under `unavailable` in
    `radar/history.json`. Pages not read go under `page_failed` and those robots.txt
    disallowed under `page_robots_skipped`, never under `failed`: `pairadar.health`
    reports them as `page_watch_failing` / `page_watch_robots_skipped` and never as
    struggling, so the source-repair job (which can only repair a declared feed) never
    picks one.
  - A real run (not `--offline`, not `--out`) also writes one freshness record per day
    to `$PAIRADAR_STATE_DIR/page-watch/<day>.json` (default
    `~/.local/state/pairadar/page-watch/`), schema `pairadar-page-watch-v1`, channel
    `page_watch`. `status` is `ok` only when at least one watched page was read;
    `all-failed` when pages were attempted and none was read; `firecrawl-unavailable`;
    `skipped` when the channel is disabled or reached no entry; `error` when the run
    itself raised. Only `ok` counts as success. The record also carries
    `sources_checked`, `sources_failed`, `sources_robots_skipped` and `finished_at`.
    Writing it is observational: a failure there is logged and never fails the run;
  - **Static sources** (`"fetcher": "static"` on a `page_watch` entry; the default and the only
    value any shipped entry has is `firecrawl`; an unknown value makes the entry invalid, it never
    falls back). `pairadar/static_fetch.py` does one `GET` of a public `https://<domain>/…` URL on
    port 443 and nothing else: URL parsed strictly (no user information, IP literal, odd port,
    control character or fragment); the name is resolved for IPv4 only and EVERY answer must be a
    public address (an answer with any other, or IPv6 in any form, is refused whole, before a
    connection); the connection goes to an approved address and its peer is checked to be that
    address; TLS uses the original name for SNI and certificate verification; no proxy, cookie,
    credential, redirect or content-encoding; only a complete `200` of an accepted type
    (`text/html`/`application/xhtml+xml`, `text/plain` for robots.txt) within 2 MB, behind a
    status line and headers of at most 16 KB, is content; TLS 1.2 or newer with ALPN `http/1.1`
    only. The size limit counts bytes actually read, including chunked bodies. The
    whole request (name resolution, connect, handshake, headers, body) runs under one monotonic
    deadline, 30 s per source and never past the run's `budget_seconds`; a name lookup that does
    not return is abandoned at the deadline (at most two such lookups may be alive). Per source:
    at most 4 requests, 2 s between requests to one host. robots.txt is read through the same
    fetcher: a `200 text/plain` of valid UTF-8 is the rules, read WHOLE (the file is at most 512000
    bytes, or it is `too-large` and unreadable) by `pairadar/robots_rules.py`, which follows RFC 9309
    for the product token `physical-ai-radar`: groups naming it are merged (else the `*` groups),
    the longest matching `Allow`/`Disallow` wins, a tie goes to `Allow`, `*` and a final `$` are
    wildcards, the matched path includes the query and percent-encoding is compared in one form.
    (`urllib.robotparser`, which the Firecrawl path still uses, takes the first match in file order and
    would let `Allow: /` before `Disallow: /private` through.) A file with more than 5000 rules, a rule
    longer than 2000 characters, or bytes that are not UTF-8 cannot be judged completely and is
    unreadable, not guessed at. `404`/`410` means there are no rules, and anything else (401, 403, 429,
    5xx, a challenge, a redirect, an HTML page, a timeout, a refused address) disallows the page for the
    run.
  - A `200` is still not necessarily the page: a challenge or interstitial (by title, or by a
    bot-management marker on a page with little text), a login or consent page, an empty
    JavaScript shell, a page-mode source with almost no text and a listing with no link under its
    `link_prefix` are `unavailable` and leave the baseline unchanged. These checks are heuristics; a
    disguised `200` they cannot recognise is a residual risk of this fetcher.
  - **Every static entry says what a real page of it looks like**, or it is invalid and not run:
    a listing needs `min_links` (1 to 1000); a page needs `expect_text` (3 to 200 characters that
    must appear in its visible text, case-insensitive) and `min_text_chars` (100 to 100000). A
    listing that has fewer links than `min_links` is `no-links`, a page without its marker is
    `marker-missing`. A listing whose link count falls below half of the count on record (the
    record keeps `link_count`, and only when at least 6 were on record) is `link-drop` and is not
    read: a damaged page is more likely than a purge. The way out is a reviewed `allow_link_drop:
    true` on that entry (or a fresh baseline for it). These settings show that a page is not an
    obvious wrong page; they do not show that it is the right or a complete one.
  - **Nothing is cut silently, and the baseline must be able to hold what it is given.** The
    extracted text of a source may not exceed 200000 characters and a listing may not have more than
    3000 links (a page-mode source keeps the text of its links, so only the text cap applies to it);
    over either, the source is `blocked: content-cap`, not read from a truncated copy. The baseline
    records at most 300 links (a listing) or 2000 distinct lines (a page). A listing with more than
    300 links is `blocked: too-many-links` and a page with more than 2000 distinct lines is
    `blocked: too-many-lines`, with the baseline unchanged: a page that could only be recorded in
    part would show its unrecorded part as new the next day. A listing that fits keeps every one of
    its links; only links that have left the page are forgotten when the record is full.
  - **Observe first.** `page_watch_settings.static_mode` is `observe` unless it is exactly
    `"live"`; any other value, or none, is `observe`. In `observe` a static source is fetched and
    judged under every rule above, and the run takes nothing from it: no baseline change, no item,
    no entry in `attempted`, `checked` or `failed`, so no freshness claim (an observe-only run's
    status stays `skipped`, or `firecrawl-unavailable` when Firecrawl is down). What each source
    showed is written to `$PAIRADAR_STATE_DIR/page-watch-observe/<day>.json` (default
    `~/.local/state/pairadar/page-watch-observe/`, mode 0600), outside the repository because the
    radar commits what it writes there: per source the outcome, the fixed reason code, the HTTP
    status, byte count, link count, visible-text length, a short digest of the extracted text and
    the robots verdict; no page text, URL or query. Going live is the one-word change
    `"static_mode": "live"`, made after someone has read that file.
  - A redirect is never followed. It is `blocked: redirect`, and only the target's domain name
    is kept as `detail` (`relative` for the same host, `invalid` for an IP literal or anything
    that is not a plain name), never its path, query or port.
  - Every static source ends a run as `checked`, `observed`, `unavailable`, `blocked` (our policy
    refused: address, redirect, content type or encoding, size, header block, allowance), `robots`
    or `not-attempted` (`budget`, `firecrawl-unavailable`), with one fixed reason code from
    `static_fetch` (`robots-unreadable` also carries the code of the fetch that failed as `detail`),
    never a URL, status text or server message. An unexpected error in one source is that source's
    `internal-error`; the run goes on within its budget, and KeyboardInterrupt and SystemExit pass
    through. The run log carries only the source id and the code. The freshness record gains
    `sources_planned`, `sources` (per source: fetcher, outcome, reason, optional detail),
    `sources_blocked`, `sources_unavailable`, `sources_not_attempted`, `sources_observed`,
    `degraded` (a planned source was not read) and `sources_escalated`, and only in a run that has a
    static source. A source that is blocked or unavailable in the last 3 daily records (read from
    the earlier `page-watch/<day>.json` files; no extra state) is listed in `sources_escalated` and
    printed as `ESCALATE`. `status` stays `ok` when one page was read, which is a floor; read
    `sources_checked` against `sources_planned`. A Firecrawl channel that is down no longer hides
    static sources read live in the same run.
  - **Baseline migration.** A baseline records the extractor that made it (`"extractor":
    "static-1"`; a Firecrawl baseline has no key). The first successful read by a different
    extractor re-seeds the baseline instead of comparing text across extractors: every link already
    known keeps its first-seen day, a link first seen now counts as old unless it states its own
    date inside the window, a page re-seeds its line digests and reports nothing, and the old
    baseline is recorded as `migrated_from` (extractor, old digest, day). A real change made between
    the last old read and the first new one is not reported. Going back to Firecrawl re-seeds in the
    same way. A failed read never changes a baseline.
  - Where the radar runs, `scripts/radar-run` resets the clone to `origin/main`, so a merge is
    live. This change is inert on merge: no shipped entry has `"fetcher": "static"`. A source is
    switched by a later change to `data/sources.json` that adds the key to that entry, and it is
    observed only until `static_mode` is changed to `"live"` in a further reviewed change. Entry
    points that fetch (network mode of `python3 -m pairadar`): `scripts/publish_daily.sh`, run at
    07:40 host time (Asia/Singapore, `scripts/cron.sg`) and as the `publish` stage of the 21:30
    nightly batch and its 02:30 `--previous-day` resume (`nightly_plan.py`, `nightly_batch.py`);
    `draft_daily_notes.sh` uses `--offline` and `--rerender`, `repair_sources.sh` only reads
    history. Any of these may carry the first observation after the enabling change merges.
- every request is timeout-bounded and retried once before the source is given up on. A
  4xx refusal is final and is not retried; 429 waits for `Retry-After`, at most 30 seconds;
- arXiv calls are spaced about three seconds apart, as its API guidance asks;
- a failing source is skipped with a log line and never fails the run — but if **no**
  source answers, the run exits non-zero and nothing is committed, because a radar
  rebuilt from the curated baseline alone must not look like a quiet news day;
- `radar/latest.json` carries a `fetch` block (items, sources attempted / answered,
  per-source counts, names of sources that did not answer) so partial degradation is
  visible without reading the workflow log;
- every run also records that day's unanswered sources in `radar/history.json`, and
  `python3 -m pairadar.health` turns the last fourteen days into a verdict: a source
  that missed three or more days and also missed the latest run is reported as
  struggling. When the arXiv API refuses and the category feeds serve the day instead,
  the run records `arxiv` under `fallback`: visible in the verdict, but not a failure. The daily run prints the
  verdict and stays green; `--fail-on-struggling` exits 1 for automation that should
  only act when there is something to act on;
- a feed contributes at most its `filters.max_entries_per_feed` newest entries (default
  100). Some feeds publish their whole archive (the Hugging Face blog serves about 900
  posts); without the cap one source floods the pool;
- a feed description longer than 2000 characters is cut at a word boundary. Some feeds
  carry the full article, and a full article matches every lane;
- adding a source requires a weight and an evidence tag in `data/sources.json`. A source
  whose every entry is about robots is also marked `"topical": true` (see section 2). A
  general source (OpenAI, Google Research, TechCrunch, 量子位…) names its own `keywords`
  instead, and only entries naming one of them are read, before the per-feed cap: the
  taxonomy anchors are looser than a feed of 1,300 posts allows ("cosmos", the NVIDIA world
  model, let an Azure Cosmos DB post through). `max_entries` lowers the cap for one feed,
  and `title_prefix` names the project in front of a bare release title ("3.17.0");
- RSS 1.0 (RDF) feeds are read as well as Atom and RSS 2.0; Nature's journal feeds are
  RSS 1.0, and their "Journal, Published online: date; doi:…" opening is dropped;
- robots.txt is read once per host per run, before any feed or attention API on that host.
  A URL it disallows for `physical-ai-radar` or `*` is not requested, and the feed is
  recorded under `robots_skipped` in `radar/history.json`, never under `failed` (the
  Science Robotics eTOC lives under a disallowed `/action/` and is therefore not a source).
  As RFC 9309 section 2.3.1 says: a robots.txt answering 401 or 403 refuses the whole host
  (`forbidden`); any other 4xx (404, 410) means there are no rules; a 5xx, a 429 after its
  retry, no HTTP answer at all or anything else that cannot be classified leaves the host
  `unreachable`, and nothing on it is requested that run. Every refused feed or attention
  API is recorded under `robots_skipped` with its reason in `robots_reasons`
  (`disallowed`, `forbidden`, `unreachable`) and never under `failed`; `pairadar.health`
  counts them per source and reason (`robots_reasons`), never as struggling. Two requests
  to one host are at
  least one second apart, and every request carries the `physical-ai-radar/1.0` User-Agent.
  Only titles, links, dates and a cut excerpt are stored; no article body is reproduced;
- the attention APIs (section 3) that do not answer are recorded under `signal_failed` and
  reported by `pairadar.health` as `signals_unavailable`, never as struggling.

## 2. Classification

`data/taxonomy.json` defines eight lanes. Each lane has a keyword list and a weight.
An item is matched case-insensitively against `title + summary`, **as whole terms**: a
keyword counts only where it is not preceded or followed by a letter or digit, with an
optional plural `s` / `es`. So `ppo` no longer fires inside "support", `thor` inside
"author" or `droid` inside "Android". The lane with the highest `hits × lane_weight` wins. On a tie, any lane with hits beats
`foundation`: nearly every robot-learning paper names the VLA or world model it builds
on, and one that also names what it does with it belongs to that lane. Other ties
resolve to the first lane in file order, which makes classification deterministic.
Keywords and anchors include Chinese and Japanese terms for the zh and ja feeds; those
items are quoted in their own language on all three pages. Their descriptions are cut
at a third of the English length, which holds about as much text, so they do not
collect three times the keywords.
A keyword that is just as common outside its lane ("encoder", "memory", "standard")
is listed as a phrase ("rotary encoder", "episodic memory", "safety standard").

A curated entry in `data/baseline.json` that declares a `lane` keeps it: the lane is an
editorial decision, and the classifier is not allowed to overrule it. Such an entry is
scored on its own lane's keywords rather than the best-matching lane's.

Before any lane counts, an item must be **Physical AI at all**. Items from a topical
source (a `"topical": true` feed, or a paper listed in `cs.RO`) are on-topic by
construction. Everything else, which means general company blogs and papers found only
through other arXiv categories, must name at least one term from `anchors` in
`data/taxonomy.json`: robot, humanoid, embodied, manipulation, VLA, world model, ROS 2,
Isaac, LeRobot, Jetson and similar. Words like "agent", "benchmark" or "inference" are
lane keywords but not anchors, because a coding-agent post uses them just as much.

Signals are orthogonal flags: `closed_loop`, `real_robot`, `open_release`, `latency`,
`numbers`. They are shown as badges and feed the score.

## 3. Scoring

```
score = min(lane_hits, 6) × 0.35 × lane_weight
      + evidence_bonus            # O 0.60 · R 0.35 · M 0.10
      + Σ signal_bonus            # closed_loop 0.50 · real_robot 0.40 · numbers 0.30
                                  # open_release 0.25 · latency 0.20
      + recency_bonus             # same day 1.0 · 1 day 0.8 · ≤3 days 0.5 · ≤7 days 0.25
      + (source_weight − 1.0)
```

Selection then applies `min_score = 0.9`, a per-lane cap of 2 and a global limit of 8,
so a single hot topic cannot take over the page. Two soft caps sit next to it:
`filters.per_source` (3) and `filters.per_evidence` (4). Selection makes two passes in
rank order. The first pass honours every cap. The second pass fills whatever slots are
still empty with the lane cap only. A soft cap therefore reserves room for other sources
and evidence kinds but never leaves the page short. Without them, a day when the arXiv
API refused and the category feeds answered came back as eight `[R]` preprints. The
shortlist is always shown in rank order. Three further gates apply:

- **on-topic** (the anchor rule in section 2);

- **at least one lane keyword.** Evidence and same-day recency alone clear `min_score`, so
  without this an off-topic hit from the broad arXiv queries would be published under the
  fallback lane, labelled as if it belonged there. Curated entries are exempt, since a
  person chose their lane.
- **not published in the last `filters.repeat_days` days** (30). Each run records the
  normalised URLs it published in `radar/history.json`; the arXiv window looks back four
  days and feeds keep entries for thirty, so without this a strong item would headline
  again the next day. The guard has to cover both horizons: at seven days, a post still
  in the thirty-day pool was picked again on the eighth. Matching is on a normalised URL, not id, so it survives a feed reassigning ids. The
normal form drops query, fragment and trailing slash, lowercases, and upgrades `http` to
`https`. arXiv links are reduced to `https://arxiv.org/abs/<id>` without the version,
because the API answers `http://arxiv.org/abs/<id>v1` and the feeds answer
`https://arxiv.org/abs/<id>`: they are the same paper. Every term is visible and tunable; no
learned model sits in this path. This is a bias-by-design choice: the radar prefers
items with real-robot or closed-loop evidence over pure benchmark deltas.

A fourth gate keeps one story to one slot. A vendor post and a trade outlet's retelling
have different URLs, so the repeat guard lets both through: on 2026-09-22 NVIDIA's Isaac
ROS 5.0 post and The Robot Report's account of it took two of the eight places. Two
titles tell the same story when they share at least three distinctive words and those
make up at least half of the shorter title. Function words, press verbs and the names
that appear in half the pool (NVIDIA, Google, robot, AI…) do not count, so two unrelated
Isaac Sim posts or two Video Friday issues stay apart. The higher-ranked telling keeps the
slot, and a story told on an earlier day inside the repeat window is not told again.
Papers are exempt on both sides: two preprints with similar titles are two papers.

### Heat (attention)

`pairadar.heat` measures how much attention an item is getting, for ranking only:

```
heat  = (0.40 × cross + 0.25 × hf + 0.20 × hn + 0.15 × stars) / 1.00      # each 0..1
score += 0.8 × heat
```

- `cross`: independent sources telling one event. Items are one event when their links
  normalise to the same URL or, for anything but a paper, when their titles tell the same
  story (the rule above). The count is distinct sources, capped at 4: one source scores 0,
  two 1/3, four or more 1. One outlet posting twice is not two sources.
- `hf`: Hugging Face Daily Papers upvotes for the arXiv paper, `hn`: points of a recent
  Hacker News story linking the item or titled as the same story, `stars`: stars per day
  gained by a tracked repository (its release feed, or a link into it), against the count
  kept in `radar/heat-state.json` from an earlier day. Each is capped (100 upvotes, 300
  points, 200 stars a day) and log-scaled to 0..1, so one viral item cannot outweigh the
  rest of the score.

Heat never changes an item's evidence class: a story every robotics site retold is still
`[M]`, and a paper with many upvotes is still a self-reported `[R]`. An item with two or
more sources, or heat of at least 0.3, carries the `trending` signal. All weights and caps
are in `data/sources.json` `heat`.

### Press releases

An announcement in press-release form — "announces", wire-service copy, a stock ticker —
carries the `press_release` signal and loses 0.4 of score. Its evidence class is unchanged:
an official source announcing its own product is still a first-party statement. Papers and
curated baseline entries are never matched.

### Highlights quota

The shortlist is the page's highlights. `filters.highlights` adds three rules on top of the
caps above:

- arXiv takes at most half of the limit (4 of 8). Unlike the soft caps this holds in the
  fill pass too: a day with nothing else that qualifies publishes a shorter list, not an
  arXiv-only one;
- one slot is reserved for the best qualifying official (`[O]`) item, and one for the best
  qualifying non-arXiv item marked `trending`. A reservation passes every gate above (topic,
  lane keyword, repeat guard, same-story, lane cap); one that nothing can fill stays empty,
  and the run log records it under `quota.unmet` in `radar/history.json`. A name other than
  `official` or `hot` is ignored and recorded there as `unknown:<name>`.

A day may publish fewer than eight picks, or none. When no live item meets these rules, the
daily page says so and then lists the curated baseline under its own heading. The radar
block in each README does the same: directly under the day's heading and window line it
quotes the same "no items" sentence, then a bold baseline label, then the table of
curated entries. Before this, the README put the baseline under today's heading with
ranks and reasons, as if it were the day's selection. Days with picks render unchanged.

## 4. Quantitative claim extraction

`distill.extract_numbers` matches percentages, multipliers, `ms` / `Hz` / `fps`,
parameter counts, hours / episodes / demonstrations / trajectories / tasks, and
`GB / TB / W / kg / DoF`. Up to four are shown per item, in source order, unmodified.
Numbers are **quoted, not recomputed** — if the source is wrong, the radar is wrong in
the same way, and the link is there to check.

A bare `48.1% · 68.9%` does not say what was measured, so each figure on a page is
followed by the few words around it in the source (`distill.number_context`): up to four
before and three after, cut at the clause. The metric is not named by guessing from
nearby words; in "raises success rate and tracking accuracy by at least 24% and 47%" the
nearest metric to 24% is the wrong one, and a quote lets the reader see that. A figure is
quoted where it stands alone, so `17.6%` is not quoted from inside `12.1-17.6%` when the
text also gives it on its own.

Feed furniture is removed before anything is quoted: the WordPress footer "The post …
appeared first on …" and the fixed Video Friday preamble.

Published figures can be reviewed against their sources (roadmap RA-01) with
`python3 -m pairadar.claims [--store PATH] [--days N] [--limit N]`. The tool is offline and
read-only. It reads `radar/feed.json` and prints a Markdown worksheet with one row per figure
published in the `--days` (1–30, default 7) before the newest entry date. It never uses the
wall clock. Each row gives the day, evidence tag, source link, a status, a quoted context and an
empty review column. The header and a whole-window status line count every figure, even when
`--limit` (default 30, no upper bound) caps the table. A truncation line then says how many
unlocated and no-excerpt figures are hidden and gives the `--limit` value that lists them all.
The statuses come from `number_context` over what the store kept:

- **quoted**: the figure is in the stored excerpt, and the context is the quoted clause;
- **title**: the figure is only in the title;
- **no-excerpt**: the entry has no excerpt, as with baseline entries;
- **unlocated**: the figure is in neither. The store keeps only a one- or two-sentence
  excerpt, while figures are extracted from the full summary. Unlocated therefore means
  "check the original source first", not "unsupported".

No status verifies a date, unit, population or result. Items that are not objects, items
without a `_radar` object, and entries whose date is not a canonical `YYYY-MM-DD` string are
skipped and counted in the header. Non-string text fields are shown as empty. An unreadable
store, invalid JSON, the wrong JSON Feed version, a non-list `items` or no dated entries exits
2 with one `claims:` line.

## 5. Translation policy

This is the part most repositories get wrong, so it is explicit here:

1. **Titles are never translated.** They stay citable and searchable.
2. **The English abstract excerpt is an excerpt**, rendered as a block quote, shown only
   for live (non-curated) items.
3. **The analytical layer is authored, not translated.** Lane names, evidence labels,
   signal badges and the "why it matters" line come from `data/glossary.json`
   (`ui`, `why_parts`) and from hand-written trilingual fields in
   `data/baseline.json`.

   Without a curated or drafted line, the line is composed from what the item's text
   shows (`render.composed_why`): what its evidence tag means, which of the checks
   expected for that tag it gives or leaves out (real-robot results, closed-loop
   results, figures for a preprint; figures for a release or a report), and what to watch
   in its lane. Hardware and safety items are not asked for a closed loop. A single
   sentence per lane used to be the whole line: the week of 2026-09-19 published 48 lines
   of which 8 were distinct, and a README opened with two identical ones.
4. **No machine translation is invoked anywhere in the pipeline.** Titles, numbers and
   evidence tags are never produced or altered by a model.
5. **Drafted analysis is opt-in, per item, and labelled.** The "why it matters" line may
   come from a draft in `data/notes/<date>.json` instead of the composed line. Such a
   file is written outside the pipeline by `scripts/draft_daily_notes.sh`, which runs on a
   maintainer's machine on a schedule and never in CI. The pull request is merged
   automatically once every deterministic check passes and a reviewer agent approves it;
   no human reads it first, and the page says so. Precedence is fixed: a human-curated
   line beats a draft, a draft beats the composed line. Every drafted line carries a visible
   label next to its heading, the page names the agent and model that drafted it, and each
   language is drafted separately rather than translated. A missing language falls back to
   the composed line, and a malformed or foreign notes file is ignored with a log line: notes
   can never break a run or silently replace authored text.

## 6. Charts

Charts are emitted as hand-written SVG (`pairadar/charts.py`): lane distribution,
daily cadence and evidence mix. No plotting dependency, no binary diffs, readable in a
pull request. One set is written per language, plus an unsuffixed English set because
already-published pages link to those filenames.

The lane and evidence-mix charts count the picks published in the last seven days
(`cli.CHART_DAYS`), read from the feed store, and their titles say so. They used to add
the 29 curated baseline entries to the day's eight picks, so the bars mostly showed the
baseline. Seven days, not thirty, because the store keeps at most 100 entries and eight
picks a day would overflow a thirty-day count. The charts are embedded only in the
READMEs, which always show the current day. A daily page is frozen when it is published,
so it states its lane counts, source mix and recent cadence as text instead of linking a
chart file that the next run overwrites.

Bundling a font was never necessary:
these are SVG *text*, so the glyphs come from the reader's browser and the bytes this
repository commits are identical either way.

Each lane may declare a short `chart_label` per language in `data/taxonomy.json`. The
full English names run 43–53 characters and, drawn at 12px from `x=20`, overran the bars
that start at `x=250`. Without a font library the renderer estimates advance width per
character — East Asian wide and fullwidth forms at one em, everything else at a
deliberately generous 6.8px — and trims anything still over budget with an ellipsis.

The estimate is per character because a single average is wrong for CJK by a factor of
two. Chinese lane names fit the gutter unshortened; Japanese ones did not, which was
visible only after rendering the charts and looking at them: five of eight labels came
back cut. Short Japanese forms fixed that, and a test now asserts that no lane label in
any language needs trimming at all — a cut label is a content problem to fix in the
taxonomy, not a rendering detail to tolerate.

## 7. The site

The Pages site opens on a landing page per language (`index.html` in Chinese, `en/`,
`ja/`), written by `pairadar/site.py` from the same context as the day's pages, so the
two cannot disagree. It shows the day's picks as cards (lane, evidence tag, the lead
figure with its quote, the "why it matters" line) and the eight lanes as a radar: the
filled shape is the last seven days of picks, and each of today's picks is a dot on its
lane's spoke that links to its card.

The landing pages are plain HTML without front matter, so Jekyll copies them unchanged
and no fetched text reaches Liquid; every fetched string is escaped. The daily, weekly
and archive pages are still Markdown, rendered with `_layouts/default.html`, which takes
each page's language from its file name so the page says which language it is in.

A shared link is previewed from its Open Graph tags, and previewers read neither scripts
nor SVG, so each language has a 1200×630 PNG card, `assets/og.<lang>.png`. It is drawn
from the site stylesheet by `python3 -m pairadar.site --og`, which needs a local Chrome,
and committed; the daily run never starts a browser. A share button links the day's
frozen page rather than the landing page, which changes every morning. CI builds the site
with the same builder as Pages and checks each page's language, stylesheet and card.

## 8. Reproducibility

- Python 3.10+, standard library only.
- `python3 -m pairadar --offline` reproduces a full render from repository data alone.
  It writes in place -- the radar block in all three READMEs, `radar/` and `assets/` --
  because that is what the daily job does.
- `python3 -m pairadar --offline --out DIR` writes the same output under `DIR` instead,
  leaving the checkout untouched, for readers who want to inspect before committing to a
  rewrite. The run log is still read from the repository so the repeat window applies.
- `python3 -m pairadar --date 2026-09-19` pins the run date.
- The window line on each page states both horizons: papers from `date − lookback_days`
  and feed posts from `date − max_age_days`, plus the repeat window. An earlier version
  printed only the paper horizon, which suggested a month-old blog post was new.
- `radar/history.json` keeps the cadence series plus the URLs published inside the repeat
  window; older entries keep their counts and drop their URL list, so the log does not grow
  without bound. `radar/latest.json` is the machine-readable snapshot for downstream
  consumers.
- `python3 -m pairadar --rerender` rebuilds the most recent published day from
  `radar/latest.json` -- same picks, same counts, no network. A day is rendered once at
  publish time, so this is how anything that arrives later (drafted notes, a wording
  fix) reaches that day's pages without re-running selection against a moved pool. The
  snapshot therefore stores the source excerpt each page quotes; `--rerender` reads the
  snapshot and never rewrites it.
- `radar/feed.json` (JSON Feed 1.1), `radar/feed.xml`, `radar/feed.zh.xml` and
  `radar/feed.ja.xml` (Atom, RFC 4287) carry the picks of the last thirty days, at most
  100 entries. `feed.json` is also the store: each item keeps its language-neutral record
  under a `_radar` extension, and a run replaces its own day's entries, so re-running a
  day never duplicates it. A link already delivered on an earlier day is not delivered
  again. Entry ids are tag URIs built from the item id, so they are stable across runs.
  The feeds carry the title, lane, evidence tag, numbers, signals and source excerpt, and
  link to the day's page for the "why it matters" line. That line stays off the feeds so
  a drafted line can never appear without its label, and so `--rerender` never has to
  rewrite them.
- `radar/weekly/<ISO week>.<lang>.md` groups the week's picks by lane. Each run rewrites
  the current week from the feed store; earlier weeks are left as published.
- `python3 -m pairadar.feeds --backfill` rebuilds the store from the `radar/latest.json`
  of each day in the git history, then rewrites the feeds and every week present. The
  store began on 2026-09-25, so the W39 page counted 8 picks where 32 had been published.
  Days already in the store are kept as they are; a day's last commit wins; days replay
  oldest first, so a link repeated on a later day stays on its first. It needs a full
  clone: a shallow checkout has no history to read and backfills nothing.
- `data/notes/<date>.json` is an optional input. Without it the render is unchanged, so
  a reproduction from repository data alone stays deterministic.
- Item ids are content-derived (`arxiv:<id>`, `<feed>:<sha1(link)[:12]>`) and therefore stable
  across processes and days.
- The daily workflow commits only when the working tree actually changed.

## 9. Known limits

- Extractive summaries can miss the real contribution of a paper; the lane and signals are
  a triage aid, not a review.
- Keyword classification will misfile interdisciplinary work; corrections are welcome as PRs
  against `data/taxonomy.json`. To see whether a taxonomy edit helps, score the classifier
  against a file of hand-labeled items before and after the edit:

  ```
  python3 -m pairadar.lanes --labels my-labels.json --margin 0.35
  ```

  The file is UTF-8 JSON, `{"items": [{"id": "a1", "title": "...", "summary": "...", "lane": "data"}]}`.
  `title` and `lane` are required non-empty strings, and `lane` must be a lane id from
  `data/taxonomy.json`. `id` and `summary` are optional, but when present they must be strings.
  Each entry is scored on `title. summary`, the same text the pipeline classifies, and the file
  is only read. The report gives the file's basename (`labels`), `labeled`, `margin`,
  `agreement` and `disagreements`. `agreement` has `n`, `agree` and `rate` for three groups:
  `all` entries; `confident` entries, which carry no thin or ambiguous flag; and `flagged`
  entries, which do. If the flags work, `flagged` agreement should be lower than `confident`.
  `rate` is `null` when a group is empty. `disagreements` lists the misfiles in input order,
  with the label, the predicted lane, the flag reasons and the top two lane scores. An entry
  with no id shows `id: null`. A misfile with `reasons: []` is one the flags missed, and
  those are the cases a keyword edit most needs to address. The command exits 0 whenever it
  prints a report. It exits 2 with a single `lanes: ` line on stderr, and prints nothing on
  stdout, when the file is unreadable, is not UTF-8 JSON, has no `items` list, contains a
  malformed entry (the message names the entry's number) or uses an unknown lane (the message
  lists the valid ones). It also exits 2 when `--labels` is combined with `--latest` or
  `--fail-on-suspicious`. A small hand-labeled set shows agreement on those items only; it is
  not general accuracy or calibration. Keep it separate from the examples used to tune keywords,
  otherwise the number only confirms the edit that was fitted to it.
- `[M]` items frequently disagree on shipment and market numbers. When two credible outlets
  conflict, the curated baseline says so instead of picking a winner.
- Nothing here is a safety argument. Standards entries report status and dates only.
