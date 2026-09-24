# Contributing · 贡献指南 · 貢献ガイド

Thanks for helping keep this radar accurate. Four kinds of contribution are especially useful.

## 1. Add a source

Edit `data/sources.json`. Requirements:

- the endpoint must be **machine-readable** (Atom, RSS or a documented API) and public;
- declare an `evidence` tag: `O` first-party official, `R` paper/preprint, `M` media/secondary;
- declare a `weight` and say why in the PR description;
- no scrapers, no paywall workarounds, no endpoints that forbid automated access.
- set `"topical": true` only if every entry the feed publishes is about robots or Physical AI.
  A general company blog stays unmarked; its posts must then name an anchor term
  (`anchors` in `data/taxonomy.json`) to be considered at all.

## 2. Fix a classification or an evidence tag

Lane keywords live in `data/taxonomy.json`. If an item was filed in the wrong lane, the fix is
usually a keyword, not a special case in code. Keywords match whole terms (with an optional plural),
so a short keyword such as `ppo` is safe. If an off-topic post got in at all, check `anchors` too. Evidence tags are not negotiable by preference:
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

## Reviewing a drafted-notes pull request

`notes/<date>` branches carry one file: `data/notes/<date>.json`, per-item analysis
drafted by an agent on a maintainer's machine. The script already checked the schema,
that all three languages are present per item, and that nothing else changed. What a
reviewer has to judge is the content:

- does each line say what decision changes, rather than restate the title?
- is every number in it actually in the source, and is a self-reported result worded as
  self-reported?
- is each language written as a practitioner in that language would phrase it, not as a
  translation of the English?

Reject or rewrite freely — a human-curated line in `data/baseline.json` always outranks
a draft, and deleting the file simply restores the per-lane templates.

## Workflows and agent automation

This repository is public, so **no workflow may run on a self-hosted runner**: a
`pull_request` trigger on such a runner would execute a contributor's branch on
someone's computer. There is no runner today, and `tests/test_workflows.py` keeps it
that way — it also forbids `--trust-all-tools` anywhere, because that flag bypasses an
agent's write path limits.

The agent work runs on a maintainer's machine on a schedule and opens pull requests.
[docs/automation.md](docs/automation.md) explains what runs where and why.

## Reporting a mistake

Open an issue with the source link, what the radar said and what the source actually says.
Corrections about numbers and evidence tags are treated as bugs, not opinions.
