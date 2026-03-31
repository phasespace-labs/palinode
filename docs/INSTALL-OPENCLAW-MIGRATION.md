# Migrating from OpenClaw Built-in Memory to Palinode

Palinode replaces OpenClaw's built-in memory system (MEMORY.md + Mem0 plugin). This guide covers what to disable, what to keep, and how to avoid double-injecting.

---

## What OpenClaw Does By Default (and What to Change)

### 1. MEMORY.md Bootstrap Injection → DISABLE

OpenClaw injects `MEMORY.md` from your workspace into every session via bootstrap. If Palinode is also injecting core memory, you're burning tokens twice.

**Fix:** Stop OpenClaw from injecting MEMORY.md:

Option A: Delete or rename MEMORY.md:
```bash
mv ~/workspace/MEMORY.md ~/workspace/MEMORY.md.archive
```

Option B: Set bootstrap max chars to 0 for MEMORY.md (keeps the file for reference):
```json
// In openclaw.json under agents.defaults:
"bootstrapMaxChars": 0
```

**What replaces it:** Palinode's `core: true` files — typed, searchable, git-versioned, and injected via `before_agent_start` hook with token budgets.

### 2. Mem0 Plugin → REMOVE

If you see `openclaw-mem0 config required` errors, Mem0 is broken and wasting startup time.

```bash
# Remove from all profiles
rm -rf ~/.openclaw/extensions/openclaw-mem0
rm -rf ~/.openclaw-field/extensions/openclaw-mem0
rm -rf ~/.openclaw-attractor/extensions/openclaw-mem0
rm -rf ~/.openclaw-governor/extensions/openclaw-mem0
rm -rf ~/.openclaw-gradient/extensions/openclaw-mem0
```

**What replaces it:** Palinode's `palinode_search` and `palinode_save` tools — hybrid search (BM25 + vector), typed memories, git-versioned.

### 3. session-memory Hook → DISABLE

OpenClaw's bundled `session-memory` hook is **enabled by default**. It saves raw session dumps to `<workspace>/memory/` on `/new` or `/reset`. This overlaps with Palinode's `agent_end` + `before_reset` extraction, which produces better structured output.

Add to your `openclaw.json`:
```json
{
  "hooks": {
    "bundled": {
      "session-memory": { "enabled": false }
    }
  }
}
```

Or via CLI:
```bash
openclaw hooks disable session-memory
```

Palinode's daily capture is strictly better: it writes structured, dated notes to `daily/YYYY-MM-DD.md` that feed directly into the weekly consolidation pipeline. The session-memory hook writes raw dumps that nothing reads.

### 4. bootstrap-extra-files Hook → OPTIONAL

This hook injects extra files from configured patterns. If you're using Palinode for core injection, you don't need this.

```bash
# Check if enabled
openclaw hooks info bootstrap-extra-files
```

If it's injecting files that overlap with Palinode core memory, disable it.

### 5. Pin Plugin Trust

Doctor warns about untracked local code. Fix:

```json
// In openclaw.json:
"plugins": {
  "allow": ["openclaw-palinode"]
}
```

---

## Hook Migration Note

⚠️ **`before_agent_start` is legacy.** OpenClaw doctor recommends `before_model_resolve` or `before_prompt_build` for new work. Palinode currently uses `before_agent_start` and it works, but a future OpenClaw update may deprecate it.

**Plan:** Track OpenClaw releases and migrate the injection hook when `before_agent_start` is formally deprecated. The change is ~20 lines in `plugin/index.ts`.

---

## Why Palinode > OpenClaw Built-in Memory

| Feature | OpenClaw Built-in | Palinode |
|---|---|---|
| Storage | `MEMORY.md` flat file | Typed markdown + YAML frontmatter |
| Search | None (full file injected) | Hybrid BM25 + vector (RRF) |
| Injection | Bootstrap dump (truncated at 18K chars) | 4-phase: Core → Topic → Associative → Triggered |
| Consolidation | None (manual editing) | Operation-based (KEEP/UPDATE/MERGE/SUPERSEDE/ARCHIVE) |
| History | None | Git blame/diff/rollback |
| Capture | `session-memory` hook (raw dump) | `agent_end` (structured extraction to daily notes) |
| Quick capture | None | `-es` flag on any message |
| Scaling | File grows until truncated | Layered files + weekly compaction = memory gets smaller over time |
| Multi-agent | Separate MEMORY.md per profile | Shared `palinode-data` repo, entity graph cross-references |
| Failure mode | File missing = no memory | Services down = `cat` and `grep` still work |
| Extensibility | None | 13 MCP tools, FastAPI, OpenClaw plugin |

---

## Token Savings

Before migration (measured on a real deployment):

| Source | Tokens/session | After Migration |
|---|---|---|
| MEMORY.md bootstrap (21K chars, 17% truncated) | ~5,000 | **0** (archived) |
| Mem0 startup errors (6 profiles × 3 retries) | ~200 wasted | **0** (removed) |
| session-memory hook (raw dumps, nothing reads them) | ~500 | **0** (disabled) |
| Palinode core injection (8K cap, typed, searched) | ~2,000 | 2,000 (kept) |

Net savings: **~5,700 tokens per session** from removing duplicate injection alone.

---

## Recommended Setup After Migration

```bash
# 1. Palinode services running
systemctl --user status palinode-api palinode-watcher

# 2. Plugin installed (one per profile you use)
ls ~/.openclaw-field/extensions/openclaw-palinode/

# 3. Mem0 removed
ls ~/.openclaw-field/extensions/openclaw-mem0/ 2>/dev/null && echo "REMOVE THIS" || echo "✅ Clean"

# 4. MEMORY.md archived
ls ~/workspace/MEMORY.md 2>/dev/null && echo "Consider archiving" || echo "✅ Clean"

# 5. Health check
curl -s http://localhost:6340/status | python3 -m json.tool
```

---

## Rollback

If anything goes wrong:
```bash
# Restore MEMORY.md
mv ~/workspace/MEMORY.md.archive ~/workspace/MEMORY.md

# Re-enable session-memory hook
openclaw hooks enable session-memory

# Palinode keeps running alongside — no conflicts
```

Palinode is designed to coexist with OpenClaw's built-in memory during transition. You can run both and gradually shift.
