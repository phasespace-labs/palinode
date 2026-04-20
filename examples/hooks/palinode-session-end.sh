#!/bin/bash
# palinode-session-end.sh — Auto-capture Claude Code sessions to Palinode.
#
# Fires on SessionEnd (including /clear, logout, exit). Reads the transcript
# from stdin JSON, extracts a minimal summary, and POSTs to palinode-api.
#
# Fail-silent by design — never block Claude Code exit. If the API is down
# we drop the capture and move on. Nightly consolidation will pick up the
# snapshot on the next pass.
#
# Install:
#   1. Copy to .claude/hooks/palinode-session-end.sh (or ~/.claude/hooks/…)
#   2. chmod +x .claude/hooks/palinode-session-end.sh
#   3. Register in .claude/settings.json — see ./settings.json in this dir.
#
# Or just run: `palinode init` — it installs all of this for you.

set -euo pipefail

PALINODE_API="${PALINODE_API_URL:-http://localhost:6340}"
MIN_MESSAGES="${PALINODE_HOOK_MIN_MESSAGES:-3}"

INPUT=$(cat)
TRANSCRIPT_PATH=$(echo "$INPUT" | jq -r '.transcript_path // empty')
CWD=$(echo "$INPUT" | jq -r '.cwd // empty')
SOURCE_REASON=$(echo "$INPUT" | jq -r '.source // .reason // "other"')

# No transcript → nothing to capture.
if [ -z "$TRANSCRIPT_PATH" ] || [ ! -f "$TRANSCRIPT_PATH" ]; then
  exit 0
fi

# Claude Code transcript format (JSONL):
#   user:      {type: "user", message: {role: "user", content: "text"}}
#   assistant: {type: "assistant", message: {content: [{type: "text", text: "..."}]}}
MSG_COUNT=$(jq -r 'select(.type == "user") | .message.content // empty' \
  "$TRANSCRIPT_PATH" 2>/dev/null | grep -c '.' 2>/dev/null || echo "0")

# Skip trivial sessions (few messages = not worth a memory).
if [ "$MSG_COUNT" -lt "$MIN_MESSAGES" ]; then
  exit 0
fi

PROJECT=$(basename "$CWD" 2>/dev/null || echo "unknown")
FIRST_PROMPT=$(jq -r 'select(.type == "user") | .message.content // empty' \
  "$TRANSCRIPT_PATH" 2>/dev/null | head -1 | cut -c1-200)

SUMMARY="Auto-captured (${SOURCE_REASON}, ${MSG_COUNT} messages). Topic: ${FIRST_PROMPT}"

curl -sS -o /dev/null \
  -X POST "${PALINODE_API}/session-end" \
  -H "Content-Type: application/json" \
  -d "$(jq -n \
    --arg summary "$SUMMARY" \
    --arg project "$PROJECT" \
    --arg source "claude-code-hook" \
    '{summary: $summary, project: $project, source: $source, decisions: [], blockers: []}'
  )" \
  --connect-timeout 5 \
  --max-time 10 || true

exit 0
