# Changelog

All notable changes to Palinode. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.6.2] — 2026-04-12

### Added

**Multi-platform MCP setup guide**
- `docs/MCP-SETUP.md` — setup instructions for Claude Code, Cursor, VS Code (Continue + Cline), Zed, and Windsurf
- Replaces the old Claude Code-only setup doc

**Agent plugin restored**
- `plugin/` re-added after scrubbing internal references
- Hook migrated from `before_agent_start` to `before_prompt_build`
- Fixed `cfg.apiUrl` → `cfg.palinodeApiUrl` runtime bug

### Changed

- `palinode_timeline` merged into `palinode_history` — one tool with `--follow`, diff stats, structured JSON return, and `limit` parameter (closes #1)
- Tool count: 18 → 17 (timeline/history consolidated)
- Claude Code plugin manifest updated to v0.6.2

### Removed

- `docs/claude-code-setup.md` — replaced by `docs/MCP-SETUP.md`

---

## [0.6.1] — 2026-04-12

### Added

**RETRACT operation**
- New executor operation: `RETRACT` — marks a memory fact as wrong with a visible tombstone
- Strikethrough formatting with `[RETRACTED date — reason]` annotation
- Fact ID preserved (not deleted) so readers know what was retracted and why
- History file records retraction provenance
- Compaction and update prompts updated with RETRACT guidance
- 4 new tests (121 total)

Maps to IETF Knowledge Unit `retract` lifecycle state — see Paul-Kyle/palinode#17 for the interop discussion.

---

## [0.6.0] — 2026-04-11

### Added

**Write-time contradiction check (ADR-004)**
- When saving a memory, the system now checks for contradictions against existing files in the same entity scope
- Contradiction candidates are surfaced before the save completes, with configurable thresholds
- Background worker runs via asyncio queue (API) or disk-backed marker files (CLI/plugin)

**Ambient context search (ADR-008)**
- Search results are now boosted by project context inferred from the caller's working directory
- Resolution chain: `PALINODE_PROJECT` env var → config project map → CWD auto-detect
- Existing RRF hybrid search pipeline extended with a context scoring channel

**Claude Code plugin scaffold**
- `claude-plugin/` directory with plugin manifest for Claude Code marketplace submission

**Claude Code skills**
- `palinode-claude-code` — MCP setup and usage for Claude Code sessions
- `palinode-memory` — general memory operations skill
- `palinode-session` — automatic session lifecycle memory capture

**Architecture Decision Records**
- ADR-004: Event-driven consolidation (write-time contradiction check)
- ADR-005: Debounced reflection executor
- ADR-006: On-read reconsolidation
- ADR-007: Access metadata and decay
- ADR-008: Ambient context search

**Documentation**
- WHY-LOCAL-MEMORY.md — positioning document for local-first memory
- Research paper: Memory Compaction and Augmented Recall for Persistent AI Agents
- PRD (product requirements document)

**Tests**
- `test_write_time.py` — write-time contradiction check coverage
- `test_context.py` — ambient context search and project boosting

### Changed
- MCP server uses Streamable HTTP transport (renamed from SSE entry point)
- Search API accepts optional `context` parameter for entity-scoped boosting
- Consolidation cron scheduling improvements

### Removed
- `palinode/migration/` — internal migration tooling removed
- `plugin/` — old OpenClaw plugin (replaced by `claude-plugin/` scaffold)

---

## [0.5.0] — 2026-04-10

First tagged release. Persistent memory for AI agents with git-versioned markdown as source of truth, hybrid SQLite-vec + FTS5 search, and LLM-driven consolidation applied by a deterministic executor.

### Added

**Core storage and search**
- SQLite-vec vector store with BGE-M3 embeddings (1024d) via any OpenAI-compatible endpoint (Ollama, vLLM, etc.)
- Hybrid search: vector similarity + BM25 (FTS5) fused via reciprocal rank fusion
- Hash-on-read freshness validation — detects out-of-band file edits without a full reindex
- File watcher daemon with debounced reindex and fault isolation

**Consolidation and compaction**
- Deterministic executor applying `KEEP` / `UPDATE` / `MERGE` / `SUPERSEDE` / `ARCHIVE` operations proposed by an LLM (see [ADR-001](../ADR-001-tools-over-pipeline.md))
- Weekly full-corpus consolidation with configurable LLM backend
- Nightly lightweight consolidation pass (`--nightly` flag) bounded to `UPDATE`/`SUPERSEDE` for safer incremental updates
- Model fallback chains — primary → fallback → fallback on timeout or HTTP error
- Prompt versioning system — extraction/compaction prompts stored as memory files with `active: true` frontmatter

**Interfaces (all four expose the same capabilities)**
- **MCP server** — Streamable HTTP transport (also supports stdio) with 18 tools. Stateless HTTP client, point it at any Palinode API server
- **REST API** — FastAPI on port 6340, 20+ endpoints covering search, save, diff, triggers, history, blame, rollback, consolidation, session-end, lint
- **CLI** — 26 commands wrapping the REST API via Click. TTY-aware (human output interactive, JSON when piped). Remote access via `PALINODE_API` env var
- **Plugin** — OpenClaw lifecycle hooks for agent frameworks with inject/extract patterns

**New MCP tools in this release**
- `palinode_lint` — scan memory for orphaned files, stale active files, missing frontmatter, potential contradictions
- `palinode_blame` / `palinode_history` / `palinode_timeline` / `palinode_rollback` — git-backed provenance tools
- `palinode_trigger` — register prospective triggers that inject memory files when matching context is detected
- `palinode_prompt` — list, read, and activate versioned LLM prompt files

**Capture and session management**
- `/session-end` endpoint — appends session summary to daily notes + one-liner to project status files
- Session-end hook for Claude Code — auto-captures sessions on exit, idempotent, non-blocking
- Entity extraction from daily notes with keyword fallback for untagged content

**Security and hardening**
- Path validation on all file operations (rejects `..`, symlinks outside memory directory)
- Secret scrubbing on save path via configurable regex patterns
- Exclude-paths list prevents search results from surfacing files in `.secrets`, `credentials`, etc.

**Documentation**
- [ADR-001: Tools Over Pipeline](../ADR-001-tools-over-pipeline.md) — why the executor is deterministic
- Remote MCP setup guides for Claude Code, Claude Desktop, Cursor, Zed
- Example memory files (`examples/people/`, `examples/projects/`, `examples/decisions/`, `examples/insights/`)
- Compaction walkthrough (`examples/compaction-demo/`) — a memory file across 3 passes with blame + diff output

**Tests**
- 92 tests covering parser, store, executor, API, CLI, migration, and hybrid search

### Changed
- All inference is local by default. Cloud API keys (Gemini, OpenAI) are opt-in via environment variables
- REST API binds to `127.0.0.1` by default; set `PALINODE_API_HOST=0.0.0.0` to expose on LAN
- FastAPI uses lifespan protocol (deprecated `on_event` removed)
- Git commits stage only `*.md` files, never the SQLite journal/WAL/SHM

### Fixed
- Watcher no longer crashes the API server if the memory directory is temporarily unavailable
- CLI display keys match API response keys across all commands
- Migration tool correctly handles frontmatter with embedded colons

### Removed
- Deprecated SSE MCP transport (replaced by Streamable HTTP per canonical MCP SDK pattern)

---

## [0.1.0] — 2026-03-22

Initial public release. Minimum viable memory system.

### What worked at launch

- SQLite-vec vector store with BGE-M3 embeddings via Ollama
- File watcher daemon auto-indexing markdown on create/modify/delete
- FastAPI server with `/search`, `/save`, `/status`, `/reindex` endpoints
- Markdown parser with YAML frontmatter extraction and heading-level section chunking
- CLI with `search` and `stats` commands
- Plugin with core memory injection, topic-specific retrieval, and three tools (`palinode_search`, `palinode_save`, `palinode_status`)
- Session capture to daily notes on agent end
- Systemd user services for `palinode-api` (port 6340) and `palinode-watcher`

### Architecture decisions

| Decision | Choice | Rationale |
|---|---|---|
| Source of truth | Markdown files, git-versioned | Human-readable, survives everything |
| Vector store | SQLite-vec (embedded) | No server, matches file-based philosophy |
| Embeddings | BGE-M3 via Ollama | Local, private, strong on structured text |
| Prompts | Read from `specs/prompts/*.md` | Version-controlled, editable, diffable |

### Known limitations at 0.1.0

- No WAL mode on SQLite — concurrent writes can lock
- Session capture wrote raw text to daily notes (no LLM extraction)
- No consolidation scheduler
- No entity linking
- No MCP server (plugin-only integration)

All of these are addressed in 0.5.0.
