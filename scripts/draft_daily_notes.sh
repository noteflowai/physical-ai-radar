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
# Timing constraint: the notes are drafted for the current UTC date, whose radar is
# published at 01:40 UTC. Schedule this after that, not near it. At 02:30 Asia/Singapore
# (18:30 UTC) the day's radar is already about seventeen hours old, which is the intent.
#
# Requirements: kiro-cli on PATH and authenticated (KIRO_API_KEY or a stored login),
# gh authenticated for the pull request. Exit codes: 0 nothing to do or PR opened,
# 1 something failed loudly.
set -euo pipefail

# A dedicated clone by default: this script resets hard, which must never happen
# inside somebody's working checkout.
REPO_DIR="${RADAR_REPO:-$HOME/.local/share/physical-ai-radar}"
CLONE_URL="${RADAR_CLONE_URL:-https://github.com/noteflowai/physical-ai-radar.git}"
BRANCH_BASE="${RADAR_BRANCH:-main}"
AGENT="${RADAR_AGENT:-radar-analyst}"
REVIEWER="${RADAR_REVIEWER:-radar-reviewer}"
EFFORT="${RADAR_EFFORT:-medium}"
AGENT_TIMEOUT="${RADAR_AGENT_TIMEOUT:-20m}"
DAY=""
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
DAY="${DAY:-$(date -u +%F)}"
NOTES="data/notes/${DAY}.json"
log() { printf '[notes %s] %s\n' "$(date -u +%H:%M:%S)" "$*"; }

PREVIEW=""
LOGFILE=""
cleanup() {
  [ -n "$PREVIEW" ] && rm -rf "$PREVIEW"
  [ -n "$LOGFILE" ] && rm -f "$LOGFILE"
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
  local status=0
  timeout --kill-after=30 "$AGENT_TIMEOUT" kiro-cli chat --no-interactive "$@" >"$out" 2>&1 || status=$?
  sed -e 's/\x1b\[[0-9;]*m//g' "$out" | tail -20
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
git checkout --quiet -B "$BRANCH_BASE" "origin/${BRANCH_BASE}"
git reset --hard --quiet "origin/${BRANCH_BASE}"
git clean -qfdx

if [ -f "$NOTES" ]; then
  log "$NOTES already exists on ${BRANCH_BASE}; nothing to draft"
  exit 0
fi
if [ ! -f "radar/daily/${DAY}.zh.md" ]; then
  log "no published radar for ${DAY} yet (the 01:40 UTC run comes first); nothing to draft"
  exit 0
fi
# A pull request left open is waiting for a human. Drafting again would spend two model
# calls to force-push over the draft they are about to read.
if [ "$DRY_RUN" = "0" ] &&
   gh pr list --head "notes/${DAY}" --state open --json number -q '.[0].number' 2>/dev/null | grep -q .; then
  log "a pull request for notes/${DAY} is already open; nothing to draft"
  exit 0
fi

# `agent list` prints to stderr, and capturing it through `| grep -q` would also
# trip pipefail via SIGPIPE. Capture both streams into a variable instead.
AGENTS="$(kiro-cli agent list 2>&1 || true)"
for required in "$AGENT" "$REVIEWER"; do
  case "$AGENTS" in
    *"$required"*) : ;;
    *) log "agent '${required}' is not defined on ${BRANCH_BASE}; refusing to fall back to the default agent"; exit 1 ;;
  esac
done

LOGFILE="$(mktemp "/tmp/radar-notes-${DAY}-XXXXXX.log")"
log "drafting notes for ${DAY}"
# Granular trust only: --trust-all-tools would bypass the agent's write path limits.
if ! ask "$LOGFILE" --agent "$AGENT" --effort "$EFFORT" \
  --trust-tools=read,grep,glob,write \
  "Draft today's per-item analysis for ${DAY} and write it to ${NOTES}. Follow your agent instructions exactly."; then
  log "no draft for ${DAY}"
  restore_tree
  exit 1
fi

CHANGED="$(changed_paths)"
if [ -z "$CHANGED" ]; then
  log "the agent wrote nothing; stopping without a pull request"
  exit 0
fi
if [ "$CHANGED" != "$NOTES" ]; then
  log "unexpected files touched: ${CHANGED}; reverting"
  restore_tree
  exit 1
fi

# Last night the agent wrote a file that stopped being JSON at line 23, and the suite
# reported it as a traceback from an unrelated test. The most likely agent failure
# deserves its own diagnosis, in one line, before anything else reads the file.
if ! DIAGNOSIS="$(python3 - "$NOTES" <<'PY' 2>&1
import json, sys
try:
    json.load(open(sys.argv[1], encoding="utf-8"))
except json.JSONDecodeError as error:
    sys.exit(f"{error.msg}, line {error.lineno} column {error.colno}")
except OSError as error:
    sys.exit(str(error))
PY
)"; then
  log "the draft is not valid JSON: ${DIAGNOSIS}; reverting"
  restore_tree
  exit 1
fi

# The deterministic half: the schema, the render and the full suite must all accept it.
# stdout is the pipeline talking to itself; the verdict and any failure go to
# stderr, so dropping stdout keeps this log about tonight's run.
python3 -m unittest discover -s tests >/dev/null
PREVIEW="$(mktemp -d "/tmp/radar-notes-preview-${DAY}-XXXXXX")"
python3 -m pairadar --offline --out "$PREVIEW" --date "$DAY" >/dev/null
python3 - "$DAY" "$NOTES" <<'PY'
import json, sys
day, path = sys.argv[1], sys.argv[2]
document = json.load(open(path, encoding="utf-8"))
assert document.get("schema") == "pairadar-notes-1", "wrong schema"
assert document.get("date") == day, "date does not match the run"
assert isinstance(document.get("notes"), dict) and document["notes"], "no notes written"
for key, note in document["notes"].items():
    missing = [lang for lang in ("zh", "en", "ja") if not note.get(lang)]
    assert not missing, f"{key}: missing {missing}"
print(f"validated {len(document['notes'])} notes for {day}")
PY

# The day's pages were rendered before these notes existed. Rebuild them from the
# published snapshot -- no fetch, same picks -- so the pull request carries the reader
# facing result. Skipped when the snapshot predates stored excerpts, because the
# rebuilt pages would silently lose their source quotes.
if python3 -c "import json,sys; picks=json.load(open('radar/latest.json'))['picked']; sys.exit(0 if picks and any(p.get('summary') for p in picks) else 1)"; then
  log "re-rendering ${DAY} from the published snapshot"
  python3 -m pairadar --rerender --date "$DAY"
  RENDERED="$(changed_paths)"
  EXPECTED="$(printf '%s\n' "$NOTES" README.en.md README.ja.md README.md \
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

log "reviewing ${DAY} with ${REVIEWER}"
REVIEW="$(mktemp "/tmp/radar-review-${DAY}-XXXXXX.log")"
if ! ask "$REVIEW" --agent "$REVIEWER" --effort "$EFFORT" \
  --trust-tools=read,grep,glob \
  "Review ${NOTES} against radar/daily/${DAY}.zh.md, radar/daily/${DAY}.en.md and radar/daily/${DAY}.ja.md. End with APPROVE or REJECT as instructed."; then
  log "no review; discarding the draft"
  rm -f "$REVIEW"
  restore_tree
  exit 1
fi
VERDICT="$(sed -e 's/\x1b\[[0-9;]*m//g' "$REVIEW" | grep -oE '^(APPROVE|REJECT:.*)$' | tail -1 || true)"
case "$VERDICT" in
  APPROVE) log "review: approved" ;;
  REJECT:*) log "review: ${VERDICT}; discarding the draft"; rm -f "$REVIEW"; restore_tree; exit 1 ;;
  *) log "review produced no verdict; discarding the draft"; rm -f "$REVIEW"; restore_tree; exit 1 ;;
esac
rm -f "$REVIEW"

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

Drafted on the Tokyo workstation by ${AGENT} via kiro-cli, validated against the
schema and the full test suite. Every line is labelled as a draft in the rendered
pages; titles and numbers are untouched. Review before merging."
git push -q -u --force-with-lease origin "$BRANCH"
if gh pr list --head "$BRANCH" --state open --json number -q '.[0].number' | grep -q .; then
  log "updated the open pull request for ${BRANCH}"
  exit 0
fi
gh pr create --base main --head "$BRANCH" \
  --title "notes: drafted per-item analysis for ${DAY}" \
  --body-file <(printf '%s\n\n```\n%s\n```\n\n%s\n' \
    "Per-item analysis drafted for ${DAY} by \`${AGENT}\` on the Tokyo workstation, outside GitHub Actions." \
    "$(sed -e 's/\x1b\[[0-9;]*m//g' "$LOGFILE" | tail -25)" \
    "The agent can write only under \`data/notes/\`. This script checked that nothing else changed, validated the schema and all three languages per item, and ran the full test suite. Rendered pages label every drafted line and name the drafter. Merge only if the analysis is right.")
PR="$(gh pr list --head "$BRANCH" --state open --json number -q '.[0].number')"
log "pull request #${PR} opened for ${DAY}; waiting for checks"
for _ in $(seq 1 30); do
  sleep 20
  STATUS="$(gh pr checks "$PR" 2>&1 || true)"
  case "$STATUS" in
    *pending*) continue ;;
    *fail*) log "checks failed on #${PR}; leaving it open for a human"; exit 1 ;;
    *) break ;;
  esac
done
case "$(gh pr checks "$PR" 2>&1 || true)" in
  *pending*) log "checks still pending on #${PR}; leaving it open"; exit 0 ;;
  *fail*) log "checks failed on #${PR}; leaving it open for a human"; exit 1 ;;
esac
gh pr merge "$PR" --merge
log "merged #${PR} for ${DAY}"
