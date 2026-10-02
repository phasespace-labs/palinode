# Palinode Claude Code hooks

Drop-in hooks that wire a Claude Code session to Palinode end to end: inject
memory at session start, recall relevant memory before each prompt, auto-capture
at session end.

## What's here

| File | What it does |
|------|--------------|
| `palinode-session-start.sh` | SessionStart hook — injects a bounded digest of `core: true` memories into the fresh session (plus a recall reminder), and warms server-side session context via `/context/prime` |
| `palinode-user-prompt-submit.sh` | UserPromptSubmit hook — per-turn implicit recall: checks prospective triggers (server-side cooldowns keep firings self-limiting) and runs a strict-threshold search over the prompt, injecting compact snippets before the model sees it |
| `palinode-session-end.sh` | SessionEnd hook — captures a snapshot of the transcript to palinode-api on session exit, including `/clear`, logout, and normal exit |
| `settings.json` | The Claude Code hook registration that points at all three scripts |

## Zero-friction install

From your project root:

```bash
palinode init
```

That scaffolds everything below into the current project — `.claude/CLAUDE.md`,
`.claude/settings.json`, the hook script, and `.mcp.json`. Idempotent; re-run with
`--force` to overwrite.

## Manual install

If you prefer to wire it up by hand:

```bash
mkdir -p .claude/hooks
cp palinode-session-start.sh palinode-user-prompt-submit.sh palinode-session-end.sh .claude/hooks/
chmod +x .claude/hooks/palinode-*.sh
cp settings.json .claude/settings.json   # or merge into an existing one
```

Make sure `palinode-api` is running (default: `http://localhost:6340`). Override
with `PALINODE_API_URL` if you run it on another host.

## Why `/clear` matters

`/clear` in Claude Code resets the conversation context. Without a hook, every
insight, decision, and bug root cause from that session vanishes. The SessionEnd
hook captures a fallback snapshot for `/clear` and a few other lifecycle
reasons, so even if you forget to call `palinode_session_end` manually, the
session isn't lost.

The hook is registered without a `matcher` field — Claude Code's hook layer
fires it on every SessionEnd reason, and the script itself filters down to the
reasons worth capturing (`clear`, `logout`, `prompt_input_exit`, `other` by
default). The script-side filter is set this way so users can adjust scope via
the `PALINODE_HOOK_REASONS` env var without editing JSON. See "Tuning" below.

For the best record, have the agent call `palinode_session_end` explicitly
*before* `/clear` runs — the hook's fallback only has the transcript to work
with, whereas the agent can synthesize a structured summary with decisions and
blockers.

## What session start injects

On `startup` and `/clear`, the SessionStart hook fetches your `core: true`
memories (`GET /list?core_only=true`) and returns them as `additionalContext` —
one line per file (`- [file] name — summary`), newest first, capped at 10 files
/ 4000 chars by default — prefixed with a deterministic reminder that recall
goes through `palinode_search` / `palinode_read`. The session starts already
knowing your standing context instead of depending on the agent remembering to
search for it.

It also POSTs `/context/prime` so the server can warm per-session ambient
context. On servers that don't have that endpoint yet, the call is a harmless
404 — the hook is forward-compatible and needs no re-install when the endpoint
ships.

Mark a memory as core with `palinode_save(..., core=true)` or by setting
`core: true` in its frontmatter.

## What per-turn recall injects

Before each prompt (skipping trivial ones under 12 chars), the UserPromptSubmit
hook does two things, both bounded and both optional:

1. **Prospective triggers** — POSTs the prompt to `/check-triggers`. Triggers
   you registered with `palinode_trigger` ("when I'm discussing deployment,
   surface this runbook") fire here, and the server's per-trigger cooldown
   means the same trigger won't re-fire every turn. Fired files are fetched
   and injected (up to 2 files, 1200 chars each).
2. **Strict search** — runs a hybrid search over the prompt text with a high
   similarity floor (0.75) and injects at most 3 results as 300-char snippets.
   Deliberately conservative: this runs on every prompt, and injected bytes
   stay in the conversation for the rest of the session.

Everything arrives as `additionalContext`, which Claude Code adds to the
**conversation** — not the system prompt. That placement is deliberate:
Anthropic's prompt cache is a strict prefix match, so per-turn content in the
system prompt would invalidate the cached prefix (tools + system + history) on
every single turn. Injected this way, the cache survives.

When nothing relevant is found, the hook says nothing — silence is the common
case and costs no tokens.

## Tuning

Environment variables the hooks respect:

| Variable | Default | Purpose |
|----------|---------|---------|
| `PALINODE_API_URL` | `http://localhost:6340` | Where the API lives (both hooks) |
| `PALINODE_API_TOKEN` | *(unset)* | Bearer token for token-protected deployments (session-start hook) |
| `PALINODE_PROJECT` | *(unset)* | This session's project slug. Sent with the session-start prime and the per-turn resolve; when unset the server resolves the project from the session's `cwd`. The server may be on another host, so it never uses its own directory |
| `PALINODE_HOOK_MIN_MESSAGES` | `3` | Minimum user messages before capture fires (skips trivial sessions) |
| `PALINODE_HOOK_REASONS` | `clear logout prompt_input_exit other` | Space-separated SessionEnd reasons to capture on. Narrow to e.g. `"clear"` for /clear-only, or extend with `resume` / `bypass_permissions_disabled` if you want to capture those lifecycle events too |
| `PALINODE_HOOK_START_SOURCES` | `startup clear` | Space-separated SessionStart sources to fire on. Add `resume` / `compact` to re-inject after those events |
| `PALINODE_HOOK_START_TIMEOUT` | `8` | Per-request timeout (seconds) for the session-start hook. Keep tight — SessionStart blocks the session becoming interactive |
| `PALINODE_HOOK_INJECT_MAX_FILES` | `10` | Max core memories injected at session start; `0` disables injection (prime-only mode) |
| `PALINODE_HOOK_INJECT_MAX_CHARS` | `4000` | Total cap on injected context size |
| `PALINODE_HOOK_RECALL_MAX_RESULTS` | `3` | Search hits injected per prompt; `0` disables the search channel |
| `PALINODE_HOOK_RECALL_THRESHOLD` | `0.5` | Similarity floor for per-turn search — see "Tuning the recall threshold" below |
| `PALINODE_HOOK_RECALL_TRIGGERS` | `1` | `0` disables the trigger channel |
| `PALINODE_HOOK_RECALL_MIN_CHARS` | `12` | Prompts shorter than this skip recall entirely |
| `PALINODE_HOOK_RECALL_MAX_CHARS` | `3000` | Total cap on per-turn injected context |
| `PALINODE_HOOK_RECALL_TIMEOUT` | `4` | Per-request timeout (seconds) for the recall hook |
| `PALINODE_HOOK_RESOLVE` | `1` | `0` routes the per-turn memory channel through plain `/search` instead of bounded resolution — the pre-resolution payload, byte for byte |
| `PALINODE_HOOK_RESOLVE_DEADLINE` | `250` | Per-turn resolution deadline in **milliseconds**. A latency budget spent on every prompt, not a failure timeout: past it the turn falls back to search rather than waiting |

### What the per-turn hook asks for

The recall hook calls `POST /resolve` first, not `/search`: one call that
returns what stands now, what replaced what, conflicts with both sides intact,
and what is explicitly unknown — already rendered. Past the deadline it falls
back to today's search hits **with a marker** (`resolution unavailable
(deadline) …`), because an unchecked hit presented with the authority of a
resolved answer is exactly what the resolution path exists to prevent.

The bundle is asked for at the room actually left (`MAX_CHARS` minus the
injection frame and any fired triggers), so the server can pack conflicts whole
instead of having them sliced by the hook's final truncation. Below ~300
characters of remaining room the channel injects nothing: silence is safe, half
a conflict is not.

## Tuning the recall threshold

The threshold is a **raw cosine-similarity floor** — the same scale as the
percentage shown on each injected hit (`- [file] (62%) …`), so what you see is
what the knob filters on. The values below aren't guesses: they come from a
measured calibration against real BGE-M3 embeddings (54 query/chunk pairs
spanning natural questions, keyword queries, and exact identifiers), confirmed
by a live sweep on a real store.

| Value | What it does |
|-------|--------------|
| `0.4` | Recalls everything plausibly relevant (100% measured recall) — but nonsense queries also surface hits. Use when you'd rather over-recall |
| `0.5` | **Default.** 98% measured recall, and the live-store elbow: full recall on relevant queries with zero hits for irrelevant ones |
| `0.6` | Strict: drops ~1 in 4 genuinely relevant memories overall, ~2 in 5 for natural-language questions |
| `0.7`+ | Near-silent: only ~28% of true matches clear 0.7. Effectively disables the channel |

If recall feels noisy, try `0.55` before `0.6` — the drop-off past `0.6` is a
cliff, not a slope. If it feels too quiet, `0.45`. Set it in your
`~/.claude/settings.json` `env` block (or per-shell) as
`PALINODE_HOOK_RECALL_THRESHOLD`.

## Fail-silent

All three hooks are designed to never block Claude Code. If the API is down,
the session-start hook injects nothing, per-turn recall stays silent, and the
session-end capture is dropped — all exit 0. Check `palinode status` to verify
the API is reachable — and re-run sessions that matter.
