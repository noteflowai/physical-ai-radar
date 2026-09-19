# Contributing · 贡献指南 · 貢献ガイド

Thanks for helping keep this radar accurate. Four kinds of contribution are especially useful.

## 1. Add a source

Edit `data/sources.json`. Requirements:

- the endpoint must be **machine-readable** (Atom, RSS or a documented API) and public;
- declare an `evidence` tag: `O` first-party official, `R` paper/preprint, `M` media/secondary;
- declare a `weight` and say why in the PR description;
- no scrapers, no paywall workarounds, no endpoints that forbid automated access.

## 2. Fix a classification or an evidence tag

Lane keywords live in `data/taxonomy.json`. If an item was filed in the wrong lane, the fix is
usually a keyword, not a special case in code. Evidence tags are not negotiable by preference:
a vendor blog is `O` even when the claim is weak, a preprint is `R` even when the result is
strong, and a great article in the trade press is still `M`.

## 3. Extend the curated baseline

`data/baseline.json` is the hand-curated part of the repository. Each entry needs:

- `lane`, `evidence`, `date`, `title`, `publisher`, `url`;
- `numbers`: quoted from the source, never recomputed;
- `why`: one sentence **each** in `zh`, `en` and `ja`, written by a human.

Entries that only restate the title are rejected. The "why" must tell a practitioner what
decision changes because of this item.

## 4. Improve the trilingual wording

`data/glossary.json` holds UI strings, lane names, per-lane "why it matters" templates and the
term glossary. Rules:

- do not machine-translate; if you are not comfortable in a language, leave it and say so in
  the PR — a missing improvement is better than an awkward one;
- keep terminology consistent with the `terms` list;
- titles are never translated.

## Local checks before opening a PR

```bash
python3 -m unittest discover -s tests
python3 -m pairadar --offline --no-readme --date 2026-01-01
```

Both must pass. `--no-readme` keeps the committed READMEs untouched while you test.

## Style

- Standard library only. A dependency needs a strong argument in the PR.
- Every new ranking term must be explainable in one line and documented in
  `docs/METHODOLOGY.md`.
- Prefer deterministic behaviour: same input, same output, same order.

## Reporting a mistake

Open an issue with the source link, what the radar said and what the source actually says.
Corrections about numbers and evidence tags are treated as bugs, not opinions.
