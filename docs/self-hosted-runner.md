# Self-hosted runner safety

This repository is public and one workflow lane runs on a self-hosted runner. That
combination is the dangerous one GitHub warns about: a workflow triggered by a pull
request runs code from the pull request's branch, so pointing such a workflow at a
self-hosted runner hands that machine to anyone who can open a pull request.

## The invariant

**Only `workflow_dispatch` and `schedule` may reach the self-hosted runner.** Both
can be started only by someone with write access. `tests/test_workflows.py` enforces
this: any workflow file that mentions `self-hosted` must not declare a trigger that
an outside contributor can cause, `push` and `workflow_run` included.

Current lanes:

| Workflow | Triggers | Runs on |
| --- | --- | --- |
| `ci.yml` | push, pull_request, workflow_dispatch | `ubuntu-latest` (GitHub-hosted) |
| `daily.yml` | schedule, workflow_dispatch | `ubuntu-latest` (GitHub-hosted) |
| `selfhosted-smoke.yml` | workflow_dispatch | `[self-hosted, l40s]` |
| `curator.yml` | workflow_dispatch | `[self-hosted, tokyo]` |

Untrusted code therefore never reaches the machine today. The test is what keeps that
true after someone adds a trigger in a hurry.

## Machine checklist

- **The runner account must not have passwordless `sudo`.** A job on the runner is
  code execution as that account; with `NOPASSWD: ALL` it is code execution as root.
  Check with `sudo -l -U <runner-user>` and remove the entry if present.
- Run the runner as a dedicated account with no access to other users' homes. A home
  directory at mode `750` is enough to stop the runner reading another account's
  credentials.
- Register the runner **per repository**, not per organisation, so another repository
  cannot schedule work onto it.
- Prefer `--ephemeral` registration, so each job gets a fresh runner registration
  instead of reusing a long-lived workspace.
- Keep the runner service stopped when it is not in use. An offline runner cannot run
  anything, and dispatch-only jobs simply queue until it is back.
- In Settings → Actions, require approval for workflow runs from outside
  contributors. This is defence in depth: the invariant above already keeps fork code
  off the machine.
- Keep `Settings → Actions → Workflow permissions` on read-only by default. Workflows
  that need to write, like `curator.yml`, declare it themselves.
- Do not store model or cloud credentials on the runner. `curator.yml` receives
  `KIRO_API_KEY` from repository secrets for the length of one step; the runner
  account itself stays unauthenticated (`kiro-cli whoami` reports "Not logged in").

## Why the curator agent has no shell

Measured on kiro-cli 2.21.2, the trust flags differ in what they enforce:

| Invocation | Write outside `allowedPaths` | Shell command outside `allowedCommands` with `denyByDefault` |
| --- | --- | --- |
| `--trust-all-tools` | ran | ran |
| `--trust-tools=write` | blocked by the policy | — |
| `--trust-tools=shell` | — | ran |

So the write path allowlist is a real boundary under granular trust, while a shell
allowlist is not one at all. `.kiro/agents/radar-curator.json` therefore grants no
shell, and `curator.yml` uses `--trust-tools=…` rather than `--trust-all-tools`. The
workflow — not the agent — runs the tests, checks that only `data/sources.json`
changed, and opens the pull request. A human still reviews it.
