#!/usr/bin/env bash
# Publish the day's radar from this machine.
#
# This used to be a scheduled GitHub Actions job. It moved here because the machine
# already runs the drafting and repair loops, because one scheduler is easier to
# reason about than two, and because a hosted runner needed `contents: write` to do
# it -- a token with push rights on a public repository, for a job that only ever
# commits generated files.
#
# What Actions keeps is the job hosted CI is actually good at: verifying on a clean
# machine that the committed state builds and passes. It publishes only a day this
# machine missed (daily.yml, 03:20 UTC), with a token that lives for one run.
#
#   scripts/publish_daily.sh [--limit N] [--dry-run] [--force]
#
# A day is published once. A second run on the same UTC date -- cron plus a manual
# retry, or this machine plus the Actions fallback -- used to fetch again and commit
# a new "update" that differed only in timestamps and whatever the feeds had added
# since. It now stops when main already carries the day; --force republishes it.
#
# Exit codes: 0 published or nothing changed, 1 something failed loudly.
set -euo pipefail
export GIT_TERMINAL_PROMPT=0 GH_PROMPT_DISABLED=1 PIP_NO_INPUT=1

REPO_DIR="${RADAR_REPO:-$HOME/.local/share/physical-ai-radar}"
CLONE_URL="${RADAR_CLONE_URL:-https://github.com/noteflowai/physical-ai-radar.git}"
BRANCH_BASE="${RADAR_BRANCH:-main}"
LIMIT="${RADAR_LIMIT:-8}"
DRY_RUN=0
FORCE=0

while [ $# -gt 0 ]; do
  case "$1" in
    --limit) LIMIT="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --force) FORCE=1; shift ;;
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
  printf '[publish] cloning %s into %s\n' "$CLONE_URL" "$REPO_DIR"
  git clone --quiet "$CLONE_URL" "$REPO_DIR"
fi
cd "$REPO_DIR"
DAY="$(date -u +%F)"
log() { printf '[publish %s] %s\n' "$(date -u +%H:%M:%S)" "$*"; }
verify_publication() {
  [ "$DRY_RUN" = "1" ] && return 0
  python3 scripts/agent_pipeline.py verify-commit --repo noteflowai/physical-ai-radar \
    --commit "$(git rev-parse HEAD)" --workflow CI --workflow pages-build-deployment \
    --url https://noteflowai.github.io/physical-ai-radar/ \
    --output "$HOME/.local/state/pairadar/publications/daily-${DAY}.json"
}
published_day() {
  python3 -c 'import json; print(json.load(open("radar/latest.json", encoding="utf-8")).get("date", ""))' \
    2>/dev/null || true
}

changed_paths() {
  git status --porcelain=v1 -z --untracked-files=all |
    tr '\0' '\n' | sed -e 's/^...//' -e '/^$/d' | sort | tr '\n' ' ' | sed 's/ *$//'
}

# Start from a known state: leftovers from an interrupted run carry no information.
if [ -n "$(changed_paths)" ]; then
  log "clearing leftovers from an interrupted run"
fi
git fetch --quiet origin
git reset --hard --quiet HEAD
git checkout --quiet -B "$BRANCH_BASE" "origin/${BRANCH_BASE}"
git reset --hard --quiet "origin/${BRANCH_BASE}"
git clean -qfdx

if [ "$FORCE" = "0" ] && [ "$(published_day)" = "$DAY" ]; then
  log "${BRANCH_BASE} already carries the ${DAY} radar; nothing to do (--force republishes it)"
  verify_publication
  exit 0
fi
log "generating the ${DAY} radar"
python3 -m pairadar --limit "$LIMIT"
# stdout is the pipeline talking to itself; the verdict and any failure go to
# stderr, so dropping stdout keeps this log about tonight's run.
python3 -m unittest discover -s tests >/dev/null

if [ -z "$(changed_paths)" ]; then
  log "nothing changed today"
  verify_publication
  exit 0
fi
if [ "$DRY_RUN" = "1" ]; then
  log "dry run: leaving $(changed_paths) in the working tree"
  exit 0
fi

git config user.name "physical-ai-radar publisher"
git config user.email "admin@noteflowai.com"
git add -A
git commit -q -m "radar: ${DAY} update"

# Another change can land while this runs. The output is deterministic, so catch up
# rather than merge: rebase, and if that cannot resolve, regenerate on top of main.
# Every catch-up is followed by a push: a loop that caught up and then ended threw
# its last rebase away and reported a failure it had just fixed.
attempt=1
until git push --quiet origin "$BRANCH_BASE"; do
  if [ "$attempt" -ge 3 ]; then
    log "could not publish ${DAY} after ${attempt} attempts"
    exit 1
  fi
  log "push rejected on attempt ${attempt}; catching up with origin/${BRANCH_BASE}"
  attempt=$((attempt + 1))
  git fetch --quiet origin "$BRANCH_BASE"
  if git rebase --quiet "origin/${BRANCH_BASE}"; then
    continue
  fi
  log "rebase failed; regenerating on top of the new ${BRANCH_BASE}"
  git rebase --abort || true
  git reset --hard --quiet "origin/${BRANCH_BASE}"
  if [ "$FORCE" = "0" ] && [ "$(published_day)" = "$DAY" ]; then
    log "${DAY} was published meanwhile by another run; keeping that one"
    verify_publication
    exit 0
  fi
  python3 -m pairadar --limit "$LIMIT"
  if [ -z "$(changed_paths)" ]; then
    log "the new ${BRANCH_BASE} already carries an equivalent update"
    verify_publication
    exit 0
  fi
  python3 -m unittest discover -s tests >/dev/null
  git add -A
  git commit -q -m "radar: ${DAY} update"
done
verify_publication
log "published ${DAY} on attempt ${attempt}"
exit 0
