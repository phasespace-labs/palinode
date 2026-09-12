#!/usr/bin/env bash
# palinode-user-prompt-submit.sh — per-turn implicit recall.
#
# Fires on every UserPromptSubmit, BEFORE the model sees the prompt. Two
# recall channels, both fail-silent:
#
#   1. POST /check-triggers — prospective triggers (palinode_trigger). This
#      hook is the delivery mechanism that machinery was waiting for: the
#      server matches the prompt against registered triggers and honors
#      per-trigger cooldowns, so firings are self-limiting. Fired files are
#      fetched via GET /read and injected (bounded).
#   2. POST /search — strict-threshold hybrid search over the prompt text,
#      rendered as compact snippets. Deliberately conservative defaults
#      (few results, tight snippets, high threshold): this runs every
#      prompt, and injected bytes live in the conversation for the rest of
#      the session.
#
# The output is additionalContext, which Claude Code adds to the
# CONVERSATION — not the system prompt. That placement is load-bearing:
# Anthropic's prompt cache is a strict prefix match, so per-turn content in
# the system prompt would invalidate the cached prefix every turn. This
# hook is cache-safe by construction (ADR-019).
#
# Fail-silent by design — never block a prompt. API down, jq missing,
# short prompt → no output, exit 0. Explicit recall (palinode_search) is
# unaffected either way.
#
# Tuning (env):
#   PALINODE_HOOK_RECALL_MAX_RESULTS   search hits injected (default 3; 0 disables search channel)
#   PALINODE_HOOK_RECALL_THRESHOLD     search similarity floor (default 0.5 —
#                                      raw cosine, the calibrated api_threshold
#                                      tier; see SearchConfig's measured table)
#   PALINODE_HOOK_RECALL_TRIGGERS      1/0 — trigger channel on/off (default 1)
#   PALINODE_HOOK_RECALL_MIN_CHARS     skip prompts shorter than this (default 12)
#   PALINODE_HOOK_RECALL_MAX_CHARS     total injection ceiling (default 3000)
#   PALINODE_HOOK_RECALL_TIMEOUT       per-curl max seconds (default 4)
#
# Install:
#   1. Copy to .claude/hooks/palinode-user-prompt-submit.sh
#   2. chmod +x .claude/hooks/palinode-user-prompt-submit.sh
#   3. Register in .claude/settings.json — see ./settings.json in this dir.
#
# Or just run: `palinode init` — it installs all of this for you.

set -euo pipefail

# No jq → no way to parse the hook payload or build JSON. Bail silently.
command -v jq >/dev/null 2>&1 || exit 0

PALINODE_API="${PALINODE_API_URL:-http://localhost:6340}"
HOOK_TIMEOUT="${PALINODE_HOOK_RECALL_TIMEOUT:-4}"
MAX_RESULTS="${PALINODE_HOOK_RECALL_MAX_RESULTS:-3}"
# Raw-cosine floor, NOT the rank score shown per hit. Calibrated in
# SearchConfig against real bge-m3 (54 pairs): true matches clear 0.4 at
# 100%, 0.5 at 98%, 0.6 at only 74%, 0.7 at only 28% — an earlier 0.75
# default made this channel silently dead. 0.5 is the measured elbow:
# full recall with zero nonsense-query passthrough on a live store.
THRESHOLD="${PALINODE_HOOK_RECALL_THRESHOLD:-0.5}"
TRIGGERS_ON="${PALINODE_HOOK_RECALL_TRIGGERS:-1}"
MIN_CHARS="${PALINODE_HOOK_RECALL_MIN_CHARS:-12}"
MAX_CHARS="${PALINODE_HOOK_RECALL_MAX_CHARS:-3000}"
# Per-fired-trigger content cap and max fired triggers injected per prompt.
TRIGGER_READ_CHARS=1200
TRIGGER_MAX_FIRED=2

# Optional bearer auth (PALINODE_API_TOKEN) — same idiom as the session
# hooks; ${AUTH[@]+…} is the bash-3.2-safe empty-array expansion.
AUTH=()
if [ -n "${PALINODE_API_TOKEN:-}" ]; then
  AUTH=(-H "Authorization: Bearer ${PALINODE_API_TOKEN}")
fi

INPUT=$(cat)
PROMPT=$(echo "$INPUT" | jq -r '.prompt // empty')

# Trivial-prompt gate: "yes", "ok", "continue" carry no recall signal, and
# this hook runs on every prompt — skip cheap, skip early.
[ "${#PROMPT}" -ge "$MIN_CHARS" ] || exit 0

# Dry-run: print what would happen, touch nothing.
if [ "${PALINODE_HOOK_DRYRUN:-0}" = "1" ]; then
  echo "[palinode-user-prompt-submit DRYRUN] would POST ${PALINODE_API}/check-triggers and /search (limit=${MAX_RESULTS}, threshold=${THRESHOLD}) for prompt of ${#PROMPT} chars"
  exit 0
fi

QUERY_PAYLOAD=$(jq -n --arg q "$PROMPT" '{query: $q}')

SECTIONS=""

# ── Channel 1: prospective triggers ──────────────────────────────────────
if [ "$TRIGGERS_ON" = "1" ]; then
  FIRED=$(curl -s -f \
    -X POST "${PALINODE_API}/check-triggers" \
    ${AUTH[@]+"${AUTH[@]}"} \
    -H "Content-Type: application/json" \
    -d "$QUERY_PAYLOAD" \
    --connect-timeout 1 \
    --max-time "${HOOK_TIMEOUT}" 2>/dev/null) || FIRED=""

  if [ -n "$FIRED" ]; then
    FILES=$(echo "$FIRED" | jq -r --argjson max "$TRIGGER_MAX_FIRED" '
      if type == "array" then .[:$max] | .[].memory_file else empty end' 2>/dev/null) || FILES=""
    for f in $FILES; do
      BODY=$(curl -s -f -G \
        ${AUTH[@]+"${AUTH[@]}"} \
        --data-urlencode "file_path=$f" \
        "${PALINODE_API}/read" \
        --connect-timeout 1 \
        --max-time "${HOOK_TIMEOUT}" 2>/dev/null \
        | jq -r '.content // empty' 2>/dev/null) || BODY=""
      if [ -n "$BODY" ]; then
        SECTIONS="${SECTIONS}
### Trigger fired: ${f}
${BODY:0:${TRIGGER_READ_CHARS}}
"
      fi
    done
  fi
fi

# ── Channel 2: strict-threshold search ───────────────────────────────────
if [ "$MAX_RESULTS" -gt 0 ]; then
  SEARCH_PAYLOAD=$(jq -n --arg q "$PROMPT" \
    --argjson limit "$MAX_RESULTS" --argjson thr "$THRESHOLD" \
    '{query: $q, limit: $limit, threshold: $thr, max_chars: 300}')
  HITS=$(curl -s -f \
    -X POST "${PALINODE_API}/search" \
    ${AUTH[@]+"${AUTH[@]}"} \
    -H "Content-Type: application/json" \
    -d "$SEARCH_PAYLOAD" \
    --connect-timeout 1 \
    --max-time "${HOOK_TIMEOUT}" 2>/dev/null) || HITS=""

  if [ -n "$HITS" ]; then
    # The API returns a bare array; `{results: [...]}` is accepted for
    # forward-compat. The type check must come FIRST: `.results` on an array
    # is a hard jq ERROR (not null), so `.results // .` dies on the real
    # response shape and fail-open turns the crash into permanent silence.
    LINES=$(echo "$HITS" | jq -r '
      def fmt2: (. * 100 | round) as $c
        | (($c / 100) | floor | tostring) + "." + ((($c % 100) + 100 | tostring)[1:]);
      def describe:
        if (has("raw_score") | not) then "rank " + ((.score // 0) | fmt2)
        elif .raw_score == null then "keyword match, rank " + ((.score // 0) | fmt2)
        else ((.raw_score * 100 | round | tostring) + "% match") end;
      (if type == "object" then (.results // []) else . end) as $r
      | if ($r | type) == "array" and ($r | length) > 0 then
          $r | map("- [" + (.rel_path // .file_path // "?") + "] ("
                   + describe + ") "
                   + ((.snippet // .content // "") | gsub("\n"; " ")))
             | join("\n")
        else empty end' 2>/dev/null) || LINES=""
  # `score` is the post-fusion RANK value: the top hit reads ~100% even for an
  # irrelevant query. `raw_score` is the cosine the THRESHOLD knob filters on,
  # so showing the knob's own scale is what makes the lever tunable from what
  # the user sees. The two missing cases are not the same. A null raw_score is
  # a BM25-only hit the ranker marked, and it has no similarity to report, so
  # none is claimed. An absent raw_score is a pre-0.12 server, where the arm is
  # unknown and the rank is all that can be said. Same three cases as
  # describe_match in palinode/core/scoring.py.
    if [ -n "$LINES" ]; then
      SECTIONS="${SECTIONS}
### Related memories
${LINES}
"
    fi
  fi
fi

# Nothing recalled → say nothing. Silence is the common case and must be free.
[ -n "$SECTIONS" ] || exit 0

CONTEXT="## Palinode recall (this prompt)

Retrieved from persistent memory; may be stale — verify before relying on
it. More detail: palinode_search / palinode_read.
${SECTIONS}"

# Bound total size so a pathological store can't flood the conversation.
CONTEXT="${CONTEXT:0:${MAX_CHARS}}"

jq -n --arg ctx "$CONTEXT" \
  '{hookSpecificOutput: {hookEventName: "UserPromptSubmit", additionalContext: $ctx}}'

exit 0
