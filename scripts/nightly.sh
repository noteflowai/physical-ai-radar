#!/usr/bin/env bash
# One ordered, resumable night batch, including when started by hand.
set -euo pipefail
export GIT_TERMINAL_PROMPT=0 GH_PROMPT_DISABLED=1 PIP_NO_INPUT=1
exec </dev/null
if [ -z "${RADAR_LOCK_HELD:-}" ]; then
  # The bootstrap owns radar.lock and selects the dedicated clone, including for a
  # manual invocation from a different checkout. Every stage must see that same tree.
  SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
  exec "$SCRIPT_DIR/radar-run" scripts/nightly.sh "$@"
fi
cd "$(dirname "$0")/.."
exec python3 scripts/nightly_batch.py "$@"
