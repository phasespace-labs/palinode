# Changelog

All notable changes to Palinode. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## Unreleased

### Added

**Search quality (M0.5)**
- **Score-gap dedup** (#91, `52966ae`) — additional chunks from the same file are kept only if within `dedup_score_gap` (default 0.2) of the file's best score. Reduces noise from multi-section files dominating results.
- **G1 context boost fix** (#92, `f156a3d`) — `store.search()` now accepts `context_entities` for ambient context boost. Previously the boost only fired through `search_hybrid`.
- **Raw cosine exposure** (#94, `ee8931a`) — search results include a `raw_score` field with the original cosine similarity before RRF normalization.
- **Daily penalty** (#93, `d18240c`) — `daily/` files receive `score * daily_penalty` (default 0.3) to prevent daily notes from dominating search results. New `include_daily` parameter opts out of the penalty. Exposed as an MCP tool parameter.
- **Canonical question frontmatter** (#83, `dbe5703`) — `canonical_question` frontmatter field (string or list) is prepended as `"Q: ..."` to the first chunk before embedding, anchoring each memory to the question it answers.

**Tests**
- 175 tests passing (up from 149)

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
- Deterministic executor applying `KEEP` / `UPDATE` / `MERGE` / `SUPERSEDE` / `ARCHIVE` operations proposed by an LLM
- Weekly full-corpus consolidation with configurable LLM backend
- Nightly lightweight consolidation pass (`--nightly` flag) bounded to `UPDATE`/`SUPERSEDE` for safer incremental updates
- Model fallback chains — primary → fallback → fallback on timeout or HTTP error
- Prompt versioning system — extraction/compaction prompts stored as memory files with `active: true` frontmatter

**Interfaces (all four expose the same capabilities)**
- **MCP server** — Streamable HTTP transport (also supports stdio) with 18 tools. Stateless HTTP client, point it at any Palinode API server
- **REST API** — FastAPI on port 6340, 20+ endpoints covering search, save, diff, triggers, history, blame, rollback, consolidation, session-end, lint, migrate
- **CLI** — 26 commands wrapping the REST API via Click. TTY-aware (human output interactive, JSON when piped). Remote access via `PALINODE_API` env var
- **Plugin** — OpenClaw lifecycle hooks for agent frameworks with inject/extract patterns

**New MCP tools in this release**
- `palinode_lint` — scan memory for orphaned files, stale active files, missing frontmatter, potential contradictions
- `palinode_blame` / `palinode_history` / `palinode_rollback` — git-backed provenance tools
- `palinode_trigger` — register prospective triggers that inject memory files when matching context is detected
- `palinode_prompt` — list, read, and activate versioned LLM prompt files

**Capture and session management**
- `/session-end` endpoint — appends session summary to daily notes + one-liner to project status files
- Session-end hook for Claude Code — auto-captures sessions on exit, idempotent, non-blocking
- Entity extraction from daily notes with keyword fallback for untagged content

**Migration**
- `palinode migrate` — import existing markdown memory systems (OpenClaw format) with `--review` mode for dry-run inspection

**Security and hardening**
- Path validation on all file operations (rejects `..`, symlinks outside memory directory)
- Secret scrubbing on save path via configurable regex patterns
- Exclude-paths list prevents search results from surfacing files in `.secrets`, `credentials`, etc.

**Documentation**
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
