#!/bin/bash
# palinode-session-start.sh — warm + inject Palinode context on session start.
#
# Fires on Claude Code SessionStart (startup and /clear by default). Two
# actions, both fail-silent:
#
#   1. POST /context/prime — warms server-side session context for this CWD
#      (ADR-012 Layer 4 + ADR-009 Layer 1). The endpoint returns the
#      scope-aware context digest; this hook discards the body and injects
#      via the /list digest below. An older server (pre-0.9.3) 404s
#      harmlessly.
#   2. GET /list?core_only=true — injects a bounded digest of core memories
#      into the session as additionalContext, with a deterministic recall
#      reminder. This is the "sessions start smart" half: grounding that does
#      not depend on the agent remembering to search.
#
# Fail-silent by design — never block session start. API down → no output,
# exit 0. The agent-side pull path (palinode_search) is unaffected either way.
#
# Install:
#   1. Copy to .claude/hooks/palinode-session-start.sh (or ~/.claude/hooks/…)
#   2. chmod +x .claude/hooks/palinode-session-start.sh
#   3. Register in .claude/settings.json — see ./settings.json in this dir.
#
# Or just run: `palinode init` — it installs all of this for you.

set -euo pipefail

# No jq → no way to parse the hook payload or build JSON. Bail silently.
command -v jq >/dev/null 2>&1 || exit 0

PALINODE_API="${PALINODE_API_URL:-http://localhost:6340}"
# SessionStart blocks the session becoming interactive — keep timeouts tight.
# This is per-curl total time; the settings.json hook timeout must exceed 2x.
HOOK_TIMEOUT="${PALINODE_HOOK_START_TIMEOUT:-8}"
# Sources to fire on. startup + clear = fresh context that needs grounding.
# resume and compact are excluded by default (prior context usually still
# carries the injection); extend via PALINODE_HOOK_START_SOURCES if you want
# re-injection after compaction, e.g. "startup clear compact".
ALLOWED_SOURCES="${PALINODE_HOOK_START_SOURCES:-startup clear}"
# Injection bounds. MAX_FILES=0 disables injection entirely (prime-only mode).
MAX_FILES="${PALINODE_HOOK_INJECT_MAX_FILES:-10}"
MAX_CHARS="${PALINODE_HOOK_INJECT_MAX_CHARS:-4000}"

# Optional bearer auth for token-protected deployments (PALINODE_API_TOKEN).
# The ${AUTH[@]+…} expansion is the bash-3.2-safe empty-array idiom (set -u).
AUTH=()
if [ -n "${PALINODE_API_TOKEN:-}" ]; then
  AUTH=(-H "Authorization: Bearer ${PALINODE_API_TOKEN}")
fi

INPUT=$(cat)
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // empty')
CWD=$(echo "$INPUT" | jq -r '.cwd // empty')
SOURCE=$(echo "$INPUT" | jq -r '.source // "startup"')

# Word-boundary match on a space-padded allowlist so substrings don't
# false-positive (same pattern as palinode-session-end.sh).
case " $ALLOWED_SOURCES " in
  *" $SOURCE "*) ;;
  *) exit 0 ;;
esac

# Dry-run: print what would happen, touch nothing.
if [ "${PALINODE_HOOK_DRYRUN:-0}" = "1" ]; then
  echo "[palinode-session-start DRYRUN] would POST ${PALINODE_API}/context/prime (cwd=${CWD}, session=${SESSION_ID}) and GET ${PALINODE_API}/list?core_only=true"
  exit 0
fi

# 1. Warm server-side session context (/context/prime — ADR-012 Layer 4 +
#    ADR-009 Layer 1). No -f: an older server (pre-0.9.3) without the
#    endpoint 404s harmlessly; only connection errors fail, and those are
#    swallowed.
PRIME_PAYLOAD=$(jq -n --arg cwd "$CWD" --arg session_id "$SESSION_ID" \
  '{cwd: $cwd, session_id: $session_id}')
curl -s -o /dev/null \
  -X POST "${PALINODE_API}/context/prime" \
  ${AUTH[@]+"${AUTH[@]}"} \
  -H "Content-Type: application/json" \
  -d "$PRIME_PAYLOAD" \
  --connect-timeout 2 \
  --max-time "${HOOK_TIMEOUT}" 2>/dev/null || true

# 2. Inject a bounded core-memory digest as session context.
if [ "$MAX_FILES" -le 0 ]; then
  exit 0
fi

CORE_JSON=$(curl -s -f \
  ${AUTH[@]+"${AUTH[@]}"} \
  "${PALINODE_API}/list?core_only=true" \
  --connect-timeout 2 \
  --max-time "${HOOK_TIMEOUT}" 2>/dev/null) || exit 0

# Build "- [file] name — summary" lines inside jq (string concatenation, no
# shell loop). /list sorts newest-first, so [:$max] keeps the freshest files.
DIGEST=$(echo "$CORE_JSON" | jq -r --argjson max "$MAX_FILES" '
  if type == "array" and length > 0 then
    .[:$max]
    | map("- [" + .file + "] " + (.name // "untitled")
          + (if (.summary // "") != "" then " — " + .summary else "" end))
    | join("\n")
  else empty end' 2>/dev/null) || exit 0

if [ -z "$DIGEST" ]; then
  exit 0
fi

CONTEXT="## Palinode memory (session start)

Persistent memory is connected. Recall details with the palinode_search /
palinode_read MCP tools — they read the live store; session notes are NOT
files in this repo.

Core memories:
${DIGEST}"

# Bound total size so a pathological store can't flood the context window.
CONTEXT="${CONTEXT:0:${MAX_CHARS}}"

jq -n --arg ctx "$CONTEXT" \
  '{hookSpecificOutput: {hookEventName: "SessionStart", additionalContext: $ctx}}'

exit 0
