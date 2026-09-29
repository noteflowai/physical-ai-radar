# Physical AI Radar roadmap

Updated: 2026-09-29 · Owner: NoteFlowAI maintainer · Review: weekly; monthly source-quality review.

This proposed roadmap prioritizes outcomes. Now / Next / Later is conditional sequencing, not a promise to ship one feature each day.

## User and outcome

A practitioner quickly identifies **what changed in Physical AI, why the evidence matters, and which original source deserves attention**.

Radar owns collection, provenance, freshness, selection and clear multilingual presentation. Course production, generic coding-agent orchestration and robot simulation have separate owners.

## Current baseline

Reviewed `fac4451` / v1.3.0. Daily collection, source classification, freshness, deduplication, source-linked claims and ZH/EN/JA presentation exist. Recent source corrections demonstrate why evidence checks matter.

The repository also contains a general multi-repository development/release controller. That is useful operational code but a different product boundary. Extract it incrementally after preserving receipts and release gates.

## Now

| ID | Outcome | Acceptance evidence |
| --- | --- | --- |
| RA-01 | A reader can verify every highlighted numeric claim | Review a sample of 30 recent claims against original sources, including date, unit, population and study limitations. Fix confirmed errors with dated correction records. Reuse extraction/provenance checks; report unsupported claims instead of inferring precision. |
| RA-02 | Daily issues maximize relevance rather than item count | Align public copy and selection rules around a quality threshold and up to the configured maximum. A sparse day may have fewer items with a visible explanation. Verify freshness, topic diversity and 30-day duplicate behavior on saved inputs. |
| RA-03 | Three languages convey the same evidence | Maintain a small terminology/claim corpus; preserve numbers, citations, uncertainty and category labels across ZH/EN/JA. Compare generated pages, URLs and feeds from the same issue snapshot. |

## Next

| ID | Outcome | Entry and exit gates |
| --- | --- | --- |
| RA-04 | Returning readers can find a useful development | Obtain consented feedback from three practitioners; baseline time to locate a relevant source and reasons an item was unhelpful. Add filters/history only when these observations identify a missing journey. |
| RA-05 | Radar can run independently of the portfolio Agent | Move generic scheduling, engine calls and release state to a private controller using a reviewed adapter boundary. Radar retains its CLI/domain tests. Verify identical issue outputs and no duplicate publication during a rollback rehearsal. |

## Later

More sources and trend analytics need demonstrable incremental relevance and manageable maintenance. Avoid unlimited aggregation, speculative rankings and growth measured only by daily item count.

## Measures and decisions

- Track sampled claim accuracy, correction turnaround, original-source coverage, stale/duplicate rate and source availability.
- Baseline returning readership and useful-source discoveries; do not equate page views with research value.
- Treat a correction as quality work. Investigate recurrent claim errors before broadening source coverage.
- Preserve saved source dates and evidence; do not silently rewrite historical claims.

## Delivery policy

Each task references a milestone ID, reader problem, source/reproduction evidence and an observable acceptance condition. Keep one active product milestone. A bug fix, quality correction or justified no-change outcome is valid.

The current feature controller does not yet enforce this roadmap or support a value-based no-change result. Integrating those policies is a separate controller change, with replay tests for task states, reporting and retry behavior. Product documentation must not imply that integration is already deployed.
