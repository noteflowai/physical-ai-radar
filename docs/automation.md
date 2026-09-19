# Automation: what runs where

Three things update this repository. Two of them run on GitHub, one runs on a
maintainer's machine, and none of them lets an outside contributor execute code
anywhere that matters.

| What | Where | Trigger | Writes |
| --- | --- | --- | --- |
| `ci.yml` | GitHub-hosted runner | push, pull request | nothing |
| `daily.yml` | GitHub-hosted runner | 01:30 UTC, manual | commits the day's radar to `main` |
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
3. the agent, with granular trust only (`--trust-tools=…`), a write path allowlist,
   and no shell;
4. verification by this repository's own deterministic checks — the touched file set,
   the schema, the full test suite;
5. a pull request, for a human.

Measured on kiro-cli 2.21.2, `--trust-all-tools` bypasses the agent's own write path
allowlist while `--trust-tools=write` enforces it, and a `shell` command allowlist is
not enforced at all. That is why neither agent is given a shell and why the scripts,
not the agents, run the commands.

## Timing

The notes are drafted for the current UTC date, and that date's radar publishes at
01:30 UTC. At 02:30 Asia/Singapore — 18:30 UTC — the day's radar is seventeen hours
old, so the order is right with plenty of margin. Moving the cron job near 09:30
Asia/Singapore (01:30 UTC) would race the publish for the same day's output.

## Credentials

The model key lives in the login environment of the maintainer's account, or in
`~/.config/pairadar/env` at mode 600 for cron, which is why the cron entries use a
login shell. Nothing writes the key anywhere, and no GitHub secret is needed: with no
self-hosted runner, no workflow calls a model.
