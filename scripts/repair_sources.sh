#!/usr/bin/env bash
# Repair a source endpoint that stopped answering, on this machine, on a schedule.
#
# The counterpart to draft_daily_notes.sh, same shape: outbound only, nothing on
# GitHub triggers it. pairadar.health decides whether there is anything to do, so a
# quiet fortnight costs nothing. The agent may edit only data/sources.json and has no
# shell; this script validates and opens the pull request.
#
#   scripts/repair_sources.sh [--threshold N] [--dry-run]
#
# Exit codes: 0 nothing to repair or PR opened, 1 something failed loudly.
set -euo pipefail

REPO_DIR="${RADAR_REPO:-$HOME/.local/share/physical-ai-radar}"
CLONE_URL="${RADAR_CLONE_URL:-https://github.com/noteflowai/physical-ai-radar.git}"
BRANCH_BASE="${RADAR_BRANCH:-main}"
AGENT="${RADAR_AGENT:-radar-curator}"
EFFORT="${RADAR_EFFORT:-medium}"
THRESHOLD="${RADAR_THRESHOLD:-3}"
AGENT_TIMEOUT="${RADAR_AGENT_TIMEOUT:-20m}"
# Tried in order until one answers; the machine's default model has been unavailable
# for whole nights.
MODELS="${RADAR_MODELS:-claude-fable-5.1 claude-opus-5 claude-sonnet-5}"
DRY_RUN=0

while [ $# -gt 0 ]; do
  case "$1" in
    --threshold) THRESHOLD="$2"; shift 2 ;;
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
  printf '[sources] cloning %s into %s\n' "$CLONE_URL" "$REPO_DIR"
  git clone --quiet "$CLONE_URL" "$REPO_DIR"
fi
cd "$REPO_DIR"
log() { printf '[sources %s] %s\n' "$(date -u +%H:%M:%S)" "$*"; }

LOGFILE=""
cleanup() { [ -n "$LOGFILE" ] && rm -f "$LOGFILE"; return 0; }
trap cleanup EXIT

changed_paths() {
  git status --porcelain=v1 -z --untracked-files=all |
    tr '\0' '\n' | sed -e 's/^...//' -e '/^$/d' | sort | tr '\n' ' ' | sed 's/ *$//'
}
restore_tree() {
  git checkout -- . 2>/dev/null || true
  git clean -qfdx
}
# One bounded model call; see draft_daily_notes.sh.
ask() {
  local out="$1"; shift
  local status=0
  timeout --kill-after=30 "$AGENT_TIMEOUT" kiro-cli chat --no-interactive "$@" >>"$out" 2>&1 || status=$?
  sed -e 's/\x1b\[[0-9;]*m//g' "$out" | tail -20
  case "$status" in
    0) return 0 ;;
    124|137) log "kiro-cli was still running after ${AGENT_TIMEOUT}; stopped it" ;;
    *) log "kiro-cli exited ${status}" ;;
  esac
  return 1
}

command -v kiro-cli >/dev/null || { log "kiro-cli is not on PATH"; exit 1; }
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

if [ -n "$(changed_paths)" ]; then
  log "clearing leftovers from an interrupted run"
fi
git fetch --quiet origin
git checkout --quiet -B "$BRANCH_BASE" "origin/${BRANCH_BASE}"
git reset --hard --quiet "origin/${BRANCH_BASE}"
git clean -qfdx

# The gate: no model call unless a source has actually been failing.
python3 -m pairadar.health --threshold "$THRESHOLD"
if python3 -m pairadar.health --threshold "$THRESHOLD" --fail-on-struggling >/dev/null; then
  log "no source is over the threshold; nothing to repair"
  exit 0
fi
SOURCE="$(python3 -c "
import json, subprocess
verdict = json.loads(subprocess.run(['python3', '-m', 'pairadar.health', '--threshold', '${THRESHOLD}'],
                                    capture_output=True, text=True, check=True).stdout)
print(verdict['struggling'][0]['source'])")"
log "worst struggling source: ${SOURCE}"
# The branch is named by date, so an unreviewed repair used to be proposed again, as a
# new pull request, every night until somebody merged one of them.
if [ "$DRY_RUN" = "0" ] &&
   gh pr list --state open --json headRefName -q '.[].headRefName' 2>/dev/null |
     grep -q "^sources/repair-${SOURCE}-"; then
  log "a repair for ${SOURCE} is already waiting for review; nothing to do"
  exit 0
fi

AGENTS="$(kiro-cli agent list 2>&1 || true)"
case "$AGENTS" in
  *"$AGENT"*) : ;;
  *) log "agent '${AGENT}' is not defined on ${BRANCH_BASE}; refusing to fall back to the default agent"; exit 1 ;;
esac
# The agent file carries the write limits and the fetch blocklist; check it before any model call.
if ! VALID="$(kiro-cli agent validate --path ".kiro/agents/${AGENT}.json" 2>&1)"; then
  log "agent '${AGENT}' does not validate: $(printf '%s' "$VALID" | tail -3 | tr '\n' ' ')"
  exit 1
fi
# The first model on the list that answers. A failed attempt may leave a half-made
# edit, so the source list goes back to the published one before each retry.
USED=""
ask_first() {
  local out="$1" model
  shift
  for model in $MODELS; do
    log "asking ${model}"
    if ask "$out" --model "$model" "$@"; then USED="$model"; return 0; fi
    git checkout -- data/sources.json
  done
  log "no model answered (tried: ${MODELS})"
  return 1
}

LOGFILE="$(mktemp "/tmp/radar-sources-XXXXXX.log")"
# Granular trust only: --trust-all-tools would bypass the agent's write path limits.
if ! ask_first "$LOGFILE" --agent "$AGENT" --effort "$EFFORT" \
  --trust-tools=read,grep,glob,web_fetch,write \
  "The health verdict says '${SOURCE}' has stopped answering. Check whether its endpoint still returns a feed. If it moved, fix only that entry in data/sources.json. If it is merely down, change nothing. Then write your report."; then
  log "no repair proposed for ${SOURCE}"
  restore_tree
  exit 1
fi

CHANGED="$(changed_paths)"
if [ -z "$CHANGED" ]; then
  log "the agent changed nothing, which is the right answer for a transient outage"
  exit 0
fi
if [ "$CHANGED" != "data/sources.json" ]; then
  log "unexpected files touched: ${CHANGED}; reverting"
  restore_tree
  exit 1
fi

# stdout is the pipeline talking to itself; the verdict and any failure go to
# stderr, so dropping stdout keeps this log about tonight's run.
python3 -m unittest discover -s tests >/dev/null
python3 -c "import json; json.load(open('data/sources.json'))"

if [ "$DRY_RUN" = "1" ]; then
  log "dry run: keeping the edit in the working tree, no branch, no pull request"
  exit 0
fi

BRANCH="sources/repair-${SOURCE}-$(date -u +%Y%m%d)"
git checkout -q -B "$BRANCH"
git add data/sources.json
git commit -q -m "sources: repair ${SOURCE}, which stopped answering

Proposed by ${AGENT} (${USED}) via kiro-cli on the Tokyo workstation after ${SOURCE} missed
at least ${THRESHOLD} days in the health window. Endpoint verified by the agent;
confirm it yourself before merging."
git push -q -u --force-with-lease origin "$BRANCH"
if gh pr list --head "$BRANCH" --state open --json number -q '.[0].number' | grep -q .; then
  log "updated the open pull request for ${BRANCH}"
  exit 0
fi
gh pr create --base main --head "$BRANCH" \
  --title "sources: repair ${SOURCE}, which stopped answering" \
  --body-file <(printf '%s\n\n```\n%s\n```\n\n%s\n' \
    "\`pairadar.health\` reported \`${SOURCE}\` failing on at least ${THRESHOLD} days. Proposed by \`${AGENT}\` (${USED}) on the Tokyo workstation, outside GitHub Actions." \
    "$(sed -e 's/\x1b\[[0-9;]*m//g' "$LOGFILE" | tail -25)" \
    "The agent has no shell and can write only \`data/sources.json\`. This script checked that nothing else changed and ran the full test suite. Confirm the endpoint yourself before merging: a source entry must stay a public machine-readable endpoint with a weight and an evidence tag.")
log "pull request opened for ${SOURCE}"
