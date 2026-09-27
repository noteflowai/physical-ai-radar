# Daily specialist feature development

The local nightly controller develops **at most one complete feature per repository
per Singapore day**. It carries an unfinished feature into the next run and day.
Completing yesterday's feature consumes today's allowance. A failed test, review,
deployment or package readback remains unfinished; it is never reported as released.
The release allowance uses the actual Singapore calendar date even when a 02:30
catch-up retains yesterday's batch date.

| Repository | Specialty | A useful feature must serve |
| --- | --- | --- |
| Physical AI Radar | Research intelligence | Source discovery, provenance, freshness and evidence navigation |
| Skills Anywhere | Portable skill delivery | Reliable delivery of skills and their resources across agent clients |
| EvalArc | Evaluation and reproducibility | Behavioral regressions, experiment comparison and verifiable evidence |
| Robot Reel | Robot experiments and replay | Simulation, failure inspection, policy comparison and measured GPU experiments |
| AI Chat for Amazon Bedrock | WordPress knowledge delivery | Grounded knowledge retrieval and useful Physical AI report workflows |

The shared Physical AI focus does not remove existing general-purpose workflows.
Skills Anywhere retains Agent Skills compatibility; EvalArc continues serving general
agent evaluations; the WordPress plugin retains its established chat and Bedrock use.
The projects should connect through documented resources and evidence rather than
duplicate each other's engines.

## What happens each day

At 21:30, source repair, daily publication recovery, fresh collection and Radar notes
run first. The feature stage then visits all five repositories. At 02:30 the previous
evening's unfinished stages resume. The 07:40 publisher remains separate.

Each feature gets a maximum of 35 minutes per worker invocation, including planning,
coding, local validation, review, CI and publication. The feature stage is capped at
three hours; the whole nightly batch remains capped at four hours. Catch-up and
bootstrap retries reuse durable transactions. Time limits prioritize finishing small
increments and prevent a single project from consuming the entire night.
Planning, implementation, validation, acceptance review, push and publication retain
separate checkpoints. Validated CPU work is reused on the identical commit when a GPU
phase resumes. Commit bundles also survive removal of the disposable checkout.
After nine unsuccessful planning or implementation attempts, the proposal is explicitly
deferred, its diagnostics retained and no release claimed. A smaller plan can be
selected on the following day; repeated retries cannot start more features that day.
Nine unsuccessful resumptions of a validation or publication phase also produce an
explicit deferred record with the partial publication state, rather than blocking
that repository forever or claiming that an unchecked release succeeded.

1. Read the actual repository, recent commits, open issues and fresh research.
2. Choose one bounded capability with a user, problem, expected behavior and one to
   eight acceptance conditions. A separate review call checks project fit and scope.
3. Read relevant implementation and test files. Implement the complete feature,
   introduce behavioral tests in new files, and document usage.
4. Independently review the exact proposed diff before executing candidate code.
5. Run the new acceptance test against the old product: it must fail. Run it against
   the new implementation: it must pass, together with the project's existing checks.
   Per-command receipts distinguish a real test failure from a crashed harness or
   missing runner. Regenerated files trigger validation of the final commit again.
6. If the feature justifies GPU work, run its reviewed Python experiment under the
   shared GPU lock. Record the actual device, CUDA computation, commands, output
   checksum, image identity and elapsed time. GPU availability alone is not a result.
7. Review the final diff and actual validation logs in another independent call.
8. Save the reviewed commit before pushing. Merge only after the required checks pass
   on that commit, then verify the merged commit's deployment and public content.

Every author and reviewer requests **Claude Fable 5.1** (`claude-fable-5.1`) with
`high` effort, as pinned centrally in `scripts/agent_model.py`. Daily notes, source
repairs and their reviewers use the same policy. A failed call stays with this model
on controller retry or catch-up; no other model is substituted. Review independence
comes from a fresh call and separate context, not from a different model family.
The code and acceptance gates still run independently of model judgments.
These are requested CLI model names, not provider attestations. All calls pin Kiro
CLI engine v1, run noninteractively, and reject model/agent fallback diagnostics.
Legacy `RADAR_MODELS`, `RADAR_REVIEW_MODELS` and `RADAR_EFFORT` overrides no longer
change the pinned policy.

## Functional code and execution

The old homepage-only path is replaced by `scripts/develop_repos.py`.
`scripts/feature_policy.py` defines each product's code, documentation and test
directories. PHP, Python, JavaScript and TypeScript product code can change.
New tests and GPU experiments can be added. Existing tests, dependencies, CI,
release gates, credentials and the automation itself are protected from daily edits.
Those infrastructure changes remain separately reviewed maintenance work.

Models receive bounded text and return exact edits; they have no tools or host
credentials. The controller creates dedicated marked clones under
`~/.local/share/ai-feature-agent/`, exports tracked source and validates changes in
Docker containers. Candidate containers have no network, host home, Docker socket,
GitHub token, AWS credentials or access to a user's working checkout. They run as
the ordinary user with dropped capabilities, process/memory/CPU limits and explicit
timeout cleanup. Only a private result directory is mounted writable.
Source inspection supports tracked product directory searches and read-only build
metadata. Bounded notes preserve verified source facts when old excerpts are evicted.
Each implementation retry discards those notes when resetting to the published base;
the author receives an explicit snapshot stating that no candidate edits are applied.
Author JSON uses a fenced code block because the CLI's Markdown renderer otherwise
alters literal code characters such as PHP comment asterisks. Independent approval
responses retain strict object parsing.

Dependency preparation uses the already published base commit. Each project's
prepared image is cached by base commit and setup policy, and its image ID is recorded.
The GPU image is pinned to a locally installed CUDA 12.8 / PyTorch 2.9.1 image.
GPU experiments use device 0 serially, wait briefly if another workload occupies it,
and defer when the device is busy. Only features that need GPU computation use it.

## WordPress distribution

The source of truth is `noteflowai/ai-chat-for-amazon-bedrock`. Daily development never
edits the maintainer's existing WordPress.org SVN checkout.

A complete plugin feature increments its version consistently and includes changelog
and upgrade notes. A pre-merge guard requires the next minor version from the exact
base commit. The required CI checks are the release gate on PHP 7.4 and 8.3,
plus the official Plugin Check on a real WordPress installation. After merge, the
controller downloads the checked `plugin-package` artifact from that exact main
workflow run.
If that artifact has expired, its original workflow run is rerun once to regenerate
the package on the same commit. Cached packages retain their commit and checksum.

The controller publishes those exact files to SVN trunk and a new immutable version
tag in one `svnmucc` transaction. It uses the maintainer's existing noninteractive SVN
authentication cache, or the publisher configuration below. It refuses to overwrite a version with different contents or
publish a non-increasing version. It verifies SVN files and then compares every file
inside the public downloads.wordpress.org ZIP with the checked package. Interrupted
readback retries the existing release without creating another version.

For unattended WordPress publishing, create
`~/.config/ai-feature-agent/wordpress.json` on the scheduling host:

```json
{
  "username": "YOUR_WORDPRESS_ORG_USERNAME",
  "password_file": "/absolute/path/to/private-svn-token"
}
```

The token file must belong to the scheduling user, be a regular file rather than
a symlink, have no group/other permissions (`chmod 600`), and contain only the SVN
password/token on one line. Keep both files outside project checkouts. The publisher
passes the token through standard input with `--no-auth-cache`; it is never a command
argument or mounted into development containers. A configured but missing or unsafe
token fails closed. Without this configuration, the existing SVN cache is used.
The cron process reads this file directly; no interactive shell export is required.
After supplying or renewing credentials, rerun the selected project command below.
An already merged feature resumes publishing the same checked package.

No daily worker publishes WordPress posts or connects to physical robot actuators.

## Receipts and operation

- `~/.local/state/ai-feature-agent/YYYY-MM-DD/results.json`: daily outcomes.
- `tasks/REPO/active.json`: the one unfinished feature and its exact reviewed commit.
- `tasks/REPO/FEATURE/`: plans, prompts, reviews, attempts, logs and publication proof.
- `gpu.lock`: the shared local experiment lock.

Run the real installed entry point for a selected project:

```sh
radar-run scripts/improve_repos.sh --repo ai-chat-for-amazon-bedrock
```

Run the full scheduled path:

```sh
RADAR_TIMEOUT=4h RADAR_LOCK_WAIT=900 radar-run scripts/nightly.sh
```

`--date` is a controlled manual override. A normal nightly restart retains the most
recent 21:30 Singapore batch. No entry needs a foreground Codex conversation.

Prerequisites are Docker with the NVIDIA runtime, the pinned tool/GPU images,
Kiro CLI, GitHub CLI, SVN with noninteractive publishing credentials, and the
installed `radar-run` bootstrap. A missing prerequisite is recorded as a failure;
it cannot produce a successful release receipt.
