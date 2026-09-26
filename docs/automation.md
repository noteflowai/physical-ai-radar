# Automation: what runs where

Three things update this repository. Two of them run on GitHub, one runs on a
maintainer's machine, and none of them lets an outside contributor execute code
anywhere that matters.

| What | Where | Trigger | Writes |
| --- | --- | --- | --- |
| `ci.yml` | GitHub-hosted runner | push, pull request | nothing |
| `daily.yml` | GitHub-hosted runner | 03:20 UTC, and by hand | publishes only a day the machine missed |
| `scripts/publish_daily.sh` | maintainer's machine, cron | 09:40 Asia/Singapore | commits the day's radar to `main` |
| `scripts/draft_daily_notes.sh` | maintainer's machine, cron | 02:30 Asia/Singapore | validates, reviews, merges and verifies publication |
| `scripts/repair_sources.sh` | maintainer's machine, cron | 03:15 Asia/Singapore | verifies a replacement feed, reviews, merges and verifies publication |
| `scripts/improve_repos.sh` | maintainer's machine, cron | 10:10 Asia/Singapore; 16:10 catch-up | maintains the three companion projects through review, CI and publication |

All local jobs are noninteractive. A completed publication receipt requires a
checked PR head, the merge commit's required deployment workflows, and public
HTTP readback. Failed work is recorded and retried automatically; a failed,
cancelled, missing or wholly skipped check set cannot authorize a merge.
Closed or externally changed proposals are retired rather than retried forever.
An interrupted older receipt can be marked `superseded` only after a newer main
commit is proven to descend from it and its deployment and public content pass.
Radar publication waits for both `CI` and `pages-build-deployment` on the merge
commit before checking the public site.
The deterministic daily publisher verifies the same gates on its direct main
commit, including when a retry finds that today's data is already present.

### Companion-project maintenance

The daily maintenance job consumes this radar's published JSON, Hugging Face
model metadata, GitHub repository search and the three projects' public metadata.
Search results are described as recently pushed repositories ordered by total
stars, not weekly star gains or independent user adoption.

It maintains the homepages and bilingual READMEs of Skills Anywhere, EvalArc and
Robot Reel. It uses dedicated marked clones under
`~/.local/share/ai-repo-agent/`; it never resets a user's working checkout or
archived worktree. The author and reviewer receive bounded text snapshots and
have **no tools**. The controller applies exact, uniquely matching replacements
to an explicit path allowlist. Executable scripts, interaction IDs, recorded media,
resource bindings, tests, CI, dependencies and package versions are protected.
This job does not run new GPU experiments or manufacture daily version releases.

An author call requests Sonnet 5, with a disjoint fallback list from the separate
Opus 5 review. The model names recorded are the explicit CLI selections; the
provider does not independently attest an underlying model identity here.
The controller rejects unsupported agent/model selection and malformed output.
Validation or review findings return to the author for up to two corrections.
CI failures get an infrastructure retry and a separately reviewed correction.
Pending publication transactions survive the process and the calendar day.
The reviewed commit is saved before pushing or creating its PR. Receipts are
replaced atomically; a matching remote branch name alone is never an approval.

When there is no justified change, the reviewer must agree. The controller reuses
the passing deployment checks for the identical commit and exercises the actual
public pages at 1440, 390 and 320 pixels, including result-image downloads and
recorded playback or model-seed selection. A no-op is a successful maintenance
result, not a reason to create a cosmetic commit.

Changes run each project's own checks. After merging only the expected head, the
controller waits for the required `main` workflows and verifies the public pages.
Reports and screenshots stay under `~/.local/state/ai-repo-agent/YYYY-MM-DD/`.
Source snapshots and model inputs retain timestamped copies. The catch-up exits
without another model call when all three projects already completed that day.
An unsuccessful attempt is retained as `retry-needed`, not labelled published.
The bootstrap retries failed jobs twice within its time budget; the next scheduled
run resumes remaining work without requesting human approval.
Public checks bind each served homepage to its build manifest or exact committed
HTML. Radar additionally checks the Pages build commit and committed public JSON.
Unchanged results explicitly distinguish reused CI from local checks not repeated;
browser receipts enumerate the controls actually exercised at each viewport.

The maintenance-only tools require the already configured Kiro CLI, GitHub CLI,
Node, pnpm and the companion projects' pinned development dependencies. The radar
collector and renderer retain their Python-standard-library-only runtime.

### Pin the compatible noninteractive engine

Kiro CLI 2.24.0 accepts the existing JSON agent configurations on **engine v1**.
In an actual routing probe, v3 selected the requested model but rejected the old
agent schema and fell back to its default agent. Earlier scheduled logs also
contained a failed model-selection warning. The local jobs therefore explicitly
pass `--agent-engine v1 --no-interactive` and reject fallback diagnostics.
Do not remove that pin without verifying the replacement agent's tool boundaries.
The CLI's v1 terminal renderer may emit a bare `json` language label; the JSON
parser accepts that known rendering form and rejects additional prose.

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
6. a pull request that merges itself once every check has passed on the commit the
   script pushed.

Nobody reads the notes before they are published. The rendered pages say exactly that:
drafted by the agent, merged automatically after deterministic checks and an agent
review, **without human review**. If a check fails, is cancelled, or has not passed
within the bounded wait -- including when no check has registered yet -- the pull
request remains unmerged and its receipt is retained for automatic correction or
resumption. No human approval is part of the scheduled path.

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

Re-runs preserve completed work: publishing stops when `main` already carries the
day (`--force` republishes it); open notes and source-repair PRs resume through the
same commit-bound gate. A failed proposed change is regenerated through validation
and a separate review. Publication receipts distinguish a completed merge from a
completed deployment.

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
