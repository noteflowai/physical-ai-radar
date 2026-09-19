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

## Workflows and the self-hosted runner

This repository is public and one lane runs on a self-hosted machine. **Only
`workflow_dispatch` and `schedule` may reach it** — a `pull_request` trigger on that
runner would execute a contributor's branch on someone's computer.
`tests/test_workflows.py` enforces this, and
[docs/self-hosted-runner.md](docs/self-hosted-runner.md) explains the rest of the
posture, including why the curator agent is given no shell.

## Reporting a mistake

Open an issue with the source link, what the radar said and what the source actually says.
Corrections about numbers and evidence tags are treated as bugs, not opinions.
