#!/usr/bin/env bash
# Draft the day's per-item analysis with kiro-cli, on this machine, on a schedule.
#
# Outbound only: nothing on GitHub can trigger this. The machine pulls the published
# branch, asks a narrow agent to draft notes for that day's picks, verifies the result
# with the deterministic pipeline, and opens a pull request. It never pushes to main,
# and it never edits anything outside data/notes/.
#
#   scripts/draft_daily_notes.sh [--date YYYY-MM-DD] [--dry-run]
#
# The 21:30 Singapore batch verifies the 07:40 issue before drafting its notes.
# A 02:30 catch-up passes the preceding evening's date explicitly, even after midnight.
#
# Requirements: kiro-cli on PATH and authenticated (KIRO_API_KEY or a stored login),
# gh authenticated for the pull request. Exit codes: 0 nothing to do or PR opened,
# 1 something failed loudly.
set -euo pipefail
export GIT_TERMINAL_PROMPT=0 GH_PROMPT_DISABLED=1

# A dedicated clone by default: this script resets hard, which must never happen
# inside somebody's working checkout.
REPO_DIR="${RADAR_REPO:-$HOME/.local/share/physical-ai-radar}"
CLONE_URL="${RADAR_CLONE_URL:-https://github.com/noteflowai/physical-ai-radar.git}"
BRANCH_BASE="${RADAR_BRANCH:-main}"
AGENT="${RADAR_AGENT:-radar-analyst}"
REVIEWER="${RADAR_REVIEWER:-radar-reviewer}"
EFFORT="${RADAR_EFFORT:-medium}"
AGENT_TIMEOUT="${RADAR_AGENT_TIMEOUT:-20m}"
# Tried in order until one answers: "temporarily unavailable" on the machine's default
# model cost the nights of 09-24 and 09-25. The reviewer takes the first model on its
# list that did not write the draft, so no model is the only check on its own work.
MODELS="${RADAR_MODELS:-claude-fable-5.1 claude-opus-5 claude-sonnet-5}"
REVIEW_MODELS="${RADAR_REVIEW_MODELS:-claude-opus-5 claude-sonnet-5 claude-fable-5.1}"
FIX_ROUNDS="${RADAR_FIX_ROUNDS:-2}"        # validator findings handed back to the drafter
REVIEW_ROUNDS="${RADAR_REVIEW_ROUNDS:-3}"  # bounded revisions after a rejection
DAY="${RADAR_RUN_DAY:-}"
DRY_RUN=0

while [ $# -gt 0 ]; do
  case "$1" in
    --date) DAY="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    *) echo "unknown argument: $1" >&2; exit 1 ;;
  esac
done

# One job at a time in the shared clone. Under cron, radar-run already holds the lock.
if [ -z "${RADAR_LOCK_HELD:-}" ]; then
  LOCK="${RADAR_LOCK:-${XDG_STATE_HOME:-$HOME/.local/state}/pairadar/radar.lock}"
  mkdir -p "$(dirname "$LOCK")"
  exec 9>>"$LOCK"
  flock -n 9 || { echo "another radar job holds $LOCK; run this when it has finished" >&2; exit 1; }
fi

if [ ! -d "$REPO_DIR/.git" ]; then
  printf '[notes] cloning %s into %s\n' "$CLONE_URL" "$REPO_DIR"
  git clone --quiet "$CLONE_URL" "$REPO_DIR"
fi
cd "$REPO_DIR"
DAY="$(python3 scripts/job_schedule.py --date "$DAY")"
NOTES="data/notes/${DAY}.json"
FEEDBACK_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/pairadar/feedback"
FEEDBACK="$FEEDBACK_DIR/notes-${DAY}.txt"
mkdir -p "$FEEDBACK_DIR"
log() { printf '[notes %s] %s\n' "$(date -u +%H:%M:%S)" "$*"; }

PREVIEW=""
LOGFILE=""
REVIEW=""
cleanup() {
  [ -n "$PREVIEW" ] && rm -rf "$PREVIEW"
  [ -n "$LOGFILE" ] && rm -f "$LOGFILE"
  [ -n "$REVIEW" ] && rm -f "$REVIEW"
  return 0
}
trap cleanup EXIT

# -z survives paths with spaces and rename entries; awk on porcelain does not.
changed_paths() {
  git status --porcelain=v1 -z --untracked-files=all |
    tr '\0' '\n' | sed -e 's/^...//' -e '/^$/d' | sort | tr '\n' ' ' | sed 's/ *$//'
}
# Detection covers the whole tree, so recovery has to as well: a stray untracked file
# left behind would otherwise become tomorrow's blocker.
restore_tree() {
  git checkout -- . 2>/dev/null || true
  git clean -qfdx
}
# One model call, bounded, its output kept in a file and its tail shown. A call that
# hangs must not hold the lock all night, and one that fails must say so: under
# pipefail a failed `kiro-cli | tee | tail` ended the script without a line of its own.
ask() {
  local out="$1"; shift
  local status=0 call_log diagnostic_log
  call_log="$(mktemp /tmp/radar-agent-call-XXXXXX.log)"
  diagnostic_log="$(mktemp /tmp/radar-agent-diagnostic-XXXXXX.log)"
  timeout --kill-after=30 "$AGENT_TIMEOUT" kiro-cli chat --agent-engine v1 --no-interactive "$@" >"$call_log" 2>"$diagnostic_log" || status=$?
  cat "$diagnostic_log" >>"$out"
  cat "$call_log" >>"$out"
  if grep -Eiq 'failed to set model|needs upgrading|using ["\x27]?default|Method not found' "$diagnostic_log"; then
    log "agent/model selection was not honored; rejecting this call"
    status=1
  fi
  sed -e 's/\x1b\[[0-9;]*m//g' "$call_log" | tail -20
  rm -f "$call_log" "$diagnostic_log"
  case "$status" in
    0) return 0 ;;
    124|137) log "kiro-cli was still running after ${AGENT_TIMEOUT}; stopped it" ;;
    *) log "kiro-cli exited ${status}" ;;
  esac
  return 1
}

command -v kiro-cli >/dev/null || { log "kiro-cli is not on PATH"; exit 1; }

# cron does not inherit an interactive shell's environment. Keep the key in a file
# only this account can read; the script never writes it anywhere.
ENV_FILE="${RADAR_ENV_FILE:-$HOME/.config/pairadar/env}"
if [ -z "${KIRO_API_KEY:-}" ] && [ -r "$ENV_FILE" ]; then
  set -a
  # shellcheck source=/dev/null
  . "$ENV_FILE"
  set +a
fi
if [ -z "${KIRO_API_KEY:-}" ] && ! kiro-cli whoami >/dev/null 2>&1; then
  log "no credentials: set KIRO_API_KEY in $ENV_FILE (chmod 600) or run kiro-cli login"
  exit 1
fi

# Start from a known state. Refusing on a dirty tree would let one interrupted run
# silently stop every following night, and in a dedicated clone leftovers carry no
# information worth keeping.
if [ -n "$(changed_paths)" ]; then
  log "clearing leftovers from an interrupted run"
fi
git fetch --quiet origin
git reset --hard --quiet HEAD
git clean -qfd
git checkout --quiet -B "$BRANCH_BASE" "origin/${BRANCH_BASE}"
git reset --hard --quiet "origin/${BRANCH_BASE}"
git clean -qfdx

if [ -f "$NOTES" ]; then
  rm -f "$FEEDBACK"
  log "$NOTES already exists on ${BRANCH_BASE}; nothing to draft"
  exit 0
fi
if [ ! -f "radar/daily/${DAY}.zh.md" ]; then
  log "no published radar for ${DAY} yet (the 07:40 Singapore run comes first); nothing to draft"
  exit 0
fi
# Resume a previously checked PR; on a real failure the model gets a fresh attempt.
if [ "$DRY_RUN" = "0" ]; then
  PENDING="$(gh pr list --head "notes/${DAY}" --state open --json number -q '.[0].number')"
  if [ -n "$PENDING" ]; then
    PENDING_HEAD="$(gh pr view "$PENDING" --json headRefOid -q .headRefOid)"
    if python3 scripts/agent_pipeline.py finish --repo noteflowai/physical-ai-radar \
      --pr "$PENDING" --head "$PENDING_HEAD" --workflow CI --workflow pages-build-deployment \
      --url https://noteflowai.github.io/physical-ai-radar/ \
      --output "$HOME/.local/state/pairadar/publications/notes-${DAY}.json"; then
      log "resumed and published #${PENDING}"
      exit 0
    fi
    log "the pending PR did not pass; rebuilding through validation and independent review"
  fi
fi

# `agent list` prints to stderr, and capturing it through `| grep -q` would also
# trip pipefail via SIGPIPE. Capture both streams into a variable instead.
AGENTS="$(kiro-cli agent list 2>&1 || true)"
for required in "$AGENT" "$REVIEWER"; do
  case "$AGENTS" in
    *"$required"*) : ;;
    *) log "agent '${required}' is not defined on ${BRANCH_BASE}; refusing to fall back to the default agent"; exit 1 ;;
  esac
  # The agent files carry the write limits. A malformed one must stop the night before
  # the first model call, not be discovered by what the agent was allowed to do.
  if ! VALID="$(kiro-cli agent validate --path ".kiro/agents/${required}.json" 2>&1)"; then
    log "agent '${required}' does not validate: $(printf '%s' "$VALID" | tail -3 | tr '\n' ' ')"
    exit 1
  fi
done

# ask_first OUT "MODELS" "SKIP" ARGS...: the first model on the list, and not in SKIP,
# that answers; its name is left in USED. A failed attempt may leave a half-written
# draft, so the notes file goes back to how it was before each retry.
USED=""
ask_first() {
  local out="$1" models="$2" skip="$3" model tried="" saved=""
  shift 3
  if [ -f "$NOTES" ]; then saved="$(mktemp)"; cp "$NOTES" "$saved"; fi
  USED=""
  for model in $models; do
    case " $tried $skip " in *" $model "*) continue ;; esac
    tried="$tried $model"
    log "asking ${model}"
    if ask "$out" --model "$model" "$@"; then USED="$model"; break; fi
    if [ -n "$saved" ]; then cp "$saved" "$NOTES"; else rm -f "$NOTES"; fi
  done
  [ -n "$saved" ] && rm -f "$saved"
  [ -n "$USED" ] && return 0
  log "no model answered (tried:${tried:- none})"
  return 1
}
# Every model that wrote any part of the draft, in order. The reviewer may be none of them.
DRAFTERS=""
add_drafter() {
  case " $DRAFTERS " in *" $USED "*) ;; *) DRAFTERS="${DRAFTERS:+$DRAFTERS }$USED" ;; esac
}
# Granular trust only: --trust-all-tools would bypass the agent's write path limits.
draft() {
  ask_first "$LOGFILE" "$DRAFTERS $MODELS" "" --agent "$AGENT" --effort "$EFFORT" \
    --trust-tools=read,grep,glob,write "$1" || return 1
  add_drafter
  local changed
  changed="$(changed_paths)"
  if [ -n "$changed" ] && [ "$changed" != "$NOTES" ]; then
    log "unexpected files touched: ${changed}; reverting"
    return 1
  fi
}
# The validator's findings go back to the drafter as its next instruction. On 09-22 a
# stray comma and on 09-23 two invented figures each cost the night, and each fix was
# one sentence long.
correct() {
  local round=0 problems
  while ! problems="$(python3 -m pairadar.notes check "$DAY")"; do
    if [ "$round" -ge "$FIX_ROUNDS" ]; then
      log "the draft still fails the validator after ${FIX_ROUNDS} corrections:"
      printf '%s\n' "$problems"
      return 1
    fi
    round=$((round + 1))
    log "correction ${round}/${FIX_ROUNDS}; the validator found:"
    printf '%s\n' "$problems"
    draft "The validator rejected ${NOTES}. Fix exactly these problems in that file and keep every other note as it is. Lines marked (optional) are suggestions only.
${problems}" || return 1
  done
  [ -z "$problems" ] || printf '%s\n' "$problems"
  log "the validator accepts ${NOTES}"
}
# The deterministic half, in the tree the pull request will carry: the suite, an
# offline render, and the reader-facing pages rebuilt around the notes, naming the
# models that actually wrote them. stdout is the pipeline talking to itself; the
# verdict and any failure go to stderr, so dropping stdout keeps this log about
# tonight's run. Called outside any condition, so `set -e` still applies inside.
prepare() {
  python3 -m pairadar.notes stamp "$DAY" --drafter "${DRAFTERS// /, }"
  if ! python3 -m unittest discover -s tests >/dev/null; then
    log "the suite rejects the draft; reverting"
    restore_tree
    exit 1
  fi
  [ -n "$PREVIEW" ] || PREVIEW="$(mktemp -d "/tmp/radar-notes-preview-${DAY}-XXXXXX")"
  python3 -m pairadar --offline --out "$PREVIEW" --date "$DAY" >/dev/null

  # The day's pages were rendered before these notes existed. Rebuild them from the
  # published snapshot -- no fetch, same picks. Skipped when the snapshot predates
  # stored excerpts, because the rebuilt pages would silently lose their source quotes.
  if python3 -c "import json,sys; picks=json.load(open('radar/latest.json'))['picked']; sys.exit(0 if picks and any(p.get('summary') for p in picks) else 1)"; then
    log "re-rendering ${DAY} from the published snapshot"
    python3 -m pairadar --rerender --date "$DAY"
    RENDERED="$(changed_paths)"
    # The landing pages quote the day's lines too, so they are rebuilt with it.
    EXPECTED="$(printf '%s\n' "$NOTES" README.en.md README.ja.md README.md \
      index.html en/index.html ja/index.html \
      "radar/daily/${DAY}.en.md" "radar/daily/${DAY}.ja.md" "radar/daily/${DAY}.zh.md" |
      sort | tr '\n' ' ' | sed 's/ *$//')"
    if [ "$RENDERED" != "$EXPECTED" ]; then
      log "re-render touched unexpected files: ${RENDERED}; reverting"
      restore_tree
      exit 1
    fi
    python3 -m unittest discover -s tests >/dev/null
  else
    log "snapshot has no stored excerpts; leaving the published pages untouched"
  fi
}

LOGFILE="$(mktemp "/tmp/radar-notes-${DAY}-XXXXXX.log")"
REVIEW="$(mktemp "/tmp/radar-review-${DAY}-XXXXXX.log")"
log "drafting notes for ${DAY}"
PRIOR_FINDINGS=""
if [ -f "$FEEDBACK" ]; then PRIOR_FINDINGS="$(cat "$FEEDBACK")"; fi
if ! draft "Draft today's per-item analysis for ${DAY} and write it to ${NOTES}. Follow your agent instructions exactly. Use the quoted evidence and signals visible on radar/daily/${DAY}.zh.md, radar/daily/${DAY}.en.md and radar/daily/${DAY}.ja.md. Longer stored summaries are not additional evidence for this task.
Prior review findings from an interrupted attempt (avoid repeating these problems):
${PRIOR_FINDINGS}"; then
  log "no draft for ${DAY}"
  restore_tree
  exit 1
fi
if [ -z "$(changed_paths)" ]; then
  log "the agent wrote nothing; stopping without a pull request"
  exit 0
fi
if ! correct; then
  log "discarding the draft"
  restore_tree
  exit 1
fi

# A rejection is a finding like any other: within the correction budget, the draft goes
# back through the validator and the suite, and a reviewer judges it afresh.
REVISION=0
while :; do
  prepare
  log "reviewing ${DAY} with ${REVIEWER}"
  : >"$REVIEW"
  if ! ask_first "$REVIEW" "$REVIEW_MODELS" "$DRAFTERS" --agent "$REVIEWER" --effort "$EFFORT" \
    --trust-tools=read,grep,glob \
    "Review ${NOTES} against radar/daily/${DAY}.zh.md, radar/daily/${DAY}.en.md and radar/daily/${DAY}.ja.md. End with APPROVE or REJECT as instructed."; then
    log "no review by a model other than the drafter's (${DRAFTERS}); discarding the draft"
    restore_tree
    exit 1
  fi
  REVIEW_MODEL="$USED"
  VERDICT="$(sed -e 's/\x1b\[[0-9;]*m//g' "$REVIEW" | grep -oE '^(APPROVE|REJECT:.*)$' | tail -1 || true)"
  case "$VERDICT" in
    APPROVE) log "review: approved by ${REVIEW_MODEL}"; break ;;
    REJECT:*) ;;
    *) log "review produced no verdict; discarding the draft"; restore_tree; exit 1 ;;
  esac
  python3 - "$REVIEW" "$FEEDBACK" <<'PY'
from pathlib import Path
import re, sys
source, target = map(Path, sys.argv[1:])
text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", source.read_text())
temporary = target.with_suffix(".tmp")
temporary.write_text(text[-12000:])
temporary.replace(target)
PY
  if [ "$REVISION" -ge "$REVIEW_ROUNDS" ]; then
    log "review: ${VERDICT}; discarding the draft"
    restore_tree
    exit 1
  fi
  REVISION=$((REVISION + 1))
  log "review: ${VERDICT}; revision ${REVISION}/${REVIEW_ROUNDS}"
  # Drop the rebuilt pages; the draft itself is untracked and stays.
  git checkout -- .
  if ! draft "The reviewer rejected ${NOTES}. Review findings:
$(cat "$FEEDBACK")
Revise that file to address every listed unsupported claim in all three languages. Recheck the published page excerpts and keep unaffected notes as they are." || ! correct; then
    log "discarding the draft"
    restore_tree
    exit 1
  fi
done
python3 -m pairadar.notes stamp "$DAY" --drafter "${DRAFTERS// /, }" \
  --reviewer "$REVIEWER" --reviewer-model "$REVIEW_MODEL"
python3 -m unittest discover -s tests >/dev/null

if [ "$DRY_RUN" = "1" ]; then
  log "dry run: keeping $NOTES in the working tree, no branch, no pull request"
  exit 0
fi

# -B and --force-with-lease so a second attempt on the same day updates its branch
# instead of dying on an existing one.
BRANCH="notes/${DAY}"
git checkout -q -B "$BRANCH"
git add -A
git commit -q -m "notes: drafted per-item analysis for ${DAY}

Drafted on the Tokyo workstation by ${AGENT} (${DRAFTERS// /, }) via kiro-cli and
approved by ${REVIEWER} (${REVIEW_MODEL}), after pairadar.notes check and the full
test suite accepted it. Every line is labelled as a draft in the rendered pages;
titles and numbers are untouched. Merged automatically only after CI passes."
git push -q -u --force-with-lease origin "$BRANCH"
if gh pr list --head "$BRANCH" --state open --json number -q '.[0].number' | grep -q .; then
  log "updated the open pull request for ${BRANCH}"
else
gh pr create --base main --head "$BRANCH" \
  --title "notes: drafted per-item analysis for ${DAY}" \
  --body-file <(printf '%s\n\n```\n%s\n```\n\n%s\n' \
    "Per-item analysis drafted for ${DAY} by \`${AGENT}\` (${DRAFTERS// /, }) and approved by \`${REVIEWER}\` (${REVIEW_MODEL}) on the Tokyo workstation, outside GitHub Actions." \
    "$(sed -e 's/\x1b\[[0-9;]*m//g' "$LOGFILE" | tail -25)" \
    "The agent can write only under \`data/notes/\`. This script checked that nothing else changed, ran \`pairadar.notes check\` (schema, keys, all three languages, no figure the page lacks) and the full test suite, and had a model other than the drafter's review it. Rendered pages label every drafted line and name the drafter. No human review is claimed; the controller merges only the checked commit and verifies publication.")
fi
PR="$(gh pr list --head "$BRANCH" --state open --json number -q '.[0].number')"
log "pull request #${PR} opened for ${DAY}; waiting for checks"
HEAD_SHA="$(git rev-parse HEAD)"
python3 scripts/agent_pipeline.py finish --repo noteflowai/physical-ai-radar \
  --pr "$PR" --head "$HEAD_SHA" --workflow CI --workflow pages-build-deployment \
  --url https://noteflowai.github.io/physical-ai-radar/ \
  --output "$HOME/.local/state/pairadar/publications/notes-${DAY}.json"
log "published #${PR} for ${DAY} after CI and public readback"
rm -f "$FEEDBACK"
