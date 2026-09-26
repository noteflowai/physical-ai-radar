#!/usr/bin/env bash
# One ordered, resumable night batch, including when started by hand.
set -euo pipefail
export GIT_TERMINAL_PROMPT=0 GH_PROMPT_DISABLED=1 PIP_NO_INPUT=1
exec </dev/null
if [ -z "${RADAR_LOCK_HELD:-}" ]; then
  LOCK="${RADAR_LOCK:-${XDG_STATE_HOME:-$HOME/.local/state}/pairadar/radar.lock}"
  mkdir -p "$(dirname "$LOCK")"
  exec 9>>"$LOCK"
  flock -w "${RADAR_LOCK_WAIT:-7200}" 9 || { echo "Another scheduled job holds the lock"; exit 1; }
  export RADAR_LOCK_HELD=1
fi
cd "$(dirname "$0")/.."
exec python3 scripts/nightly_batch.py "$@"
