---
created: 2026-03-22T20:45:00Z
category: documentation
---

# Palinode Quickstart

## Prerequisites

- Python 3.11+
- Ollama with BGE-M3 on a reachable host (default: localhost:11434)
- Git

## Setup

```bash
cd /path/to/palinode

# Create venv and install
python3 -m venv venv
source venv/bin/activate
pip install -e .

# Initialize the database
python3 -c "from palinode.core.store import init_db; init_db()"

# Pull BGE-M3 if not already on your Ollama host
ollama pull bge-m3
```

## Running

### Option A: Systemd services (recommended)

```bash
# Required environment variables
export PALINODE_HOME=/path/to/palinode             # code root + venv/
export PALINODE_DATA_DIR=/path/to/palinode-data    # memory markdown files
export OLLAMA_URL=http://localhost:11434
export EMBEDDING_MODEL=bge-m3

# Install + enable the three user units (palinode-api, palinode-mcp, palinode-watcher)
bash deploy/systemd/install.sh --enable

# Check status
systemctl --user status palinode-api palinode-mcp palinode-watcher
```

See [`deploy/systemd/README.md`](../deploy/systemd/README.md) for full variable reference, troubleshooting, and uninstall.

### Option B: Manual

```bash
# Terminal 1: API server
source venv/bin/activate
uvicorn palinode.api.server:app --host 127.0.0.1 --port 6340

# Terminal 2: File watcher
source venv/bin/activate
python3 -m palinode.indexer.watcher
```

## Usage

### Create a memory file

Write a markdown file anywhere in the palinode directory:

```bash
cat > people/alice.md << 'EOF'
---
id: person-alice
category: person
name: Alice
core: true
entities: [project/my-app]
last_updated: 2026-03-22T20:00:00Z
---
# Alice

Designer and product lead for My App. Controls the design system.
EOF
```

The watcher auto-indexes it within ~10 seconds.

### Save via API

```bash
curl -X POST http://localhost:6340/save \
  -H "Content-Type: application/json" \
  -d '{
    "content": "Alice wants 5 modules instead of 3",
    "type": "Decision",
    "slug": "app-five-modules",
    "entities": ["person/alice", "project/my-app"]
  }'
```

### Search

```bash
# Via API
curl -X POST http://localhost:6340/search \
  -H "Content-Type: application/json" \
  -d '{"query": "how many acts", "limit": 5}'

# Via CLI
python3 -m palinode.cli search "how many acts"
```

### Check status

```bash
curl http://localhost:6340/status
# or
python3 -m palinode.cli stats
```

### Verify with `palinode doctor`

After install, run a quick health check:

```bash
palinode doctor
```

It runs 18 read-only checks across path integrity, service health, config drift, index sanity, disk/backup, and a forward-looking CLAUDE.md scan. Every check has a remediation string; pass `--verbose` to also see remediation for passing checks. Use `--fix` (with per-action confirmation) to apply the small whitelist of safe automated fixes — doctor never moves user data. Full guide: [`docs/DOCTOR.md`](DOCTOR.md).

### Rebuild index from scratch

```bash
# Delete and recreate
rm .palinode.db
python3 -c "from palinode.core.store import init_db; init_db()"
curl -X POST http://localhost:6340/reindex
```

## File Format

All memory files use YAML frontmatter:

```yaml
---
id: unique-id
category: person | project | decision | insight | research
core: true | false       # true = loaded at every session start
status: active | archived | superseded
entities: [person/name, project/slug]
last_updated: 2026-03-22T20:00:00Z
---

# Title

Content here.
```

## Architecture

```
/path/to/palinode/
├── .palinode.db              ← SQLite-vec (auto-generated, in .gitignore)
├── people/*.md             ← who you know
├── projects/*.md           ← what you're building
├── decisions/*.md          ← choices made
├── insights/*.md           ← lessons learned
├── research/*.md           ← reference material
├── daily/*.md              ← session logs
├── specs/prompts/*.md      ← system prompts (read by the memory manager)
├── PROGRAM.md              ← memory manager behavioral spec
└── PRD.md                  ← what Palinode is
```

## Ports & Services

| Service | Port | Process |
|---|---|---|
| Palinode API | 6340 | uvicorn (FastAPI) |
| Palinode Watcher | — | python watchdog daemon |
| Ollama (embeddings) | 11434 | ollama serve |
| Qdrant (Mem0, separate) | 6333 | qdrant |

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `PALINODE_DIR` | `/path/to/palinode` | Root directory for memory files |
| `OLLAMA_URL` | `http://localhost:11434` | Ollama endpoint for embeddings |
| `EMBEDDING_MODEL` | `bge-m3` | Ollama model name |

## Verify your setup with palinode doctor

After install and service start, run:

```bash
palinode doctor
```

This checks 18+ conditions — DB path validity, watcher connectivity, config consistency, index health — and prints a pass/warn/fail report. If something is misconfigured, run `palinode doctor --fix` to apply safe automated repairs. See [docs/DOCTOR.md](DOCTOR.md) for the full check catalog.

## Obsidian integration

Palinode stores everything as plain markdown with YAML frontmatter, so your Palinode directory is already a valid Obsidian vault. Run `palinode init --obsidian /path/to/vault` for an opinionated scaffold (graph defaults, daily-notes wiring, a starter `_index.md` MOC), then open the directory in Obsidian. You get the graph view, backlinks, and Bases on top of Palinode's hybrid search and consolidation — same files, two surfaces.

See [OBSIDIAN.md](OBSIDIAN.md) for the comprehensive guide: quickstart, the wiki-maintenance contract, the embedding tools the LLM calls (`palinode_dedup_suggest`, `palinode_orphan_repair`), and migration paths.
## Connecting your IDE via MCP

Once the API is running, connect it to your AI coding assistant:

| Client | Recipe |
|--------|--------|
| Claude Code | [INSTALL-CLAUDE-CODE.md](INSTALL-CLAUDE-CODE.md) |
| Claude Desktop | [MCP-SETUP.md](MCP-SETUP.md#claude-desktop) |
| Cursor | [MCP-INSTALL-RECIPES.md](MCP-INSTALL-RECIPES.md#1-cursor) |
| Windsurf | [MCP-INSTALL-RECIPES.md](MCP-INSTALL-RECIPES.md#2-windsurf) |
| Continue (VS Code) | [MCP-INSTALL-RECIPES.md](MCP-INSTALL-RECIPES.md#3-continue-vs-code) |
| Cline (VS Code) | [MCP-INSTALL-RECIPES.md](MCP-INSTALL-RECIPES.md#4-cline-vs-code) |
| Zed | [MCP-INSTALL-RECIPES.md](MCP-INSTALL-RECIPES.md#5-zed) |

After connecting, verify with `palinode mcp-config --diagnose`.
