#!/usr/bin/env bash
# Scheduled daily transfer: write queued tracks until the quota is exhausted.
#
# Designed to be safe to run when there is nothing to do — it exits quietly
# with status 0 — so a scheduler can fire it every day without special cases.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

LOG_DIR="${PLAYLISTPORT_LOG_DIR:-$REPO/.data/logs}"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/drain.log"

PY="$REPO/.venv/bin/python"
if [[ ! -x "$PY" ]]; then
  echo "$(date '+%F %T')  ERROR: no interpreter at $PY" >>"$LOG"
  exit 1
fi

OUTPUT="$(cd "$REPO" && PYTHONPATH="$REPO/src" "$PY" -m playlistport drain --commit 2>&1)"
status=$?

{
  echo "===== $(date '+%F %T %Z') ====="
  echo "$OUTPUT"
  echo "exit status: $status"
  echo
} >>"$LOG"

# The likeliest unattended failure is an expired Google refresh token, and
# recovering needs a browser. Since this runs while you are at the machine,
# say so on screen rather than only in a log nobody reads.
if [[ $status -ne 0 ]] && command -v osascript >/dev/null 2>&1; then
  message="Transfer failed — see .data/logs/drain.log"
  if grep -qiE "authoriz|invalid_grant|credential" <<<"$OUTPUT"; then
    message="Google authorization expired. Run: playlistport auth youtube"
  fi
  osascript -e "display notification \"${message}\" with title \"PlaylistPort\"" \
    >/dev/null 2>&1 || true
fi

exit "$status"
