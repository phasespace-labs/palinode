# Palinode — Feature Reference

Complete feature inventory for Palinode v0.5+.

---

## Capture

### `-es` Smart Routing (OpenClaw plugin)

The `-es` flag in any OpenClaw conversation triggers smart capture routing:

| Input type | Destination |
| --- | --- |
| Bare URL only | `POST /ingest-url` → fetches page → `research/` |
| Long text (>500 chars) | ResearchRef with any URLs extracted and ingested |
| Short text + URL | Saves note as ResearchRef + fetches each URL |
| Short text, no URL | Saves as Insight |

**Edge cases handled:**

- Won't fire inside fenced code blocks
- Requires word boundary (won't match mid-word)
- Input <5 chars is ignored
- URL regex strips trailing punctuation (`,`, `.`, `)`, etc.)

### `palinode_save` Tool

Explicit save from any conversation. Accepts content, type, optional metadata. Creates a properly-formatted markdown file with YAML frontmatter.

### `palinode_ingest` Tool

URL ingestion from conversation. Fetches page, extracts readable content, saves to `research/` with metadata.

### MBP Drop Folder

- Drop zone: `~/Palinode-Inbox/` on your Mac (must be created: `mkdir ~/Palinode-Inbox`)
- Sync: rsync cron every 2 min → `inbox/raw/` on your-server
- Pipeline processes all dropped files automatically

### Inbox Pipeline

Files dropped into `inbox/raw/` are processed by type:

| File type | Processing |
| --- | --- |
| `.pdf` | Text extraction (pdftotext/pymupdf) → LLM summary → `research/` |
| Audio | Whisper transcription (Transcriptor at YOUR_EMBEDDING_SERVER:8787) → `research/` |
| URL `.txt` | Fetches page → extracts content → `research/` |
| Plain text | Classified and saved to appropriate bucket |

All processed files moved to `inbox/processed/`.

---

## Recall (autoRecall)

### `before_agent_start` Hook

Fires on every agent turn. Four-phase injection:

**Phase 1 — Core Memory (always injected)**

- All files with `core: true` in frontmatter
- Injected in full (up to 3KB per file)
- Files >3KB truncated: `[truncated — summary: ...]` if summary exists, else `[truncated — full file at path]`
- If `summary:` frontmatter field is present, injected as a blockquote header before file content

**Phase 2 — Semantic Search (topic-specific)**

- Top 5 chunks, 700 chars each
- SKIPPED for trivial messages: <15 chars, or common acks (`ok`, `yep`, `thanks`, `sure`, `np`, etc.)

**Phase 3 — Associative Context (Graph Search)**
- If the user discusses known entities, spreading activation retrieves related files via the entity graph.
- Up to 3 related files added to the context horizontally.

**Phase 4 — Prospective Triggers**
- User message is embedded and checked against the `triggers_vec` table.
- If it semantically matches a trigger description, the associated `memory_file` is forcibly injected.

### Tiered Core Injection (token budget control)

Core injection is tiered to avoid burning ~3K tokens every turn:

| Turn | Phase 1 | Est. tokens |
|---|---|---|
| Turn 1 (session start) | Full core content | ~3K |
| Turns 2–24 | Summaries only (~1 line per file) | ~200 |
| Turn 25, 50, 75… | Full core refresh | ~3K |

Phase 2 topic search runs on every non-trivial turn regardless of tier.

**Effect on long sessions (50 turns):** ~8K tokens from core injection vs ~150K with flat injection every turn.

Files without a `summary:` field are skipped entirely on summary-only turns — they only appear on full turns.

### Auto-Summary

Core files saved via `/save` with `core: true` automatically get a generated `summary:` field injected into their frontmatter:

- Powered by Ollama `llama3.2:3b` (fast, small)
- Prompt produces ≤120 char factual summary
- Skipped if: Ollama unreachable, content <200 chars, summary already exists
- Forward-only (not retroactive on existing files)

### `palinode_search` Tool

Hybrid search from conversation with optional `category` filter. Combines BM25 exact-keyword matching with BGE-M3 semantic vectors using Reciprocal Rank Fusion (RRF). Returns ranked chunks with hybridized scores.

---

## Session Capture

### `agent_end` Hook

Appends last 10 messages (capped at 2000 chars total) to `daily/YYYY-MM-DD.md`.

### `before_reset` Hook

Flushes last 20 messages to `daily/` before context reset.

- **Active on:** field agent only
- **Active on:** all profiles (field, attractor, default, gradient)

---

## Storage

### File Format

Markdown + YAML frontmatter.

**Required fields:**

```yaml
id: category-slug
category: people|projects|insights|decisions|research|daily
core: true|false
entities: [person/name, project/name]
last_updated: 2026-01-01T00:00:00Z
```

**Optional fields:**

```yaml
summary: "One-sentence summary (auto-generated or manual)"
status: active|archived|draft
confidence: high|medium|low
source_urls: [https://...]
source_type: pdf|audio|url|text
tags: [tag1, tag2]
```

### Layered File Architecture

Core memory files (people, projects) are split into three layers to isolate active context from archival records:

- `{name}.md` — Identity: immutable architecture, context, and core characteristics.
- `{name}-status.md` — Active status: current milestones, open tasks, unverified claims.
- `{name}-history.md` — Append-only archive of superseded facts, competed tasks, and deprecated context.

Only the Identity and Status layers are injected into the agent's core context. History is indexed for semantic search but never burns core tokens.

### Fact IDs & Compaction Operations

Rather than re-summarizing entire files from scratch, Palinode uses structured JSON operations to precisely manipulate facts during consolidation. Every manipulatable fact is wrapped with a persistent ID (e.g., `- <!-- fact:slug --> text`).

Operations include:
- **KEEP** — Retain the fact unchanged.
- **UPDATE** — Modify the fact (e.g., progress update, typo fix).
- **MERGE** — Combine multiple related facts into a single stronger statement.
- **SUPERSEDE** — Replace an older fact with a newer, contradictory one (auto-archives the original).
- **ARCHIVE** — Move a completed task or outdated fact to the `-history.md` layer.

### Buckets

| Bucket | Contents |
|---|---|
| `people/` | Person memory files |
| `projects/` | Project snapshots |
| `insights/` | Distilled learnings |
| `decisions/` | ADRs and key decisions |
| `research/` | Ingested documents, URLs, PDFs |
| `daily/` | Session logs (YYYY-MM-DD.md) |
| `inbox/raw/` | Unprocessed drop zone |
| `inbox/processed/` | Processed (excluded from index) |

### Database & Deduplication

- SQLite-vec + FTS5 at `~/.palinode.db`
- BGE-M3 embeddings, 1024 dimensions, via Ollama on YOUR_EMBEDDING_SERVER
- FTS5 virtual table (`chunks_fts`) provides exact keyword matching for code, model names, and IDs
- Content-hash deduplication (`content_hash` column) prevents redundant LLM embedding calls for unmodified chunks
- Watcher: Python watchdog auto-indexes on file create/modify/delete
- Excludes: `.git/`, `.palinode.db-journal/wal/shm`, `inbox/processed/`, `venv/`, `node_modules/`
- Git-versioned repo; `.palinode.db` in `.gitignore` (derived, rebuildable via `/reindex` or `/rebuild-fts`)

---

## Dual Embeddings

| Embedding | Provider | Dimensions | Use |
|---|---|---|---|
| BGE-M3 | Ollama (YOUR_EMBEDDING_SERVER:11434) | 1024 | All core memory — fully private |
| gemini-embedding-2-preview | Gemini cloud API | 768 (Matryoshka) | Research ingestion (optional) |

---

## API

FastAPI server on port 6340. Default bind: `127.0.0.1` (localhost only). Set `api.host: "0.0.0.0"` in config for LAN access. **Not authenticated — do not expose publicly.**

### Endpoints

| Method | Path | Body | Description |
|---|---|---|---|
| `GET` | `/status` | — | `total_files`, `total_chunks`, `ollama_reachable` |
| `POST` | `/search` | `{query, category?, limit?}` | Semantic search → ranked chunks |
| `POST` | `/search-associative` | `{query, seed_entities?, limit?}` | Entity graph spreading activation |
| `POST` | `/save` | `{content, type, slug?, entities?, metadata?}` | Create markdown memory file |
| `POST` | `/ingest` | — | Process all files in `inbox/raw/` |
| `POST` | `/ingest-url` | `{url, name?}` | Fetch URL, extract content, save to `research/` |
| `POST` | `/reindex` | — | Full reindex of all markdown files |
| `POST` | `/rebuild-fts` | — | Rebuild BM25 index only |
| `POST` | `/triggers` | `{description, memory_file, trigger_id?}` | Register a prospective trigger |
| `POST` | `/check-triggers` | `{query}` | Check message against triggers |
| `GET` | `/triggers` | — | List all triggers |
| `DELETE`| `/triggers/{trigger_id}` | — | Delete a trigger |

---

## OpenClaw Plugin (`openclaw-palinode`)

### Installation

Installed to all agent prefixes:

- `~/.openclaw-field/` (field agent)
- `~/.openclaw-attractor/` (attractor)
- `~/.openclaw/` (main)
- `~/.openclaw-gradient/` (gradient/coding agent)

### Hooks

| Hook | Status | Purpose |
|---|---|---|
| `before_agent_start` | ✅ field + attractor | Core memory + semantic context injection |
| `agent_end` | ✅ field + attractor | Session capture to daily notes |
| `before_reset` | ✅ all profiles | Flush messages on context reset |
| `message_received` | ✅ all profiles | `-es` capture, smart routing |

> **Note:** Hook names use underscore syntax (`message_received`, `before_reset`), not colon syntax. Fixed 2026-03-23.

### Tools Registered

| Tool | Description |
|---|---|
| `palinode_search` | Semantic search with optional category filter |
| `palinode_save` | Save a memory from conversation |
| `palinode_trigger` | Register a prospective trigger schema to fire on future matches |
| `palinode_ingest` | Ingest a URL from conversation |
| `palinode_status` | File counts, chunk counts, Ollama status |

### Scrub Patterns

`specs/scrub-patterns.yaml` — applied at injection time to redact:

- Credentials and passwords
- Phone numbers
- Other PII patterns

---

## Security

- Source files cleaned of all credentials (git commit `0b9588f`)
- Scrub patterns active at injection time (before context is sent to LLM)
- All secrets referenced as "credentials in `~/.secrets`" — never embedded in files
- API bound to localhost only

---

## Known Limitations / Roadmap

| Item | Status |
|---|---|
| `-es` on Telegram (attractor) | ✅ Working — fixed 2026-03-23 (correct hook names) |
| `-es` receipt in chat | ⚠️ Silent save — `message_received` is read-only, receipt not appended (GH #5) |
| Consolidation cron | ✅ Implemented (weekly, operation-based KEEP/UPDATE/MERGE/SUPERSEDE/ARCHIVE) |
| Auto-summary retroactive | ❌ Forward-only; existing files not backfilled |
| Slack capture | 🔜 Deferred (GitHub Issue #1) |
| MBP drop folder | ⚠️ Requires `mkdir ~/Palinode-Inbox` on your machine first |
| Phase 1 (retire MEMORY.md) | 🔜 Next milestone — switch to file-based CAG |
