# Changelog

All notable changes to Palinode. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## Unreleased

## [0.7.3] — 2026-04-26

### Added

**Session-end semantic dedup (#126)**
- `palinode_session_end` now checks recently indexed saves (default: last 60 minutes) for semantic overlap with the new content via cosine similarity on the existing BGE-M3 embeddings. When the best match scores at or above 0.85, the indexed individual file is skipped and the response carries `deduplicated_against: <slug>`. Daily-note and project-status appends are unchanged — only the duplicate indexed file is suppressed. Defaults `SESSION_END_DEDUP_WINDOW_MINUTES` and `SESSION_END_DEDUP_THRESHOLD` live in `palinode/core/defaults.py`. Embedder failures degrade gracefully: both files written, warning logged.

**Search quality (M0.5)**
- **Score-gap dedup** (#91, `52966ae`) — additional chunks from the same file are kept only if within `dedup_score_gap` (default 0.2) of the file's best score. Reduces noise from multi-section files dominating results.
- **G1 context boost fix** (#92, `f156a3d`) — `store.search()` now accepts `context_entities` for ADR-008 ambient context boost. Previously the boost only fired through `search_hybrid`.
- **Raw cosine exposure** (#94, `ee8931a`) — search results include a `raw_score` field with the original cosine similarity before RRF normalization.
- **Daily penalty** (#93, `d18240c`) — `daily/` files receive `score * daily_penalty` (default 0.3) to prevent daily notes from dominating search results. New `include_daily` parameter opts out of the penalty. Exposed as an MCP tool parameter.
- **Canonical question frontmatter** (#83, `dbe5703`) — `canonical_question` frontmatter field (string or list) is prepended as `"Q: ..."` to the first chunk before embedding, anchoring each memory to the question it answers.

**Tests**
- 175 tests passing (up from 149)
- **L1-L3 end-to-end test for `palinode session-end` (#139)** — `tests/integration/test_session_end_e2e_l1_l3.py` covers Tool fired / Data on disk / Retrievable layers of the four-layer validation model from `docs/VALIDATION-STRATEGY.md`. Drives the real CLI through `CliRunner` with the dual-write API call redirected into the in-process `TestClient`, manually triggers the watcher's per-file index path, and asserts a fresh client surfaces the record via `POST /search`. L4 (LLM-in-the-loop behavioural test) is deferred to `docs/L4-BEHAVIORAL-TESTING-DESIGN.md`.

### Changed

- **ADR-010 cross-surface monopoly is now total (#170)** — all CLI surfaces (`session-end`, `ingest`, `prompt`, `lint`, `list`) now route through `palinode/cli/_api.py`. The `GRANDFATHERED` list in `scripts/check-httpx-monopoly.sh` is now empty, and plugin parity tests were added on the TypeScript side. v0.7.3 ships with a single canonical API surface, eliminating silent feature drift between CLI and API.

### Fixed

- **Timestamp inconsistency in `chunks.created_at`** (#191) — two related bugs together made the column unreliable as a recency signal. `save_api` wrote `time.strftime("%Y-%m-%dT%H:%M:%SZ")` (local time formatted with a `Z` UTC suffix); switched to `_utc_now().isoformat()`. The watcher read `metadata.get("created", "")` while every producer writes the key as `created_at`; fixed the read. Existing rows still parse — values are just shifted by the local-vs-UTC offset until rewritten — so no migration is shipped. `palinode/core/store.py`'s file-mtime fallback (added in #183) remains as a defense for downstream consumers.
- **Timestamp inconsistency in batch surfaces** (#193) — same Z-on-local-time hazard as #191/#192, this time on `last_updated` writes in four batch paths: `palinode/ingest/pipeline.py` (research files), `palinode/consolidation/layer_split.py` (identity / status / history), `palinode/migration/mem0_generate.py`, and `palinode/migration/openclaw.py`. All switched to `datetime.now(UTC).isoformat()` (or the existing `_utc_now().isoformat()` where the helper was already in scope). One incidental fifth surface — `palinode/consolidation/runner.py`'s `## Consolidation Log (...)` heading — was switched to a human-readable `... UTC` suffix to satisfy the project-wide `strftime("...Z")` audit (the audit now returns zero non-comment matches). `last_updated` isn't currently load-bearing for recency filters, so no migration is shipped.
- **`.gitignore` no longer hides legitimate code paths** — the broad private-data rules (`people/`, `projects/`, `decisions/`, `insights/`, `migration/`, etc.) caught `palinode/migration/` and `examples/{people,projects,decisions,insights,sample-memory}/`, requiring `git add -f` for any new file under those paths. Added `!palinode/**` and `!examples/**` exemptions so the broad defense-in-depth pattern still catches accidental nested data anywhere else. As a consequence, four sample-memory files that the `examples/sample-memory/README.md` already advertised but were silently dropped on commit are now tracked: `decisions/api-design.md`, `people/alice.md`, `insights/testing.md`, `projects/my-app.md`. The sample tree now matches its README inventory (3 people, 1 project, 2 decisions, 1 insight).

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

- **SSH keepalive options for remote-stdio MCP (`docs/MCP-SETUP.md`, `docs/INSTALL-CLAUDE-CODE.md`)** — `ServerAliveInterval=30`, `ServerAliveCountMax=3`, `TCPKeepAlive=yes` added to the example configs. Fixes the "MCP dies after laptop sleep / WiFi change" failure mode that hits anyone running palinode-api on a remote homelab box and SSH-spawning the MCP from a laptop. NAT and Tailscale's DERP relay drop idle TCP after a few minutes; without keepalives the IDE's MCP log fills with `Connection reset by peer`.

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
