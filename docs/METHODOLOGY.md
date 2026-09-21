# Methodology · 方法论 · 方法論

This document is the contract between the repository and its readers. If the pipeline
changes, this file changes in the same pull request.

## 1. Sources

| Kind | Endpoint | Evidence tag | Why it is trusted at this level |
| --- | --- | --- | --- |
| arXiv API | `https://export.arxiv.org/api/query`, six `cs.RO` queries | `R` | primary author text, but self-reported and often not peer reviewed |
| Official vendor blogs | AWS Physical AI, NVIDIA Technical Blog / NVIDIA Blog, Google DeepMind, Hugging Face | `O` | first-party statements about their own products |
| Trade press | IEEE Spectrum Robotics | `M` | professional reporting, still secondary |
| Standards watch | ISO, EUR-Lex, Council of the EU | `O` | authoritative status and dates |

Rules:

- only declared machine-readable endpoints (Atom / RSS / documented API);
- no HTML scraping of article bodies, no paywall circumvention;
- every request is timeout-bounded and retried once before the source is given up on;
- arXiv calls are spaced about three seconds apart, as its API guidance asks;
- a failing source is skipped with a log line and never fails the run — but if **no**
  source answers, the run exits non-zero and nothing is committed, because a radar
  rebuilt from the curated baseline alone must not look like a quiet news day;
- `radar/latest.json` carries a `fetch` block (items, sources attempted / answered,
  per-source counts, names of sources that did not answer) so partial degradation is
  visible without reading the workflow log;
- every run also records that day's unanswered sources in `radar/history.json`, and
  `python3 -m pairadar.health` turns the last fourteen days into a verdict: a source
  that missed three or more days is reported as struggling. The daily run prints the
  verdict and stays green; `--fail-on-struggling` exits 1 for automation that should
  only act when there is something to act on;
- adding a source requires a weight and an evidence tag in `data/sources.json`.

## 2. Classification

`data/taxonomy.json` defines eight lanes. Each lane has a keyword list and a weight.
An item is matched case-insensitively against `title + summary`; the lane with the
highest `hits × lane_weight` wins. Ties resolve to the first lane in file order, which
makes classification deterministic.

A curated entry in `data/baseline.json` that declares a `lane` keeps it: the lane is an
editorial decision, and the classifier is not allowed to overrule it. Such an entry is
scored on its own lane's keywords rather than the best-matching lane's.

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
so a single hot topic cannot take over the page. Two further gates apply:

- **at least one lane keyword.** Evidence and same-day recency alone clear `min_score`, so
  without this an off-topic hit from the broad arXiv queries would be published under the
  fallback lane, labelled as if it belonged there. Curated entries are exempt, since a
  person chose their lane.
- **not published in the last `filters.repeat_days` days** (default 7). Each run records the
  normalised URLs it published in `radar/history.json`; the arXiv window looks back three
  days and feeds keep entries for thirty, so without this a strong item would headline
  again the next day. Matching is on URL, not id, so it survives a feed reassigning ids. Every term is visible and tunable; no
learned model sits in this path. This is a bias-by-design choice: the radar prefers
items with real-robot or closed-loop evidence over pure benchmark deltas.

## 4. Quantitative claim extraction

`distill.extract_numbers` matches percentages, multipliers, `ms` / `Hz` / `fps`,
parameter counts, hours / episodes / demonstrations / trajectories / tasks, and
`GB / TB / W / kg / DoF`. Up to four are shown per item, in source order, unmodified.
Numbers are **quoted, not recomputed** — if the source is wrong, the radar is wrong in
the same way, and the link is there to check.

## 5. Translation policy

This is the part most repositories get wrong, so it is explicit here:

1. **Titles are never translated.** They stay citable and searchable.
2. **The English abstract excerpt is an excerpt**, rendered as a block quote, shown only
   for live (non-curated) items.
3. **The analytical layer is authored, not translated.** Lane names, evidence labels,
   signal badges and the "why it matters" line come from `data/glossary.json`
   (`ui`, `why_templates`) and from hand-written trilingual fields in
   `data/baseline.json`.
4. **No machine translation is invoked anywhere in the pipeline.** Titles, numbers and
   evidence tags are never produced or altered by a model.
5. **Drafted analysis is opt-in, per item, and labelled.** The "why it matters" line may
   come from a draft in `data/notes/<date>.json` instead of the per-lane template. Such a
   file is written outside the pipeline by `scripts/draft_daily_notes.sh`, which runs on a
   maintainer's machine on a schedule and never in CI. The pull request is merged
   automatically once every deterministic check passes and a reviewer agent approves it;
   no human reads it first, and the page says so. Precedence is fixed: a human-curated
   line beats a draft, a draft beats the template. Every drafted line carries a visible
   label next to its heading, the page names the agent and model that drafted it, and each
   language is drafted separately rather than translated. A missing language falls back to
   the template, and a malformed or foreign notes file is ignored with a log line: notes
   can never break a run or silently replace authored text.

## 6. Charts

Charts are emitted as hand-written SVG (`pairadar/charts.py`): lane distribution,
daily cadence and evidence mix. No plotting dependency, no binary diffs, readable in a
pull request. One set is written per language, plus an unsuffixed English set because
already-published pages link to those filenames. Bundling a font was never necessary:
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

## 7. Reproducibility

- Python 3.10+, standard library only.
- `python3 -m pairadar --offline` reproduces a full render from repository data alone.
  It writes in place -- the radar block in all three READMEs, `radar/` and `assets/` --
  because that is what the daily job does.
- `python3 -m pairadar --offline --out DIR` writes the same output under `DIR` instead,
  leaving the checkout untouched, for readers who want to inspect before committing to a
  rewrite. The run log is still read from the repository so the repeat window applies.
- `python3 -m pairadar --date 2026-09-19` pins the run date.
- `radar/history.json` keeps the cadence series plus the URLs published inside the repeat
  window; older entries keep their counts and drop their URL list, so the log does not grow
  without bound. `radar/latest.json` is the machine-readable snapshot for downstream
  consumers.
- `python3 -m pairadar --rerender` rebuilds the most recent published day from
  `radar/latest.json` -- same picks, same counts, no network. A day is rendered once at
  publish time, so this is how anything that arrives later (drafted notes, a template
  fix) reaches that day's pages without re-running selection against a moved pool. The
  snapshot therefore stores the source excerpt each page quotes; `--rerender` reads the
  snapshot and never rewrites it.
- `data/notes/<date>.json` is an optional input. Without it the render is unchanged, so
  a reproduction from repository data alone stays deterministic.
- Item ids are content-derived (`arxiv:<id>`, `<feed>:<sha1(link)[:12]>`) and therefore stable
  across processes and days.
- The daily workflow commits only when the working tree actually changed.

## 8. Known limits

- Extractive summaries can miss the real contribution of a paper; the lane and signals are
  a triage aid, not a review.
- Keyword classification will misfile interdisciplinary work; corrections are welcome as PRs
  against `data/taxonomy.json`.
- `[M]` items frequently disagree on shipment and market numbers. When two credible outlets
  conflict, the curated baseline says so instead of picking a winner.
- Nothing here is a safety argument. Standards entries report status and dates only.
