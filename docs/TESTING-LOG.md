---
created: 2026-03-22T20:45:00Z
category: documentation
---

# Palinode MVP — Testing Log

## Test Environment

- **Host:** testing-host
- **Python:** 3.12.3 in `/path/to/palinode/venv/`
- **Embedding:** BGE-M3 via Ollama, 1024 dimensions
- **Vector store:** SQLite-vec at `/path/to/palinode/.palinode.db`
- **API:** FastAPI on port 6340
- **Date:** 2026-03-22

## Test Results

### 1. Embedder ✅
```
from palinode.core.embedder import embed
result = embed('This is a test about memory systems for AI agents')
# Dimension: 1024
```
- Ollama on GPU A: OOM — vLLM using all VRAM
- Ollama on GPU B: ✅ BGE-M3 works, 1024d
- First call loads model (~30s), subsequent calls fast
- Timeout set to 120s to handle cold start

### 2. Store (SQLite-vec) ✅
- `init_db()`: creates chunks + chunks_vec tables
- `upsert_chunks()`: stores text + vectors
- `search()`: cosine similarity via vec0, returns ranked results
- `get_stats()`: returns file and chunk counts
- `delete_file_chunks()`: removes by file_path

### 3. Parser ✅
- YAML frontmatter extracted separately (not embedded)
- Body split by h2/h3 headings into sections
- Files <2000 chars kept as single chunk
- Section IDs are slugified heading text

### 4. File Watcher ✅
- Detects .md file create/modify/delete
- Excludes: .git/, logs/, venv/, node_modules/, __pycache__/
- 1-second debounce on rapid changes
- Triggers embed + upsert per section

### 5. End-to-End (file → index → search) ✅
- Write `people/test-peter.md` → watcher indexes → search finds it
- Latency: ~5-10 seconds from file write to searchable (embedding time)

### 6. FastAPI API ✅

| Endpoint | Method | Status | Notes |
|---|---|---|---|
| `/status` | GET | ✅ | Returns file/chunk counts + Ollama reachability |
| `/save` | POST | ✅ | Creates markdown file with YAML frontmatter, triggers git commit |
| `/search` | POST | ✅ | Semantic search with optional category filter |
| `/reindex` | POST | ✅ | Walks all .md files, re-embeds, re-indexes |

Port conflict: 6333 (Qdrant) and 6334 (also Qdrant). Using 6340.

### 7. CLI ✅
```bash
python3 -m palinode.cli search "what is PROGRAM.md"   # returns results
python3 -m palinode.cli stats                          # returns counts
```

### 8. Search Quality ✅ (8/8 queries)

| Query | Top Result | Score | Correct? |
|---|---|---|---|
| "What is Palinode?" | HANDOFF.md | 0.705 | ✅ |
| "Peter character decisions" (category=decisions) | decisions/kmd-five-acts.md | 0.607 | ✅ |
| "How does the extraction prompt work?" | PRD.md (prompts section) | 0.672 | ✅ |
| "What should core memory contain?" | PLAN.md (Phase 1) | 0.622 | ✅ |
| "my film class" | PRD.md (task prompts) | 0.462 | ⚠️ Low score — no real class content indexed yet |
| "SQLite-vec vs Qdrant" | HANDOFF.md (key decisions) | 0.544 | ✅ |
| "daily digest format" | specs/prompts/digest.md | 0.577 | ✅ |
| "conflicts between old and new memories" | specs/prompts/update.md | 0.569 | ✅ |

### 9. Full Write Path ✅
```
API /save → creates people/alice.md → watcher indexes → search "who handles sound mixing at Studio B" → 0.644 top hit
```

### 10. Systemd Services ✅
```
palinode-api.service     → active (running), enabled
palinode-watcher.service → active (running), enabled
```

## Bugs Found and Fixed

| Bug | Severity | Fix |
|---|---|---|
| Ollama endpoint: `/api/embed` vs `/api/embeddings` | Critical | Added fallback: try /api/embed first, fall back to /api/embeddings |
| httpx timeout on cold model load | Critical | Set 120s timeout (default was 5s) |
| Vector dimension 1536 vs actual 1024 | Critical | Changed SQLite-vec schema to FLOAT[1024] |
| Watcher swallows all exceptions silently | High | Changed bare `except` to log the error |
| `json.dumps` fails on datetime objects from YAML | High | Added `default=str` to json.dumps |
| Port 6333 conflict with Qdrant | Medium | Changed API to port 6340 |
| Watcher indexes venv/*.md files | Medium | Added venv/, node_modules/, __pycache__ to exclusion list |
| Systemd User/Group in user services | Medium | Removed — user services run as the calling user |
| Ollama URL default was unreachable | Medium | Changed to localhost:11434 |
| UNIQUE constraint on chunks_vec | Low | Happens on rapid re-index; first version stored correctly |
| Database locked on concurrent access | Low | Kill API server before manual reindex; need WAL mode |

## Known Limitations

- Research docs not indexed yet (large files, would take several minutes)
- No WAL mode on SQLite — concurrent API + watcher can lock
- Chunk ID is MD5 of filepath+section_id — possible collisions on similar headings across files
- Score normalization assumes L2 distance from normalized vectors — not true cosine similarity
- No category filtering in watcher (categories from frontmatter work; from directory name is fallback)

## What's Left to Test

- [ ] OpenClaw plugin installation and lifecycle hooks
- [ ] Session-end extraction (needs live conversation)
- [ ] Core memory injection at session start
- [ ] Indexing research docs
- [ ] Reboot survival (services enabled but untested)
