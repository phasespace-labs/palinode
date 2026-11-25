#!/bin/bash
# palinode-hook-version: 0.24.0
# palinode-cursor-hook.sh — run a Palinode Claude Code hook under Cursor.
#
# Cursor's hooks hand over a different payload and read a different answer
# than Claude Code's, but the work is the same, so this adapter translates
# instead of forking the recall scripts:
#
#   in   Cursor's stdin → the fields the Claude Code script reads:
#        workspace_roots[0] → cwd, conversation_id → session_id, prompt
#   run  the Palinode hook script named by $1
#   out  hookSpecificOutput.additionalContext → Cursor's additional_context
#
# Registered in .cursor/hooks.json (`palinode init --cursor-hooks` does it):
#
#   sessionStart        palinode-cursor-hook.sh …/palinode-session-start.sh
#                       /context/prime's scoped core memories. Documented,
#                       and verified to reach the model in cursor-agent
#                       (2026.10.16 and 2026.10.22), print mode included.
#   beforeSubmitPrompt  palinode-cursor-hook.sh …/palinode-user-prompt-submit.sh
#                       per-prompt recall. Cursor documents only continue and
#                       user_message for this event; additional_context was
#                       verified to reach the model as a system reminder in
#                       the interactive cursor-agent 2026.10.22. The event
#                       does not fire in `cursor-agent -p`.
#
# Fail-open by design: the answer is always {"continue": true}, with
# additional_context only when the script produced some. A missing jq,
# script or server never blocks a prompt or a session.

set -uo pipefail

ALLOW='{"continue":true}'
command -v jq >/dev/null 2>&1 || { printf '%s\n' "$ALLOW"; exit 0; }

SCRIPT="${1:-}"
INPUT=$(cat)

# Project hooks run from the project root, so the registered relative path
# resolves there. Fall back to this adapter's own directory, where init
# installs the scripts it pairs with.
if [ -n "$SCRIPT" ] && [ ! -f "$SCRIPT" ]; then
  SCRIPT="$(cd "$(dirname "$0")" 2>/dev/null && pwd)/$(basename "$SCRIPT")"
fi
[ -n "$SCRIPT" ] && [ -f "$SCRIPT" ] || { printf '%s\n' "$ALLOW"; exit 0; }

# Which Claude Code event the paired script answers. Named by the script
# rather than Cursor's event, so a mis-registered pair fails silent instead
# of answering the wrong event.
case "$(basename "$SCRIPT")" in
  palinode-session-start.sh) EVENT=SessionStart ;;
  palinode-user-prompt-submit.sh) EVENT=UserPromptSubmit ;;
  *) printf '%s\n' "$ALLOW"; exit 0 ;;
esac

# Cursor's payload has no cwd: the project is the first workspace root.
CWD=$(printf '%s' "$INPUT" | jq -r '(.workspace_roots // [])[0] // empty' 2>/dev/null) || CWD=""
[ -n "$CWD" ] || CWD="${CURSOR_PROJECT_DIR:-$PWD}"

# sessionStart fires once per new conversation (never on resume), which is
# the Claude Code "startup" source the session-start script admits.
PAYLOAD=$(printf '%s' "$INPUT" | jq -c --arg cwd "$CWD" --arg event "$EVENT" '{
    session_id: (.conversation_id // .session_id // ""),
    cwd: $cwd,
    hook_event_name: $event,
    source: "startup",
    prompt: (.prompt // "")
  }' 2>/dev/null) || { printf '%s\n' "$ALLOW"; exit 0; }

OUT=$(printf '%s' "$PAYLOAD" | bash "$SCRIPT" 2>/dev/null) || OUT=""
CONTEXT=$(printf '%s' "$OUT" | jq -r '.hookSpecificOutput.additionalContext // empty' 2>/dev/null) || CONTEXT=""

if [ -z "$CONTEXT" ]; then
  printf '%s\n' "$ALLOW"
  exit 0
fi
jq -cn --arg ctx "$CONTEXT" '{continue: true, additional_context: $ctx}' 2>/dev/null \
  || printf '%s\n' "$ALLOW"
exit 0
