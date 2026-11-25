#!/bin/bash
# palinode-hook-version: 0.24.1
# palinode-post-compact.sh — save Claude Code's compaction summary as a checkpoint.
#
# Fires on Claude Code PostCompact (manual /compact and auto-compaction). The
# hook input carries `compact_summary`, the summary Claude Code wrote of the
# conversation so far. This saves it as one ProjectSnapshot per session, slug
# compaction-checkpoint-<session_id>, so a long session has a recent
# checkpoint long before it reaches SessionEnd. A later compaction in the same
# session replaces it in place; git keeps the earlier versions.
#
# It writes no daily note, status line or session-end entry, and the record is
# not core memory. The summary is a model's account of the session, so it is
# saved as epistemic: inference, under a header saying that approvals and
# instructions recorded in it applied to that session only.
#
# Fail-silent by design. Gated by the same persisted capture controls as the
# SessionEnd hook: a denial or unavailable service exits before anything is
# sent, and the API rechecks a capture pause at write time.
#
# Install: `palinode init --hook` copies this to .claude/hooks/ and registers
# it on PostCompact, synchronously: Claude Code cancels an async hook.

set -euo pipefail

# No jq → no way to parse the hook payload or build JSON. Bail silently.
command -v jq >/dev/null 2>&1 || exit 0

PALINODE_API="${PALINODE_API_URL:-http://localhost:6340}"
# Max time (seconds) each curl call may run. The hook's settings.json timeout
# must be greater than this value.
HOOK_TIMEOUT="${PALINODE_HOOK_TIMEOUT:-30}"
# Longest summary body saved, in characters. A longer one is cut with a marker.
MAX_CHARS="${PALINODE_HOOK_COMPACT_MAX_CHARS:-32000}"
case "$MAX_CHARS" in
  ''|*[!0-9]*) MAX_CHARS=32000 ;;
esac

# Optional bearer auth for token-protected deployments (PALINODE_API_TOKEN).
# The ${AUTH[@]+…} expansion is the bash-3.2-safe empty-array idiom (set -u).
AUTH=()
if [ -n "${PALINODE_API_TOKEN:-}" ]; then
  AUTH=(-H "Authorization: Bearer ${PALINODE_API_TOKEN}")
fi

INPUT=$(cat)
TRANSCRIPT_PATH=$(echo "$INPUT" | jq -r '.transcript_path // empty' 2>/dev/null) || exit 0
CWD=$(echo "$INPUT" | jq -r '.cwd // empty' 2>/dev/null) || exit 0
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // empty' 2>/dev/null) || exit 0
SUMMARY_LEN=$(echo "$INPUT" | jq -r '.compact_summary // "" | length' 2>/dev/null) || exit 0

# The session id names the checkpoint, and an empty summary has nothing to keep.
[ -n "$SESSION_ID" ] || exit 0
[ "${SUMMARY_LEN:-0}" -gt 0 ] || exit 0

# This preflight intentionally precedes sending any summary text. A malformed
# or unavailable response fails closed without emitting user content.
CONTROL_PAYLOAD=$(jq -n \
  --arg cwd "$CWD" --arg source_path "$TRANSCRIPT_PATH" \
  '{action: "capture", cwd: $cwd, source_path: $source_path, automatic: true}')
CONTROL=$(curl -sS -f \
  -X POST "${PALINODE_API}/controls/check" \
  ${AUTH[@]+"${AUTH[@]}"} \
  -H "Content-Type: application/json" \
  -d "$CONTROL_PAYLOAD" \
  --connect-timeout 2 \
  --max-time "${HOOK_TIMEOUT}" 2>/dev/null) || exit 0
[ "$(echo "$CONTROL" | jq -r 'if .allowed == true then "yes" else "no" end' 2>/dev/null)" = "yes" ] || exit 0
PROJECT=$(echo "$CONTROL" | jq -r '.project // empty' 2>/dev/null) || exit 0

NOW=$(date -u +%Y-%m-%dT%H:%M:%SZ)

# The whole payload is built inside jq from the hook input, so the summary
# never passes through shell word-splitting or quoting.
PAYLOAD=$(echo "$INPUT" | jq -c \
  --arg project "$PROJECT" --arg now "$NOW" --argjson max "$MAX_CHARS" '
  # Claude Code wraps its summary in <analysis>…</analysis> and
  # <summary>…</summary> lines. A closing </summary> left at the end of the
  # content is what the API refuses as tool-envelope markup, so whole-line
  # wrapper tags become Markdown headings before the save.
  (.compact_summary | split("\n") | map(
      if test("^\\s*<analysis>\\s*$") then "## Analysis"
      elif test("^\\s*<summary>\\s*$") then "## Summary"
      elif test("^\\s*</(analysis|summary)>\\s*$") then ""
      else . end) | join("\n")) as $summary
  | .session_id as $sid
  | (.trigger // "unknown") as $trigger
  | (if ($summary | length) > $max
     then $summary[0:$max] + "\n\n[Truncated: kept \($max) of \($summary | length) characters.]"
     else $summary end) as $body
  | {
      type: "ProjectSnapshot",
      slug: ("compaction-checkpoint-" + $sid),
      title: ("Compaction checkpoint, Claude Code session " + $sid[0:8]),
      content: ("Automatic checkpoint from Claude Code. When session `\($sid)` compacted"
        + " (\($trigger), \($now)), Claude Code wrote the summary below of the"
        + " conversation up to that point. Nobody reviewed it. Approvals,"
        + " instructions and plans recorded in it applied to that session only;"
        + " they are not standing permission.\n\n---\n\n" + $body),
      epistemic: "inference",
      source: "claude-code-compaction",
      metadata: {
        session_id: $sid,
        compaction_trigger: $trigger,
        compacted_at: $now,
        harness: "claude-code"
      }
    }
  + (if $project != "" then {project: $project} else {} end)' 2>/dev/null) || exit 0

# Dry-run: print what would be POSTed and write nothing.
# PALINODE_HOOK_DRYRUN=1 to enable.
if [ "${PALINODE_HOOK_DRYRUN:-0}" = "1" ]; then
  echo "[palinode-post-compact DRYRUN] would POST ${PALINODE_API}/save"
  echo "$PAYLOAD"
  exit 0
fi

# No fallback file on failure: a 403 may mean capture was paused after the
# preflight, and a summary written to disk would outlive that decision.
printf '%s' "$PAYLOAD" | curl -sS -o /dev/null -f \
    -X POST "${PALINODE_API}/save" \
    ${AUTH[@]+"${AUTH[@]}"} \
    -H "Content-Type: application/json" \
    --data-binary @- \
    --connect-timeout 5 \
    --max-time "${HOOK_TIMEOUT}" 2>/dev/null || true

exit 0
