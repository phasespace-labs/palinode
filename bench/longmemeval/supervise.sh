#!/bin/zsh
# Supervisor for multi-hour LongMemEval runs.
#
#   supervise.sh <out-dir> -- <command that runs bench.longmemeval.run ...>
#
# The command is started under `caffeinate -i` (macOS: no idle sleep) with
# `--resume --retry-errors` appended, and restarted when it
#   - exits without <out-dir>/status.json reaching phase=done, or
#   - stops updating status.json for STALL_MIN minutes (default 12) — the
#     heartbeat is written at every question start and end, and one question
#     never legitimately takes that long.
# Gives up after MAX_RESTARTS (default 25). Log: <out-dir>/supervise.log
set -uo pipefail
OUT="$1"; shift
[[ "$1" == "--" ]] && shift
STALL_MIN="${STALL_MIN:-12}"
STALL_S="${STALL_S:-$(( STALL_MIN * 60 ))}"   # override in seconds (tests)
POLL_S="${POLL_S:-30}"
RESTART_DELAY_S="${RESTART_DELAY_S:-20}"
MAX_RESTARTS="${MAX_RESTARTS:-25}"
mkdir -p "$OUT"
LOG="$OUT/supervise.log"
STATUS="$OUT/status.json"
CAFF=""; command -v caffeinate >/dev/null && CAFF="caffeinate -i"

log() { print -r -- "$(date '+%F %T') $*" | tee -a "$LOG" >&2; }
phase() { [[ -f "$STATUS" ]] && sed -n 's/.*"phase": *"\([a-z]*\)".*/\1/p' "$STATUS" || echo "none"; }
# Seconds since the last heartbeat, or since this attempt started if the
# heartbeat is older than the attempt (or missing) — a crash before the first
# question must count as a stall too.
age_s() {
  local hb=0
  [[ -f "$STATUS" ]] && hb=$(stat -f %m "$STATUS")
  local ref=$(( hb > started ? hb : started ))
  echo $(( $(date +%s) - ref ))
}

restarts=0
while true; do
  log "starting attempt $((restarts + 1)): $* --resume --retry-errors"
  started=$(date +%s)
  ${=CAFF} "$@" --resume --retry-errors >> "$OUT/run.log" 2>&1 &
  pid=$!
  while kill -0 "$pid" 2>/dev/null; do
    sleep "$POLL_S"
    if (( $(age_s) > STALL_S )) && [[ "$(phase)" != "done" ]]; then
      log "no heartbeat for $(( $(age_s) / 60 )) min — killing pid $pid"
      kill "$pid" 2>/dev/null; sleep 3; kill -9 "$pid" 2>/dev/null
    fi
  done
  wait "$pid"; rc=$?
  if [[ "$(phase)" == "done" && $rc -eq 0 ]]; then
    log "run complete (rc=0, phase=done)"; exit 0
  fi
  restarts=$((restarts + 1))
  if (( restarts >= MAX_RESTARTS )); then
    log "giving up after $restarts restarts (rc=$rc, phase=$(phase))"; exit 1
  fi
  log "exited rc=$rc phase=$(phase) — restarting in ${RESTART_DELAY_S}s"
  sleep "$RESTART_DELAY_S"
done
