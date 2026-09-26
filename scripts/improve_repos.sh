#!/usr/bin/env bash
# Outbound local maintenance; no contributor can trigger this through GitHub.
set -euo pipefail
export GIT_TERMINAL_PROMPT=0 GH_PROMPT_DISABLED=1 PIP_NO_INPUT=1
if [ -z "${RADAR_LOCK_HELD:-}" ]; then
  LOCK="${RADAR_LOCK:-${XDG_STATE_HOME:-$HOME/.local/state}/pairadar/radar.lock}"
  mkdir -p "$(dirname "$LOCK")"
  exec 9>>"$LOCK"
  flock -w "${RADAR_LOCK_WAIT:-7200}" 9 || { echo "Another scheduled job holds the lock"; exit 1; }
fi
ENV_FILE="${RADAR_ENV:-$HOME/.config/pairadar/env}"
if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck source=/dev/null
  source "$ENV_FILE"
  set +a
fi
cd "$(dirname "$0")/.."
exec python3 scripts/improve_repos.py "$@"
