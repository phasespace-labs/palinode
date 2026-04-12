# Changelog

All notable changes to Palinode. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.7.0] — 2026-04-12

### Added

**Search quality**
- **Score-gap dedup** (#91) — additional chunks from the same file are kept only if within `dedup_score_gap` (default 0.2) of the file's best score. Reduces noise from multi-section files dominating results.
- **G1 context boost fix** (#92) — `store.search()` now accepts `context_entities` for ADR-008 ambient context boost. Previously the boost only fired through `search_hybrid`.
- **Raw cosine exposure** (#94) — search results include a `raw_score` field with the original cosine similarity before RRF normalization.
- **Daily penalty** (#93) — `daily/` files receive `score * daily_penalty` (default 0.3) to prevent daily notes from dominating search results. New `include_daily` parameter opts out of the penalty. Exposed as an MCP tool parameter.
- **Canonical question frontmatter** (#83) — `canonical_question` frontmatter field (string or list) is prepended as `"Q: ..."` to the first chunk before embedding, anchoring each memory to the question it answers.
- **Confidence field + content_hash in frontmatter** (#113, #114) — new `confidence` field for memory files, full SHA-256 content hash stored in frontmatter for integrity verification.

**Security**
- CORS, rate limiting, request size limits, stack trace sanitization for the API server
- MCP audit log — structured JSONL tool call logging (#116)

**CI/CD**
- GitHub Actions pipeline — unit tests + security scan (#121)

**Testing**
- Integration test suite — 14 API roundtrip tests (#120)
- 175 tests passing (up from 149)

**Documentation**
- Multi-platform MCP setup guide (`docs/MCP-SETUP.md`) — Claude Code, Cursor, VS Code, Zed, Windsurf
- PyPI-ready pyproject.toml with metadata and classifiers

### Changed

- `palinode_timeline` merged into `palinode_history` — one tool with `--follow`, diff stats, structured JSON return, and `limit` parameter
- Tool count: 18 → 17 (timeline/history consolidated)
- README repositioned as memory substrate, not just persistent memory

### Removed

- Migration endpoints and CLI commands (`palinode.migration` module removed)
- `docs/claude-code-setup.md` — replaced by `docs/MCP-SETUP.md`

---

## [0.6.0] — 2026-04-11

### Added

**Write-time contradiction check (ADR-004)**
- When saving a memory, the system checks for contradictions against existing files in the same entity scope
- Contradiction candidates surfaced before the save completes, with configurable thresholds

**Ambient context search (ADR-008)**
- Search results boosted by project context inferred from the caller's working directory
- Resolution chain: `PALINODE_PROJECT` env var → config project map → CWD auto-detect

**RETRACT operation**
- New executor operation: `RETRACT` — marks a memory fact as wrong with a visible tombstone
- Strikethrough formatting with `[RETRACTED date — reason]` annotation
- Fact ID preserved so readers know what was retracted and why

**Claude Code plugin scaffold**
- `claude-plugin/` directory with plugin manifest for Claude Code marketplace submission

**Claude Code skills**
- `palinode-claude-code` — MCP setup and usage for Claude Code sessions
- `palinode-session` — automatic session lifecycle memory capture

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
- **MCP server** — Streamable HTTP transport (also supports stdio). Stateless HTTP client, point it at any Palinode API server
- **REST API** — FastAPI on port 6340, 20+ endpoints covering search, save, diff, triggers, history, blame, rollback, consolidation, session-end, lint
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

**Security and hardening**
- Path validation on all file operations (rejects `..`, symlinks outside memory directory)
- Secret scrubbing on save path via configurable regex patterns
- Exclude-paths list prevents search results from surfacing files in `.secrets`, `credentials`, etc.

**Documentation**
- Remote MCP setup guides for Claude Code, Claude Desktop, Cursor, Zed
- Example memory files (`examples/people/`, `examples/projects/`, `examples/decisions/`, `examples/insights/`)
- Compaction walkthrough (`examples/compaction-demo/`) — a memory file across 3 passes with blame + diff output

**Tests**
- 92 tests covering parser, store, executor, API, CLI, and hybrid search

### Changed
- All inference is local by default. Cloud API keys (Gemini, OpenAI) are opt-in via environment variables
- REST API binds to `127.0.0.1` by default; set `PALINODE_API_HOST=0.0.0.0` to expose on LAN
- FastAPI uses lifespan protocol (deprecated `on_event` removed)
- Git commits stage only `*.md` files, never the SQLite journal/WAL/SHM

### Fixed
- Watcher no longer crashes the API server if the memory directory is temporarily unavailable
- CLI display keys match API response keys across all commands

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
