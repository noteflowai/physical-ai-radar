# Automation: what runs where

Three things update this repository. Two of them run on GitHub, one runs on a
maintainer's machine, and none of them lets an outside contributor execute code
anywhere that matters.

| What | Where | Trigger | Writes |
| --- | --- | --- | --- |
| `ci.yml` | GitHub-hosted runner | push, pull request | nothing |
| `daily.yml` | GitHub-hosted runner | 01:20 UTC / 09:20 Asia/Singapore, and by hand | publishes only a day the machine missed |
| `scripts/publish_daily.sh` | maintainer's machine, cron | 07:40 Asia/Singapore | commits the day's radar to `main` |
| `oneai-content-run` | maintainer's machine, cron | 10:00 Asia/Singapore; 16:00 retry | publishes up to three distinct, reviewed, illustrated zh/en/ja article groups on oneai.host when sources and quality justify them |
| `oneai-course-run` | maintainer's machine, cron | 11:30 Asia/Singapore; 18:30 retry | produces and reviews one course lesson, then publishes its video on oneai.host |
| `scripts/nightly.sh` | maintainer's machine, cron | 21:30 Asia/Singapore | collects research, develops specialist features, publishes updates, maintains Radar and reports results |
| `scripts/nightly.sh --previous-day` | maintainer's machine, cron | 02:30 Asia/Singapore | resumes only the preceding evening's unfinished stages |

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

### Daily specialist features

The nightly job develops at most one complete feature per repository per Singapore
day across Physical AI Radar, Skills Anywhere, EvalArc, Robot Reel and the AI Chat
for Amazon Bedrock WordPress plugin. Each has a distinct specialty, documented in
[daily feature development](daily-features.md).

The controller reads actual product code and issues as well as fresh research.
Functional code is editable. A saved feature persists across failures and calendar
days until implementation, behavioral acceptance, independent review and publication
are complete. Existing regression tests and release infrastructure remain protected.
Candidate code runs in isolated containers; reviewed GPU experiments share a bounded
L40S execution lane and retain measured results. No incomplete feature or failed
release is counted as a successful day.

Release changelog preparation uses the commit-pinned private Agent Control
utility `~/.local/bin/noteflow-release-changelog`. It moves the leading plain
`Unreleased` notes into the version that ships them and keeps historical release
sections intact. Missing tools, ambiguous layouts or mismatched result digests
stop preparation before native metadata is written. The full resulting diff
still requires independent review. The domain writer retains version files,
task identities and release authority; this utility has no model, network or
publication effect.

The WordPress plugin uses its PHP compatibility matrix, official Plugin Check,
CI-built installable package, an atomic SVN release and public ZIP verification.
Other projects retain their existing CI and deployment checks with exact commit
provenance. The previous text-only homepage maintenance implementation is retained
for historical tests and shared helper functions; the installed shell entry now
invokes `scripts/develop_repos.py`.

See [project roles, execution controls and receipts](daily-features.md) for the
complete contract. No foreground Codex conversation is needed to run the schedule.

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
   each struggling source also carries `streak_days` (consecutive runs it has failed,
   counted back from the latest run) and `failing_since` (the date of the oldest run
   in that streak), so a feed dark since one date can be told from one that flaps;
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

- **The requested model.** Every role uses `claude-opus-5.5` with `high` effort,
  pinned in `scripts/agent_model.py`. A failed attempt's partial file is rolled back
  before the controller retries the same model. Legacy `RADAR_MODELS`,
  `RADAR_REVIEW_MODELS` and `RADAR_EFFORT` settings do not override this policy.
- **Findings, not tracebacks.** `python3 -m pairadar.notes check DAY` reports, item by
  item, what is wrong: JSON that stops parsing and where, a key that matches no pick,
  a missing language, a figure the page does not contain. Those lines go back to the
  drafter as its next instruction, at most `RADAR_FIX_ROUNDS` (2) times, and the
  suite runs only on a draft the validator accepts.
- **A second opinion in a separate context.** The same pinned model is called afresh
  through the read-only reviewer agent. It does not resume the drafting conversation.
  A rejection goes back to the drafter within `RADAR_REVIEW_ROUNDS`, through the
  validator and the suite again, and to a fresh review; exhausted review attempts or
  no reviewer response discard the draft.
- **The record says who wrote it.** `pairadar.notes stamp` writes the models that
  actually ran into `author.model` and `review`, so the rendered pages, the commit and
  the pull request name them. The agent used to report its own model, which is not
  evidence.

The source repair job and its separate reviewer use the same pinned model policy.

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
The two night entries override this with a fifteen-minute lock wait and a four-hour
whole-batch budget, leaving a gap between the evening batch, its catch-up and the
07:40 publisher. Each stage has its own timeout; stopping a stage also stops its
descendants. A stopped batch retains its incomplete stage for the next retry.
Stage limits are individual caps, not reserved allocations: the whole-batch deadline
takes precedence. A slow evening can finish its remaining stages in the catch-up.
Starting `nightly.sh` by hand delegates to the same bootstrap, so all stages and
their output checks use the dedicated clone even when invoked from a working checkout.
With no date option, it resumes the most recent **21:30 Singapore** batch. A new
manual process at 01:40 therefore resumes the preceding evening; it cannot silently
publish the upcoming morning issue early. The bootstrap pins `RADAR_RUN_STARTED_AT`
before lock waits and retries. `--date YYYY-MM-DD` is an explicit manual override,
including for controlled validation of an already published issue before the evening.

Re-runs preserve completed work: publishing stops when `main` already carries the
day (`--force` republishes it); open notes and source-repair PRs resume through the
same commit-bound gate. A failed proposed change is regenerated through validation
and a separate review. Publication receipts distinguish a completed merge from a
completed deployment.

## When the machine misses a day

`daily.yml` wakes at 01:20 UTC / 09:20 Singapore, an hour and forty minutes after the publish. When
`radar/latest.json` on `main` already names the day, it stops there. Otherwise it
publishes the day with the per-run `GITHUB_TOKEN`, asks Pages for a build, and opens an
issue titled "The local publisher missed a day" (or comments on the open one). If
publishing fails there as well, the scheduled run fails, which GitHub mails to the
maintainer. Run it by hand with `force` to republish a day that is already on `main`.

## Timing

The checked-in schedule is [`scripts/cron.sg`](../scripts/cron.sg). Install it on a
host whose timezone is **Asia/Singapore**, replacing the earlier standalone notes,
source-repair and 10:10/16:10 companion entries. Do not add both schedules.

The 10:00 article job uses the private
[`wordpress-aws` content controller](https://github.com/noteflowai/wordpress-aws/blob/main/content/README.md)
from an independent clone. It begins after the 09:20 hosted Radar fallback and
does not consume the 21:30 specialist-feature budget. Its 16:00 invocation only
resumes an unfinished receipt; it cannot publish a second article for the date.

The 11:30 course job uses the private
[`physical-ai-academy` course controller](https://github.com/noteflowai/physical-ai-academy/blob/main/scripts/daily_course.py)
in a dedicated clone. It renders one planned lesson, runs audio, visual and
source QA, and uses an independent Kiro reviewer before publishing the exact
approved MP4 and transcript through WordPress. Its 18:30 run resumes a failed
episode. The video is hosted through the `wordpress-aws` CourseMedia stack.

The issue date follows Singapore's calendar. At 07:40 it is still 23:40 UTC on the
preceding date; using `date -u` here would skip the new issue or label it yesterday.
The bootstrap pins `RADAR_RUN_DAY` before waiting for a lock or retrying, and the
publisher passes it explicitly to the collector. The hosted fallback uses the same
date helper and retains the chosen date in `GITHUB_ENV`. Source timestamps, source
windows and generation timestamps remain UTC; historical issues are not renamed.
A late catch-up cannot replace a newer issue with an older one.

At **21:30** a single controller performs:

1. fresh collection into local state (5 minutes);
2. five specialist feature workers, with a durable fairness cursor (3 hours total);
3. verified-release updates and RSS publication (10 minutes);
4. source-health repair with independent review (10 minutes);
5. daily publish/recovery and public deployment checks (10 minutes);
6. notes for the verified morning issue, with review and deployment gates (20 minutes);
7. a seven-day delivery report and bounded release-download metrics (1 minute).

These caps total 236 minutes within the four-hour batch. New feature work keeps its
own source, distribution and announcement checkpoints; successful stages are reused.

Research inputs keep each item's original `published` value as its source `date`,
separate from `issue_date` and the collection timestamp. Bad entries are recorded
as source failures without discarding other valid research.

07:40 precedes arXiv's regular 20:00 US Eastern announcement (08:00 or 09:00
Singapore, depending on US daylight saving). The morning issue contains sources
available at its cutoff. The evening research includes later sources for maintenance;
the following morning can include them in the next public shortlist. No claim is made
that 07:40 already includes that day's arXiv batch.

Atomic stage receipts and append-only logs live under
`~/.local/state/pairadar/nightly/YYYY-MM-DD/`. Retries skip successful stages.
A failed repair does not block independent research or companion work; failed
publication blocks its dependent notes. The batch remains `retry-needed` if any
stage fails. A notes command that produces no notes cannot count as completed.
At **02:30**, `--previous-day` resumes the preceding evening's Singapore date, so
the batch and research issue keep their original date. Feature releases additionally
consume the actual Singapore calendar day's allowance: a feature completed during
catch-up prevents a second feature for that repository in the following evening.
Completed batches exit without new model calls. All execution is noninteractive;
an exhausted retry budget records failure for the next automatic catch-up.

## Credentials

The primary model key lives in the login environment of the maintainer's account, or in
`~/.config/pairadar/env` at mode 600 for cron, which is why the cron entries use a
login shell. The key is never written to the repository or logs, and no GitHub secret is needed: with no
self-hosted runner, no workflow calls a model.

### Kiro 额度耗尽时切换密钥

定时入口 `radar-run` 优先通过本机
`~/.local/share/kiro-failover/bin/kiro-cli` 运行非交互 Kiro 会话。
该入口先使用当前 `KIRO_API_KEY`（或已登录的主账号）；只有在请求一开始
返回额度耗尽、且尚无正文输出时，才从权限为 `0700` 的
`~/.config/kiro-failover/` 中读取权限为 `0600` 的 `backup.key` 重试一次。
密钥不进入仓库、命令行参数或任务日志。已有正文输出时不会重跑，以免重复执行工具操作。

`scripts/kiro_failover.py` 是本机入口的可版本化源文件。安装为上述
`kiro-cli` 路径，并确保它可执行；没有安装该入口的机器仍按原方式使用 Kiro CLI。
