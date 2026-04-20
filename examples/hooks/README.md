# Palinode Claude Code hooks

Drop-in hooks that auto-capture Claude Code sessions to Palinode.

## What's here

| File | What it does |
|------|--------------|
| `palinode-session-end.sh` | SessionEnd hook — captures a snapshot of the transcript to palinode-api on session exit, including `/clear`, logout, and normal exit |
| `settings.json` | The Claude Code hook registration that points at the script |

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
cp palinode-session-end.sh .claude/hooks/
chmod +x .claude/hooks/palinode-session-end.sh
cp settings.json .claude/settings.json   # or merge into an existing one
```

Make sure `palinode-api` is running (default: `http://localhost:6340`). Override
with `PALINODE_API_URL` if you run it on another host.

## Why `/clear` matters

`/clear` in Claude Code resets the conversation context. Without a hook, every
insight, decision, and bug root cause from that session vanishes. The SessionEnd
hook fires on `/clear` (matcher: `clear`), so even if you forget to call
`palinode_session_end` manually, a fallback snapshot is captured.

For the best record, have the agent call `palinode_session_end` explicitly
*before* `/clear` runs — the hook's fallback only has the transcript to work
with, whereas the agent can synthesize a structured summary with decisions and
blockers.

## Tuning

Environment variables the hook respects:

| Variable | Default | Purpose |
|----------|---------|---------|
| `PALINODE_API_URL` | `http://localhost:6340` | Where to POST the capture |
| `PALINODE_HOOK_MIN_MESSAGES` | `3` | Minimum user messages before capture fires (skips trivial sessions) |

## Fail-silent

The hook is designed to never block Claude Code exit. If the API is down, the
capture is dropped and the hook exits 0. Check `palinode status` to verify the
API is reachable — and re-run sessions that matter.
