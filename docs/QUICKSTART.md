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
# Copy service files
mkdir -p ~/.config/systemd/user/
cp systemd/palinode-api.service ~/.config/systemd/user/
cp systemd/palinode-watcher.service ~/.config/systemd/user/

# Start and enable
systemctl --user daemon-reload
systemctl --user enable --now palinode-api palinode-watcher

# Check status
systemctl --user status palinode-api
systemctl --user status palinode-watcher
```

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
cat > people/peter.md << 'EOF'
---
id: person-peter
category: person
name: Peter
core: true
entities: [project/mm-kmd]
last_updated: 2026-03-22T20:00:00Z
---
# Peter

Writer and IP creator for MM-KMD. Controls canon.
EOF
```

The watcher auto-indexes it within ~10 seconds.

### Save via API

```bash
curl -X POST http://localhost:6340/save \
  -H "Content-Type: application/json" \
  -d '{
    "content": "Peter wants 5 acts instead of 3",
    "type": "Decision",
    "slug": "kmd-five-acts",
    "entities": ["person/peter", "project/mm-kmd"]
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
