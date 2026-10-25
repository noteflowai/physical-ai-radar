# Editorial style for daily deep reads

Status: proposal with a side-by-side pilot (2026-10-25 → 10-27). The live daily page is
unchanged until the pilot's blind review favours the new version and an implementation
PR replaces the drafting step. The checks are `pairadar/editorial.py`; the phrase lists
and thresholds are `data/editorial-style.json`.

## Why

The radar's notes were drafted from a one-sentence excerpt of an abstract. With no
methods, tables or limitations to work from, the writer filled every item with the same
caution ("the visible excerpt only says…", "the page gives no figures", "read the paper
first"). That is accurate and useless. A specialist reads the paper, picks the one number
that matters, says what it is compared against and where it is in the paper, and then
says whether they believe it.

## Shape of a day

- **Three deep reads**, chosen from the day's picks: `[R]` papers in different lanes
  whose full text is available as arXiv HTML (`pairadar/fulltext.py`), highest score
  first. Each is written from the full text (sections and tables), the project page and
  the code repository when they exist. The abstract alone is never enough.
- **Five index lines** for the rest: one sentence each, at most 40 English words (80
  Chinese/Japanese characters), saying what the item is and the one fact that decides
  whether to open it.

Each language is written on its own, not translated (METHODOLOGY §5).

## A deep read

Four labelled parts, in this order. The labels are fixed per language so the checker can
find them:

| en | zh | ja |
|---|---|---|
| **Bottom line** | **结论** | **結論** |
| **What is new** | **新在哪里** | **新しい点** |
| **Limits** | **边界** | **限界** |
| **Our take** | **我们的判断** | **私たちの判断** |

1. **Bottom line.** The first sentence is the result: who, on what task, against which
   baseline, by how much. "On six real-robot tasks, method X lifts success from 37% to
   88% over the same policy without memory (Table 4)." Not background, not the problem
   statement.
2. **What is new.** One or two sentences on the mechanism that is actually new. Skip
   what every paper in the area does.
3. **Limits.** Where the result holds and where it has not been tested: simulation or
   real robot, how many tasks/trials/embodiments, whether the evaluation flatters the
   method, whether code, data or weights are released.
4. **Our take.** A judgement with a reason. It may disagree with the authors, prefer an
   ablation over the headline number, or say the result is not yet decision-relevant.

### Numbers

Every number carries its unit, its baseline and its location in the paper: `51% vs 73%
(Table 3)`, `20% lower error than baseline Y (Table 1)`, `30 trials per task (Sec.
5.5)`. Points and percent are not the same: an absolute gain is "12 points", a relative
one "20% lower". Do not add a number the paper does not contain; do not round a number
into a different claim. (Examples in this document are illustrative, not quotes.)

### Wording

- No stock phrases. The lists per language are in `data/editorial-style.json`
  (`banned`); they include "it is worth noting", "game-changer", "delve", "in
  conclusion", "值得注意的是", "赋能", "革命性", "注目すべきは", "画期的な", and the old
  excerpt-caution template.
- Vary sentence length. A short sentence after a long one is how people write.
- No emoji, no exclamation marks, no empty adjectives ("impressive", "powerful",
  "强大的", "優れた") without the number that would justify them.
- Self-reported results are worded as such once, where it matters, not as a refrain on
  every item.
- Name the baseline the authors chose and say if it is weak.

## Deterministic checks (no model)

`python3 -m pairadar.editorial <draft.md> --lang <zh|en|ja>` reports per item and per
day:

| Check | Rule |
|---|---|
| Parts | all four labelled parts present and non-empty |
| Stock phrases | zero hits from the language's list |
| Evidence triple | at least one sentence with a number + unit, a baseline marker ("vs", "compared with", "from … to", "相比", "从", "比べ", …) and a locator (Table/Fig./Sec./p., 表/图/第…页, 表/図) |
| Concrete opening | the Bottom line's first sentence has a number and a comparison |
| Limits | at least 80 English / 30 Chinese / 40 Japanese characters |
| Rhythm | coefficient of variation of sentence length ≥ 0.25 (items with three or more sentences) |
| Template rate | at most 15% of the day's sentences may reuse the wording of another item (4-word / 6-character shingles, digits folded) |
| Counts and form | 3 deep reads, 5 index lines, index lines within length, no emoji |

A failing draft returns to its writer with the findings, at most twice. An item that
still fails is demoted to an index line; a day never ships a failing deep read.

## Independent review

The reviewer's rubric gains one item: **would a specialist in this area have written
it?** The answer must name the sentence and the reason ("the Bottom line restates the
abstract's motivation", "the ablation number is quoted without the baseline it
belongs to"). A bare score is not an answer.

## Figures (house style, owner 06)

One data chart per deep read, drawn from numbers in the paper's tables (method vs its
main baseline, bar or line, axes and units labelled), and when useful one architecture
sketch redrawn in our style rather than copied from the paper. No AI-generated
illustrations. Inter / Noto Sans SC / Noto Sans JP, a fixed palette of at most six
colours, fixed grid and margins, 2x resolution, "Source: <paper>, Table N" in the footer
and alt text. The numbers for each chart come from the same table rows the deep read
cites (`fulltext.Document.tables`).

Contract (06 r1, agreed with 03): this repository only produces and validates the
chart's data (`figure.json`: paper, table label, metric and unit, direction, rows as
printed, highlighted method and main baseline); the three-language images are rendered
in the WordPress content pipeline, which already carries the fonts and imaging library,
so this repository stays standard-library only. Under each chart the page shows 06's
`{lang}.data.html`: the same rows as a visible table with a clickable source line. The
alt text is one sentence stating the chart's conclusion, at most 140 characters, and it
must agree with the deep read's Bottom line. Media items get no data chart.

## Pilot

`scripts/editorial_pilot.py` builds the evidence packet for a day (picks, full text,
writer brief), checks a new draft, measures the old page with the same checks where
they apply (stock phrases, template rate), and prepares a blind A/B pair for review by
03 and 05. Pilot output lives in a private scratch directory, never in this repository
or on the live site.
