# Automation: what runs where

Three things update this repository. Two of them run on GitHub, one runs on a
maintainer's machine, and none of them lets an outside contributor execute code
anywhere that matters.

| What | Where | Trigger | Writes |
| --- | --- | --- | --- |
| `ci.yml` | GitHub-hosted runner | push, pull request | nothing |
| `daily.yml` | GitHub-hosted runner | 03:20 UTC, and by hand | publishes only a day the machine missed |
| `scripts/publish_daily.sh` | maintainer's machine, cron | 09:40 Asia/Singapore | commits the day's radar to `main` |
| `scripts/draft_daily_notes.sh` | maintainer's machine, cron | 02:30 Asia/Singapore | opens a pull request |
| `scripts/repair_sources.sh` | maintainer's machine, cron | 03:15 Asia/Singapore | opens a pull request |

## There is no self-hosted runner, on purpose

A self-hosted runner was registered briefly and has been removed. The reason is not
that an agent might exceed its permissions — the tool policy handles that, and the
agents here have no shell — but that **this repository is public**, and a workflow
triggered by a pull request runs code from that pull request's branch. Pointing such a
workflow at a self-hosted runner hands the machine to anyone who can open one. Fork
pull requests now also require approval from a maintainer for all external
contributors, but that is a mitigation, not a boundary.

`tests/test_workflows.py` keeps the door shut: any workflow file that mentions
`self-hosted` must not declare a trigger an outside contributor can cause, and no
workflow or script may pass `--trust-all-tools`. Both tests pass trivially today,
which is the point — they exist so that re-adding a runner lane has to be a decision
rather than an accident.

## Why the agent work runs on a machine instead

Outbound instead of inbound. The machine pulls the published branch, does its work,
and opens a pull request. Nothing on GitHub can trigger execution, so there is no
fork-pull-request path, no runner to keep patched, and no approval button standing
between a stranger and someone's computer.

Both scripts share the same shape:

1. a dedicated clone, reset hard to the published branch, cleaned — never a working
   checkout, and never blocked by leftovers from an interrupted run;
2. a gate before spending anything: notes are skipped if the day already has them,
   repairs are skipped unless `pairadar.health` reports a source over the threshold;
3. the agent, checked with `kiro-cli agent validate` before the first call, then run
   with granular trust only (`--trust-tools=…`), a write path allowlist, no shell,
   and an explicit `--model`;
4. verification by this repository's own deterministic checks — the touched file set,
   `pairadar.notes check`, the full test suite;
5. a reviewer agent -- read-only, no shell, no writes -- which must end its reply with
   `APPROVE` or `REJECT: <reason>`;
6. a pull request that merges itself once every check is green.

Nobody reads the notes before they are published. The rendered pages say exactly that:
drafted by the agent, merged automatically after deterministic checks and an agent
review, **without human review**. If a check is pending or fails, the pull request is
left open instead of merged, which is the only path by which a human gets involved.

Measured on kiro-cli 2.21.2, `--trust-all-tools` bypasses the agent's own write path
allowlist while `--trust-tools=write` enforces it, and a `shell` command allowlist is
not enforced at all. That is why neither agent is given a shell and why the scripts,
not the agents, run the commands.

## How a draft gets from the model to the pull request

The drafting job treats the model as a colleague whose work is checked, not as a
single shot that either lands or loses the night:

- **A model that answers.** Every call names its model and falls back down a list
  (`RADAR_MODELS`, default `claude-fable-5.1 claude-opus-5 claude-sonnet-5`) when one
  is unavailable. The machine's default model was "temporarily unavailable" on 09-24
  and 09-25, and both nights were lost. A failed attempt's partial file is rolled back
  before the next model starts.
- **Findings, not tracebacks.** `python3 -m pairadar.notes check DAY` reports, item by
  item, what is wrong: JSON that stops parsing and where, a key that matches no pick,
  a missing language, a figure the page does not contain. Those lines go back to the
  drafter as its next instruction, at most `RADAR_FIX_ROUNDS` (2) times, and the
  suite runs only on a draft the validator accepts.
- **A second opinion from a different model.** The reviewer takes the first model on
  `RADAR_REVIEW_MODELS` that wrote no part of the draft. A rejection goes back to the
  drafter once (`RADAR_REVIEW_ROUNDS`), through the validator and the suite again, and
  to a fresh review; a second rejection, or no independent reviewer, discards the draft.
- **The record says who wrote it.** `pairadar.notes stamp` writes the models that
  actually ran into `author.model` and `review`, so the rendered pages, the commit and
  the pull request name them. The agent used to report its own model, which is not
  evidence.

The repair job uses the same model list.

## The machine's jobs share a clone, so they share a lock

cron calls `~/.local/bin/radar-run`, an installed copy of `scripts/radar-run`
(`install -m 755 scripts/radar-run ~/.local/bin/radar-run` after it changes). It takes
`~/.local/state/pairadar/radar.lock` before resetting the clone and holds it for the
whole job: every job resets hard, and a draft still waiting for its checks at 03:15 had
its branch reset under it by the repair job. A job waits up to two hours for the lock
(`RADAR_LOCK_WAIT`), runs for at most three (`RADAR_TIMEOUT`), and each model call gets
twenty minutes (`RADAR_AGENT_TIMEOUT`). A script started by hand takes the same lock
and refuses to start while a job holds it. Every failure ends with a `FAILED` line in
the job's log under `~/.local/state/pairadar/`.

Re-runs are no-ops: the publish stops when `main` already carries the day (`--force`
republishes it), the draft stops when the day's pull request is already open, and the
repair stops when a repair for that source is already waiting for review.

## When the machine misses a day

`daily.yml` wakes at 03:20 UTC, an hour and forty minutes after the publish. When
`radar/latest.json` on `main` already names the day, it stops there. Otherwise it
publishes the day with the per-run `GITHUB_TOKEN`, asks Pages for a build, and opens an
issue titled "The local publisher missed a day" (or comments on the open one). If
publishing fails there as well, the scheduled run fails, which GitHub mails to the
maintainer. Run it by hand with `force` to republish a day that is already on `main`.

## Timing

The notes are drafted for the current UTC date, and that date's radar publishes at
01:40 UTC. At 02:30 Asia/Singapore — 18:30 UTC — the day's radar is about seventeen hours
old, so the order is right with plenty of margin. Moving the cron job near 09:40
Asia/Singapore (01:40 UTC) would race the publish for the same day's output.

## Credentials

The model key lives in the login environment of the maintainer's account, or in
`~/.config/pairadar/env` at mode 600 for cron, which is why the cron entries use a
login shell. Nothing writes the key anywhere, and no GitHub secret is needed: with no
self-hosted runner, no workflow calls a model.
