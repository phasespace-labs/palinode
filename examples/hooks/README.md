# Palinode Claude Code hooks

Drop-in hooks that bracket a Claude Code session with Palinode: inject memory
at session start, auto-capture at session end.

## What's here

| File | What it does |
|------|--------------|
| `palinode-session-start.sh` | SessionStart hook — injects a bounded digest of `core: true` memories into the fresh session (plus a recall reminder), and warms server-side session context via `/context/prime` |
| `palinode-session-end.sh` | SessionEnd hook — captures a snapshot of the transcript to palinode-api on session exit, including `/clear`, logout, and normal exit |
| `settings.json` | The Claude Code hook registration that points at both scripts |

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
cp palinode-session-start.sh palinode-session-end.sh .claude/hooks/
chmod +x .claude/hooks/palinode-session-*.sh
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

## Tuning

Environment variables the hooks respect:

| Variable | Default | Purpose |
|----------|---------|---------|
| `PALINODE_API_URL` | `http://localhost:6340` | Where the API lives (both hooks) |
| `PALINODE_API_TOKEN` | *(unset)* | Bearer token for token-protected deployments (session-start hook) |
| `PALINODE_HOOK_MIN_MESSAGES` | `3` | Minimum user messages before capture fires (skips trivial sessions) |
| `PALINODE_HOOK_REASONS` | `clear logout prompt_input_exit other` | Space-separated SessionEnd reasons to capture on. Narrow to e.g. `"clear"` for /clear-only, or extend with `resume` / `bypass_permissions_disabled` if you want to capture those lifecycle events too |
| `PALINODE_HOOK_START_SOURCES` | `startup clear` | Space-separated SessionStart sources to fire on. Add `resume` / `compact` to re-inject after those events |
| `PALINODE_HOOK_START_TIMEOUT` | `8` | Per-request timeout (seconds) for the session-start hook. Keep tight — SessionStart blocks the session becoming interactive |
| `PALINODE_HOOK_INJECT_MAX_FILES` | `10` | Max core memories injected at session start; `0` disables injection (prime-only mode) |
| `PALINODE_HOOK_INJECT_MAX_CHARS` | `4000` | Total cap on injected context size |

## Fail-silent

Both hooks are designed to never block Claude Code. If the API is down, the
session-start hook injects nothing and the session-end capture is dropped —
both exit 0. Check `palinode status` to verify the API is reachable — and
re-run sessions that matter.
