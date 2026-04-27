# Changelog

All notable changes to Palinode. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## Unreleased

## [0.8.0] — 2026-04-27

### Changed

- README.md and docs/QUICKSTART.md updated to promote Obsidian integration and `palinode doctor` as headline features for the v0.8.x rollout.
- `/save` is now the canonical mid-session checkpoint slash command; `/ps` remains as a back-compat alias and `palinode init` scaffolds both.

### Added

- `palinode doctor` across CLI, API, and MCP, including a constrained `--fix` mode for safe setup repairs.
- `docs/DOCTOR.md`, a full diagnostic guide covering the check catalog, `--fix`, and common failure cases.
- `palinode init --obsidian`, `palinode obsidian-sync`, and a broader Obsidian workflow with scaffolding, wiki footer support, and migration guidance.
- `palinode_dedup_suggest` and `palinode_orphan_repair` across CLI, API, and MCP.
- Expanded MCP config diagnostics for Claude Code, Claude Desktop, Cline, Zed, and project-local `.mcp.json`.
- Version-controlled systemd unit templates and an installer under `deploy/systemd/`.
- Search improvements including score-gap dedup, daily-note penalty tuning, canonical-question anchoring, and raw cosine exposure.
- Session-end semantic dedup for recently indexed saves.
- Additional docs around MCP setup and Obsidian workflows.

### Fixed

- `/list` now sorts newest first instead of filesystem glob order.
- `POST /save` now embeds inline before returning, and the watcher verifies vector-index presence before skipping unchanged content.
- Default `db_path` now follows `PALINODE_DIR` overrides more reliably.
- `palinode doctor --json` now emits only JSON on stdout.
- `/health` now reports live chunk and entity counts.
- Fresh-empty-database creation is refused when `memory_dir` already contains memories.
- Startup validation is stricter around `db_path`, `memory_dir`, and reindex concurrency.
- Several timestamp surfaces now use consistent UTC handling.
- `.gitignore` no longer hides legitimate code paths under `palinode/` and `examples/`.
- Worktree test resolution is more reliable for editable installs.

### Removed

- Root-level placeholder systemd unit files superseded by the version-controlled templates and installer under `deploy/systemd/`.

---

## [0.7.2] — 2026-04-26

Bug-fix release with small UX additions. Brings the public repo up to date with two production-impacting fixes (MCP array coercion, SessionEnd hook reasoning) plus search filters and structured session metadata. v0.8.0 remains reserved for a later major feature release.

### Added

- **`/search` filters and recency-only mode (#141)** — `types: [...]` and `since_days: N` are now honored. Empty query routes to a recency-only path that orders by `created_at desc` with no semantic ranking. Backed by a new `store.list_recent()` that pushes type filtering into SQL via `json_extract`. Downstream consumers that wanted "recent Insights" or "recent Decisions from the last 14 days" can now do it in a single call.
- **Structured `session_end` metadata (#145)** — `palinode_session_end` accepts `harness`, `cwd`, `model`, `trigger`, `session_id`, `duration_seconds`. Fields land in the daily-note text and the indexed file's frontmatter. If `project` is omitted but `cwd` is provided, the project slug auto-derives from the cwd's basename via the same slug rules `palinode init` uses.
- **`palinode_save` `ps=true` shortcut (#136)** — MCP parity with the CLI `--ps` flag. `palinode_save` accepts either an explicit `type` or `ps=true` (shorthand for ProjectSnapshot). Conflict between `ps=true` and a non-ProjectSnapshot `type` errors clearly rather than silently ignoring one.
- **Scope frontmatter parsing (#107)** — `scope`, `visibility`, `access` fields recognized in memory frontmatter. Parser-only in this release; search-time use lands in a follow-up.
- **CLI banner** — `palinode --version` and `palinode banner` print the ASCII density-gradient mark.
- **`docs/VALIDATION-STRATEGY.md` (#137)** — formalizes a four-layer model (Tool fired / Data on disk / Retrievable / Behavioral) for validating any cross-session feature, with PR-checklist template. Cross-linked from `docs/INSTALL-CLAUDE-CODE.md`.

### Fixed

- **MCP tolerates JSON-encoded array arguments (#147)** — some MCP transports double-encode array args as JSON strings. Previous behavior rejected with Pydantic 422 ("expected array, received string") on `palinode_session_end` and `palinode_save`, and silently corrupted the `paths` filter on `palinode_diff` (`",".join(string)` was joining char-by-char). New `_coerce_str_array` helper decodes JSON-array strings while passing native lists through unchanged.
- **SessionEnd hook reason filter (#149)** — the hook's README claimed a `clear` matcher that never existed in `settings.json`. Replaced with explicit script-side filtering via a new `PALINODE_HOOK_REASONS` env var. Default allowlist: `clear logout prompt_input_exit other`. Override to narrow (e.g. `"clear"`) or extend (`+resume`, `+bypass_permissions_disabled`).
- **Empty-transcript captures (#151)** — SessionEnd hook was capturing 0-message sessions despite `PALINODE_HOOK_MIN_MESSAGES=3`. Root cause: `grep -c '.'` on empty input emitted `"0"` and exited 1; the `|| echo "0"` fallback then appended another `"0"`, producing the literal three-byte string `"0\n0"` which made the integer test error and fall through. Fix: drop the `|| echo "0"` (grep always emits an integer regardless of exit), use `|| true` to absorb the non-zero, default to `0` if the pipeline produces nothing.
- **Test isolation** — full-suite test runs no longer hit 5 failures in `test_write_time` due to scope-chain reload state pollution.

### Documentation

- **SSH keepalive options for remote-stdio MCP (`docs/MCP-SETUP.md`, `docs/INSTALL-CLAUDE-CODE.md`)** — `ServerAliveInterval=30`, `ServerAliveCountMax=3`, and `TCPKeepAlive=yes` added to the example configs. This helps long-lived SSH-backed MCP sessions survive idle network drops and reconnect more cleanly after laptop sleep or WiFi changes.

### Tests

- 325 tests passing at release time.

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
- Architecture decision records covering the deterministic executor design
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
