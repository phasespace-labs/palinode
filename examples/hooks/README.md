# Palinode Claude Code and Codex hooks

Drop-in hooks that wire a Claude Code session to Palinode end to end: inject
memory at session start, recall relevant memory before each prompt, checkpoint
each compaction, auto-capture at session end. Codex runs the same session-start
and per-prompt scripts; see [Codex](#codex) below.

## What's here

| File | What it does |
|------|--------------|
| `palinode-session-start.sh` | SessionStart hook — injects a bounded digest of `core: true` memories into the fresh session (plus a recall reminder), and warms server-side session context via `/context/prime` |
| `palinode-user-prompt-submit.sh` | UserPromptSubmit hook — per-turn implicit recall: checks prospective triggers (server-side cooldowns keep firings self-limiting) and runs a strict-threshold search over the prompt, injecting compact snippets before the model sees it |
| `palinode-session-end.sh` | SessionEnd hook — captures a snapshot of the transcript to palinode-api on session exit, including `/clear`, logout, and normal exit |
| `palinode-post-compact.sh` | PostCompact hook — saves the summary Claude Code writes when it compacts the conversation as one checkpoint per session (see "Compaction checkpoints" below) |
| `settings.json` | The Claude Code hook registration that points at all four scripts |
| `codex-hooks.json` | The Codex hook registration (`.codex/hooks.json`) for the session-start, per-prompt and session-end scripts |
| `palinode-cursor-hook.sh` | Cursor adapter — runs `palinode-session-start.sh` on Cursor's `sessionStart` and `palinode-user-prompt-submit.sh` on `beforeSubmitPrompt`, translating Cursor's payload and answer (see "Cursor" below) |
| `palinode-agy-hook.sh` | Antigravity adapter — runs `palinode-session-start.sh` on `SessionStart` and `palinode-user-prompt-submit.sh` on the first `PreInvocation` of each turn, answering with `injectSteps` (see "Antigravity" below) |

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
cp palinode-session-start.sh palinode-user-prompt-submit.sh palinode-session-end.sh palinode-post-compact.sh .claude/hooks/
chmod +x .claude/hooks/palinode-*.sh
cp settings.json .claude/settings.json   # or merge into an existing one
```

Make sure `palinode-api` is running (default: `http://localhost:6340`). Override
with `PALINODE_API_URL` if you run it on another host.

## Upgrading

Installed hooks do not update themselves when palinode does. After upgrading,
run:

```bash
palinode hooks update --dry-run   # see the diff
palinode hooks update             # refresh; old copies kept as <name>.bak-<timestamp>
```

It refreshes the palinode hooks registered in `~/.claude/settings.json`, in
the current project's `.claude/settings*.json`, and in Codex's `hooks.json` and
`config.toml` (under `$CODEX_HOME` or `~/.codex`, and the project's `.codex/`).
It leaves every other hook alone. Each script's second line, `# palinode-hook-version: X.Y.Z`, names the
release in which it last changed. `palinode doctor` (check `hooks_current`)
warns when an installed copy has drifted.

## Codex

Codex (codex-cli 0.165.0 and later) runs `palinode-session-start.sh` and
`palinode-user-prompt-submit.sh` unchanged. Its `SessionStart` and
`UserPromptSubmit` payloads carry the `session_id`, `cwd`, `source` and
`prompt` fields the scripts read, and it takes the same
`hookSpecificOutput.additionalContext` back. Codex adds that text to the
conversation as a developer message: before the first prompt for session start,
after the user's prompt for per-prompt recall. So, as with Claude Code, the
cached prompt prefix is left alone. On Codex's `SessionEnd`, the same
`palinode-session-end.sh` floor Claude Code runs captures the session's topic
and its last reply, so a session where the agent never saved still reaches the
next one. The script reads Codex's rollout transcript itself. There is no Codex
compaction hook yet.

From your project root:

```bash
palinode init --codex-hook
```

That writes:

- `.codex/hooks/palinode-session-start.sh`,
  `.codex/hooks/palinode-user-prompt-submit.sh` and
  `.codex/hooks/palinode-session-end.sh`, the same scripts Claude Code runs;
- the registration in `.codex/hooks.json` (the same as `codex-hooks.json`).

`hooks.json` often already holds another tool's hooks. Palinode's entries are
added after them, and a re-run adds nothing twice. A file that is not valid
JSON, or whose `hooks` are not shaped as Codex expects, is left untouched.

**Trust the hooks before they run.** Codex loads a project's `.codex/` only
when the project is trusted, and runs each hook only after it has been reviewed.
Open Codex in the project and run `/hooks` to trust the three palinode entries.
Codex ties trust to the `hooks.json` entry (its command and timeout), not to
the script's contents, so `palinode hooks update` can refresh the scripts
without re-trusting. Codex also records trust by position
(`<file>:<event>:<group>:<handler>`), which is why palinode only ever appends
entries after the existing ones.

How Codex runs them differs from Claude Code in four ways, each handled
without a fork:

- **No project-directory variable.** Codex runs each command as
  `$SHELL -lc '<command>'` in the session's cwd, which can be a subdirectory.
  The registered command finds the scripts with
  `"$(git rev-parse --show-toplevel 2>/dev/null || pwd)/.codex/hooks/…"`, and
  the scripts send the repository root as `cwd` either way.
- **An extra session source.** Codex's `source` can also be `fork`. Like
  `resume`, it is not in the default `PALINODE_HOOK_START_SOURCES`
  (`startup clear`), so a forked thread is not re-primed.
- **Matchers.** Codex applies a `SessionStart` matcher to `source` and ignores
  one on `UserPromptSubmit`. The registration has none; the script filters the
  source, so the environment variable keeps working.
- **A short SessionEnd budget.** Codex gives a `SessionEnd` hook 1 second by
  default and at most 3, so the registration asks for 3. The session-end script
  recognises a Codex rollout (its first line is `session_meta`) and reads each
  user input, tool output and agent message from it, so the message-count gate,
  the `PALINODE_HOOK_ENDING_MAX_CHARS` cap, the capture preflight and the
  project tag work as they do for Claude Code. Its captures are marked
  `source: codex-hook`.

The tuning variables below apply unchanged. Set them in the environment Codex
is started from; hooks inherit it.

To switch them off, remove the three palinode entries from `.codex/hooks.json`
(and from `~/.codex/hooks.json` or `config.toml` if you added any there), then
delete the scripts. `palinode controls status` lists the Codex hooks it
observes.

## Compaction checkpoints

A long Claude Code session can run for days and compact several times before it
ends, and SessionEnd capture writes nothing until it does. When Claude Code
compacts (`/compact` or automatically), it writes a summary of the conversation
so far and passes it to PostCompact hooks. `palinode-post-compact.sh` saves that
summary through `POST /save`:

- as a `ProjectSnapshot` with slug `compaction-checkpoint-<session id>`. Each
  session has one checkpoint; a later compaction replaces it in place and git
  keeps the earlier versions;
- marked `epistemic: inference` and `source: claude-code-compaction`, because
  it is a model's summary, not something the user wrote or checked;
- under a header saying that approvals, instructions and plans recorded in it
  applied to that session only.

It writes no daily note, status line or session-end entry, and the checkpoint is
not core memory. It passes the same capture controls as the SessionEnd hook
first, and does nothing when capture is paused or the project or path is
excluded. It is registered `async`, so the next turn does not wait for it.

The hook cannot change what Claude Code keeps after compacting. To re-inject the
memory digest after a compaction too, add `compact` to
`PALINODE_HOOK_START_SOURCES`.

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

On `startup` and `/clear`, the SessionStart hook POSTs `/context/prime` with the
session's directory (and `PALINODE_PROJECT` when it is set) and injects the
`core: true` memories in the digest the server returns, as `additionalContext`:
one line per memory (`- [file] title — description`, plus any `[⚠ contradicts: …]`
qualifiers), newest first, capped at 10 memories / 4000 chars by default, and
prefixed with a deterministic reminder that recall goes through
`palinode_search` / `palinode_read`. The session starts already knowing its
standing context instead of depending on the agent remembering to search for it.

The server selects those memories for the session's project. A core memory
tagged to another project is withheld, like a retired, private or restricted
one, so a store shared by several projects doesn't open every session with
every project's core decisions. A core memory tagged to no project is global and
reaches every session. If the server can't produce a digest (API down, recall
paused, a server older than `/context/prime`), nothing is injected.

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

The SessionEnd floor sends the message count, a first-prompt topic hint bounded
to 200 characters, and the last assistant reply capped at 2000 characters by
default. `PALINODE_HOOK_ENDING_MAX_CHARS=0` disables the ending.

## Tuning

Environment variables the hooks respect:

| Variable | Default | Purpose |
|----------|---------|---------|
| `PALINODE_API_URL` | `http://localhost:6340` | Where the API lives (both hooks) |
| `PALINODE_API_TOKEN` | *(unset)* | Bearer token for token-protected deployments (session-start hook) |
| `PALINODE_PROJECT` | *(unset)* | This session's project slug. Sent with the session-start prime and the per-turn resolve; when unset the server resolves the project from the session's `cwd`. The server may be on another host, so it never uses its own directory |
| `PALINODE_HOOK_ENDING_MAX_CHARS` | `2000` | Maximum characters from the last assistant reply; `0` omits the ending, non-numeric values use the default. Truncation is marked |
| `PALINODE_HOOK_MIN_MESSAGES` | `3` | Minimum user messages before capture fires (skips trivial sessions) |
| `PALINODE_HOOK_REASONS` | `clear logout prompt_input_exit other` | Space-separated SessionEnd reasons to capture on. Narrow to e.g. `"clear"` for /clear-only, or extend with `resume` / `bypass_permissions_disabled` if you want to capture those lifecycle events too |
| `PALINODE_HOOK_COMPACT_MAX_CHARS` | `32000` | Longest compaction summary saved, in characters. A longer one is cut, with a note saying how much was kept |
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

## Cursor

`palinode init --cursor-hooks` installs the same session-start and per-turn
recall for Cursor:

- `.cursor/hooks/palinode-cursor-hook.sh`, `palinode-session-start.sh` and
  `palinode-user-prompt-submit.sh`;
- two entries merged into `.cursor/hooks.json`. Other entries, such as another
  tool's, are kept as they are:

```json
{
  "version": 1,
  "hooks": {
    "sessionStart": [{"command": ".cursor/hooks/palinode-cursor-hook.sh .cursor/hooks/palinode-session-start.sh", "timeout": 20}],
    "beforeSubmitPrompt": [{"command": ".cursor/hooks/palinode-cursor-hook.sh .cursor/hooks/palinode-user-prompt-submit.sh", "timeout": 20}]
  }
}
```

The adapter changes no recall logic; it only translates:
- **Input:** Cursor's `workspace_roots[0]` becomes the script's `cwd`, `conversation_id` its `session_id`, and `prompt` is passed through.
- **Output:** the script's `additionalContext` comes back as Cursor's `additional_context`.

The scripts' `PALINODE_*` knobs work the same way, and the hooks see the environment Cursor was started with.

It always answers `{"continue": true}`, so it never blocks a prompt.

What reaches the model, verified against `cursor-agent` (see
[HARNESSES.md](../docs/HARNESSES.md#cursor-hooks)):
- `sessionStart` works in every mode.
- `beforeSubmitPrompt` works in the interactive CLI and does not fire in
  `cursor-agent -p`.

## Antigravity

`palinode init --agy-hooks` installs the same session-start and
per-turn recall for Antigravity:

- `.agents/hooks/palinode-agy-hook.sh`, `palinode-session-start.sh`
  and `palinode-user-prompt-submit.sh`;
- a `palinode` hook definition merged into `.agents/hooks.json`. Other hook
  definitions are kept as they are.

The commands use absolute paths. Antigravity runs workspace hooks with the cwd set to `.agents/`, and its docs don't say so, so a relative path would depend on that. Re-run `palinode init --agy-hooks` in a checkout that has moved.

The adapter translates, and changes no recall logic:
- **Input:** `workspacePaths[0]` becomes the script's `cwd`, `conversationId` its `session_id`, and `lastUserInput` its `prompt`.
- **Session start:** the context goes back as a `systemMessage` step, which stays in the conversation.
- **Per-prompt recall:** the context goes back as an `ephemeralMessage` step, which isn't carried into later turns.
- **Once per turn:** `PreInvocation` fires before every model call, including after each tool result, so recall runs only on the first call of a turn (`invocationNum` 0).

## Fail-silent

All four hooks are designed to never block Claude Code. If the API is down,
the session-start hook injects nothing, per-turn recall stays silent, and the
compaction checkpoint and session-end capture are dropped — all exit 0. Check `palinode status` to verify
the API is reachable — and re-run sessions that matter.
