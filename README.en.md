# Physical AI Radar

[![last radar](https://img.shields.io/github/last-commit/noteflowai/physical-ai-radar/main?path=radar%2Flatest.json&label=last%20radar)](radar/INDEX.md)
[![site](https://img.shields.io/badge/site-live-34d399.svg)](https://noteflowai.github.io/physical-ai-radar/en/)
[![feed: Atom · JSON](https://img.shields.io/badge/feed-Atom%20%C2%B7%20JSON%20Feed-f26522.svg)](https://noteflowai.github.io/physical-ai-radar/radar/feed.xml)
[![CI](https://github.com/noteflowai/physical-ai-radar/actions/workflows/ci.yml/badge.svg)](https://github.com/noteflowai/physical-ai-radar/actions/workflows/ci.yml)
[![code: MIT](https://img.shields.io/badge/code-MIT-blue.svg)](LICENSE)
[![content: CC BY 4.0](https://img.shields.io/badge/content-CC%20BY%204.0-lightgrey.svg)](LICENSE-CONTENT)
[![published daily 01:40 UTC](https://img.shields.io/badge/published-daily%2001%3A40%20UTC-7300e5.svg)](docs/automation.md)

**Language: [中文](README.md) · English · [日本語](README.ja.md)**

A daily, automatically updated radar on the Physical AI frontier. It does exactly three things: **sort work into eight lanes**, **tag every claim with an evidence level**, and **state why it matters in Chinese, English and Japanese**.

How this differs from a paper list or a news aggregator:

- **Traceable evidence, no mixed units**: `[O]` first-party official, `[R]` paper/preprint, `[M]` media/secondary. Vendor self-reports and peer-reviewed results are never blended.
- **Only checkable numbers**: success rates, latency, Hz, parameter counts and data hours are extracted automatically. An item with no numbers does not get sold as a breakthrough.
- **Forward-looking without hand-waving**: the radar covers self-improvement loops, test-time compute, world-model evaluation, runtime safety, compliance timing and reliability economics — each with the original link attached.
- **Zero dependencies, reproducible**: the whole pipeline is Python standard library only, charts are hand-written SVG, and anyone can reproduce the same output locally.

**Subscribe:** [Atom](https://noteflowai.github.io/physical-ai-radar/radar/feed.xml) · [JSON Feed](https://noteflowai.github.io/physical-ai-radar/radar/feed.json) · [weekly roundups](radar/INDEX.md). Paste a feed URL into any reader; each entry carries its lane, evidence tag and key numbers.

<!-- RADAR:START -->
### Today's radar · 2026-09-26

`Generated: 2026-09-26 01:40 UTC` ｜ `Window: papers 2026-09-22 → 2026-09-26 · posts 2026-08-27 → 2026-09-26 · no repeats within 30 days (UTC)`

- `[R]` **[RoboRecover: Benchmarking Robot Policy Recovery under Execution Deviations](https://arxiv.org/abs/2609.28952)** — Simulation & evaluation (sim2real / closed-loop)
  - Preprint, not yet peer-reviewed; the text gives no real-robot results or figures. Watch how well the simulated result predicts real-robot performance.
- `[R]` **[Albireo: Adaptive, Energy-Efficient Inference Framework for Video Object Detection on the Edge](https://arxiv.org/abs/2609.29648)** — Edge & real-time (on-device inference / control rate) ｜ `17.6%` · `14.4%`
  - Preprint, not yet peer-reviewed; the text gives no real-robot results or closed-loop results. Watch whether the speed-up holds on the target hardware at control rate.
- `[R]` **[Rolling-WAM: World Action Models with Rolling Imagination](https://arxiv.org/abs/2609.30247)** — Simulation & evaluation (sim2real / closed-loop) ｜ `4.5x`
  - Preprint, not yet peer-reviewed; the text gives real-robot results, closed-loop results and figures. Watch how well the simulated result predicts real-robot performance.
- `[R]` **[Continuous Online Fault Detection for Mobile Robots via Adaptive Edge Models](https://arxiv.org/abs/2609.29194)** — Edge & real-time (on-device inference / control rate) ｜ `4.30 ms`
  - Preprint, not yet peer-reviewed; the text gives no closed-loop results. Watch whether the speed-up holds on the target hardware at control rate.
- `[M]` **[Video Friday: Life’s Better With a Little Robot Goose](https://spectrum.ieee.org/video-friday-goose-household-robots)** — Embodiment & supply chain (hardware / cost)
  - Secondary report, so check the primary source before citing; the text gives no figures. Watch price, availability and who is shipping it in volume.

[Today's radar ›](radar/daily/2026-09-26.en.md) · [Weekly roundup ›](radar/weekly/2026-W39.en.md) · [Archive ›](radar/INDEX.md)

![lane distribution](assets/lane-distribution.en.svg)

![cadence](assets/cadence.en.svg)
<!-- RADAR:END -->

## The eight lanes

| Lane | What it tracks | Why it deserves its own lane |
| --- | --- | --- |
| Foundation models (VLA / WAM) | vision-language-action and world-action models, open weights | sets the starting point and cross-embodiment transfer for everything downstream |
| Data engine | teleoperation, egocentric human video, curation, scaling | sets the data cost per new task |
| Training & self-improvement | RL post-training, advantage conditioning, fleet-scale loops | decides whether a policy keeps improving after deployment |
| Simulation & evaluation | sim2real, real-to-sim, closed-loop success, eval platforms | offline metrics are not closed-loop success |
| Edge & real-time | on-device inference, quantization, async inference, control rate | a model that misses the real-time budget cannot enter the control loop |
| Safety, permissions & compliance | safety filters, CBFs, ISO and regulatory dates | during the certification gap, safety rests on architecture, not certificates |
| Systems & orchestration | long-horizon tasks, orchestrators, memory, policy routing | the operated object becomes a set of capability boundaries, not one model |
| Embodiment & supply chain | humanoids, actuators, tactile, BOM and production lines | the real rate limiter on cost curve and delivery cadence |

## How to read it

1. **Check the evidence tag before the claim.** `[R]` numbers are author-reported, `[M]` shipment figures often disagree across outlets, and even `[O]` needs the distinction between "platform available" and "customer deployed".
2. **Prefer numbers over adjectives.** Each entry aims to carry a success rate, latency or data volume. No numbers means the item is still narrative.
3. **Read compliance as dates.** Standards and regulation entries state effective or stage dates so they can be copied straight into a project plan.

## How the automation works

```
scripts/publish_daily.sh (cron on a maintainer's machine, 01:40 UTC / 09:40 CST / 10:40 JST)
  └─ fetch    arXiv API (six cs.RO queries; category RSS when the API refuses) + official and press Atom/RSS (AWS / NVIDIA / DeepMind / HF / TRI / IEEE / The Robot Report / Leiphone / MONOist)
  └─ distill  relevance gate → classify into eight lanes → extract quantitative claims → explainable score → per-lane, per-source and per-evidence caps
  └─ charts   hand-written SVG: lane distribution / daily cadence / evidence mix
  └─ render   trilingual daily page + inject three READMEs + refresh archive index and latest.json
  └─ feeds    radar/feed.json (JSON Feed 1.1) + Atom per language + this week's roundup in radar/weekly/
  └─ site     landing pages index.html · en/ · ja/ (dark theme, lane radar, share cards)
  └─ commit   commit and push to main only when something changed
```

The run is scheduled after the arXiv daily announcement so the first thing you read in the morning is the newest batch.

## Run it locally

```bash
git clone https://github.com/noteflowai/physical-ai-radar.git
cd physical-ai-radar

python3 -m pairadar --offline --out /tmp/radar   # no network, writes elsewhere, repo untouched
python3 -m pairadar --offline                   # no network, rewrites the pages and READMEs in place
python3 -m pairadar                             # fetch today's live items
python3 -m unittest discover -s tests           # tests
```

No pip install, no virtualenv. Python 3.10+ is enough.

Without `--out` a run **rewrites in place**: the radar block in all three READMEs,
`radar/` and `assets/` — which is what the daily job is for. Use `--out` to look
first. Either way the run log is read from the repository, so the 30-day
no-repeat window still applies.

## Layout

```
data/        sources.json (sources and weights) · taxonomy.json (lanes and signals)
             glossary.json (trilingual UI and terms) · baseline.json (curated baseline)
pairadar/    fetch / distill / charts / render / site / cli — standard library only
radar/       daily/YYYY-MM-DD.{zh,en,ja}.md · INDEX.md · latest.json · history.json
assets/      generated SVG charts · site.css · social cards og.*.png
docs/        METHODOLOGY.md (method, translation policy, known limits)
```

## Known limits (stated up front)

- Summaries are **extractive**: the English source excerpt is shown as a quote and never machine-translated. The analytical layer above it is written per language, never translated sentence by sentence — on most days by an agent, whose every line is labelled as drafted and whose figures must already appear on that day's published page; where no draft exists the per-lane template fills in, unlabelled. [How a draft is reviewed and what it may not claim](docs/METHODOLOGY.md).
- Each language gets its own chart set, labelled in that language. These are SVG text, so the glyphs come from the reader's browser and nothing is bundled. Long names carry a curated short form in `data/taxonomy.json`; a label is trimmed only if it still does not fit, and a test asserts none currently is.
- Lane assignment is **keyword matching** (whole terms, never substrings), not classification. An item from a general-purpose blog must also name a Physical AI anchor term (robot, humanoid, VLA, world model, ...), a lane needs at least one keyword hit, and `python3 -m pairadar.lanes` flags a pick decided by a single keyword (`thin`) or close to its runner-up (`ambiguous`). A wrong lane is findable; it is not prevented.

## Deliberate choices (not limits)

These will not be "fixed". Each one is what makes the rest checkable.

- Ranking is an **explainable weighted sum**, not learned relevance. All weights live in `data/`, so a fork can retune them. An agent may propose new weights as a pull request you can read; it does not get to replace the model with one you cannot.
- Sources are limited to public machine-readable endpoints. No article scraping, no paywall bypass. This is enforced rather than promised: the drafting and reviewing agents declare `read`, `grep` and `glob` only — no shell, no network — so they can only reason over what the pipeline already fetched and published, and a test asserts it stays that way.

## Related project

The radar tracks frontier claims. [**Robot Reel**](https://huggingface.co/spaces/glayguo/robot-reel) does the other half: it records some of those claims as evidence you can inspect.

- [SmolVLA Stress Lab](https://noteflowai.github.io/robot-reel/stress/) — 30 real closed-loop rollouts of one task under reference lighting, reduced light and a shifted camera (NVIDIA L40S / CUDA inference), with paired seeds, confidence intervals, applied actions and separate inference timings
- [Butterfly Lab](https://noteflowai.github.io/robot-reel/chaos/) — twelve Newton worlds released 0.05° apart, their trajectories drawn as a 3D time sculpture
- [Paired-outcome dataset](https://huggingface.co/datasets/glayguo/robot-reel-paired-outcomes) — the recorded results, machine-readable

For items in the **Edge & real-time** and **Simulation & evaluation** lanes there is often a matching experiment over there that you can open and step through.

**Disclosure**: both projects are maintained by the same authors. That is why no entry in the curated baseline (`data/baseline.json`) points at our own work — these links live here and nowhere else.

## Contributing

PRs welcome: new machine-readable sources, evidence-tag corrections, baseline additions, better trilingual wording. Start with [CONTRIBUTING.md](CONTRIBUTING.md) and [docs/METHODOLOGY.md](docs/METHODOLOGY.md).

## License

- Code: [MIT](LICENSE)
- Content (curated text and generated pages): [CC BY 4.0](LICENSE-CONTENT)

---

*This repository is technical observation. It is not investment advice, a product commitment, or a safety certification conclusion. Paper and vendor claims are reported as such; follow the original link before citing.*
