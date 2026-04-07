# ADR-003: Memory System / Harness Boundary

**Status:** Accepted
**Date:** 2026-04-06
**Deciders:** Paul Kyle

## Decision

Palinode defines a strict boundary between the **memory system** (storage, indexing, retrieval, consolidation) and the **harness** (the LLM orchestration layer that provides hooks, context injection, and session lifecycle). The memory system must never assume the harness will retrieve what was saved. The harness must verify the memory system is healthy before trusting its results. Neither system may silently degrade.

## The Problem: Save and Pray

Most LLM memory integrations follow a pattern:

1. Agent finishes session
2. Hook fires, saves a summary to some backend
3. Next session starts
4. Hook fires, searches the backend, injects results

This looks correct. It is not. It is **save and pray** — the implicit assumption that what was written will be readable later. In practice:

- The indexer may have crashed between save and retrieval (watcher stall, database lock, embedding service down)
- The search index may be stale while the files are current (index lag, failed upsert, schema migration)
- The backend may be a headless cloud service with no way to verify what it actually stored
- The next session may start in a different harness (Claude Code vs Cursor vs API) with different injection behavior
- The context window may be too small to hold what was retrieved, silently truncating critical memories

The failure mode is always the same: **the agent doesn't know what it doesn't know.** It proceeds with confidence on incomplete context. There is no error. There is no warning. The dashboard is green. The memories are gone.

This is not a theoretical risk. Early benchmarks on agent memory retrieval suggest that recall degrades significantly under task complexity — agents complete work while missing large portions of their stored knowledge. Task completion metrics stay green even as retrieval quality degrades. The agent finishes the work. The work is wrong.

## Architecture: Two Systems, One Contract

### The Memory System

Responsible for the **data plane** — making memories durable, findable, and honest about their state.

| Guarantee | Why |
|-----------|-----|
| **Durable writes** | A saved memory must survive process restart, index rebuild, and schema migration. Files on disk are the source of truth, not the index. |
| **Idempotent saves** | Repeated saves of the same content must not create duplicates. Content-hash deduplication, not timestamp-based. |
| **Freshness metadata** | Every memory carries timestamps, source provenance, and content hashes. The system never claims freshness it cannot verify. |
| **Health signal** | A status endpoint reports: index staleness (last indexed vs latest file mtime), embedding service reachability, database integrity. This is not optional. |
| **Graceful degradation** | When the index is stale, return results with a staleness warning — never return empty results that could be mistaken for "nothing relevant exists." |
| **Retrievability proof** | A save operation is not complete until the memory is retrievable by search. If the indexer is down, the save succeeds (file written) but the system reports degraded status. |

### The Harness

Responsible for the **control plane** — deciding when to save, when to load, what fits in context, and what to do when memory is unavailable.

| Guarantee | Why |
|-----------|-----|
| **Lifecycle signals** | Reliable SessionStart and SessionEnd events. These are the memory system's only chance to inject and capture. If the harness crashes without firing SessionEnd, work is lost. |
| **Backchannel initialization** | At session start, the harness must verify the memory system is reachable and healthy before proceeding. A session that starts without memory context must know it is degraded. |
| **Bounded injection** | The harness manages context window budget. Memory systems should not need to know about token limits. The harness decides what fits, not the memory system. |
| **Fallback declaration** | When memory is unavailable, the harness must inject a notice: "Memory system unreachable — operating without prior context." The model can then ask the user for context it would normally have. |
| **Save metadata** | The harness provides structured context with every save: project, category, session ID, timestamp. Raw text saves without metadata are noise. |

### The Gray Zone

These responsibilities are contested across the industry. Palinode's position:

| Responsibility | Our position | Rationale |
|----------------|-------------|-----------|
| **Relevance ranking** | Memory system | The memory system has the embeddings, the BM25 index, and the entity graph. It ranks better than the harness can. |
| **Consolidation** | Memory system, triggered by harness | LLM judgment for what to merge/supersede/archive. Deterministic executor for applying operations. Cron or hook for triggering. |
| **Staleness detection** | Memory system | Content hashes in frontmatter, checked at read time. The harness consumes freshness badges, not raw timestamps. |
| **Admission control** | Memory system | Not every piece of text deserves to be a memory. Type detection, deduplication, and minimum-signal thresholds belong in the memory system. |

## Why Backchannel Initialization Is Non-Negotiable

The backchannel is the path by which the memory system tells the harness (and therefore the model) what it knows. If this path is broken, every subsequent decision is made on incomplete information.

"Backchannel initialization" means: at session start, before the model sees any user input, the harness:

1. **Checks health** — calls the memory system's status endpoint. Is the index current? Is the embedding service reachable? How many files are indexed vs how many exist on disk?
2. **Reports degradation** — if the memory system is unhealthy, injects a warning into context. The model proceeds knowing it may be missing information.
3. **Injects core context** — retrieves core memories (always-inject), project-specific context, and recent session summaries.
4. **Confirms injection** — the model's first context includes a receipt: "Loaded N core memories, M project memories, index last updated T."

Without step 1, the model trusts results from a stale index. Without step 2, the model doesn't know to ask for help. Without step 4, there is no way to audit whether the session started with the right context.

The alternative — saving to a headless cloud and hoping the next session retrieves it — is the dominant pattern in 2026. It is also the reason most agent memory systems fail silently. The memory exists somewhere. The agent cannot find it. Nobody notices.

## Why Not Just Trust the Cloud?

Cloud-hosted memory services (vector databases, managed embeddings, SaaS memory APIs) solve durability but not verifiability. When you save to a cloud service:

- You cannot verify the index is current without querying it
- You cannot verify embeddings were generated correctly without re-embedding and comparing
- You cannot verify what the service actually stored without reading it back
- You cannot inspect the ranking algorithm that decides what is "relevant"
- You cannot run the service locally when the network is down

File-based memory with local indexing solves this: the files are on disk (inspectable with `cat`), the index is in SQLite (inspectable with `sqlite3`), the embeddings are reproducible (same model, same input, same output), and the entire system works offline.

This is not an argument against cloud services. It is an argument against **opaque persistence** — saving to a system you cannot inspect, debug, or verify. Any memory backend (cloud or local) must provide the health signal and retrievability proof described above.

## The Deterministic / Judgment Split

A principle for deciding what belongs in the harness vs the memory system vs the model:

| Type | Where | Examples |
|------|-------|---------|
| **Deterministic** | Harness (hooks, cron) | Save at session end. Inject core memory at session start. Trigger nightly consolidation. Check health before proceeding. |
| **Judgment** | Model (via tools) | Decide what is worth saving. Compose a session summary. Identify contradictions in memories. |
| **Structured judgment** | Memory system (LLM + executor) | Consolidation: LLM proposes operations, deterministic executor applies them. The LLM has judgment; the executor has guarantees. |

This split is why Palinode's consolidation uses a **deterministic executor** rather than letting the LLM write files directly. The LLM decides "these two facts should merge." The executor validates the operation, applies it atomically, and commits with provenance. Judgment where it helps. Determinism where it matters.

## Consequences

**For Palinode:**
- `palinode_status` must report index freshness: file count vs indexed count, last index timestamp, embedding service health
- Search results must include staleness warnings when the index is behind
- Session-start injection must include a context receipt
- The MCP server must handle "memory system degraded" as a first-class state, not an error

**For harness integrators:**
- SessionStart hooks must check `palinode_status` before injecting memories
- SessionEnd hooks must fire reliably — consider write-ahead saves (periodic, not just on exit) for crash resilience
- Context injection must include a fallback notice when memory is unavailable
- The harness is responsible for context budget — do not blame the memory system for oversized injection

**For the industry:**
- Memory systems without health endpoints are black boxes. Black boxes fail silently.
- Save-and-pray is the default pattern. It is also the reason agent memory has a trust problem.
- The hook contract between memory and harness needs standardization. MCP provides transport; it does not provide behavioral guarantees. Someone needs to define the behavioral layer.

## References

- [arxiv:2603.05344 — Building AI Coding Agents for the Terminal](https://arxiv.org/abs/2603.05344) (OpenDev paper, context engineering layer)
- [ICLR 2026 MemAgents Workshop](https://sites.google.com/view/memagent-iclr26/) (April 26-27, 2026)
- [Chanl AI — Your Agent Completed the Task. It Also Forgot 87%](https://www.chanl.ai/blog/memory-silent-failure-mode)
- [SQLite Virtual Table Interface — xUpdate](https://www.sqlite.org/vtab.html#xupdate)
- [MCP Specification](https://modelcontextprotocol.io/specification/2025-11-25)
- ADR-001: Tools Over Pipeline
- ADR-002: Watcher Fault Isolation
