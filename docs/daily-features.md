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

At 21:30, fresh collection runs first, followed by the five feature workers and
verified-release announcements. Source repair, publication recovery and Radar notes
follow, then a delivery report. At 02:30 the previous
evening's unfinished stages resume. The 07:40 publisher remains separate.

Each feature gets a maximum of 35 minutes per worker invocation, including planning,
coding, local validation, review, CI and publication. The feature stage is capped at
three hours; the whole nightly batch remains capped at four hours. Catch-up and
bootstrap retries reuse durable transactions. Time limits prioritize finishing small
increments and prevent a single project from consuming the entire night. A durable
cursor starts after the last worker launched if a batch was interrupted.
Planning, implementation, validation, acceptance review, push and publication retain
separate checkpoints. Validated CPU work is reused on the identical commit when a GPU
phase resumes. Commit bundles also survive removal of the disposable checkout.
After nine unsuccessful planning or implementation attempts, the proposal is explicitly
deferred, its diagnostics retained and no release claimed. A smaller plan can be
selected on the following day; repeated retries cannot start more features that day.
Nine unsuccessful resumptions of a validation or source-publication phase also
produce an explicit deferred record. Once distribution starts, the merged/tagged
version is retained until repaired and verified; a registry failure cannot start
another feature or replace an immutable package.

Unapproved planning revisions retain the full previous proposal, source excerpts
bound to the base commit, and an append-only record of findings. Attempt numbers
remain unique across process restarts. The author and reviewer both receive the
previous proposal and the three most recent findings, so corrections preserve
earlier decisions. After two unsuccessful rounds, narrow the proposal to one
complete input-to-result workflow; optional formats and integrations can wait.
Existing safeguards and the retained behavior's acceptance criteria remain required.
Behavior descriptions target 2,000–3,500 characters and permit up to 6,000 to
express complete input, output and error contracts. The serialized prompt budget,
one-workflow review, acceptance count and executable-diff limits remain enforced.
A next-day task can inherit an unapproved deferred proposal and its findings as
data, with a `recovery_of` link; it must inspect today's source and obtain fresh
independent approval. It never reopens or resets the old attempt budget.

Reports distinguish planning, implementation and later-phase exhaustion, including
counts, final feedback and the next eligible batch date. If all project outcomes
are published or deferred, the worker exits **20** for deferrals and the night batch
finishes as `complete-with-deferrals`. This is a terminal scheduling outcome, not
a claim that every feature shipped. Announcements and the delivery report still run;
the bootstrap and 02:30 catch-up do not retry the exhausted projects. Actual
retryable failures still exit 1 and resume through the existing bounded retries.

Before starting a paid feature worker, the controller checks the scheduling identity's
repository push permission, required workflow availability, and npm/PyPI environment
rules. A version tag must match a **tag** deployment rule; a rule for branch `main`
does not authorize a tagged workflow dispatch. Required environment reviewers block
unattended publication. Wait timers are recorded and can resume through catch-up.
Robot Reel also requires `PYPI_PUBLISH_ENABLED=true`.

Preflight is read-only, bounded to 45 seconds per project, and repeated on retries.
A blocked project gets a `publication-preflight` result without creating a feature;
other projects continue. An existing reviewed/releasing feature checks its saved tag
instead of a hypothetical next version. These checks verify GitHub prerequisites;
registry credentials and actual package bytes are still verified during publication.
The model cannot change environment protection rules or its own preflight checks.

Dependency images use stable, worker-owned XDG data/cache/state directories during
both installation and offline verification. This retains pnpm's verified package
store across the root-to-worker and temporary-HOME transition. A release-version
change may trigger pnpm dependency revalidation; it must find the already-installed
package bytes without network access. Candidate containers still receive no host
credentials, Docker socket or network.

1. Read the actual repository, recent commits, open issues and fresh research.
2. Choose one bounded capability with a user, problem, expected behavior and one to
   eight acceptance conditions. Specify the target user, an observable success
   measure, a usage example, limitations and upgrade/rollback guidance. A separate
   review call checks project fit and scope.
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
9. Publish its versioned release and verify installable package bytes from the public
   registry. Only then count the feature as complete and enqueue its announcement.

After validation, a separate author call reconciles delivery metadata with the
actual commit and test receipts before final acceptance. Pre-test statements such
as “not run” must not survive once the controller has measured those checks.
This call can return metadata only; it cannot edit code, tests or documentation.
Its result is bound to the commit and complete verification receipts. The final
independent reviewer still checks code and evidence. If every remaining blocker
is confined to delivery metadata, correction retains the validated commit and
reruns metadata finalization and review. Code, test, product-documentation or
validation blockers return to implementation. Neither path can publish without
fresh approval, and the existing phase-attempt limit still applies.

Every author and reviewer requests **Claude Opus 5.5** (`claude-opus-5.5`) with
`high` effort, as pinned centrally in `scripts/agent_model.py`. Daily notes, source
repairs and their reviewers use the same policy. A failed call stays with this model
on controller retry or catch-up; no other model is substituted. Review independence
comes from a fresh call and separate context, not from a different model family.
The code and acceptance gates still run independently of model judgments.
These are requested CLI model names, not provider attestations. All calls pin Kiro
CLI engine v1, run noninteractively, and reject model/agent fallback diagnostics.
Legacy `RADAR_MODELS`, `RADAR_REVIEW_MODELS` and `RADAR_EFFORT` overrides no longer
change the pinned policy.

## UI and user experience

Interface quality is part of each complete feature. Planning, implementation, code
review and final acceptance all ask for professional, attractive, distinctive and
easy-to-use results within the project's established design language. Inspect the
existing components and styles before choosing the design; WordPress admin controls
should remain familiar to site administrators.

Use clear hierarchy, deliberate typography and spacing, consistent controls and an
obvious primary action. Purposeful visualizations and restrained microinteractions
can make Physical AI evidence easier to understand. Honor reduced-motion preferences
and keep effects within the feature's performance and dependency budget.

The existing behavior and acceptance fields describe the path from entry to result,
relevant loading/empty/error/disabled/success states, recovery, responsive behavior
and accessibility. Use semantic controls, accessible names, keyboard navigation,
visible focus, readable contrast and comfortable touch targets. Avoid accidental
horizontal overflow. CLI/API features instead need clear help, output and actionable
errors; they do not need an artificial graphical interface.

Use the project's existing browser/DOM test infrastructure to exercise changed
flows at desktop and narrow mobile widths, keyboard operation and relevant failures
where available. Rendered screenshots support visual review when available.
The Kiro author/review protocol currently receives text only: it must not claim to
have viewed an image or operated a browser. Source inspection and unit-test success
are not visual verification. Missing browser or visual evidence must be stated in
`delivery.limitations`; this policy does not add a screenshot evaluator or browser
runtime to projects that lack one.

Reviewers block concrete usability, accessibility, layout and integration defects,
and reconcile missing planned validation before approval. Subjective taste alone
is not a blocker. UI polish stays within the day's single complete feature. The
plan/delivery JSON schema is unchanged, so saved tasks remain resumable.

## Turning current topics into useful features

Agents actively consider emerging models, methods and tools, then select one
concrete improvement for an existing user. A trend-inspired plan records its dated
source, inspected code gap, project ownership, simple baseline and observable
benefit in the existing plan fields. Popularity alone does not justify the feature.
The same integration should not be copied across all five projects.

The research context takes up to 20 distinct URLs within 18 KB, round-robin across fresh evening
research, the morning shortlist, GitHub discoveries, HF model rankings and other
sources. This prevents early entries from exhausting the budget before evening
research is reached. Source dates, collection/issue dates and the meaning of each
signal remain distinct; total stars are not recent star growth or proof of adoption.

Reviewed notes in `data/feature-topics.json` provide additional project-specific
opportunities and primary links. Each has a checked date and expiry date. At most
three current notes and 12 KB reach a worker; malformed, unavailable or expired
notes produce diagnostics and do not prevent ordinary work. Notes are maintained
through reviewed controller changes; daily agents cannot edit them. They are
research leads, not commitments, and do not execute code, add providers or grant
network/credential access. Fresh research continues independently after a note expires.

The initial note covers [TypeSafe Jev](https://docs.typesafe.ai/introduction):
a model for typed probabilistic decisions over text/structured state. Suggested
directions are distinct and must still survive source inspection:

| Project | Candidate contribution |
| --- | --- |
| EvalArc | First investigate a complete recorded-decision import/report flow with error-versus-coverage evaluation |
| Radar | Evidence-linked relevance or deduplication decisions with uncertainty retained |
| Skills Anywhere | Portable recommendations over known skill IDs, including a no-match result |
| Robot Reel | Replay recorded policy choices against a rule baseline in one bounded simulation |
| WordPress | Optional grounded-passage relevance or intent routing with useful admin feedback |

Jev API integration is **not installed or enabled** by this note. The Kiro developer
and all independent reviewers remain Opus 5.5/high. The official Jev model docs
currently describe text-only input; video understanding or local GPU inference
must not be inferred. Type-safe outputs can still make wrong judgments, and a
confidence statistic is not a correctness certificate. Thresholds need validation
on held-out domain data, with abstention and deterministic checks retained.

Without authorized provider access, a recorded-data workflow can still be complete
and useful. Mark synthetic fixtures honestly; they validate parsing/policy behavior,
not live model performance. Never claim a Jev integration by renaming a heuristic.
Actual comparisons should retain data provenance, resolved model version, question
schema, errors/abstention and measured latency/cost. UI work presents that evidence
clearly under the shared interface-quality policy.

## Functional code and execution

The old homepage-only path is replaced by `scripts/develop_repos.py`.
`scripts/feature_policy.py` defines each product's code, documentation and test
directories. PHP, Python, JavaScript and TypeScript product code can change.
Radar's existing `assets/site.css` and `assets/site.js` are also readable and editable
for functional interface work; other assets and generated landing pages remain
outside this source edit allowance.
New tests and GPU experiments can be added. Existing tests, dependencies, CI,
release gates, credentials and the automation itself are protected from daily edits.
Those infrastructure changes remain separately reviewed maintenance work.
The controller advances the next minor version and synchronizes existing package,
plugin, citation and installation metadata before independent review. It preserves
dependency versions and frozen experiment identities. WordPress authors continue
updating the plugin header, constant, stable tag, changelog and upgrade notice,
with the controller checking their agreement.

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

## Releases and owned-channel promotion

| Project | Distribution proof required after source publication |
| --- | --- |
| Radar | Immutable GitHub tag and feature release |
| Skills Anywhere | Release workflow gates, GitHub tarball, identical npm bytes and matching MCP Registry record |
| EvalArc | Exact main CI wheel/sdist artifact, GitHub upload digests and identical PyPI files |
| Robot Reel | Existing release workflow source/asset gates and identical GitHub/PyPI wheel and sdist |
| WordPress | Verified WordPress.org files plus the exact CI ZIP on GitHub Releases |

The existing trusted-publisher workflows own registry credentials. Dispatches and
failed-job retries retain their exact tag/commit and bounded attempt receipts.
Successful publication jobs are not rerun. A conflicting tag, release or asset
fails closed; the controller never deletes an existing version to force a retry.
Registry propagation can complete on the 02:30 catch-up or subsequent runs.
Persistent workflow, credential or provenance failures need maintenance and remain
visible in the report.

Release notes use the final reviewed behavior, usage example, limitations and upgrade
guidance. A durable outbox creates a content PR in Radar for the
[project updates site](https://noteflowai.github.io/physical-ai-radar/updates/) and
[RSS feed](https://noteflowai.github.io/physical-ai-radar/updates/feed.xml).
Its exact PR checks, deployed commit, public JSON, page and RSS entry must agree
before it is marked announced. An interrupted push or PR creation resumes the saved
commit; pending announcements do not reauthor completed features.
Failed PR checks get one automatic rerun. Persistently failing, closed or externally
changed announcement PRs are quarantined with their diagnostics, allowing later
release announcements to proceed. Contract errors also stay in separate receipts;
they cannot retain the active transaction of an already completed product feature.
After fixing the recorded cause, `python3 scripts/feature_updates.py --retry-repairs`
requeues quarantined entries. Complete uploaded packages are preserved; only an empty
failed-upload placeholder in a controller-owned draft can be removed for upload retry.

These channels provide discoverable release notes and subscriptions. The controller
does not infer audience reach or conversions, post to unspecified social accounts,
or claim a week of autonomous success from one completed feature. The seven-day
local report records actual outcomes, unfinished phases and errors. Download metrics
cover the latest announced GitHub release per project within a bounded query budget.

## Receipts and operation

- `~/.local/state/ai-feature-agent/YYYY-MM-DD/results.json`: daily outcomes.
- `tasks/REPO/active.json`: the one unfinished feature and its exact reviewed commit.
- `tasks/REPO/FEATURE/`: plans, prompts, reviews, attempts, logs and publication proof.
- `YYYY-MM-DD/REPO/preflight.json`: the latest GitHub publication prerequisite check.
- `gpu.lock`: the shared local experiment lock.
- `tasks/REPO/FEATURE/distribution/`: tag, artifact, registry and workflow receipts.
- `outbox/`: pending/announced release records and channel readback receipts.
- `announcement-transaction.json`: the recoverable announcement PR.
- `announcement-errors/` and `announcement-failures/`: retained promotion failures.
- `reports/YYYY-MM-DD.json`: seven-day delivery health and observed download counts.

Run the real installed entry point for a selected project:

```sh
radar-run scripts/improve_repos.sh --repo ai-chat-for-amazon-bedrock
```

Inspect publication prerequisites without starting models or changing GitHub settings:

```sh
python3 scripts/feature_preflight.py
python3 scripts/feature_preflight.py --repo evalarc --output /tmp/evalarc-preflight.json
```

Run the full scheduled path:

```sh
RADAR_TIMEOUT=4h RADAR_LOCK_WAIT=900 radar-run scripts/nightly.sh
```

To retry announcements independently, run `python3 scripts/feature_updates.py` from
the installed Radar checkout. To finish distribution for an older completed feature,
run `python3 scripts/feature_release.py --backfill /path/to/complete.json --root
/path/to/managed/repo` there; optional `--delivery /path/to/reviewed-copy.json` supplies
accurate final release copy. Backfill preserves the original completion and daily
allowance. It cannot create another feature.

`--date` is a controlled manual override. A normal nightly restart retains the most
recent 21:30 Singapore batch. No entry needs a foreground Codex conversation.

Prerequisites are Docker with the NVIDIA runtime, the pinned tool/GPU images,
Kiro CLI, GitHub CLI, SVN with noninteractive publishing credentials, and the
installed `radar-run` bootstrap. A missing prerequisite is recorded as a failure;
it cannot produce a successful release receipt.

Feature authors return bounded JSON chunks (target 12,000 characters). The controller
persists exact edits as inert drafts tied to the published base and approved plan;
worker restarts resume those drafts. Large new files can be continued with explicit
append fragments, and focused source reads show the draft's virtual contents. When
the CLI truncates a response, only fully decoded edit objects are recovered, never
partial strings. Authors see the saved-file manifest and complete missing work.
All files and delivery metadata must be finished before any candidate is applied.
The existing size limits, independent review, red/green acceptance, regression checks
and release verification still apply to the entire feature. Review or test rejection
retains the complete inert draft so authors can correct exact spans against its virtual
contents. Every correction repeats the full review and test gates on a fresh checkout.
Code review precedes candidate execution; the separate final acceptance review requires
actual execution receipts. A changed base or approved plan invalidates the draft.
Kiro v1 decodes HTML entities even in fenced output. JSON author and reviewer policies
therefore encode ampersands as `\u0026` (and author backticks as `\u0060`) so code,
HTML-escaping assertions and literal review findings survive terminal rendering.
# Optional local decision records

When a verified Kev-4B batch runtime is installed, planners receive `local_decisions.available`.
A plan may include one bounded `decision_probe` only when real recorded decisions support its
single user feature. Kiro independently reviews the exact probe before the controller runs it.
Each probe allows four synthetic cases, four questions per case and eight choices/score levels,
with a 12 KiB total input budget. Choice, Score and Noul use the TypeSafe request shapes.

The controller invokes the pinned local Kev runtime outside candidate containers. It supplies
only JSON data, with no model-controlled command, URL, path or credential configuration. The
runtime owns the shared GPU lock and enforces a ten-minute lifetime. GPU contention or probe
failure retains the approved feature for a retry. The input/runtime hash identifies the receipt:
a complete validated result is reused across implementation retries rather than rerun.
An invalid cached receipt is quarantined and collected again. Controller timeouts first
terminate the client gracefully, then force its process group only if necessary; cleanup
selects that attempt's unique job label and preserves other sessions and their metadata.

`decision_evidence` supplies original inputs, distributions, actual model and base revision,
immutable image ID, measured wall/model latency and timestamp to implementation, code review
and final acceptance review. A response must match every approved input/question and valid
probability ranges before the controller accepts it. These examples prove execution, not
held-out accuracy or calibration. Product fixtures must preserve and label that provenance.

Use each repository's specialty: EvalArc imports and evaluates records; Robot Reel links
decisions to replay and rule comparisons; Skills Anywhere validates portable decision contracts;
Radar covers source intelligence; WordPress presents grounded reports within its existing
permissions. Each repository still chooses one actual code gap. The optional probe does not
force five copies of the same feature or replace existing checks and release gates.
