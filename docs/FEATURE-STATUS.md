# Palinode — Feature Status (Honest Assessment)

**Date:** 2026-03-31
**Purpose:** What's battle-tested, what's built-but-unproven, what's novel.

---

## 🟢 Battle-Tested (Used Daily, Proven)

These work reliably. They've been running in production for weeks.

| Feature | Evidence |
|---|---|
| **Typed markdown memory files** | 219 files across people/, projects/, decisions/, insights/, research/ |
| **YAML frontmatter + fact IDs** | 4,234 fact IDs bootstrapped, used by compaction executor |
| **Git versioning** | Every memory change committed, blame/diff/rollback via API |
| **Hybrid search (BM25 + vector)** | FTS5 + BGE-M3 1024d embeddings, merged via RRF |
| **Content-hash dedup** | SHA-256 — skips re-embedding unchanged files (~90% savings) |
| **Core memory injection (Phase 1)** | `core: true` files injected on turn 1, always-in-context |
| **Topic search injection (Phase 2)** | Hybrid search on user message, top 3 × 500 chars |
| **OpenClaw plugin** | `before_agent_start` hook, running on 4 agent profiles |
| **MCP server** | 13 tools, tested with Claude Code |
| **FastAPI API** | localhost:6340, 20+ endpoints, daily use |
| **CLI wrapper** | `palinode` command wraps REST API, 24 commands, full MCP parity, click-based, TTY-aware output |
| **File watcher (watchdog)** | Auto-indexes on file save, systemd service |
| **`-es` quick capture** | Append `-es` to any message to route to memory |
| **Session-end extraction** | Auto-captures key facts to daily notes |
| **Dual embeddings** | BGE-M3 (core) + Gemini (research ingestion) |
| **Mem0 backfill** | 4,637 memories migrated, classified, deduplicated |

## 🟡 Built, Lightly Tested (Code exists, needs more real-world use)

These are implemented and pass unit-level tests but haven't been stress-tested in daily use.

| Feature | Status | Risk |
|---|---|---|
| **Associative recall (Phase 3)** | Entity detection works, spreading activation untested at scale | Might surface too much or too little — threshold tuning needed |
| **Prospective triggers (Phase 4)** | CRUD works, threshold matching works in isolation | Never fired in a real conversation — plugin integration is label-only, not content injection (FIXED but not tested live) |
| **Temporal decay** | Score formula exists, disabled by default | Decay constants (τ) are educated guesses, not empirically tuned |
| **Operation-based compaction** | Executor handles KEEP/UPDATE/MERGE/SUPERSEDE/ARCHIVE | Never run a full weekly consolidation with the new executor — cron fires Sunday 3am UTC |
| **Layered files (Identity/Status/History)** | `split_file()` works, keyword heuristics configurable | Classification heuristics untested on real data at scale — may need `layer_hint` overrides |
| **Security scanning (B1)** | 8 injection patterns blocked | Pattern list is minimal — real-world attackers will find bypasses |
| **FTS5 query sanitization (B3)** | Handles hyphens, quotes, booleans | May over-sanitize legitimate queries |
| **Capacity display (B2)** | Header line added to injection | Untested in live plugin — need to verify it renders correctly |
| **Iterative status summaries (B4)** | Appends to Consolidation Log section | Never triggered by real consolidation run |
| **Fact ID bootstrap** | 4,234 IDs created | IDs are MD5-based — collisions theoretically possible on large corpora |

## 🔴 Documented but Not Implemented

| Feature | Where Claimed | Reality |
|---|---|---|
| **Auto-summary on save** | FEATURES.md | Partially — runs via LLM only when consolidation fires, not on every save |
| **Contradiction detection** | PRD.md | Not implemented — was planned for Phase 2, never built |
| **Sleep-time consolidation (full LLM pass)** | PRD.md, docs | Cron exists, runner exists, but first automated run hasn't happened yet |
| **Community memory templates** | Issue #27 | Future — no registry or pull mechanism exists |

---

## Novel Features (For Positioning)

### Genuinely Novel (Not in Hermes/Letta/Zep/Mem0/memsearch)
1. **Git-versioned memory with blame/diff/rollback** — No other system offers `git blame` on agent memory. You can see exactly when and why a fact was added, who changed it, and roll back a bad consolidation.
2. **Operation-based compaction** — LLM outputs JSON operations (KEEP/UPDATE/MERGE/SUPERSEDE/ARCHIVE), deterministic executor applies them. LLM never touches files directly. Unique separation of concerns.
3. **Fact IDs via HTML comments** — `<!-- fact:slug-abc123 -->` inline in markdown. Invisible in rendering, preserved by git, targetable by compaction operations. No other system has per-fact addressability in plain markdown.
4. **4-phase injection pipeline** — Core → Topic → Associative → Triggered. Each phase adds context without repeating previous phases. Token-efficient by design.
5. **`-es` quick capture** — Append `-es` to any chat message to route it into typed memory. Zero-friction capture without explicit tool calls.

### Good But Not Unique
6. **Hybrid BM25 + vector search** — memsearch does this too. Our implementation uses RRF fusion.
7. **Typed memory categories** — LangMem and Mem0 have typed schemas. Ours are markdown + frontmatter.
8. **Content-hash dedup** — memsearch does this. Good engineering, not novel.
9. **Core memory injection** — Letta's Core Memory blocks are the same pattern.
10. **Weekly consolidation** — LangMem's background manager, Letta's sleep-time agents do similar.

---

## What to Promote

### Lead with the novel stuff:
> "**Palinode** — persistent memory for AI agents, with provenance.
> Git blame your agent's memory. Roll back a bad consolidation.
> See exactly when every fact was learned and why.
> Files are truth. If every service crashes, `cat` still works."

### Key differentiators for search/positioning:
- "git-versioned agent memory"
- "operation-based memory compaction"
- "per-fact addressability in markdown"
- "4-phase context injection"
- "memory provenance for AI agents"

### What NOT to lead with (others do it too):
- "hybrid search" (memsearch)
- "typed memories" (LangMem, Mem0)
- "agent memory system" (too generic)

---

## Honest Release Recommendation

**Ship it.** The core loop (write → index → search → inject) is rock-solid. That's what users will use on day 1.

The advanced features (associative recall, triggers, compaction) are built and will mature with real usage. Better to ship and iterate than wait for perfection.

**What to say in the README:** "Core memory and search are production-ready. Advanced features (associative recall, triggers, weekly compaction) are implemented and working but considered beta."

**First Sunday 3am UTC** will be the real stress test — that's when the consolidation cron fires for the first time with the new executor.
