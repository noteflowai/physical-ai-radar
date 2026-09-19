#!/usr/bin/env bash
# Draft the day's per-item analysis with kiro-cli, on this machine, on a schedule.
#
# Outbound only: nothing on GitHub can trigger this. The machine pulls main, asks a
# narrow agent to draft notes for today's published picks, verifies the result with
# the deterministic pipeline, and opens a pull request. It never pushes to main, and
# it never edits anything outside data/notes/.
#
#   scripts/draft_daily_notes.sh [--date YYYY-MM-DD] [--dry-run]
#
# Requirements: kiro-cli on PATH and authenticated (KIRO_API_KEY or a stored login),
# gh authenticated for the pull request. Exit codes: 0 nothing to do or PR opened,
# 1 something failed loudly.
set -euo pipefail

# A dedicated clone by default: this script resets to origin/main, which must never
# happen inside somebody's working checkout.
REPO_DIR="${RADAR_REPO:-$HOME/.local/share/physical-ai-radar}"
CLONE_URL="${RADAR_CLONE_URL:-https://github.com/noteflowai/physical-ai-radar.git}"
BRANCH_BASE="${RADAR_BRANCH:-main}"
AGENT="${RADAR_AGENT:-radar-analyst}"
EFFORT="${RADAR_EFFORT:-medium}"
DAY=""
DRY_RUN=0

while [ $# -gt 0 ]; do
  case "$1" in
    --date) DAY="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    *) echo "unknown argument: $1" >&2; exit 1 ;;
  esac
done

if [ ! -d "$REPO_DIR/.git" ]; then
  printf '[notes] cloning %s into %s\n' "$CLONE_URL" "$REPO_DIR"
  git clone --quiet "$CLONE_URL" "$REPO_DIR"
fi
cd "$REPO_DIR"
DAY="${DAY:-$(date -u +%F)}"
NOTES="data/notes/${DAY}.json"
log() { printf '[notes %s] %s\n' "$(date -u +%H:%M:%S)" "$*"; }

command -v kiro-cli >/dev/null || { log "kiro-cli is not on PATH"; exit 1; }

# cron does not inherit an interactive shell's environment. Keep the key in a file
# only this account can read; the script never writes it anywhere.
ENV_FILE="${RADAR_ENV_FILE:-$HOME/.config/pairadar/env}"
if [ -z "${KIRO_API_KEY:-}" ] && [ -r "$ENV_FILE" ]; then
  set -a; . "$ENV_FILE"; set +a
fi
if [ -z "${KIRO_API_KEY:-}" ] && ! kiro-cli whoami >/dev/null 2>&1; then
  log "no credentials: set KIRO_API_KEY in $ENV_FILE (chmod 600) or run kiro-cli login"
  exit 1
fi

# Work from published main, never from a dirty tree.
git diff --quiet && git diff --cached --quiet || { log "working tree is dirty; refusing to run"; exit 1; }
git fetch --quiet origin
git checkout --quiet -B "$BRANCH_BASE" "origin/${BRANCH_BASE}"
git reset --hard --quiet "origin/${BRANCH_BASE}"

if [ -f "$NOTES" ]; then
  log "$NOTES already exists; nothing to draft"
  exit 0
fi
if [ ! -f "radar/daily/${DAY}.zh.md" ]; then
  log "no published radar for ${DAY} yet (the 01:30 UTC run comes first); nothing to draft"
  exit 0
fi

# Capture first: `| grep -q` closes the pipe early, and under `set -o pipefail`
# the SIGPIPE from kiro-cli would read as "agent missing".
AGENTS="$(kiro-cli agent list 2>/dev/null || true)"
case "$AGENTS" in
  *"$AGENT"*) : ;;
  *) log "agent '${AGENT}' is not defined on ${BRANCH_BASE}; refusing to fall back to the default agent"; exit 1 ;;
esac

log "drafting notes for ${DAY}"
# Granular trust only: --trust-all-tools would bypass the agent's write path limits.
kiro-cli chat --no-interactive --agent "$AGENT" --effort "$EFFORT" \
  --trust-tools=read,grep,glob,write \
  "Draft today's per-item analysis for ${DAY} and write it to ${NOTES}. Follow your agent instructions exactly." \
  2>&1 | tee "/tmp/radar-notes-${DAY}.log" | sed -e 's/\x1b\[[0-9;]*m//g' | tail -20

CHANGED=$(git status --porcelain --untracked-files=all | awk '{print $2}' | tr '\n' ' ' | sed 's/ *$//')
if [ -z "$CHANGED" ]; then
  log "the agent wrote nothing; stopping without a pull request"
  exit 0
fi
if [ "$CHANGED" != "$NOTES" ]; then
  log "unexpected files touched: ${CHANGED}; reverting"
  git checkout -- . && git clean -fd data/notes
  exit 1
fi

# The deterministic half: the schema, the render and the full suite must all accept it.
python3 -m unittest discover -s tests
python3 -m pairadar --offline --out "/tmp/radar-notes-preview-${DAY}" --date "$DAY" >/dev/null
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

if [ "$DRY_RUN" = "1" ]; then
  log "dry run: keeping $NOTES in the working tree, no branch, no pull request"
  exit 0
fi

BRANCH="notes/${DAY}"
git checkout -q -b "$BRANCH"
git add "$NOTES"
git commit -q -m "notes: drafted per-item analysis for ${DAY}

Drafted on the Tokyo workstation by ${AGENT} via kiro-cli, validated against the
schema and the full test suite. Every line is labelled as a draft in the rendered
pages; titles and numbers are untouched. Review before merging."
git push -q -u origin "$BRANCH"
gh pr create --base main --head "$BRANCH" \
  --title "notes: drafted per-item analysis for ${DAY}" \
  --body-file <(printf '%s\n\n```\n%s\n```\n\n%s\n' \
    "Per-item analysis drafted for ${DAY} by \`${AGENT}\` on the Tokyo workstation, outside GitHub Actions." \
    "$(sed -e 's/\x1b\[[0-9;]*m//g' "/tmp/radar-notes-${DAY}.log" | tail -25)" \
    "The agent can write only under \`data/notes/\`. This script checked that nothing else changed, validated the schema and all three languages per item, and ran the full test suite. Rendered pages label every drafted line and name the drafter. Merge only if the analysis is right.")
log "pull request opened for ${DAY}"
rm -rf "/tmp/radar-notes-preview-${DAY}"
