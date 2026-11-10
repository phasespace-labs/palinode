#!/bin/bash
# palinode-hook-version: 0.24.0
# palinode-agy-hook.sh — run a Palinode Claude Code hook under Antigravity.
#
# Antigravity's hooks hand over a different payload and read a different
# answer than Claude Code's, but the work is the same, so this adapter
# translates instead of forking the recall scripts:
#
#   in   Antigravity's stdin → the fields the Claude Code script reads:
#        workspacePaths[0] → cwd, conversationId → session_id,
#        lastUserInput → prompt
#   run  the Palinode hook script named by $1
#   out  hookSpecificOutput.additionalContext → an injectSteps step
#
# Registered in .agents/hooks.json (`palinode init --agy-hooks`):
#
#   SessionStart   palinode-agy-hook.sh …/palinode-session-start.sh
#                  /context/prime's scoped core memories, as a systemMessage
#                  step: it stays in the conversation for the whole session.
#                  Fires once, when a conversation starts, never on resume.
#   PreInvocation  palinode-agy-hook.sh …/palinode-user-prompt-submit.sh
#                  per-prompt recall. PreInvocation fires before EVERY model
#                  call, tool steps included, so recall runs only on the first
#                  call of a turn (invocationNum 0), as an ephemeralMessage
#                  step: the model still saw it after that turn's tool steps,
#                  and it is not carried into later turns, so per-prompt
#                  recall does not pile up.
#
# Verified on the agy CLI 1.4.9: both steps reach the model; hooks run with
# the cwd set to the workspace's .agents/, which is why the project comes
# from workspacePaths, not from the cwd.
#
# Fail-silent by design: no context, no jq, no script or no server → {}.

set -uo pipefail

EMPTY='{}'
command -v jq >/dev/null 2>&1 || { printf '%s\n' "$EMPTY"; exit 0; }

SCRIPT="${1:-}"
INPUT=$(cat)

# init registers absolute paths; a relative one is looked up beside this
# adapter, where init installs the scripts it pairs with.
if [ -n "$SCRIPT" ] && [ ! -f "$SCRIPT" ]; then
  SCRIPT="$(cd "$(dirname "$0")" 2>/dev/null && pwd)/$(basename "$SCRIPT")"
fi
[ -n "$SCRIPT" ] && [ -f "$SCRIPT" ] || { printf '%s\n' "$EMPTY"; exit 0; }

# Which Claude Code event the paired script answers, and the step type its
# context goes back as. Named by the script, so a mis-registered pair fails
# silent instead of answering the wrong event.
case "$(basename "$SCRIPT")" in
  palinode-session-start.sh) EVENT=SessionStart ;;
  palinode-user-prompt-submit.sh) EVENT=UserPromptSubmit ;;
  *) printf '%s\n' "$EMPTY"; exit 0 ;;
esac

if [ "$EVENT" = "UserPromptSubmit" ]; then
  # Only the first model call of a turn: later calls of the same turn follow
  # tool results, not a new prompt.
  INVOCATION=$(printf '%s' "$INPUT" | jq -r '.invocationNum // 0' 2>/dev/null) || INVOCATION=""
  [ "$INVOCATION" = "0" ] || { printf '%s\n' "$EMPTY"; exit 0; }
fi

CWD=$(printf '%s' "$INPUT" | jq -r '(.workspacePaths // [])[0] // empty' 2>/dev/null) || CWD=""
if [ -z "$CWD" ]; then
  CWD="$PWD"
  [ "$(basename "$CWD")" = ".agents" ] && CWD="$(dirname "$CWD")"
fi

PAYLOAD=$(printf '%s' "$INPUT" | jq -c --arg cwd "$CWD" --arg event "$EVENT" '{
    session_id: (.conversationId // ""),
    cwd: $cwd,
    hook_event_name: $event,
    source: "startup",
    prompt: (.lastUserInput // "")
  }' 2>/dev/null) || { printf '%s\n' "$EMPTY"; exit 0; }

OUT=$(printf '%s' "$PAYLOAD" | bash "$SCRIPT" 2>/dev/null) || OUT=""
CONTEXT=$(printf '%s' "$OUT" | jq -r '.hookSpecificOutput.additionalContext // empty' 2>/dev/null) || CONTEXT=""

if [ -z "$CONTEXT" ]; then
  printf '%s\n' "$EMPTY"
  exit 0
fi
if [ "$EVENT" = "SessionStart" ]; then
  jq -cn --arg ctx "$CONTEXT" '{injectSteps: [{systemMessage: {systemMessage: $ctx}}]}' 2>/dev/null \
    || printf '%s\n' "$EMPTY"
else
  jq -cn --arg ctx "$CONTEXT" '{injectSteps: [{ephemeralMessage: $ctx}]}' 2>/dev/null \
    || printf '%s\n' "$EMPTY"
fi
exit 0
